"""实体链接（查询 → 种子实体）测试。

钉住五件事：
  1. 词元只用 keyword_text.build_ilike_terms + normalize_name_key 两处单一真源
     （自己另写切词/归一 = 静默 0 召回）
  2. 三层匹配的门槛语义：精确 1.0 / 前缀 0.6 / 子串 0.3，只用于排先后
  3. 全序：score 降序 + name_key 升序（同一句话两次链接结果必须一致）
  4. 前一层凑满 limit 就**不再发**下一层 SQL（省一次全表正则扫）
  5. 口径 / 租户 / kb 三件套焊在每层 SQL 里，且口径没有兜底分支
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest

from app.config import decisions
from app.services.graph import linking as linking_mod
from app.services.graph.extract import normalize_name_key
from app.services.graph.linking import LinkedEntity, link_entities, query_keys

TENANT = uuid.UUID(int=9)
KB = uuid.UUID(int=1)


@dataclass
class _Row:
    id: uuid.UUID
    name: str
    name_key: str


class _Result:
    def __init__(self, rows: list[_Row]) -> None:
        self._rows = rows

    def all(self) -> list[_Row]:
        return list(self._rows)


class FakeSession:
    """按 SQL 特征分发；记录每次调用的 (类别, 参数)，用于断言「少发一次」。"""

    def __init__(self, exact=None, fts=None, like=None) -> None:
        self._exact = exact or []
        self._fts = fts or []
        self._like = like or []
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, stmt, params=None):  # noqa: ANN001
        sql = stmt.text if hasattr(stmt, "text") else str(stmt)
        params = dict(params or {})
        if "e.name_key = ANY(:keys)" in sql:
            self.calls.append(("exact", params))
            return _Result(self._exact)
        if "@@ to_tsquery" in sql:
            self.calls.append(("fts", params))
            return _Result(self._fts)
        if "e.name_key ~*" in sql:
            self.calls.append(("like", params))
            return _Result(self._like)
        raise AssertionError(f"未识别的 SQL: {sql[:100]}")


async def _link(session: FakeSession, query: str = "差旅费报销标准", **kwargs):
    return await link_entities(
        session,  # type: ignore[arg-type]
        query=query,
        tenant_id=TENANT,
        kb_ids=[KB],
        extractor_model="qwen-test",
        prompt_version="gprompt-v1",
        **kwargs,
    )


# ── 词元 ────────────────────────────────────────────────────
def test_query_keys_normalized_and_capped():
    keys = query_keys("2024 年差旅费报销标准是什么？XYZ-1000")
    assert keys, "自然问句必须至少切出一个词元"
    assert len(keys) <= decisions.GRAPH_LINK_MAX_TERMS
    # 归一真源：与写入侧同一函数（全角/大小写/空白都会被抹平）
    assert all(key == normalize_name_key(key) for key in keys)
    assert len(set(keys)) == len(keys), "词元必须去重"


def test_query_keys_full_width_normalized_same_as_half_width():
    """全角/大小写必须归一成同一个键 —— 否则「ＡＢＣ１２３」查不到库里的 abc123。"""
    assert query_keys("ＡＢＣ１２３") == query_keys("abc123")
    assert query_keys("ＡＢＣ１２３"), "全角字母数字必须能切出词元（NFKC 之后才切）"


def test_query_keys_empty_for_pure_symbols():
    assert query_keys("！！！？？") == []


# ── 打分层次 ────────────────────────────────────────────────
def test_match_score_layers():
    keys = ["差旅费"]
    assert linking_mod._match_score("差旅费", keys) == 1.0
    assert linking_mod._match_score("差旅费报销", keys) == 0.6
    assert linking_mod._match_score("报销差旅费明细", keys) == 0.3
    assert linking_mod._match_score("完全无关", keys) == 0.0


# ── 三层补足 ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_layers_stop_when_previous_fills_limit():
    """精确层凑满 limit → 不再发 FTS / 子串两层（省一次无索引全表正则扫）。"""
    exact = [_Row(uuid.uuid4(), f"差旅费{i}", "差旅费") for i in range(3)]
    session = FakeSession(exact=exact)
    got = await _link(session, limit=3)
    assert len(got) == 3
    assert [kind for kind, _ in session.calls] == ["exact"]


@pytest.mark.asyncio
async def test_layers_fall_through_to_fts_then_like():
    session = FakeSession(
        exact=[_Row(uuid.uuid4(), "差旅费", "差旅费")],
        # 名字比词元长（不是精确命中），才能验证「层的分档」而不被精确命中抬到 1.0
        fts=[_Row(uuid.uuid4(), "差旅费报销标准说明", "差旅费报销标准说明")],
    )
    got = await _link(session, limit=8)
    assert [kind for kind, _ in session.calls] == ["exact", "fts", "like"]
    assert len(got) == 2
    # 精确命中的分必须高于 FTS 命中（同一实体被两层命中时取高分）
    assert got[0].score == 1.0
    assert got[1].score == 0.6


@pytest.mark.asyncio
async def test_same_entity_keeps_higher_score_when_seen_twice():
    entity_id = uuid.uuid4()
    session = FakeSession(
        exact=[_Row(entity_id, "差旅费", "差旅费")],
        fts=[_Row(entity_id, "差旅费", "差旅费")],
    )
    got = await _link(session, limit=8)
    assert len(got) == 1, "同一实体不得重复出现（否则一个名额被浪费两次）"
    assert got[0].score == 1.0, "被 FTS 再次命中不得把精确命中的分数覆盖成 0.6"


@pytest.mark.asyncio
async def test_order_is_total_and_deterministic():
    """score 相同是常态（同层命中都是 0.6）—— 必须靠 name_key 定序，否则不可复现。"""
    rows = [
        _Row(uuid.UUID(int=3), "差旅费报销Z", "差旅费报销z"),
        _Row(uuid.UUID(int=1), "差旅费报销A", "差旅费报销a"),
        _Row(uuid.UUID(int=2), "差旅费报销M", "差旅费报销m"),
    ]
    got = await _link(FakeSession(fts=rows), limit=8)
    assert [e.name for e in got] == ["差旅费报销A", "差旅费报销M", "差旅费报销Z"]


@pytest.mark.asyncio
async def test_limit_caps_result():
    rows = [_Row(uuid.uuid4(), f"差旅费{i}", f"差旅费{i}") for i in range(10)]
    got = await _link(FakeSession(fts=rows), limit=2)
    assert len(got) == 2
    assert all(isinstance(e, LinkedEntity) for e in got)


@pytest.mark.asyncio
async def test_empty_kb_ids_short_circuits_without_sql():
    session = FakeSession()
    got = await link_entities(
        session,  # type: ignore[arg-type]
        query="差旅费报销标准",
        tenant_id=TENANT,
        kb_ids=[],
        extractor_model="m",
        prompt_version="pv",
    )
    assert got == []
    assert session.calls == [], "没有授权库时 MUST NOT 发查询（否则是对全库的一次探测）"


# ── 口径 / 租户 / kb 焊在 SQL 里 ─────────────────────────────
@pytest.mark.asyncio
async def test_every_layer_carries_scope_params():
    session = FakeSession()
    await _link(session, limit=8)
    assert session.calls, "应当至少发了精确层"
    for _kind, params in session.calls:
        assert params["tenant_id"] == TENANT
        assert params["kb_ids"] == [KB]
        assert params["extractor_model"] == "qwen-test"
        assert params["prompt_version"] == "gprompt-v1"


def test_scope_sql_has_no_caliber_fallback():
    """口径 MUST NOT 有兜底分支（`OR ... IS NULL` 会让旧口径数据混进种子池）。"""
    scope = linking_mod._SCOPE_WHERE
    assert "m.extractor_model = :extractor_model" in scope
    assert "m.prompt_version = :prompt_version" in scope
    assert "IS NULL" not in scope.upper()
    assert " OR " not in scope.upper()


def test_scope_sql_filters_tenant_and_kb():
    scope = linking_mod._SCOPE_WHERE
    assert "e.tenant_id = :tenant_id" in scope
    assert "e.kb_id = ANY(:kb_ids)" in scope


def test_like_layer_uses_regex_not_like_underscore_wildcard():
    """`name_key LIKE '%get_retrieval%'` 里 `_` 是单字符通配符 —— 会命中 getXretrieval。"""
    sql = linking_mod._LIKE_SQL.text
    assert "~*" in sql
    assert " LIKE " not in sql.upper()
