"""WU-M-17 检索期治理加权测试 —— rerank 后叠加 / NULL 按 0 / 时效优先。

边界全替身：embedding、rerank、session 全部 monkeypatch，零网络零 DB。
钉住八件事：
  1. 三路召回 SQL 均带出 COALESCE(governance_weight, 0)（INV-M-12 SQL 层）
  2. _WHERE_BASE 含 is_latest = true（INV-M-14 钉住既有过滤）
  3. rerank 在线：final = rerank 分 + weight * SCALE，治理影响排序
  4. rerank 降级：final = 向量分 + weight * SCALE
  5. weight=0（未审查）final 与 base 精确相等 —— eval 基线零影响（DoD）
  6. 正权重可把临界分推过 L1 阈值；负权重可把临界分拉到阈值下 → refused
  7. INV-M-13：治理加权不出现在 RRF，且位于 rerank 读取之后（源码序断言）
  8. display_score 不受治理偏移污染（前端展示仍是原分口径）
"""
from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace

import pytest

from app.config import decisions
from app.services import retrieve as retrieve_mod
from app.services.retrieve import RetrievalService
from app.services.rerank import RerankTimeout
from tests.fake_settings import fake_settings

TENANT = uuid.uuid4()
KB = uuid.uuid4()
DOC = uuid.uuid4()
S = decisions.GOVERNANCE_WEIGHT_SCALE


def _row(cid: uuid.UUID, weight: int, vector_score: float) -> SimpleNamespace:
    """构造一行召回结果（Row 替身：属性访问 + _mapping 双通道）。"""
    mapping = {
        "id": cid,
        "document_id": DOC,
        "kb_id": KB,
        "content": f"内容 {cid}",
        "heading_path": "标题",
        "page_no": 1,
        "level_rank": 20,
        "acl_tags": [],
        "governance_weight": weight,
        "vector_score": vector_score,
    }
    row = SimpleNamespace(**mapping)
    row._mapping = mapping  # noqa: SLF001  (retrieve 代码读 row._mapping)
    return row


class FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def fetchall(self) -> list:
        return list(self._rows)


class FakeSession:
    """按 SQL 特征分发的 session 替身（不执行真 SQL）。"""

    def __init__(self, h: "Harness") -> None:
        self._h = h

    async def execute(self, stmt, params=None):  # noqa: ANN001, ARG002
        sql = stmt.text if hasattr(stmt, "text") else str(stmt)
        if "<=>" in sql:
            return FakeResult(self._h.vector_rows)
        if "~*" in sql:
            return FakeResult([])
        if "ts_rank" in sql:
            return FakeResult([])
        if "filename" in sql:
            return FakeResult([])
        if "FROM kb_terms" in sql:
            return FakeResult([])
        if "v(document_id, heading_path)" in sql:
            return FakeResult([])
        raise AssertionError(f"未识别的 SQL: {sql[:80]}")


class FakeEmbedding:
    async def embed_query(self, query: str) -> list[float]:  # noqa: ARG002
        return [0.01] * 8


class FakeRerank:
    def __init__(self, h: "Harness") -> None:
        self._h = h

    async def rerank(self, query, documents, top_k):  # noqa: ANN001, ARG002
        item = self._h.rerank_result
        if isinstance(item, Exception):
            raise item
        return list(item)


class Harness:
    def __init__(self, rows: list) -> None:
        self.vector_rows = rows
        self.rerank_result: list | Exception = []


@pytest.fixture
def svc(monkeypatch: pytest.MonkeyPatch):
    """构造替身齐全的 RetrievalService。用例内再设置 harness 细节。"""

    def _make(h: Harness):
        monkeypatch.setattr(retrieve_mod, "get_embedding_service", lambda: FakeEmbedding())
        monkeypatch.setattr(retrieve_mod, "get_rerank_service", lambda: FakeRerank(h))
        # ★ 重排默认关闭（settings.rerank_enabled=false）—— 本套用例专测 rerank 在线 / 降级
        #   两种口径，必须显式打开开关，否则 get_rerank_service 根本不会被调用、用例覆盖路径失效。
        monkeypatch.setattr(
            retrieve_mod,
            "get_settings",
            # graph_enabled=False：本文件测治理加权的阈值口径，图召回与此无关
            lambda: fake_settings(
                rerank_enabled=True, graph_enabled=False, wiki_enabled=False
            ),
        )
        # 关掉 FTS 路径：审查加权行为与关键词路无关，减少分支
        monkeypatch.setattr(decisions, "KEYWORD_TSQUERY_ENABLED", False)
        # retrieve(session, ...) 的 session 由调用方管理（生产是 API 层传 AsyncSession）
        return RetrievalService(), lambda: FakeSession(h)

    return _make


async def _retrieve(svc_pair, query: str = "报销制度"):
    service, make_session = svc_pair
    return await service.retrieve(
        make_session(),
        query,
        TENANT,
        [KB],
        40,
        ["dept:it"],
    )


# ── SQL 与不变量（无需跑流程）────────────────────────────


def test_sql_carries_governance_weight() -> None:
    """INV-M-12：三路召回均带 COALESCE(governance_weight, 0)。"""
    assert retrieve_mod._VECTOR_SQL_TEMPLATE.count("COALESCE(c.governance_weight, 0)") == 1
    assert "COALESCE(c.governance_weight, 0)" in retrieve_mod._KEYWORD_SQL.text
    assert "COALESCE(c.governance_weight, 0)" in retrieve_mod._KEYWORD_FTS_SQL.text


def test_where_base_filters_is_latest() -> None:
    """INV-M-14：非最新版本 MUST NOT 进入上下文。"""
    assert "c.is_latest = true" in retrieve_mod._WHERE_BASE


def test_inv_m_13_not_in_rrf_and_after_rerank() -> None:
    """治理加权不出现在 RRF；源码序上位于 rerank 分读取之后（rerank 之后叠加）。"""
    rrf_src = inspect.getsource(retrieve_mod._build_rrf_scores)
    assert "governance" not in rrf_src.lower()
    src = inspect.getsource(retrieve_mod)
    assert src.index("rerank_scores.get") < src.index("base_score + weight")


# ── 加权行为 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rerank_online_weight_shifts_final(svc) -> None:
    """rerank 在线：同分候选按治理权重重排，display_score 保持原分。"""
    cid_a, cid_b = uuid.uuid4(), uuid.uuid4()
    h = Harness([_row(cid_a, 40, 0.80), _row(cid_b, 0, 0.80)])
    h.rerank_result = [(0, 0.70), (1, 0.70)]
    result = await _retrieve(svc(h))

    assert not result.refused
    by_id = {c.chunk_id: c for c in result.chunks}
    assert by_id[cid_a].final_score == pytest.approx(0.70 + 40 * S)
    assert by_id[cid_b].final_score == pytest.approx(0.70)
    # 治理优秀的候选排前（rerank 同分时 weight 决定次序）
    assert result.chunks[0].chunk_id == cid_a
    # display_score 展示口径不受偏移污染
    assert by_id[cid_a].display_score == pytest.approx(0.70)


@pytest.mark.asyncio
async def test_rerank_degraded_weight_applies(svc) -> None:
    """rerank 降级：final = 向量分 + weight * SCALE。"""
    cid = uuid.uuid4()
    h = Harness([_row(cid, -20, 0.60)])
    h.rerank_result = RerankTimeout("timeout")
    result = await _retrieve(svc(h))

    assert not result.refused
    assert result.use_rerank is False
    assert result.chunks[0].final_score == pytest.approx(0.60 - 20 * S)


@pytest.mark.asyncio
async def test_unreviewed_weight_zero_unchanged(svc) -> None:
    """DoD：未审查（weight=0）final 与 base 精确相等，eval 基线零影响。"""
    cid = uuid.uuid4()
    h = Harness([_row(cid, 0, 0.66)])
    h.rerank_result = [(0, 0.66)]
    result = await _retrieve(svc(h))

    assert result.chunks[0].final_score == pytest.approx(0.66)
    assert result.chunks[0].final_score == result.chunks[0].rerank_score


@pytest.mark.asyncio
async def test_positive_weight_pushes_over_gate(svc) -> None:
    """治理优秀可把临界分推过 L1 阈值（rerank 在线口径）。"""
    cid = uuid.uuid4()
    h = Harness([_row(cid, 50, 0.90)])
    h.rerank_result = [(0, decisions.RELEVANCE_THRESHOLD_RERANK - 0.005)]
    result = await _retrieve(svc(h))

    assert not result.refused  # (threshold-0.005) + 50*0.001 = threshold+0.045
    assert result.chunks[0].final_score == pytest.approx(
        decisions.RELEVANCE_THRESHOLD_RERANK + 0.045
    )


@pytest.mark.asyncio
async def test_negative_weight_pulls_under_gate(svc) -> None:
    """治理差（废止/矛盾）可把临界分拉到阈值下 → 拒答。"""
    cid = uuid.uuid4()
    h = Harness([_row(cid, -50, 0.90)])
    h.rerank_result = [(0, decisions.RELEVANCE_THRESHOLD_RERANK + 0.005)]
    result = await _retrieve(svc(h))

    assert result.refused  # (threshold+0.005) - 0.05 = threshold-0.045
    assert result.refuse_reason == "NO_RELEVANT_CONTENT"


# ── DQ4B-8：降级可观测（Step 4B-1）──────────────────────────────────────
#
# ★ 为什么这条放在本文件：这里有全仓唯一一套「三路召回的 session 替身」
#   （`FakeSession` 按 SQL 特征分发）。自己再写一套等于第二份口径，
#   而第二份口径分叉时不会有任何信号（`pg_helpers` 模块 docstring 的同一理由）。


@pytest.mark.asyncio
async def test_vector_empty_metric_is_observable_and_label_stable(svc) -> None:
    """向量路回 0 行时打 `rag.caliber.vector_empty{keyword_hit="no"}`（降级不再隐形）。

    破坏方式（必须变红）：删掉 `retrieve/__init__.py` 里 `if not vector_rows:` 那段
    `metrics.incr(...)` → 计数为 0、序列键不存在 → 两条断言都红。

    ★ 为什么这条重要（F-18 / DQ4B-8）：换模型或改向量文本公式后，旧向量被判为
      不同构 → 向量路空集 → 主检索退化为仅关键词。这是
      `DNA与Policy口径对照表-v1.md:14` 明确写为**预期行为**的降级路径，
      但在此之前它**完全不可见**：本模块 30 余处打点没有一条与"某路回 0 行"有关。
      没有它，运维只能从"答案变差了"倒推。

    ★ 为什么这里断言**标签集**而不只是数值：`metrics._counter()` 只按 name 缓存，
      首次调用时的标签集就固定下来。若第一处调用不带 `keyword_hit`、后来某处带上，
      prometheus 客户端会直接 ValueError（进程内异常，且只在特定请求序列下出现）。
      所以"永远带同一个标签"这件事必须被钉住。
    """
    from app.infra import metrics as metrics_mod

    metrics_mod.reset()
    result = await _retrieve(svc(Harness([])))
    assert result.refused, "三路全空必须拒绝，而不是编一个答案"

    assert metrics_mod.get("rag.caliber.vector_empty", keyword_hit="no") == 1, (
        "向量路回 0 行没有打点 —— 降级不可观测（F-18）"
    )
    keys = [
        key for key in metrics_mod.snapshot()["counters"] if "vector_empty" in key
    ]
    # 序列键形如 `rag_caliber_vector_empty|keyword_hit=no`（`metrics._series_key`
    # 用 `|k=v` 而不是 prometheus 的 `{k="v"}`；名字里的 `.` 已被 `_normalize` 换成 `_`）。
    # 只钉两件事，都是这条打点的**契约**：恰好一条序列 + 标签恰好是 `keyword_hit`。
    assert len(keys) == 1, f"打点缺失或出现了多条序列：{keys}"
    assert keys[0].endswith("|keyword_hit=no"), (
        f"标签集不稳定或不符预期：{keys}"
    )
