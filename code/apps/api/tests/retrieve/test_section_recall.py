"""章节上下文补全测试（长文档 RAG P1）。

边界全替身（沿用 test_table_recall 的 harness 模式）。钉住七件事：
  1. 补全 SQL 带 ACL 四闸门 + 走 (document_id, heading_path) 行值匹配
  2. 命中片所在小节的其他分片被补入，is_section_recall=True、分数全 0
  3. 补全片不进 result.chunks（不参与排序 / 阈值 / 三路贡献统计）
  4. 每节最多 SECTION_RECALL_MAX_CHUNKS 片；命中片自身被排除
  5. 全局 token 预算超限即停（高分小节先补全）
  6. heading 过短（无小节语义）不触发；表格分片不重复补
  7. 无命中小节时不发起补全 SQL
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.config import decisions
from app.services import retrieve as retrieve_mod
from app.services.retrieve import RetrievalService
from app.services.rerank import RerankTimeout

TENANT = uuid.uuid4()
KB = uuid.UUID(int=1)
DOC = uuid.UUID(int=2)
HEADING = "第三章 报销标准"


def _row(
    cid: uuid.UUID,
    heading_path: str,
    vector_score: float,
    group_id: str | None = None,
) -> SimpleNamespace:
    """主召回行替身（三路 SQL 的输出形态）。"""
    mapping = {
        "id": cid,
        "document_id": DOC,
        "kb_id": KB,
        "content": f"命中 {cid}",
        "heading_path": heading_path,
        "page_no": 1,
        "level_rank": 20,
        "acl_tags": [],
        "table_group_id": group_id,
        "governance_weight": 0,
        "vector_score": vector_score,
    }
    row = SimpleNamespace(**mapping)
    row._mapping = mapping  # noqa: SLF001  (retrieve 代码读 row._mapping)
    return row


def _section_row(cid: uuid.UUID, tokens: int) -> SimpleNamespace:
    """章节补全行替身（_SECTION_RECALL_SQL 的输出形态，属性访问）。"""
    return SimpleNamespace(
        id=cid,
        document_id=DOC,
        kb_id=KB,
        content=f"同节 {cid}",
        heading_path=HEADING,
        page_no=1,
        level_rank=20,
        chunk_index=cid.int % 100,
        token_count=tokens,
    )


class FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def fetchall(self) -> list:
        return list(self._rows)


class FakeSession:
    """按 SQL 特征分发的 session 替身；记录章节补全 SQL 的调用参数。"""

    def __init__(self, h: "Harness") -> None:
        self._h = h
        self.section_calls: list[dict] = []

    async def execute(self, stmt, params=None):  # noqa: ANN001
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
            self.section_calls.append(dict(params or {}))
            return FakeResult(self._h.section_rows)
        if "table_group_id = :group_id" in sql:
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
        return [(i, 0.80) for i in range(len(documents))]


class Harness:
    def __init__(self, rows: list) -> None:
        self.vector_rows = rows
        self.rerank_result: list | Exception = []
        self.section_rows: list = []


@pytest.fixture
def svc(monkeypatch: pytest.MonkeyPatch):
    def _make(h: Harness):
        monkeypatch.setattr(retrieve_mod, "get_embedding_service", lambda: FakeEmbedding())
        monkeypatch.setattr(retrieve_mod, "get_rerank_service", lambda: FakeRerank(h))
        monkeypatch.setattr(retrieve_mod, "get_settings", lambda: SimpleNamespace(rerank_enabled=True))
        monkeypatch.setattr(decisions, "KEYWORD_TSQUERY_ENABLED", False)
        return RetrievalService(), lambda: FakeSession(h)

    return _make


async def _retrieve(svc_pair, query: str = "报销标准") -> tuple:
    service, make_session = svc_pair
    session = make_session()
    result = await service.retrieve(session, query, TENANT, [KB], 40, ["dept:it"])
    return result, session


# ── SQL 与不变量 ─────────────────────────────────────────


def test_section_sql_acl_and_key() -> None:
    """补全 SQL MUST 复用四闸门，且按 (document_id, heading_path) 行值匹配。"""
    sql = retrieve_mod._SECTION_RECALL_SQL_TEMPLATE.format(
        where_base=retrieve_mod._WHERE_BASE,
        values="(CAST(:doc_0 AS uuid), CAST(:heading_0 AS text))",
    )
    assert "c.is_latest = true" in sql
    assert "c.tenant_id = :tenant_id" in sql
    assert "c.level_rank <= :clearance" in sql
    assert "c.kb_id = ANY(:authorized_kb_ids)" in sql
    assert "c.deny_subjects && :user_subjects" in sql
    assert "c.acl_tags && :user_subjects" in sql
    assert "(c.document_id, c.heading_path) IN" in sql
    assert "ORDER BY c.document_id, c.heading_path, c.chunk_index" in sql

    # ★ 绑定参数必须真被 SQLAlchemy 识别：`:p::type` 写法的参数名后紧跟 `::`，
    #   会被解析正则的 (?!:) 负向断言排除，原样发给 asyncpg → syntax error at or near ":"。
    #   这里用 text() 真实解析一次，把「测试通过但线上 500」的口子堵住。
    bindparams = retrieve_mod.text(sql)._bindparams  # noqa: SLF001  (测试专用断言)
    assert {"doc_0", "heading_0"} <= set(bindparams)


# ── 补全行为 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hit_section_triggers_recall(svc) -> None:
    """命中片所在小节的其他分片被补入，且不进主结果列表。"""
    hit_id = uuid.UUID(int=100)
    s1, s2 = uuid.UUID(int=101), uuid.UUID(int=102)
    h = Harness([_row(hit_id, HEADING, 0.80)])
    h.rerank_result = [(0, 0.80)]
    h.section_rows = [
        _section_row(hit_id, 200),  # 命中片自身：必须被排除
        _section_row(s1, 200),
        _section_row(s2, 200),
    ]
    result, session = await _retrieve(svc(h))

    assert not result.refused
    assert [c.chunk_id for c in result.chunks] == [hit_id]
    assert [c.chunk_id for c in result.section_recall_chunks] == [s1, s2]
    for c in result.section_recall_chunks:
        assert c.is_section_recall is True
        assert c.final_score == 0.0
        assert c.rerank_score is None
    assert session.section_calls and session.section_calls[0]["heading_0"] == HEADING


@pytest.mark.asyncio
async def test_section_max_chunks_limit(svc) -> None:
    """每节最多补 SECTION_RECALL_MAX_CHUNKS 片。"""
    hit_id = uuid.UUID(int=100)
    h = Harness([_row(hit_id, HEADING, 0.80)])
    h.rerank_result = [(0, 0.80)]
    h.section_rows = [_section_row(uuid.UUID(int=i), 10) for i in range(101, 110)]
    result, _ = await _retrieve(svc(h))

    assert len(result.section_recall_chunks) == decisions.SECTION_RECALL_MAX_CHUNKS


@pytest.mark.asyncio
async def test_section_token_budget_stops(svc) -> None:
    """全局预算耗尽即停：不跳片拼残缺小节。"""
    hit_id = uuid.UUID(int=100)
    h = Harness([_row(hit_id, HEADING, 0.80)])
    h.rerank_result = [(0, 0.80)]
    h.section_rows = [
        _section_row(uuid.UUID(int=101), decisions.SECTION_RECALL_TOKEN_BUDGET),
        _section_row(uuid.UUID(int=102), 10),  # 预算已耗尽 → 不进
    ]
    result, _ = await _retrieve(svc(h))

    assert [c.chunk_id for c in result.section_recall_chunks] == [uuid.UUID(int=101)]


@pytest.mark.asyncio
async def test_short_heading_no_recall(svc) -> None:
    """heading 过短（无小节语义）→ 不发起补全 SQL。"""
    h = Harness([_row(uuid.UUID(int=100), "表", 0.80)])
    h.rerank_result = [(0, 0.80)]
    result, session = await _retrieve(svc(h))

    assert not result.refused
    assert result.section_recall_chunks == []
    assert session.section_calls == []


@pytest.mark.asyncio
async def test_table_chunk_not_section_recalled(svc) -> None:
    """表格分片走组回捞，不再按小节补一次（否则同一片进上下文两遍）。"""
    h = Harness([_row(uuid.UUID(int=100), HEADING, 0.80, group_id="g1")])
    h.rerank_result = [(0, 0.80)]
    h.section_rows = [_section_row(uuid.UUID(int=101), 10)]
    result, session = await _retrieve(svc(h))

    assert result.section_recall_chunks == []
    assert session.section_calls == []


@pytest.mark.asyncio
async def test_no_heading_no_recall(svc) -> None:
    """无标题文档不触发章节补全。"""
    h = Harness([_row(uuid.UUID(int=100), "", 0.80)])
    h.rerank_result = [(0, 0.80)]
    result, session = await _retrieve(svc(h))

    assert result.section_recall_chunks == []
    assert session.section_calls == []


@pytest.mark.asyncio
async def test_degraded_rerank_still_recalls(svc) -> None:
    """rerank 降级路径同样补全（补全在 L1 阈值之后、与打分口径无关）。"""
    hit_id = uuid.UUID(int=100)
    h = Harness([_row(hit_id, HEADING, 0.80)])
    h.rerank_result = RerankTimeout("timeout")
    h.section_rows = [_section_row(uuid.UUID(int=101), 200)]
    result, _ = await _retrieve(svc(h))

    assert not result.refused
    assert [c.chunk_id for c in result.section_recall_chunks] == [uuid.UUID(int=101)]
