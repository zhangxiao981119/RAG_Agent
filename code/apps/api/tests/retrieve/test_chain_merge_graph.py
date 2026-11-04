"""`merge_rounds()` 的图召回合并测试（纯函数，无 IO）。

为什么必须有这组用例：链式多轮走的是 `retrieve_chain → merge_rounds`，
而 `merge_rounds` 只认它显式合并的那几个字段 —— **漏一个字段就静默丢数据**，
不报错、不打点，只是「开了图以后多轮问答比单轮还差」。

钉住四件事：
  1. 各轮图召回片按 chunk_id 去重合并
  2. 与命中片 / 表格回捞 / 章节补全三处**全部**互斥（同一片不进上下文两次）
  3. graph_degraded_reason 取首个非空
  4. 拒答时图召回片一并丢弃（它不是证据，不能把拒答"救"回来）
"""
from __future__ import annotations

import uuid

from app.config import decisions
from app.services.retrieve.base import RetrievalResult, RetrievedChunk
from app.services.retrieve.chain import merge_rounds

DOC = uuid.UUID(int=2)
KB = uuid.UUID(int=1)


def _chunk(cid: uuid.UUID, *, score: float = 0.9, **flags) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=cid, document_id=DOC, kb_id=KB, filename="制度.pdf", content=f"片 {cid}",
        heading_path="第一章", page_no=1, level_rank=20,
        vector_score=score, keyword_score=None, rrf_score=0.0, rerank_score=None,
        final_score=score, **flags,
    )


def test_graph_chunks_merged_and_deduped_across_rounds():
    a, b = uuid.UUID(int=11), uuid.UUID(int=12)
    r1 = RetrievalResult(chunks=[_chunk(uuid.UUID(int=1))],
                         graph_recall_chunks=[_chunk(a, score=0.0, is_graph_recall=True)])
    r2 = RetrievalResult(chunks=[_chunk(uuid.UUID(int=2))],
                         graph_recall_chunks=[_chunk(a, score=0.0, is_graph_recall=True),
                                              _chunk(b, score=0.0, is_graph_recall=True)])
    merged = merge_rounds([r1, r2])

    assert [c.chunk_id for c in merged.graph_recall_chunks] == [a, b]
    assert all(c.is_graph_recall for c in merged.graph_recall_chunks)
    assert all(c.final_score == 0.0 for c in merged.graph_recall_chunks)


def test_graph_chunk_excluded_by_hit_chunk():
    """同一片既是命中片又是图召回片 → 只保留命中片。"""
    cid = uuid.UUID(int=11)
    r1 = RetrievalResult(chunks=[_chunk(cid)])
    r2 = RetrievalResult(chunks=[_chunk(uuid.UUID(int=1))],
                         graph_recall_chunks=[_chunk(cid, score=0.0, is_graph_recall=True)])
    merged = merge_rounds([r1, r2])

    assert merged.graph_recall_chunks == []
    assert cid in {c.chunk_id for c in merged.chunks}


def test_graph_chunk_excluded_by_table_recall():
    cid = uuid.UUID(int=11)
    r1 = RetrievalResult(chunks=[_chunk(uuid.UUID(int=1))],
                         table_recall_chunks=[_chunk(cid, score=0.0, is_table_recall=True)])
    r2 = RetrievalResult(chunks=[_chunk(uuid.UUID(int=2))],
                         graph_recall_chunks=[_chunk(cid, score=0.0, is_graph_recall=True)])
    merged = merge_rounds([r1, r2])

    assert merged.graph_recall_chunks == [], "表格回捞片已占该片，图召回不得重复补"
    assert len(merged.table_recall_chunks) == 1


def test_graph_chunk_excluded_by_section_recall():
    cid = uuid.UUID(int=11)
    r1 = RetrievalResult(chunks=[_chunk(uuid.UUID(int=1))],
                         section_recall_chunks=[_chunk(cid, score=0.0, is_section_recall=True)])
    r2 = RetrievalResult(chunks=[_chunk(uuid.UUID(int=2))],
                         graph_recall_chunks=[_chunk(cid, score=0.0, is_graph_recall=True)])
    merged = merge_rounds([r1, r2])

    assert merged.graph_recall_chunks == [], "章节补全片已占该片，图召回不得重复补"


def test_degraded_reason_takes_first_non_empty():
    r1 = RetrievalResult(chunks=[_chunk(uuid.UUID(int=1))], )
    r2 = RetrievalResult(chunks=[_chunk(uuid.UUID(int=2))], graph_degraded_reason="timeout")
    r3 = RetrievalResult(chunks=[_chunk(uuid.UUID(int=3))], graph_degraded_reason="error")
    assert merge_rounds([r1, r2, r3]).graph_degraded_reason == "timeout"
    assert merge_rounds([r1, r3, r2]).graph_degraded_reason == "error"
    assert merge_rounds([r1]).graph_degraded_reason == ""


def test_graph_path_stats_accumulated():
    r1 = RetrievalResult(chunks=[_chunk(uuid.UUID(int=1))],
                         path_stats={"graph_covered": 1, "graph_unique": 2})
    r2 = RetrievalResult(chunks=[_chunk(uuid.UUID(int=2))],
                         path_stats={"graph_covered": 3, "graph_unique": 0})
    merged = merge_rounds([r1, r2])
    assert merged.path_stats["graph_covered"] == 4
    assert merged.path_stats["graph_unique"] == 2


def test_refused_merge_drops_graph_chunks():
    """拒答 = 三路没有可用证据；图召回片不得把拒答「救」回来（它没有分数、不进阈值）。"""
    below = decisions.active_threshold(True) - 0.5
    r1 = RetrievalResult(
        chunks=[_chunk(uuid.UUID(int=1), score=below)],
        graph_recall_chunks=[_chunk(uuid.UUID(int=11), score=0.0, is_graph_recall=True)],
    )
    merged = merge_rounds([r1])
    assert merged.refused is True
    assert merged.graph_recall_chunks == []
