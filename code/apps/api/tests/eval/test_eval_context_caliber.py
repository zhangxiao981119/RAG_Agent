"""评测上下文口径（`with_recall_chunks`）测试。

钉住四件事：
  1. **默认口径逐字节不变**：仍调 `retrieve()`、仍只喂 `result.chunks` ——
     这是「旧基线继续可比」的唯一前提，破在这条上，全部历史分数作废。
  2. 开启后走 `retrieve_chain()`（与生产 chat 同一入口）+ 四类全喂，
     且 `rewrite_score` 传 `None`（不伪造复杂度信号）。
  3. `chunks_count` 与 `recall_chunks_count` 分得清「命中片变多」和「补充片变多」。
  4. `chat.py` 与 `eval_service.py` 用的是**同一个** `assemble_context_chunks`
     —— 谁把某处改回手写拼接，这里就红。
"""
from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace

import pytest

from app.services import eval_service
from app.services.eval_service import (
    CONTEXT_CALIBER_CHUNKS_ONLY,
    CONTEXT_CALIBER_WITH_RECALL,
    run_eval,
)
from app.services.retrieve.base import (
    RetrievalResult,
    RetrievedChunk,
    assemble_context_chunks,
    recall_chunks_count,
)

TENANT = uuid.uuid4()
KB = uuid.UUID(int=1)
DOC = uuid.UUID(int=2)


def _chunk(cid: int, **flags) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.UUID(int=cid), document_id=DOC, kb_id=KB, filename="制度.pdf",
        content=f"片 {cid}", heading_path="第一章", page_no=1, level_rank=20,
        vector_score=0.8, keyword_score=None, rrf_score=0.01, rerank_score=None,
        final_score=0.8, **flags,
    )


def _result() -> RetrievalResult:
    return RetrievalResult(
        chunks=[_chunk(10), _chunk(11)],
        table_recall_chunks=[_chunk(20, is_table_recall=True)],
        section_recall_chunks=[_chunk(30, is_section_recall=True)],
        graph_recall_chunks=[_chunk(40, is_graph_recall=True)],
        path_stats={"graph_covered": 2, "graph_unique": 1, "vector_covered": 2},
        graph_degraded_reason="",
    )


class _Scalars:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return list(self._rows)


class _Result:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self) -> _Scalars:
        return _Scalars(self._rows)


class FakeSession:
    def __init__(self, cases: list, kb_ids: list) -> None:
        self._cases = cases
        self._kb_ids = kb_ids

    async def execute(self, stmt):  # noqa: ANN001
        return _Result(self._cases)

    async def scalars(self, stmt):  # noqa: ANN001
        return _Scalars(self._kb_ids)


class FakeRetrieval:
    """记录走了哪个入口、收到什么参数（位置参数顺序也在断言范围内）。"""

    def __init__(self, result: RetrievalResult) -> None:
        self.result = result
        self.retrieve_calls: list[tuple] = []
        self.chain_calls: list[dict] = []

    async def retrieve(self, session, query, tenant_id, authorized_kb_ids,
                       clearance, user_subjects):  # noqa: ANN001
        self.retrieve_calls.append(
            (query, tenant_id, tuple(authorized_kb_ids), clearance, tuple(user_subjects))
        )
        return self.result

    async def retrieve_chain(self, session, query, tenant_id, authorized_kb_ids,
                             clearance, user_subjects, rewrite_score=None):  # noqa: ANN001
        self.chain_calls.append({
            "positional": (query, tenant_id, tuple(authorized_kb_ids), clearance,
                           tuple(user_subjects)),
            "rewrite_score": rewrite_score,
        })
        return self.result


class FakeGeneration:
    def __init__(self) -> None:
        self.seen: list[list[uuid.UUID]] = []

    async def generate(self, question, chunks, history=None, memory_prompt=""):  # noqa: ANN001
        self.seen.append([c.chunk_id for c in chunks])
        return SimpleNamespace(refused=False, refuse_reason=None)


def _case(question: str = "报销标准是什么", *, answerable: bool = True,
          kb_id: uuid.UUID | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        question=question, expected_answerable=answerable,
        expected_doc_ids=[], kb_id=kb_id,
    )


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch):
    def _make(result: RetrievalResult | None = None):
        retrieval = FakeRetrieval(result or _result())
        generation = FakeGeneration()
        monkeypatch.setattr(eval_service, "RetrievalService", lambda: retrieval)
        monkeypatch.setattr(eval_service, "get_generation_service", lambda: generation)
        return retrieval, generation

    return _make


# ── 共享拼装函数 ────────────────────────────────────────────
def test_assemble_order_and_counts():
    result = _result()
    out = assemble_context_chunks(result)
    assert [c.chunk_id for c in out] == [
        uuid.UUID(int=10), uuid.UUID(int=11),   # 命中片
        uuid.UUID(int=20),                      # 表格回捞
        uuid.UUID(int=30),                      # 章节补全
        uuid.UUID(int=40),                      # 图召回（最弱，超限时最先被裁）
    ]
    assert recall_chunks_count(result) == 3


def test_assemble_handles_empty_lists():
    assert assemble_context_chunks(RetrievalResult()) == []
    assert recall_chunks_count(RetrievalResult()) == 0


def test_assemble_is_single_source_for_chat_and_eval():
    """两处 MUST 都调同一个函数：谁改回手写拼接，评测就不再测生产行为了。"""
    from app.api import chat as chat_mod

    for module in (chat_mod, eval_service):
        source = inspect.getsource(module)
        assert "assemble_context_chunks(" in source, f"{module.__name__} 没调用共享拼装函数"
    # chat 里不许再出现「四类列表相加」的手写形态
    chat_src = inspect.getsource(chat_mod)
    assert "list(result.table_recall_chunks)" not in chat_src


# ── 默认口径：必须逐字节不变 ────────────────────────────────
@pytest.mark.asyncio
async def test_default_caliber_keeps_retrieve_and_hit_chunks_only(patched):
    retrieval, generation = patched()
    session = FakeSession([_case()], [KB])

    report = await run_eval(session, TENANT, ["public"], 40)

    assert retrieval.retrieve_calls and not retrieval.chain_calls, (
        "默认口径 MUST 走 retrieve()，否则旧基线不可比"
    )
    assert generation.seen == [[uuid.UUID(int=10), uuid.UUID(int=11)]], (
        "默认口径只喂命中片（与引入补充片之前的代码逐字节相同）"
    )
    assert report.cases[0].chunks_count == 2
    assert report.cases[0].recall_chunks_count == 0
    assert report.context_caliber == CONTEXT_CALIBER_CHUNKS_ONLY
    assert "不覆盖图召回" in report.context_note


# ── 开启口径：与生产同构 ────────────────────────────────────
@pytest.mark.asyncio
async def test_with_recall_caliber_uses_chain_and_all_four(patched):
    retrieval, generation = patched()
    session = FakeSession([_case()], [KB])

    report = await run_eval(session, TENANT, ["public"], 40, with_recall_chunks=True)

    assert retrieval.chain_calls and not retrieval.retrieve_calls, (
        "开启口径 MUST 走 retrieve_chain()（生产 chat 的入口），否则链式检索差异仍在"
    )
    call = retrieval.chain_calls[0]
    # 位置参数顺序与 chat.py 的调用形态一致
    assert call["positional"] == ("报销标准是什么", TENANT, (KB,), 40, ("public",))
    assert call["rewrite_score"] is None, "评测没有改写阶段：MUST NOT 伪造复杂度信号"

    expected = [c.chunk_id for c in assemble_context_chunks(retrieval.result)]
    assert generation.seen == [expected], "喂给生成的顺序 MUST 与共享拼装函数一致"
    assert len(expected) == 5
    assert report.cases[0].chunks_count == 5
    assert report.cases[0].recall_chunks_count == 3
    assert report.context_caliber == CONTEXT_CALIBER_WITH_RECALL
    assert "query 改写" in report.context_note


@pytest.mark.asyncio
async def test_refused_case_still_declares_caliber(patched):
    """检索就拒答时也要写明口径 —— 否则报告只有分数、没有"这是哪套口径"。"""
    retrieval, generation = patched()
    retrieval.result = RetrievalResult(refused=True, refuse_reason="NO_RELEVANT_CONTENT")
    session = FakeSession([_case(answerable=False)], [KB])

    report = await run_eval(session, TENANT, ["public"], 40, with_recall_chunks=True)

    assert report.cases[0].actual_refused is True
    assert report.cases[0].chunks_count == 0
    assert report.context_caliber == CONTEXT_CALIBER_WITH_RECALL
    assert generation.seen == [], "拒答不该再调生成"


# ── 图召回观测聚合（与开关无关）────────────────────────────
@pytest.mark.asyncio
async def test_graph_stats_aggregated_across_cases(patched):
    retrieval, _ = patched()
    session = FakeSession([_case("问题一"), _case("问题二")], [KB])

    report = await run_eval(session, TENANT, ["public"], 40)

    # 两条用例各贡献 covered=2 / unique=1
    assert report.graph_covered == 4
    assert report.graph_unique == 2


@pytest.mark.asyncio
async def test_graph_degraded_reasons_counted(patched):
    retrieval, _ = patched()
    retrieval.result = _result()
    retrieval.result.graph_degraded_reason = "timeout"
    session = FakeSession([_case("问题一"), _case("问题二")], [KB])

    report = await run_eval(session, TENANT, ["public"], 40)
    assert report.graph_degraded_reasons == {"timeout": 2}


@pytest.mark.asyncio
async def test_empty_case_set_still_reports_caliber(patched):
    patched()
    session = FakeSession([], [KB])
    report = await run_eval(session, TENANT, ["public"], 40, with_recall_chunks=True)
    assert report.total == 0
    assert report.context_caliber == CONTEXT_CALIBER_WITH_RECALL
    assert report.context_note
