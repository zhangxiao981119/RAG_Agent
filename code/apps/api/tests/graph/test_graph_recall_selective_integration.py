"""图召回集成测试 —— 选通路由（selective skip）+ dedup-before-trim 双夹具。

两层结构：
  (a) NO-DB 单测层（必跑，不连 PG）：
      · 用 test_recall_side_channel.py 的 FakeSession/FakeStore 习语，
        把「一篇文档的实体/提及/关系」模拟成图召回的真实输入（neighbors 来自提及的行），
        调真实 recall_by_graph，断言 exclude_chunk_ids 里的重复邻居被排掉、不占预算名额
        （先排重后裁剪，§9.8 裁决 1）。
      · selective-skip 单测：镜像 test_graph_side_channel.py 接入 retrieve() 图块，
        monkeypatch settings.graph_enabled / graph_recall_selective + spy recall_by_graph +
        监听 metrics.incr("rag.graph.skipped_by_router")，断言高置信 + 召回充足时跳过图召回，
        低分或片数不足时仍调用。
  (b) Real-DB 层（GRAPH_TEST_DB_URL 缺失则整体 skip）：
      经 PostgresGraphStore.upsert_subgraph 落一张真实子图（手搓 ExtractionResult，
      name_key 用 normalize_name_key），再调真实 recall_by_graph，断言返回 chunk、
      exclude_chunk_ids 被尊重、statement_timeout 路径在快查询上不报错；teardown 清理。

★ 不修改任何生产代码；只新增测试文件。
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from app.config import decisions
from app.config.graph_profile import GraphProfile
from app.services import retrieve as retrieve_mod
from app.services.graph import extract as extract_mod
from app.services.graph import recall as recall_mod
from app.services.graph.base import (
    ExtractedEntity,
    ExtractedMention,
    ExtractedRelation,
    ExtractionResult,
    GraphNeighbor,
)
from app.services.graph.extract import normalize_name_key
from app.services.graph.postgres_store import PostgresGraphStore, get_graph_store
from app.services.graph.recall import GraphRecallOutcome, recall_by_graph
from app.services.retrieve import RetrievalService
from app.services.retrieve.base import RetrievedChunk

TENANT = uuid.UUID(int=9)
KB = uuid.UUID(int=1)
DOC = uuid.UUID(int=2)
PROFILE = GraphProfile(extractor_model="qwen-test", prompt_version="gprompt-v1")

GRAPH_TEST_DB_URL = os.environ.get("GRAPH_TEST_DB_URL")
# 仅作用于真实 PG 层（(b)）；(a) 的 no-DB 单测层 MUST 始终运行，故不挂在模块级 pytestmark 上。
requires_db = pytest.mark.skipif(
    not GRAPH_TEST_DB_URL, reason="needs GRAPH_TEST_DB_URL (real PG)"
)


# ════════════════════════════════════════════════════════════════════════
# 公共习语：FakeSession / FakeStore（镜像 test_recall_side_channel.py）
# ════════════════════════════════════════════════════════════════════════
@dataclass
class _Seed:
    entity_id: uuid.UUID
    name: str
    score: float


@dataclass
class _ChunkRow:
    id: uuid.UUID
    document_id: uuid.UUID = DOC
    kb_id: uuid.UUID = KB
    content: str = "邻居片正文"
    heading_path: str = "第一节"
    page_no: int = 1
    level_rank: int = 20
    token_count: int = 100
    filename: str = "制度.pdf"


class _Result:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return list(self._rows)


class FakeSession:
    """记录执行过哪些 SQL；`rollback_calls` 用于断言失败路径的清理动作。

    与 test_recall_side_channel.py 保持一致：current_setting 模拟读当前超时、
    set_config 模拟设置/还原、含 `c.id = ANY(:chunk_ids)` 的 SQL 返回构造的行。
    """

    def __init__(self, rows: list | None = None, *, current_timeout: str = "0") -> None:
        self._rows = rows or []
        self._current_timeout = current_timeout
        self.sql: list[str] = []
        self.params: list[dict] = []
        self.rollback_calls = 0

    async def execute(self, stmt, params=None):  # noqa: ANN001
        sql = stmt.text if hasattr(stmt, "text") else str(stmt)
        self.sql.append(sql)
        self.params.append(dict(params or {}))
        if "set_config" in sql or "statement_timeout" in sql:
            return _Result([])
        if "current_setting" in sql:
            return _Result([])
        if "c.id = ANY(:chunk_ids)" in sql:
            return _Result(self._rows)
        raise AssertionError(f"未识别的 SQL: {sql[:100]}")

    async def scalar(self, stmt, params=None):  # noqa: ANN001
        sql = stmt.text if hasattr(stmt, "text") else str(stmt)
        assert "current_setting" in sql, f"scalar 只该用于读当前设置：{sql[:80]}"
        return self._current_timeout

    async def rollback(self) -> None:
        self.rollback_calls += 1


class FakeStore:
    """模拟图存储的图扩展结果（neighbors 来自本文档提及/关系的行）。"""

    def __init__(self, neighbors: list, *, raises: Exception | None = None) -> None:
        self._neighbors = neighbors
        self._raises = raises
        self.calls: list[dict] = []

    async def neighbors(self, session, **kwargs):  # noqa: ANN001, ARG002
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return list(self._neighbors)


PUSHDOWN = {
    "tenant_id": TENANT,
    "clearance": 40,
    "authorized_kb_ids": [KB],
    "user_subjects": ["dept:it"],
}


def _patch(monkeypatch, *, seeds: list, store: FakeStore) -> None:
    async def _fake_link(session, **kwargs):  # noqa: ANN001, ARG001
        return list(seeds)

    monkeypatch.setattr(recall_mod, "link_entities", _fake_link)
    monkeypatch.setattr(recall_mod, "get_graph_store", lambda: store)


# ════════════════════════════════════════════════════════════════════════
# (a)-1  NO-DB 单测：dedup-before-trim（先排重后裁剪，§9.8 裁决 1）
#        用「模拟子图」作为 recall_by_graph 的输入（无 LLM、无真实 PG）
# ════════════════════════════════════════════════════════════════════════
def _seeded_subgraph_fixture() -> tuple[list[_Seed], list[GraphNeighbor], list[_ChunkRow]]:
    """构造一篇文档的实体/提及/关系，并推导成 recall_by_graph 的真实输入。

    · 实体 E1（差旅费）被提及于 chunk A、B；E1—关系→E2（报销标准）。
    · 实体 E2 被提及于 chunk C。
    · 于是邻居候选 = [A, B, C]（E1 自身两片 + 一跳邻居 E2 的一片）。
    调用方用 exclude_chunk_ids={A, B} 模拟「A、B 已是三路命中片」，
    断言图召回只补真正新增的 C，且 A、B 计入 graph_covered（不占预算名额）。
    """
    e1, e2 = uuid.uuid4(), uuid.uuid4()
    a, b, c = uuid.UUID(int=101), uuid.UUID(int=102), uuid.UUID(int=103)
    seeds = [
        _Seed(e1, "差旅费", 1.0),
        _Seed(e2, "报销标准", 0.9),
    ]
    # 权重刻意让 A、B（E1 自身提及）排在队首，验证「重复邻居挤占名额」会被排掉
    neighbors = [
        GraphNeighbor(chunk_id=a, hop=1, weight=9),
        GraphNeighbor(chunk_id=b, hop=1, weight=8),
        GraphNeighbor(chunk_id=c, hop=2, weight=5),
    ]
    rows = [_ChunkRow(a), _ChunkRow(b), _ChunkRow(c)]
    return seeds, neighbors, rows


@pytest.mark.asyncio
async def test_dedup_before_trim_respects_seeded_subgraph(monkeypatch):
    """模拟子图 seeding：A、B 已是命中片 → 图召回只补新增片 C，且不占名额。"""
    seeds, neighbors, rows = _seeded_subgraph_fixture()
    session = FakeSession(rows)
    _patch(monkeypatch, seeds=seeds, store=FakeStore(neighbors))
    out = await recall_by_graph(
        session, query="差旅费报销标准", pushdown=PUSHDOWN,
        timeout_ms=800, profile=PROFILE,
        exclude_chunk_ids=frozenset({rows[0].id, rows[1].id}),
    )

    assert out.degraded_reason == ""
    assert [c.chunk_id for c in out.chunks] == [rows[2].id], (
        "seed 的 E1 自身两片已命中、E2 一跳邻居片是真正新增 → 进上下文的 MUST 是 C"
    )
    assert out.graph_covered == 2, "被跳过的重复邻居（A、B）计入 graph_covered"


@pytest.mark.asyncio
async def test_dedup_before_trim_no_overlap_returns_all(monkeypatch):
    """exclude_chunk_ids 与邻居无交集时，三片全部作为新增进上下文。"""
    seeds, neighbors, rows = _seeded_subgraph_fixture()
    session = FakeSession(rows)
    _patch(monkeypatch, seeds=seeds, store=FakeStore(neighbors))
    out = await recall_by_graph(
        session, query="差旅费报销标准", pushdown=PUSHDOWN,
        timeout_ms=800, profile=PROFILE,
        exclude_chunk_ids=frozenset(),
    )

    assert out.degraded_reason == ""
    assert {c.chunk_id for c in out.chunks} == {rows[0].id, rows[1].id, rows[2].id}
    assert out.graph_covered == 0


# ════════════════════════════════════════════════════════════════════════
# (a)-2  NO-DB 单测：selective-skip 路由（mirror test_graph_side_channel.py）
# ════════════════════════════════════════════════════════════════════════
def _row(cid: uuid.UUID, vector_score: float = 0.80) -> SimpleNamespace:
    mapping = {
        "id": cid,
        "document_id": DOC,
        "kb_id": KB,
        "content": f"分片 {cid}",
        "heading_path": "第一章",
        "page_no": 1,
        "level_rank": 20,
        "acl_tags": [],
        "table_group_id": None,
        "governance_weight": 0,
        "vector_score": vector_score,
    }
    row = SimpleNamespace(**mapping)
    row._mapping = mapping  # noqa: SLF001
    return row


class FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def fetchall(self) -> list:
        return list(self._rows)


class FakeSession_Retrieve:
    """retrieve() 主链路替身：按 SQL 特征分发；未识别到 SQL 即断言失败。"""

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


class FakeEmbedding:
    async def embed_query(self, query: str) -> list[float]:  # noqa: ARG002
        return [0.01] * 8


class FakeRerank:
    async def rerank(self, query, documents, top_k):  # noqa: ARG001, ARG002
        return []


class Harness:
    def __init__(self, rows: list) -> None:
        self.vector_rows = rows
        self.table_rows: dict[str, list] = {}
        self.section_rows: list = []


class _MetricsSpy:
    """记录 metrics.incr / observe_ms 调用，用于断言 skipped_by_router 是否触发。"""

    def __init__(self) -> None:
        self.incr_calls: list[tuple[str, dict]] = []
        self.observe_calls: list[tuple[str, object, dict]] = []

    def incr(self, name: str, **kwargs) -> None:
        self.incr_calls.append((name, kwargs))

    def observe_ms(self, name: str, value, **kwargs) -> None:  # noqa: ANN001
        self.observe_calls.append((name, value, kwargs))

    def incr_names(self) -> list[str]:
        return [name for name, _ in self.incr_calls]


def _make_svc(monkeypatch, *, graph_enabled: bool, graph_recall_selective: bool):
    """镜像 test_graph_side_channel.py 的 svc fixture，并加入 graph_recall_selective。

    返回 (service, make_session, metrics_spy)。
    """
    spy = _MetricsSpy()
    monkeypatch.setattr(retrieve_mod, "get_embedding_service", lambda: FakeEmbedding())
    monkeypatch.setattr(retrieve_mod, "get_rerank_service", lambda: FakeRerank())
    monkeypatch.setattr(
        retrieve_mod,
        "get_settings",
        lambda: SimpleNamespace(
            rerank_enabled=False,
            graph_enabled=graph_enabled,
            graph_recall_selective=graph_recall_selective,
            graph_retrieve_timeout_ms=800,
            wiki_enabled=False,
        ),
    )
    monkeypatch.setattr(retrieve_mod, "metrics", spy)
    monkeypatch.setattr(decisions, "KEYWORD_TSQUERY_ENABLED", False)
    return RetrievalService(), lambda: FakeSession_Retrieve(Harness([])), spy


def _stub_recall(monkeypatch) -> dict:
    """打桩 recall_by_graph，记录是否被调用（用于断言「关/跳时不调用」）。"""
    calls: dict = {}

    async def _fake(session, *, query, pushdown, timeout_ms, profile,  # noqa: ANN001, ARG001
                    exclude_chunk_ids=frozenset()):
        calls["called"] = True
        calls.update(query=query, pushdown=pushdown, timeout_ms=timeout_ms,
                     profile=profile, exclude_chunk_ids=exclude_chunk_ids)
        return GraphRecallOutcome()

    monkeypatch.setattr("app.services.graph.recall.recall_by_graph", _fake)
    return calls


async def _retrieve(svc_pair) -> tuple:
    service, make_session = svc_pair
    session = make_session()
    result = await service.retrieve(session, "报销标准", TENANT, [KB], 40, ["dept:it"])
    return result, session


@pytest.mark.asyncio
async def test_selective_skip_skips_recall_when_high_confidence_and_enough(monkeypatch):
    """开启 selective 且 top1>=0.85 且 召回>=8 → 跳过图召回 + 记 skipped_by_router。"""
    calls = _stub_recall(monkeypatch)
    svc, _, spy = _make_svc(monkeypatch, graph_enabled=True, graph_recall_selective=True)
    # 8 个命中片，top1 高分（final_score == vector_score，rerank 关、governance_weight=0）
    rows = [_row(uuid.UUID(int=i), vector_score=0.90) for i in range(10, 18)]
    result, _ = await _retrieve((svc, lambda: FakeSession_Retrieve(Harness(rows))))

    assert not calls.get("called"), "达跳过条件 MUST NOT 调用 recall_by_graph"
    assert result.graph_recall_chunks == []
    assert "rag.graph.skipped_by_router" in spy.incr_names(), (
        "跳过路由 MUST 递增 rag.graph.skipped_by_router 指标"
    )


@pytest.mark.asyncio
async def test_selective_skip_recalls_when_low_top1_score(monkeypatch):
    """top1 低分（0.50 < 0.85）→ 即便召回充足也必须调用图召回。"""
    calls = _stub_recall(monkeypatch)
    svc, _, spy = _make_svc(monkeypatch, graph_enabled=True, graph_recall_selective=True)
    rows = [_row(uuid.UUID(int=i), vector_score=0.50) for i in range(10, 18)]
    await _retrieve((svc, lambda: FakeSession_Retrieve(Harness(rows))))

    assert calls.get("called") is True, "低分 MUST 仍调用图召回"
    assert "rag.graph.skipped_by_router" not in spy.incr_names()


@pytest.mark.asyncio
async def test_selective_skip_recalls_when_too_few_chunks(monkeypatch):
    """命中片不足 8（仅 5）→ 即便 top1 高置信也必须调用图召回。"""
    calls = _stub_recall(monkeypatch)
    svc, _, spy = _make_svc(monkeypatch, graph_enabled=True, graph_recall_selective=True)
    rows = [_row(uuid.UUID(int=i), vector_score=0.95) for i in range(10, 15)]
    await _retrieve((svc, lambda: FakeSession_Retrieve(Harness(rows))))

    assert calls.get("called") is True, "片数不足 MUST 仍调用图召回"
    assert "rag.graph.skipped_by_router" not in spy.incr_names()


@pytest.mark.asyncio
async def test_selective_off_always_recalls(monkeypatch):
    """selective 关闭（默认）→ 与未引入路由逐字节相同，每条请求都调用图召回。"""
    calls = _stub_recall(monkeypatch)
    svc, _, spy = _make_svc(monkeypatch, graph_enabled=True, graph_recall_selective=False)
    rows = [_row(uuid.UUID(int=i), vector_score=0.95) for i in range(10, 18)]
    await _retrieve((svc, lambda: FakeSession_Retrieve(Harness(rows))))

    assert calls.get("called") is True
    assert "rag.graph.skipped_by_router" not in spy.incr_names()


# ════════════════════════════════════════════════════════════════════════
# (b)  Real-DB 层：仅当 GRAPH_TEST_DB_URL 存在时建引擎 + 定义夹具；
#      两个测试函数定义在模块级、用 @requires_db 装饰，缺失 URL 时整体 skip。
# ════════════════════════════════════════════════════════════════════════
if GRAPH_TEST_DB_URL:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.config.graph_profile import current_graph_profile
    from app.models import Chunk, Document, GraphEntityMention, GraphRelation, KnowledgeBase, Tenant

    _engine = create_async_engine(GRAPH_TEST_DB_URL, pool_pre_ping=True)
    _Session = async_sessionmaker(_engine, expire_on_commit=False)

    async def _seed_base(session) -> tuple[uuid.UUID, uuid.UUID]:
        """插入 tenant / kb / document / 两片 chunk（满足四闸门）。返回 (c1, c2)。"""
        tenant = Tenant(id=TENANT, name="sel-test", code="sel-test", status="active")
        kb = KnowledgeBase(
            id=KB, tenant_id=TENANT, name="sel-kb", visibility="restricted",
            is_public=False, owner_id=None, acl_version=1,
        )
        doc = Document(
            id=DOC, tenant_id=TENANT, kb_id=KB, doc_group_id=uuid.uuid4(),
            version=1, is_latest=True, filename="制度.pdf", ext="pdf", size_bytes=10,
            storage_key="k", status="indexed", level=1, level_rank=20,
            acl_tags=["public"], deny_subjects=[], owner_dept_path=None,
            graph_status="done", graph_extractor_model=PROFILE.extractor_model,
            graph_prompt_version=PROFILE.prompt_version,
        )
        c1 = uuid.uuid4()
        c2 = uuid.uuid4()
        chunk1 = Chunk(
            id=c1, tenant_id=TENANT, kb_id=KB, document_id=DOC, chunk_index=0,
            content="差旅费报销标准由财务部制定。", fts_text="差旅费 报销 标准 财务部 制定",
            token_count=20, heading_path="第一章", page_no=1, level_rank=20,
            acl_tags=["public"], deny_subjects=[], is_latest=True, embedding=None,
            embedding_model="test", embedding_text_version="v1", chunker_version="v1",
            governance_weight=0,
        )
        chunk2 = Chunk(
            id=c2, tenant_id=TENANT, kb_id=KB, document_id=DOC, chunk_index=1,
            content="财务制度规定了预算审批流程。", fts_text="财务 制度 规定 预算 审批 流程",
            token_count=20, heading_path="第一章", page_no=1, level_rank=20,
            acl_tags=["public"], deny_subjects=[], is_latest=True, embedding=None,
            embedding_model="test", embedding_text_version="v1", chunker_version="v1",
            governance_weight=0,
        )
        session.add_all([tenant, kb, doc, chunk1, chunk2])
        await session.flush()
        return c1, c2

    def _build_extraction_result(profile: GraphProfile, c1: uuid.UUID, c2: uuid.UUID) -> ExtractionResult:
        """手搓一篇文档的抽取产物（name_key 经 normalize_name_key）。"""
        e1_key = normalize_name_key("差旅费报销标准")
        e2_key = normalize_name_key("财务制度")
        entities = [
            ExtractedEntity(name="差旅费报销标准", name_key=e1_key, entity_type="TERM"),
            ExtractedEntity(name="财务制度", name_key=e2_key, entity_type="POLICY"),
        ]
        mentions = [
            ExtractedMention(chunk_id=c1, entity_key=e1_key, surface="差旅费报销标准", confidence=0.9),
            ExtractedMention(chunk_id=c2, entity_key=e2_key, surface="财务制度", confidence=0.9),
        ]
        relations = [
            ExtractedRelation(src_key=e1_key, dst_key=e2_key, relation="参见", document_id=DOC),
        ]
        return ExtractionResult(
            entities=entities, mentions=mentions, relations=relations,
            extractor_model=profile.extractor_model, prompt_version=profile.prompt_version,
            chunks_processed=2, llm_calls=0,
        )


@pytest.mark.asyncio
@requires_db
async def test_real_db_seed_and_recall():
    """真实 PG：upsert_subgraph 落子图 → 真实 recall_by_graph 返回邻居片。"""
    store = get_graph_store()
    async with _Session() as session:
        c1, c2 = await _seed_base(session)
        result = _build_extraction_result(current_graph_profile(), c1, c2)
        written = await store.upsert_subgraph(
            session, tenant_id=TENANT, kb_id=KB, document_id=DOC, result=result,
        )
        assert written >= 1
        await session.commit()

        # 真实召回：query 含实体名 → link_entities 命中种子 → neighbors 扩到 c1/c2
        out = await recall_by_graph(
            session, query="差旅费报销标准", pushdown=PUSHDOWN,
            timeout_ms=800, profile=current_graph_profile(),
        )
        assert out.degraded_reason in ("", "no_entities", "no_neighbors")
        chunk_ids = {c.chunk_id for c in out.chunks}
        assert c1 in chunk_ids, "种子实体自身提及片应被召回"
        assert out.graph_covered == 0
        await session.rollback()


@pytest.mark.asyncio
@requires_db
async def test_real_db_exclude_respected_and_timeout_safe():
    """真实 PG：exclude_chunk_ids 被尊重；快查询上 statement_timeout 路径不报错。"""
    store = get_graph_store()
    async with _Session() as session:
        c1, c2 = await _seed_base(session)
        result = _build_extraction_result(current_graph_profile(), c1, c2)
        await store.upsert_subgraph(
            session, tenant_id=TENANT, kb_id=KB, document_id=DOC, result=result,
        )
        await session.commit()

        # 把 c1 标记为已命中 → 图召回 MUST 跳过它（不进上下文）
        out = await recall_by_graph(
            session, query="差旅费报销标准", pushdown=PUSHDOWN,
            timeout_ms=800, profile=current_graph_profile(),
            exclude_chunk_ids=frozenset({c1}),
        )
        assert c1 not in {c.chunk_id for c in out.chunks}

        # 快查询 + 大超时：statement_timeout 设置/还原路径不得抛错
        out2 = await recall_by_graph(
            session, query="差旅费报销标准", pushdown=PUSHDOWN,
            timeout_ms=800, profile=current_graph_profile(),
        )
        assert out2.degraded_reason in ("", "no_entities", "no_neighbors")

        # 清理：删除本档图数据 + 基础行
        await store.delete_by_document(session, document_id=DOC)
        await session.execute(
            GraphRelation.__table__.delete().where(GraphRelation.document_id == DOC)
        )
        await session.execute(
            GraphEntityMention.__table__.delete().where(GraphEntityMention.document_id == DOC)
        )
        await session.execute(Chunk.__table__.delete().where(Chunk.document_id == DOC))
        await session.execute(Document.__table__.delete().where(Document.id == DOC))
        await session.execute(KnowledgeBase.__table__.delete().where(KnowledgeBase.id == KB))
        await session.execute(Tenant.__table__.delete().where(Tenant.id == TENANT))
        await session.commit()
