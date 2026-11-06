"""图召回 side channel 在 `retrieve()` 里的接入测试（边界全部替身，不连库）。

钉住五件事：
  1. **关图时零开销**：不发任何图 SQL、不写 graph_* 键、stage_ms 无 graph_recall
     —— 「关 = 与未实现时逐字节等价」这句话必须能被执行验证
  2. 开图时产物**只**进 `graph_recall_chunks`，不 append 进 `chunks`
     （进了就会污染排序与 retrieved[0] 的阈值语义）
  3. 图召回片先占 `final_id_set` → 表格回捞 / 章节补全据此排除重复
  4. `path_stats` 的 graph_covered / graph_unique 口径（§9.8 裁决 1，v1.2 定稿）：
     参照系是**交付上下文**，不是候选池 —— graph_unique = 最终进上下文的图召回片数
     （== len(graph_recall_chunks)）；graph_covered = 邻居中已在最终命中片的条数。
     v1.1 的「三路候选池」分母已被真库实测证伪（§9.9）：候选池 150 个 id 远大于
     最终 top-8，graph_unique 会结构性恒 0，§14.6 判据永远无法达成
  5. 降级（timeout / error / 无种子）不阻断主链路，原因透传到 `graph_degraded_reason`
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.config import decisions
from app.services import retrieve as retrieve_mod
from app.services.graph.base import GraphNeighbor
from app.services.graph.recall import GraphRecallOutcome
from app.services.retrieve import RetrievalService
from app.services.retrieve.base import RetrievedChunk

TENANT = uuid.uuid4()
KB = uuid.UUID(int=1)
DOC = uuid.UUID(int=2)


def _row(cid: uuid.UUID, group_id: str | None = None, vector_score: float = 0.80) -> SimpleNamespace:
    mapping = {
        "id": cid,
        "document_id": DOC,
        "kb_id": KB,
        "content": f"分片 {cid}",
        "heading_path": "第一章",
        "page_no": 1,
        "level_rank": 20,
        "acl_tags": [],
        "table_group_id": group_id,
        "governance_weight": 0,
        "vector_score": vector_score,
    }
    row = SimpleNamespace(**mapping)
    row._mapping = mapping  # noqa: SLF001
    return row


def _recall_row(cid: uuid.UUID, tokens: int = 10) -> SimpleNamespace:
    return SimpleNamespace(
        id=cid, document_id=DOC, kb_id=KB, content=f"回捞 {cid}",
        heading_path="第一章", page_no=1, level_rank=20, token_count=tokens,
    )


class FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def fetchall(self) -> list:
        return list(self._rows)


class FakeSession:
    """按 SQL 特征分发。未识别到 SQL 即断言失败 —— 这就是「没有多余 SQL」的证据。"""

    def __init__(self, h: "Harness") -> None:
        self._h = h
        self.graph_sql_seen: list[str] = []

    async def execute(self, stmt, params=None):  # noqa: ANN001
        sql = stmt.text if hasattr(stmt, "text") else str(stmt)
        params = params or {}
        if "set_config" in sql or "statement_timeout" in sql or "graph_entities" in sql:
            self.graph_sql_seen.append(sql)
            raise AssertionError("图 SQL 不该在测试替身里被执行（recall_by_graph 已打桩）")
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
        if "table_group_id = :group_id" in sql:
            return FakeResult(self._h.table_rows.get(params.get("group_id"), []))
        if "v(document_id, heading_path)" in sql:
            return FakeResult(self._h.section_rows)
        raise AssertionError(f"未识别的 SQL: {sql[:80]}")

    async def rollback(self) -> None:  # 图召回打桩后不该走到，但 AsyncSession 有这个接口
        return None


class FakeEmbedding:
    async def embed_query(self, query: str) -> list[float]:  # noqa: ARG002
        return [0.01] * 8


class FakeRerank:
    async def rerank(self, query, documents, top_k):  # noqa: ANN001, ARG002
        return []


class Harness:
    def __init__(self, rows: list) -> None:
        self.vector_rows = rows
        self.table_rows: dict[str, list] = {}
        self.section_rows: list = []


def _graph_chunk(cid: uuid.UUID, content: str = "图邻居片") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=cid, document_id=DOC, kb_id=KB, filename="制度.pdf", content=content,
        heading_path="第一章", page_no=1, level_rank=20,
        vector_score=0.0, keyword_score=None, rrf_score=0.0, rerank_score=None,
        final_score=0.0, is_graph_recall=True,
    )


@pytest.fixture
def svc(monkeypatch: pytest.MonkeyPatch):
    def _make(h: Harness, *, graph_enabled: bool):
        monkeypatch.setattr(retrieve_mod, "get_embedding_service", lambda: FakeEmbedding())
        monkeypatch.setattr(retrieve_mod, "get_rerank_service", lambda: FakeRerank())
        monkeypatch.setattr(
            retrieve_mod,
            "get_settings",
            lambda: SimpleNamespace(
                rerank_enabled=False,
                graph_enabled=graph_enabled,
                graph_retrieve_timeout_ms=800,
                wiki_enabled=False,
            ),
        )
        # FTS 路关掉：与本文件要验证的接线无关，少一个分支
        monkeypatch.setattr(decisions, "KEYWORD_TSQUERY_ENABLED", False)
        return RetrievalService(), lambda: FakeSession(h)

    return _make


def _stub_recall(monkeypatch: pytest.MonkeyPatch, outcome: GraphRecallOutcome) -> dict:
    """打桩 recall_by_graph，并记录调用参数（用于验证「关图时不调用」）。"""
    calls: dict = {}

    async def _fake(session, *, query, pushdown, timeout_ms, profile,
                    exclude_chunk_ids=frozenset()):  # noqa: ANN001, ARG001
        calls.update(
            query=query, pushdown=pushdown, timeout_ms=timeout_ms, profile=profile,
            exclude_chunk_ids=exclude_chunk_ids,
        )
        return outcome

    monkeypatch.setattr("app.services.graph.recall.recall_by_graph", _fake)
    return calls


async def _retrieve(svc_pair) -> tuple:
    service, make_session = svc_pair
    session = make_session()
    result = await service.retrieve(session, "报销标准", TENANT, [KB], 40, ["dept:it"])
    return result, session


# ── 关图 ────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_graph_disabled_has_zero_trace(svc, monkeypatch):
    """关图 = 与未实现图召回时逐字节等价：无 SQL、无 stages、无 path_stats 键。"""
    calls = _stub_recall(monkeypatch, GraphRecallOutcome())  # 万一被调用就记录下来
    hit = _row(uuid.UUID(int=10))
    result, session = await _retrieve(svc(Harness([hit]), graph_enabled=False))

    assert calls == {}, "GRAPH_ENABLED=false 时 MUST NOT 调用图召回"
    assert session.graph_sql_seen == []
    assert result.graph_recall_chunks == []
    assert result.graph_degraded_reason == ""
    assert "graph_recall" not in result.stage_ms
    assert not [k for k in result.path_stats if k.startswith("graph_")], (
        "关图时不得写 graph_* 键 —— 否则关闭态的审计输出与未实现图召回时不同"
    )


# ── 开图：产物隔离 ───────────────────────────────────────────
@pytest.mark.asyncio
async def test_graph_chunks_go_to_side_channel_only(svc, monkeypatch):
    hit = uuid.UUID(int=10)
    dup = uuid.UUID(int=11)   # 三路已召回
    new = uuid.UUID(int=12)   # 仅图路能给
    _stub_recall(
        monkeypatch,
        GraphRecallOutcome(
            degraded_reason="", seed_entity_count=2, elapsed_ms=7,
            # dup 同时也是图邻居（v1.2 口径：covered 从邻居 ∩ 命中片统计）
            neighbors=[GraphNeighbor(chunk_id=dup, hop=1, weight=3)],
            chunks=[_graph_chunk(dup), _graph_chunk(new)],
        ),
    )
    result, _ = await _retrieve(
        svc(Harness([_row(hit), _row(dup, vector_score=0.70)]), graph_enabled=True)
    )

    assert {c.chunk_id for c in result.chunks} == {hit, dup}, "图召回片 MUST NOT 进 chunks"
    assert [c.chunk_id for c in result.graph_recall_chunks] == [new], (
        "三路已召回的同片必须去重（否则上下文里出现两份相同内容的不同编号）"
    )
    assert result.graph_recall_chunks[0].is_graph_recall is True
    assert result.graph_recall_chunks[0].final_score == 0.0
    assert "graph_recall" in result.stage_ms
    # v1.2 口径（§9.8 裁决 1）：参照系是交付上下文
    #   covered = 邻居(dup) ∩ 最终命中片(hit, dup) = 1 —— 量化「重复探索」
    #   unique  = 最终进上下文的图召回片数 = len(graph_recall_chunks) = 1
    assert result.path_stats["graph_covered"] == 1
    assert result.path_stats["graph_unique"] == 1
    # ★ 存废判据的等式（§14.6）：graph_unique MUST 恒等于 len(graph_recall_chunks)
    assert result.path_stats["graph_unique"] == len(result.graph_recall_chunks)


@pytest.mark.asyncio
async def test_graph_unique_is_delivered_count_not_candidate_pool(svc, monkeypatch):
    """v1.1 口径的失效模式必须保持被证伪状态：图明明补了新片，unique 不得为 0。

    场景：三路候选池里已有很多片（这里 2 片命中），图补了 2 片**全新**内容进上下文。
    若按「三路候选池」当分母，unique 会被算成 0 —— 本用例钉死它必须等于交付数 2。
    """
    hit1, hit2 = uuid.UUID(int=10), uuid.UUID(int=11)
    g1, g2 = uuid.UUID(int=12), uuid.UUID(int=13)
    _stub_recall(
        monkeypatch,
        GraphRecallOutcome(
            degraded_reason="", seed_entity_count=3, elapsed_ms=9,
            neighbors=[GraphNeighbor(chunk_id=g1, hop=1, weight=5),
                       GraphNeighbor(chunk_id=g2, hop=1, weight=4)],
            chunks=[_graph_chunk(g1), _graph_chunk(g2)],
        ),
    )
    result, _ = await _retrieve(
        svc(Harness([_row(hit1), _row(hit2)]), graph_enabled=True)
    )

    assert len(result.graph_recall_chunks) == 2
    assert result.path_stats["graph_unique"] == 2, (
        "图补了 2 片进上下文，unique 就是 2 —— 候选池分母会把它错算成 0"
    )
    assert result.path_stats["graph_covered"] == 0, "邻居都不在命中片里，重复探索量为 0"


@pytest.mark.asyncio
async def test_graph_chunk_reserves_final_id_set(svc, monkeypatch):
    """图召回片先占 final_id_set → 表格回捞不得把同一片再补一次。"""
    hit = uuid.UUID(int=10)
    graph_only = uuid.UUID(int=12)
    other = uuid.UUID(int=13)
    _stub_recall(
        monkeypatch,
        GraphRecallOutcome(chunks=[_graph_chunk(graph_only)]),
    )
    h = Harness([_row(hit, group_id="g1")])
    h.table_rows["g1"] = [_recall_row(graph_only), _recall_row(other)]
    result, _ = await _retrieve(svc(h, graph_enabled=True))

    assert [c.chunk_id for c in result.table_recall_chunks] == [other], (
        "图召回片已进上下文，表格回捞再补一次就是同一片两份编号"
    )
    assert result.table_recall_chunks[0].is_table_recall is True


@pytest.mark.asyncio
async def test_graph_chunk_reserves_final_id_set_for_section_recall(svc, monkeypatch):
    hit = uuid.UUID(int=10)
    graph_only = uuid.UUID(int=12)
    _stub_recall(monkeypatch, GraphRecallOutcome(chunks=[_graph_chunk(graph_only)]))
    h = Harness([_row(hit)])
    h.section_rows = [_recall_row(graph_only)]
    result, _ = await _retrieve(svc(h, graph_enabled=True))

    assert result.section_recall_chunks == [], "章节补全必须排除图召回片"


# ── 开图：降级可见但不阻断 ───────────────────────────────────
@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["timeout", "error", "no_entities", "no_neighbors"])
async def test_graph_degraded_reason_surfaces_without_blocking(svc, monkeypatch, reason):
    calls = _stub_recall(monkeypatch, GraphRecallOutcome(degraded_reason=reason))
    hit = _row(uuid.UUID(int=10))
    result, _ = await _retrieve(svc(Harness([hit]), graph_enabled=True))

    assert result.graph_degraded_reason == reason
    assert result.graph_recall_chunks == []
    assert [c.chunk_id for c in result.chunks] == [hit.id], "图降级不得影响三路结果"
    assert result.refused is False
    assert calls["timeout_ms"] == 800, "超时预算必须取自 settings"


@pytest.mark.asyncio
async def test_graph_recall_receives_pushdown_and_query(svc, monkeypatch):
    """图召回拿到的 pushdown 必须与主召回同款（否则权限口径分叉）。"""
    calls = _stub_recall(monkeypatch, GraphRecallOutcome())
    await _retrieve(svc(Harness([_row(uuid.UUID(int=10))]), graph_enabled=True))

    assert calls["query"] == "报销标准"
    assert calls["pushdown"]["tenant_id"] == TENANT
    assert calls["pushdown"]["authorized_kb_ids"] == [KB]
    assert calls["pushdown"]["clearance"] == 40
    assert calls["pushdown"]["user_subjects"] == ["dept:it"]
    # §9.8 裁决 1：命中片集合必须传给图召回（先排重后裁剪的依据）
    assert calls["exclude_chunk_ids"] == {uuid.UUID(int=10)}
