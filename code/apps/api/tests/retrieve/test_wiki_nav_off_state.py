"""第 4 步 · wiki 导航在 `retrieve()` 里的两路接入与关闭态等价（段 B）。

★ 本文件是本步**最高优先级**的回归证据：`WIKI_ENABLED=false` 必须与「未实现
  第 4 步」逐字节等价。团队 wiki 底座条目 5：探针只能定位原因，**不能替代评测**；
  这里证明的是「代码路径没变」，217 条指标不劣化仍须真库评测（本机无 PG，未跑）。

钉住设计 §2.3.2 ①~⑥ / §2.6 ①：
  1. 关闭态：导航 SQL 0 条、`app.services.wiki.navigate` 未被 import、
     `stages` / `path_stats` 无 wiki_* 键、pattern 与基线逐字节相同
  2. `:needle` 恒等于 `ilike_terms[0].lower()` —— 换成 wiki 词元会改 ORDER BY
     定序 → RRF 漂移 → 217 条基线风险（开/关两态都要钉住）
  3. 开启态：一次查页两路复用，`extra_terms` / `extra_index_tokens` 注入附加
     OR 分支 / OR 项，**原词元集合冻结**；`path_stats` 两键是 int
  4. 降级（超时 / 无页）时 wiki_terms=[] → 两路实参走原分支，与关闭态字节相同
  5. ③-b 每个开启态请求都打桶；`WIKI_NAV_MAX_TERMS=2` 是硬上限，
     `bucket="3"`（gt2）结构性恒为 0 —— 这是「上限没被绕过」的可证事实
"""
from __future__ import annotations

import sys
import uuid
from types import SimpleNamespace

import pytest

from app.config import decisions
from app.config.wiki_profile import WikiProfile
from app.services import retrieve as retrieve_mod
from app.services.keyword_text import build_ilike_pattern, build_ilike_terms
from app.services.retrieve import RetrievalService

TENANT = uuid.UUID(int=9)
KB = uuid.UUID(int=1)
DOC = uuid.UUID(int=2)
QUERY = "访问令牌怎么配置"
PROFILE = WikiProfile(generator_model="qwen-test", prompt_version="wprompt-v1")


def _row(cid: uuid.UUID) -> SimpleNamespace:
    mapping = {
        "id": cid,
        "document_id": DOC,
        "kb_id": KB,
        "content": "访问令牌的配置方法",
        "heading_path": "第一章",
        "page_no": 1,
        "level_rank": 20,
        "acl_tags": [],
        "table_group_id": None,
        "governance_weight": 0,
        "vector_score": 0.80,
    }
    row = SimpleNamespace(**mapping)
    row._mapping = mapping  # noqa: SLF001
    return row


class FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def fetchall(self) -> list:
        return list(self._rows)


class FakeSession:
    """按 SQL 特征分发；任何 wiki / 图 / set_config SQL 都记下来并断言失败。"""

    def __init__(self, rows: list) -> None:
        self._rows = rows
        self.sql: list[str] = []
        self.params: list[dict] = []
        self.wiki_sql_seen: list[str] = []

    async def execute(self, stmt, params=None):  # noqa: ANN001, ANN201
        sql = stmt.text if hasattr(stmt, "text") else str(stmt)
        params = dict(params or {})
        self.sql.append(sql)
        self.params.append(params)
        if any(k in sql for k in ("wiki_pages", "set_config", "statement_timeout")):
            self.wiki_sql_seen.append(sql)
            raise AssertionError("wiki 导航 SQL 不该在替身里被执行（已打桩）")
        if "<=>" in sql:
            return FakeResult(self._rows)
        for marker in ("~*", "ts_rank", "filename", "FROM kb_terms",
                       "table_group_id = :group_id", "v(document_id, heading_path)"):
            if marker in sql:
                return FakeResult([])
        raise AssertionError(f"未识别的 SQL: {sql[:90]}")

    async def rollback(self) -> None:
        return None


class FakeEmbedding:
    async def embed_query(self, query: str) -> list[float]:  # noqa: ARG002
        return [0.01] * 8


class FakeRerank:
    async def rerank(self, query, documents, top_k):  # noqa: ANN001, ARG002
        return []


@pytest.fixture(autouse=True)
def _common_stubs(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """替身基础设施 + 捕获 metrics 打点（不污染进程级 prometheus 计数器）。"""
    monkeypatch.setattr(retrieve_mod, "get_embedding_service", lambda: FakeEmbedding())
    monkeypatch.setattr(retrieve_mod, "get_rerank_service", lambda: FakeRerank())
    monkeypatch.setattr(decisions, "KEYWORD_TSQUERY_ENABLED", False)
    monkeypatch.setattr(
        "app.config.wiki_profile.current_wiki_profile", lambda: PROFILE
    )
    calls: list[tuple] = []

    def _incr(name: str, value: int = 1, **tags: str) -> None:
        calls.append(("incr", name, value, tags))

    def _observe(name: str, milliseconds: float, **tags: str) -> None:
        calls.append(("observe", name, milliseconds, tags))

    monkeypatch.setattr(retrieve_mod.metrics, "incr", _incr)
    monkeypatch.setattr(retrieve_mod.metrics, "observe_ms", _observe)
    return calls


def _settings(monkeypatch: pytest.MonkeyPatch, *, wiki_enabled: bool) -> None:
    monkeypatch.setattr(
        retrieve_mod,
        "get_settings",
        lambda: SimpleNamespace(
            rerank_enabled=False,
            graph_enabled=False,
            graph_retrieve_timeout_ms=800,
            wiki_enabled=wiki_enabled,
            wiki_nav_timeout_ms=50,
        ),
    )


def _stub_nav(monkeypatch: pytest.MonkeyPatch, page) -> dict:
    """打桩查页函数（① 是函数内 import，monkeypatch 模块属性即生效）。"""
    calls: dict = {}

    async def _fake(session, **kwargs):  # noqa: ANN001, ARG001
        calls.update(kwargs)
        return page

    import app.services.wiki.navigate as nav_mod

    monkeypatch.setattr(nav_mod, "load_wiki_nav_page", _fake)
    return calls


async def _retrieve() -> tuple:
    session = FakeSession([_row(uuid.uuid4())])
    result = await RetrievalService().retrieve(
        session, QUERY, TENANT, [KB], 40, ["dept:it"]
    )
    return result, session


def _keyword_params(session: FakeSession) -> dict:
    idx = next(i for i, sql in enumerate(session.sql) if "~*" in sql)
    return session.params[idx]


# ── 关闭态：逐字节等价 ──────────────────────────────────────
@pytest.mark.asyncio
async def test_off_state_issues_zero_nav_sql_and_never_imports_navigate(
    monkeypatch, _common_stubs
) -> None:
    _settings(monkeypatch, wiki_enabled=False)
    # 清掉其它测试可能留下的模块缓存，才能证明「本次请求没有触发 import」
    monkeypatch.delitem(sys.modules, "app.services.wiki.navigate", raising=False)

    result, session = await _retrieve()

    assert session.wiki_sql_seen == [], "关闭态一条导航 / set_config SQL 都不该发"
    assert "app.services.wiki.navigate" not in sys.modules, "关闭态连模块都不该加载"
    assert "wiki_nav" not in result.stage_ms
    assert not [k for k in result.path_stats if k.startswith("wiki_")]
    assert not [c for c in _common_stubs if "wiki" in str(c[1])], (
        "关闭态不该打任何 wiki 指标"
    )


@pytest.mark.asyncio
async def test_off_state_pattern_and_needle_are_baseline(monkeypatch, _common_stubs) -> None:
    _settings(monkeypatch, wiki_enabled=False)
    result, session = await _retrieve()
    params = _keyword_params(session)
    terms = build_ilike_terms(QUERY)

    assert params["pattern"] == build_ilike_pattern(terms, {}), (
        "关闭态的 pattern 必须与未传 extra_terms 时逐字节相同"
    )
    assert params["needle"] == terms[0].lower()


@pytest.mark.asyncio
async def test_off_state_path_stats_keys_match_pre_step4_shape(
    monkeypatch, _common_stubs
) -> None:
    """关闭态 path_stats 逐键可比对：只有三路 covered/unique 六个键。"""
    _settings(monkeypatch, wiki_enabled=False)
    result, _ = await _retrieve()
    assert set(result.path_stats) == {
        "vector_covered", "vector_unique",
        "tsquery_covered", "tsquery_unique",
        "ilike_covered", "ilike_unique",
    }
    assert all(isinstance(v, int) for v in result.path_stats.values())


# ── 开启态：两路接入 ────────────────────────────────────────
@pytest.mark.asyncio
async def test_on_state_expands_both_paths_with_one_page_lookup(
    monkeypatch, _common_stubs
) -> None:
    from app.services.wiki.navigate import WikiNavPage

    _settings(monkeypatch, wiki_enabled=True)
    nav_calls = _stub_nav(
        monkeypatch,
        WikiNavPage(
            document_id=DOC,
            key_terms=("审计日志", "操作留痕"),
            elapsed_ms=1.25,
        ),
    )
    result, session = await _retrieve()
    params = _keyword_params(session)
    terms = build_ilike_terms(QUERY)
    baseline = build_ilike_pattern(terms, {})

    assert nav_calls["needles"] == terms, "查页判定用**未扩展**的 ilike_terms"
    assert nav_calls["timeout_ms"] == 50
    assert len([c for c in _common_stubs if c[1] == "rag.retrieve.wiki_nav"]) == 1, (
        "一次请求只查一次页"
    )
    assert params["pattern"] != baseline, "命中页后 ILIKE 应多出附加 OR 分支"
    assert any(t in params["pattern"] for t in ("审计日志", "操作留痕"))
    # ★ 原词元集合冻结：扩展只追加分支，不重排、不挤掉原词元
    assert all(t in params["pattern"] for t in terms)
    # ★ :needle 仍是原 query 最长词元，绝不能被 wiki 词元替换
    assert params["needle"] == terms[0].lower()

    # ⑥ path_stats 两键是 int，且不出现原因字符串
    assert result.path_stats["wiki_nav_hits"] == 1
    assert isinstance(result.path_stats["wiki_nav_hits"], int)
    assert isinstance(result.path_stats["wiki_nav_terms"], int)
    assert 0 <= result.path_stats["wiki_nav_terms"] <= decisions.WIKI_NAV_MAX_TERMS
    assert "wiki_nav" in result.stage_ms
    assert all(isinstance(v, int) for v in result.path_stats.values())


@pytest.mark.asyncio
async def test_on_state_always_buckets_and_never_emits_gt2(
    monkeypatch, _common_stubs
) -> None:
    """③-b：每个开启态请求都打桶（含 0 词元）；bucket="3" 结构性恒 0。"""
    from app.services.wiki.navigate import WikiNavPage

    _settings(monkeypatch, wiki_enabled=True)
    _stub_nav(monkeypatch, WikiNavPage(document_id=DOC, key_terms=("审计日志",)))

    await _retrieve()
    buckets = [c for c in _common_stubs if c[1] == "rag.wiki.nav.terms_bucket"]
    assert len(buckets) == 1, "每个开启态请求恰好打一次桶"
    assert buckets[0][3]["bucket"] in {"0", "1", "2"}
    assert not [c for c in _common_stubs
                if c[1] == "rag.wiki.nav.terms_bucket" and c[3].get("bucket") == "3"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["timeout", "no_match"])
async def test_degrade_keeps_pattern_byte_identical_to_off_state(
    monkeypatch, _common_stubs, reason
) -> None:
    """降级不改变两路实参形状：extra_* 传空序列 → 两个构造函数走原分支。"""
    from app.services.wiki.navigate import WikiNavPage

    _settings(monkeypatch, wiki_enabled=True)
    _stub_nav(monkeypatch, WikiNavPage(degraded_reason=reason))
    result, session = await _retrieve()
    terms = build_ilike_terms(QUERY)

    assert _keyword_params(session)["pattern"] == build_ilike_pattern(terms, {})
    assert _keyword_params(session)["needle"] == terms[0].lower()
    assert result.path_stats["wiki_nav_hits"] == 0
    assert result.path_stats.get("wiki_nav_terms", 0) == 0
