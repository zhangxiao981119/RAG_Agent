"""G-8 观测：FTS 别名词元被独立预算裁掉时必须打点（C4 §3.1「把静默改成可见」）。

钉住三件事（均走 `RetrievalService.retrieve` 真实入口，不是本地复制公式）：
  1. 去重别名词元 8 个、预算 6 → `rag.lexicon.fts_alias_dropped` 恰好 1 次、值 = 2；
  2. 恰好 6 个（未超预算）→ 不打点（零噪音）；
  3. 无别名 / `ALIAS_EXPANSION_ENABLED=False` → 不打点。

★ 只测观测，不改预算：`build_fts_query` 的 `alias_max_terms` 默认值不变。
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.config import decisions
from app.services import retrieve as retrieve_mod
from app.services.retrieve import RetrievalService
from tests.fake_settings import fake_settings

TENANT = uuid.uuid4()
KB = uuid.UUID(int=1)
DOC = uuid.UUID(int=2)
METRIC = "rag.lexicon.fts_alias_dropped"


def _row(cid: uuid.UUID) -> SimpleNamespace:
    """主召回行替身（向量 SQL 输出形态）。heading 留空 → 不触发章节补全。"""
    mapping = {
        "id": cid,
        "document_id": DOC,
        "kb_id": KB,
        "content": f"分片 {cid}",
        "heading_path": "",
        "page_no": 1,
        "level_rank": 20,
        "acl_tags": [],
        "table_group_id": None,
        "governance_weight": 0,
        "vector_score": 0.80,
    }
    row = SimpleNamespace(**mapping)
    row._mapping = mapping  # noqa: SLF001  (retrieve 代码读 row._mapping)
    return row


class _AliasRow:
    """kb_terms 行替身（load_alias_map 读 .term / .aliases）。"""

    def __init__(self, term: str, aliases: list[str]) -> None:
        self.term = term
        self.aliases = aliases


class FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def fetchall(self) -> list:
        return list(self._rows)


class FakeSession:
    """按 SQL 特征分发的 session 替身；kb_terms 查询回放脚本化的别名行。"""

    def __init__(self, alias_rows: list[_AliasRow]) -> None:
        self._alias_rows = alias_rows

    async def execute(self, stmt, params=None):  # noqa: ANN001, ARG002
        sql = stmt.text if hasattr(stmt, "text") else str(stmt)
        if "<=>" in sql:
            return FakeResult([_row(uuid.UUID(int=100))])
        if "~*" in sql:
            return FakeResult([])
        if "ts_rank" in sql:
            return FakeResult([])
        if "filename" in sql:
            return FakeResult([])
        if "FROM kb_terms" in sql:
            return FakeResult(self._alias_rows)
        raise AssertionError(f"未识别的 SQL: {sql[:80]}")


class FakeEmbedding:
    async def embed_query(self, query: str) -> list[float]:  # noqa: ARG002
        return [0.01] * 8


class FakeRerank:
    async def rerank(self, query, documents, top_k):  # noqa: ANN001, ARG002
        return [(i, 0.80) for i in range(len(documents))]


async def _run(monkeypatch: pytest.MonkeyPatch, alias_rows: list[_AliasRow]) -> list[tuple]:
    """跑一次完整检索，返回本次所有 incr 调用（name, value, tags）。"""
    monkeypatch.setattr(retrieve_mod, "get_embedding_service", lambda: FakeEmbedding())
    monkeypatch.setattr(retrieve_mod, "get_rerank_service", lambda: FakeRerank())
    monkeypatch.setattr(
        retrieve_mod,
        "get_settings",
        lambda: fake_settings(
            rerank_enabled=True, graph_enabled=False, wiki_enabled=False
        ),
    )
    calls: list[tuple] = []

    def _record(name: str, value: int = 1, **tags: str) -> None:
        calls.append((name, value, tags))

    monkeypatch.setattr(retrieve_mod.metrics, "incr", _record)

    service = RetrievalService()
    result = await service.retrieve(
        FakeSession(alias_rows), "alpha beta", TENANT, [KB], 40, ["dept:it"]
    )
    assert not result.refused, "夹具前提失守：主召回被拒答，测不到 FTS 路径"
    return calls


def _metric_calls(calls: list[tuple]) -> list[tuple]:
    return [c for c in calls if c[0] == METRIC]


@pytest.mark.asyncio
async def test_alias_over_budget_reports_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """8 个去重别名词元、独立预算 6 → 只打 1 次，值为被裁掉的 2。"""
    # 访问令牌 → 访问/问令/令牌；刷新凭证 → 刷新/新凭/凭证；两个 ASCII 整词 → 共 8 个。
    rows = [
        _AliasRow("alpha", ["访问令牌", "abctoken"]),
        _AliasRow("beta", ["刷新凭证", "deftoken"]),
    ]
    calls = _metric_calls(await _run(monkeypatch, rows))

    assert decisions.LEXICON_FTS_ALIAS_MAX_TERMS == 6
    assert len(calls) == 1, f"应恰好打点 1 次，实际 {calls}"
    assert calls[0][1] == 2, "丢弃数 = 8 - 6"


@pytest.mark.asyncio
async def test_alias_exactly_at_budget_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    """恰好 6 个去重别名词元（未超预算）→ 不打点。"""
    rows = [
        _AliasRow("alpha", ["访问令牌", "abctoken"]),
        _AliasRow("beta", ["刷新凭"]),
    ]
    assert _metric_calls(await _run(monkeypatch, rows)) == []


@pytest.mark.asyncio
async def test_alias_drop_counts_deduped_terms_not_raw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """原始 12 个词元、去重后 8 个 → 值 = 2（按去重算，不按原始长度算）。

    四个 4 字别名各切 3 个 bigram，共 12 个原始词元；相邻别名有公共 bigram
    （访问 / 令牌 / 凭证 / 刷新），去重后恰好 8 个。若按原始长度减预算，
    会错报 6。
    """
    rows = [
        _AliasRow("alpha", ["访问令牌", "访问凭证"]),
        _AliasRow("beta", ["刷新凭证", "令牌刷新"]),
    ]
    calls = _metric_calls(await _run(monkeypatch, rows))

    assert len(calls) == 1, f"应恰好打点 1 次，实际 {calls}"
    assert calls[0][1] == 2, "丢弃数 = 去重 8 - 预算 6（不是原始 12 - 6）"


@pytest.mark.asyncio
async def test_tsquery_disabled_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    """KEYWORD_TSQUERY_ENABLED=False → 不走 tsquery 路，不展开、不打点。"""
    monkeypatch.setattr(decisions, "KEYWORD_TSQUERY_ENABLED", False)
    rows = [
        _AliasRow("alpha", ["访问令牌", "abctoken"]),
        _AliasRow("beta", ["刷新凭证", "deftoken"]),
    ]
    assert _metric_calls(await _run(monkeypatch, rows)) == []


@pytest.mark.asyncio
async def test_no_alias_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    """词表无命中（alias_map 为空）→ 不打点。"""
    assert _metric_calls(await _run(monkeypatch, [])) == []


@pytest.mark.asyncio
async def test_alias_expansion_disabled_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    """平台关闭词表扩展 → 不查词表、不展开、不打点。"""
    monkeypatch.setattr(decisions, "ALIAS_EXPANSION_ENABLED", False)
    rows = [
        _AliasRow("alpha", ["访问令牌", "abctoken"]),
        _AliasRow("beta", ["刷新凭证", "deftoken"]),
    ]
    assert _metric_calls(await _run(monkeypatch, rows)) == []
