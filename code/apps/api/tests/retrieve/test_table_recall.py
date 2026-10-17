"""大表分片回捞测试（表格决策 A1' 检索侧）。

边界全替身（沿用 test_governance_weight 的 harness 模式）。钉住六件事：
  1. 回捞 SQL 带 ACL 四闸门下推 + 组条件 + chunk_index 保序
  2. 命中纯表格分片 → 回捞同组其余分片，is_table_recall=True、分数全 0
  3. 回捞片不进 result.chunks（不参与排序 / 阈值 / 三路贡献统计）
  4. 每组最多 TABLE_RECALL_MAX_CHUNKS 片
  5. token 预算超限即停（保行序完整，不跳片）
  6. 命中片自身被排除；无组命中不触发回捞 SQL
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

TENANT = uuid.uuid4()
KB = uuid.UUID(int=1)
DOC = uuid.UUID(int=2)


def _row(cid: uuid.UUID, group_id: str | None, vector_score: float) -> SimpleNamespace:
    """主召回行替身（三路 SQL 的输出形态）。"""
    mapping = {
        "id": cid,
        "document_id": DOC,
        "kb_id": KB,
        "content": f"分片 {cid}",
        "heading_path": "表",
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


def _recall_row(cid: uuid.UUID, tokens: int) -> SimpleNamespace:
    """回捞行替身（_TABLE_RECALL_SQL 的输出形态，属性访问）。"""
    return SimpleNamespace(
        id=cid,
        document_id=DOC,
        kb_id=KB,
        content=f"回捞 {cid}",
        heading_path="表",
        page_no=1,
        level_rank=20,
        token_count=tokens,
    )


class FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def fetchall(self) -> list:
        return list(self._rows)


class FakeSession:
    """按 SQL 特征分发的 session 替身；记录回捞 SQL 的调用参数。"""

    def __init__(self, h: "Harness") -> None:
        self._h = h
        self.recall_calls: list[dict] = []

    async def execute(self, stmt, params=None):  # noqa: ANN001
        sql = stmt.text if hasattr(stmt, "text") else str(stmt)
        if "<=>" in sql:
            return FakeResult(self._h.vector_rows)
        if "ILIKE" in sql:
            return FakeResult([])
        if "ts_rank" in sql:
            return FakeResult([])
        if "filename" in sql:
            return FakeResult([])
        if "table_group_id = :group_id" in sql:
            self.recall_calls.append(dict(params or {}))
            return FakeResult(self._h.recall_rows.get((params or {}).get("group_id"), []))
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
        self.recall_rows: dict[str, list] = {}


@pytest.fixture
def svc(monkeypatch: pytest.MonkeyPatch):
    def _make(h: Harness):
        monkeypatch.setattr(retrieve_mod, "get_embedding_service", lambda: FakeEmbedding())
        monkeypatch.setattr(retrieve_mod, "get_rerank_service", lambda: FakeRerank(h))
        monkeypatch.setattr(decisions, "KEYWORD_TSQUERY_ENABLED", False)
        return RetrievalService(), lambda: FakeSession(h)

    return _make


async def _retrieve(svc_pair, query: str = "表数据") -> tuple:
    service, make_session = svc_pair
    session = make_session()
    result = await service.retrieve(session, query, TENANT, [KB], 40, ["dept:it"])
    return result, session


# ── SQL 与不变量 ─────────────────────────────────────────


def test_recall_sql_acl_pushdown_and_order() -> None:
    """回捞 SQL MUST 复用 ACL 四闸门下推，且按 chunk_index 保序。"""
    sql = retrieve_mod._TABLE_RECALL_SQL.text
    assert "c.is_latest = true" in sql
    assert "c.tenant_id = :tenant_id" in sql
    assert "c.level_rank <= :clearance" in sql
    assert "c.kb_id = ANY(:authorized_kb_ids)" in sql
    assert "c.table_group_id = :group_id" in sql
    assert "ORDER BY c.chunk_index" in sql


def test_recall_uses_pushdown_params() -> None:
    """主召回与回捞共用同一 pushdown 参数集（四闸门取值一致）。"""
    src = inspect.getsource(retrieve_mod.RetrievalService.retrieve)
    assert "**pushdown" in src


# ── 回捞行为 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hit_slice_triggers_recall(svc) -> None:
    """命中表格分片 → 同组其余分片被回捞，且不进主结果列表。"""
    gid = uuid.uuid4().hex
    hit_id = uuid.UUID(int=100)
    r1, r2 = uuid.UUID(int=101), uuid.UUID(int=102)
    h = Harness([_row(hit_id, gid, 0.80)])
    h.rerank_result = [(0, 0.80)]
    h.recall_rows[gid] = [
        _recall_row(hit_id, 50),  # 命中片自身：必须被排除
        _recall_row(r1, 60),
        _recall_row(r2, 70),
    ]
    result, session = await _retrieve(svc(h))

    assert not result.refused
    assert [c.chunk_id for c in result.chunks] == [hit_id]  # 主列表不含回捞片
    assert [c.chunk_id for c in result.table_recall_chunks] == [r1, r2]
    for c in result.table_recall_chunks:
        assert c.is_table_recall is True
        assert c.final_score == 0.0
        assert c.rerank_score is None
        assert c.display_score == 0.0
    assert session.recall_calls and session.recall_calls[0]["group_id"] == gid


@pytest.mark.asyncio
async def test_recall_max_chunks_limit(svc) -> None:
    """每组最多回捞 TABLE_RECALL_MAX_CHUNKS 片。"""
    gid = uuid.uuid4().hex
    hit_id = uuid.UUID(int=100)
    h = Harness([_row(hit_id, gid, 0.80)])
    h.rerank_result = [(0, 0.80)]
    h.recall_rows[gid] = [
        _recall_row(uuid.UUID(int=i), 10) for i in range(101, 110)
    ]
    result, _ = await _retrieve(svc(h))

    assert len(result.table_recall_chunks) == decisions.TABLE_RECALL_MAX_CHUNKS


@pytest.mark.asyncio
async def test_recall_token_budget_stops(svc) -> None:
    """累计 token 超预算即停：保行序完整，不跳片拼残表。"""
    gid = uuid.uuid4().hex
    hit_id = uuid.UUID(int=100)
    h = Harness([_row(hit_id, gid, 0.80)])
    h.rerank_result = [(0, 0.80)]
    h.recall_rows[gid] = [
        _recall_row(uuid.UUID(int=101), 1200),
        _recall_row(uuid.UUID(int=102), 1200),  # 1200+1200 > 2048 → 停在第一片后
        _recall_row(uuid.UUID(int=103), 10),
    ]
    result, _ = await _retrieve(svc(h))

    assert [c.chunk_id for c in result.table_recall_chunks] == [uuid.UUID(int=101)]


@pytest.mark.asyncio
async def test_no_group_no_recall(svc) -> None:
    """命中片全部无组 → 不发起回捞查询。"""
    h = Harness([_row(uuid.UUID(int=100), None, 0.80)])
    h.rerank_result = [(0, 0.80)]
    result, session = await _retrieve(svc(h))

    assert not result.refused
    assert result.table_recall_chunks == []
    assert session.recall_calls == []


@pytest.mark.asyncio
async def test_degraded_rerank_still_recalls(svc) -> None:
    """rerank 降级路径同样回捞（回捞在 L1 阈值之后、与打分口径无关）。"""
    gid = uuid.uuid4().hex
    hit_id = uuid.UUID(int=100)
    h = Harness([_row(hit_id, gid, 0.80)])
    h.rerank_result = RerankTimeout("timeout")
    h.recall_rows[gid] = [_recall_row(uuid.UUID(int=101), 50)]
    result, _ = await _retrieve(svc(h))

    assert not result.refused
    assert [c.chunk_id for c in result.table_recall_chunks] == [uuid.UUID(int=101)]
