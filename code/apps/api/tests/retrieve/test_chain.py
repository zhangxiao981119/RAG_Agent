"""检索侧链式推理单元测试（问题拆解 / 证据合并 / 编排降级）。

覆盖：
  · normalize_subquestions：原始问题恒首位 / 超长丢弃 / 去重 / 上限截断
  · decompose_question：JSON 非法 / 结构缺失 / LLM 异常 → ChainDecomposeError
  · merge_rounds：跨轮去重 / 跨轮 RRF 只当排序键（final_score 保留原值）/
    条数上限 / 闸门取 max / 口径混用保守化 / 回捞片去重合并 / stage_ms 累加
  · retrieve_chain：开关与信号决定模式 / 拆解降级单趟并打点 /
    拆解后单问题退化为单趟 / 多轮串行复用同一 session
"""
from __future__ import annotations

import uuid

import pytest

import app.services.retrieve as retrieve_mod
from app.config import decisions
from app.infra import metrics
from app.services.retrieve import RetrievalService
from app.services.retrieve import chain as chain_mod
from app.services.retrieve.base import RetrievalResult, RetrievedChunk
from app.services.retrieve.chain import (
    ChainDecomposeError,
    merge_rounds,
    normalize_subquestions,
)


@pytest.fixture(autouse=True)
def _isolate_metrics():
    """指标是全局计数器，用例之间必须隔离，否则计数断言会互相污染。"""
    metrics.reset()
    yield
    metrics.reset()


def _chunk(cid: uuid.UUID, final_score: float, *, recall: bool = False) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=cid,
        document_id=uuid.UUID(int=999),
        kb_id=uuid.UUID(int=998),
        filename="f.txt",
        content="内容",
        heading_path="",
        page_no=None,
        level_rank=1,
        vector_score=final_score,
        keyword_score=None,
        rrf_score=final_score,
        rerank_score=None,
        final_score=final_score,
        is_table_recall=recall,
    )


def _round(
    chunks: list[RetrievedChunk],
    *,
    use_rerank: bool = True,
    score_source: str = "rerank",
    stage_ms: dict[str, int] | None = None,
    path_stats: dict[str, int] | None = None,
    recall: list[RetrievedChunk] | None = None,
) -> RetrievalResult:
    return RetrievalResult(
        chunks=chunks,
        stage_ms=stage_ms if stage_ms is not None else {"retrieving": 10},
        score_source=score_source,
        use_rerank=use_rerank,
        path_stats=path_stats or {},
        table_recall_chunks=recall or [],
    )


# ── normalize_subquestions ────────────────────────────────────────


def test_normalize_keeps_original_first_and_caps_to_max():
    subs = normalize_subquestions(["子问题A", "子问题B", "子问题C", "子问题D"], "原始问题")
    assert subs[0] == "原始问题"
    assert len(subs) == decisions.CHAIN_MAX_SUBQUESTIONS


def test_normalize_drops_blank_oversize_and_duplicates():
    long_text = "长" * (decisions.CHAIN_SUBQUESTION_MAX_CHARS + 1)
    subs = normalize_subquestions(
        ["   ", "原始问题", "子问题A", long_text, "子问题A"], "原始问题"
    )
    # 空串 / 超长 / 重复（含与原始问题重复）全部丢弃
    assert subs == ["原始问题", "子问题A"]


def test_normalize_original_is_not_subject_to_char_limit():
    """原始问题是既定输入，超长也必须保留（截断会改变用户语义）。"""
    original = "原始问题" * 200
    subs = normalize_subquestions(["子问题A"], original)
    assert subs[0] == original
    assert "子问题A" in subs


# ── decompose_question ───────────────────────────────────────────


class _FakeLLM:
    def __init__(self, raw: str = "", exc: Exception | None = None) -> None:
        self.raw = raw
        self.exc = exc

    async def chat(self, messages, temperature=0.1):  # noqa: ANN001
        if self.exc is not None:
            raise self.exc
        return self.raw


def _patch_decompose_env(monkeypatch, raw: str = "", exc: Exception | None = None) -> None:
    async def _resolve(key, tenant_id=None):  # noqa: ANN001
        return "拆解 {question} {max} {max_chars}"

    monkeypatch.setattr(chain_mod.prompt_registry_service, "resolve_prompt", _resolve)
    monkeypatch.setattr(chain_mod, "get_llm_service", lambda: _FakeLLM(raw, exc))


@pytest.mark.asyncio
async def test_decompose_llm_error_raises(monkeypatch):
    _patch_decompose_env(monkeypatch, exc=RuntimeError("boom"))
    with pytest.raises(ChainDecomposeError):
        await chain_mod.decompose_question("问题")


@pytest.mark.asyncio
async def test_decompose_no_json_raises(monkeypatch):
    _patch_decompose_env(monkeypatch, raw="这里没有 JSON")
    with pytest.raises(ChainDecomposeError):
        await chain_mod.decompose_question("问题")


@pytest.mark.asyncio
async def test_decompose_missing_field_raises(monkeypatch):
    _patch_decompose_env(monkeypatch, raw='{"foo": 1}')
    with pytest.raises(ChainDecomposeError):
        await chain_mod.decompose_question("问题")


@pytest.mark.asyncio
async def test_decompose_ok_normalizes(monkeypatch):
    _patch_decompose_env(monkeypatch, raw='```json\n{"subquestions": ["子A", "子B"]}\n```')
    subs = await chain_mod.decompose_question("问题")
    assert subs == ["问题", "子A", "子B"]


# ── merge_rounds ──────────────────────────────────────────────────


def test_merge_all_rounds_empty_refuses():
    merged = merge_rounds([_round([]), _round([])])
    assert merged.refused is True
    assert merged.refuse_reason == "NO_RELEVANT_CONTENT"


def test_merge_dedups_same_chunk_across_rounds_and_takes_max_score():
    shared = uuid.uuid4()
    merged = merge_rounds([_round([_chunk(shared, 0.8)]), _round([_chunk(shared, 0.9)])])
    assert len(merged.chunks) == 1
    assert merged.chunks[0].final_score == 0.9


def test_merge_orders_by_cross_round_rrf_but_keeps_final_score():
    """跨轮 RRF 只当排序键 —— final_score MUST NOT 被写回 RRF 分。

    被判为「跨轮命中越多排越前」，但同时 final_score 必须仍是各轮原值最大值，
    否则 L1 阈值口径（已按 rerank/vector 分标定）会整体失效。
    """
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    r1 = _round([_chunk(a, 0.95), _chunk(b, 0.60)])
    r2 = _round([_chunk(b, 0.60), _chunk(c, 0.70)])
    merged = merge_rounds([r1, r2])
    assert [x.chunk_id for x in merged.chunks] == [b, a, c]
    # final_score 仍是原值，不是 RRF 分（量级 0.03 上下）
    assert merged.chunks[0].final_score == 0.60


def test_merge_caps_to_top_k_rerank():
    chunks = [_chunk(uuid.uuid4(), 0.9) for _ in range(decisions.TOP_K_RERANK + 5)]
    merged = merge_rounds([_round(chunks)])
    assert len(merged.chunks) == decisions.TOP_K_RERANK


def test_merge_gate_refuses_when_max_below_threshold():
    merged = merge_rounds([_round([_chunk(uuid.uuid4(), 0.10)])])
    assert merged.refused is True
    assert merged.refuse_reason == "NO_RELEVANT_CONTENT"


def test_merge_gate_passes_when_max_above_threshold():
    merged = merge_rounds([_round([_chunk(uuid.uuid4(), 0.90)])])
    assert merged.refused is False


def test_merge_mixed_rerank_rounds_fall_back_to_vector_conservatively():
    """任一轮重排降级 → 整体按 vector 口径（保守），不冒险用 rerank 阈值。"""
    r1 = _round([_chunk(uuid.uuid4(), 0.90)], use_rerank=True, score_source="rerank")
    r2 = _round([_chunk(uuid.uuid4(), 0.30)], use_rerank=False, score_source="vector")
    merged = merge_rounds([r1, r2])
    assert merged.use_rerank is False
    assert merged.score_source == "vector"
    assert merged.refused is False


def test_merge_dedups_table_recall_chunks():
    shared_recall = _chunk(uuid.uuid4(), 0.0, recall=True)
    r1 = _round([_chunk(uuid.uuid4(), 0.9)], recall=[shared_recall])
    r2 = _round([_chunk(uuid.uuid4(), 0.9)], recall=[shared_recall])
    merged = merge_rounds([r1, r2])
    assert len(merged.table_recall_chunks) == 1
    assert merged.table_recall_chunks[0].is_table_recall is True


def test_merge_accumulates_stage_ms_and_path_stats():
    r1 = _round(
        [_chunk(uuid.uuid4(), 0.9)],
        stage_ms={"retrieving": 10, "keyword": 5},
        path_stats={"vector_covered": 2},
    )
    r2 = _round(
        [_chunk(uuid.uuid4(), 0.9)],
        stage_ms={"retrieving": 20, "keyword": 7},
        path_stats={"vector_covered": 3},
    )
    merged = merge_rounds([r1, r2])
    assert merged.stage_ms["retrieving"] == 30
    assert merged.stage_ms["keyword"] == 12
    assert merged.path_stats["vector_covered"] == 5


# ── retrieve_chain 编排 ────────────────────────────────────────────


def _service_with_stub(queries: list[str], sessions: list[int]) -> RetrievalService:
    """返回一个 retrieve() 被替换成替身的 RetrievalService。"""
    svc = RetrievalService()

    async def _stub(sess, query, tenant_id, kb_ids, clearance, subjects):  # noqa: ANN001
        sessions.append(id(sess))
        queries.append(query)
        return _round([_chunk(uuid.uuid4(), 0.9)])

    svc.retrieve = _stub
    return svc


@pytest.mark.asyncio
async def test_chain_high_score_signal_returns_single(monkeypatch):
    queries: list[str] = []
    svc = _service_with_stub(queries, [])
    result = await svc.retrieve_chain(
        object(), "问题", uuid.uuid4(), [uuid.uuid4()], 1, [], rewrite_score=95
    )
    assert result.chain_mode == "single"
    assert queries == ["问题"]


@pytest.mark.asyncio
async def test_chain_none_score_returns_single(monkeypatch):
    """拿不到复杂度信号（rewrite_score=None）→ 不做无差别多轮检索。"""
    queries: list[str] = []
    svc = _service_with_stub(queries, [])
    result = await svc.retrieve_chain(
        object(), "问题", uuid.uuid4(), [uuid.uuid4()], 1, [], rewrite_score=None
    )
    assert result.chain_mode == "single"
    assert queries == ["问题"]


@pytest.mark.asyncio
async def test_chain_switch_off_returns_off(monkeypatch):
    monkeypatch.setattr(decisions, "CHAIN_RETRIEVAL_ENABLED", False)
    queries: list[str] = []
    svc = _service_with_stub(queries, [])
    result = await svc.retrieve_chain(
        object(), "问题", uuid.uuid4(), [uuid.uuid4()], 1, [], rewrite_score=50
    )
    assert result.chain_mode == "off"
    assert queries == ["问题"]


@pytest.mark.asyncio
async def test_chain_decompose_error_degrades_and_counts(monkeypatch):
    queries: list[str] = []
    svc = _service_with_stub(queries, [])

    async def _boom(question, tenant_id=None):  # noqa: ANN001
        raise ChainDecomposeError("拆解不可用")

    monkeypatch.setattr(retrieve_mod, "decompose_question", _boom)
    result = await svc.retrieve_chain(
        object(), "问题", uuid.uuid4(), [uuid.uuid4()], 1, [], rewrite_score=50
    )
    assert result.chain_mode == "single"
    assert queries == ["问题"]
    assert metrics.get("rag.chain.degraded", reason="decompose_error") == 1


@pytest.mark.asyncio
async def test_chain_single_subquestion_degrades_to_single(monkeypatch):
    queries: list[str] = []
    svc = _service_with_stub(queries, [])

    async def _one(question, tenant_id=None):  # noqa: ANN001
        return ["问题"]  # 拆解判定已足够聚焦，只有一个子问题

    monkeypatch.setattr(retrieve_mod, "decompose_question", _one)
    result = await svc.retrieve_chain(
        object(), "问题", uuid.uuid4(), [uuid.uuid4()], 1, [], rewrite_score=50
    )
    assert result.chain_mode == "single"
    assert queries == ["问题"]
    # 拆解耗时仍被记录（即使退化单趟）
    assert "chain_decompose" in result.stage_ms


@pytest.mark.asyncio
async def test_chain_multi_reuses_same_session_serially(monkeypatch):
    session = object()
    queries: list[str] = []
    sessions: list[int] = []
    svc = _service_with_stub(queries, sessions)

    async def _three(question, tenant_id=None):  # noqa: ANN001
        return ["问题", "子问题二", "子问题三"]

    monkeypatch.setattr(retrieve_mod, "decompose_question", _three)
    result = await svc.retrieve_chain(
        session, "问题", uuid.uuid4(), [uuid.uuid4()], 1, [], rewrite_score=50
    )
    assert result.chain_mode == "multi:3"
    assert result.subquestions == ["问题", "子问题二", "子问题三"]
    assert queries == ["问题", "子问题二", "子问题三"]
    # 串行复用同一 session（同一对象 → 同一 id）
    assert sessions == [id(session)] * 3
    assert "chain_decompose" in result.stage_ms
    assert metrics.get("rag.chain.rounds") == 3
