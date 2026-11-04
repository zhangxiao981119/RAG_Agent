"""在线图召回（side channel）测试 —— `recall_by_graph` 的 8 步时序与失败语义。

钉住六件事：
  1. 产物**只**放 `chunks`，分数恒 0（进了排序/阈值 = 改写拒答判定）
  2. 正常态与故障态分得清：`no_entities` / `no_neighbors` 是**正常态**（不打降级），
     `timeout` / `error` 是故障态
  3. 永不抛异常 —— 图是增强项，图库挂了不该让一次问答失败
  4. 失败返回前 MUST rollback（否则连接留在失败事务里，后续表格/章节 SQL 连锁
     `current transaction is aborted`）
  5. rollback 自身失败不得盖住原始原因
  6. 取片 SQL 的四闸门与主召回**逐字同源**（不是副本）
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest

from app.config import decisions
from app.config.graph_profile import GraphProfile
from app.services.graph import recall as recall_mod
from app.services.graph.base import GraphNeighbor
from app.services.graph.recall import GraphRecallOutcome, recall_by_graph
from app.services.retrieve import _WHERE_BASE

TENANT = uuid.UUID(int=9)
KB = uuid.UUID(int=1)
DOC = uuid.UUID(int=2)
PROFILE = GraphProfile(extractor_model="qwen-test", prompt_version="gprompt-v1")


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

    `current_setting` 由 `current_timeout` 模拟（默认 "0"，与真实 PG 默认一致）；
    `restore_raises=True` 模拟收尾失败（连接已断），用于验证「收尾失败不掩盖结果」。
    """

    def __init__(
        self,
        rows: list | None = None,
        *,
        rollback_raises: bool = False,
        current_timeout: str = "0",
        restore_raises: bool = False,
    ) -> None:
        self._rows = rows or []
        self._rollback_raises = rollback_raises
        self._current_timeout = current_timeout
        self._restore_raises = restore_raises
        self.sql: list[str] = []
        self.scalar_sql: list[str] = []
        self.params: list[dict] = []
        self.rollback_calls = 0

    async def execute(self, stmt, params=None):  # noqa: ANN001
        sql = stmt.text if hasattr(stmt, "text") else str(stmt)
        self.sql.append(sql)
        self.params.append(dict(params or {}))
        if "set_config" in sql:
            # 还原语句：prev 取默认值 "0" 时视为「还原」，否则视为「设置预算」
            if self._restore_raises and (params or {}).get("prev") is not None:
                raise ConnectionError("连接已断（收尾失败）")
            return _Result([])
        if "current_setting" in sql:
            return _Result([])
        if "c.id = ANY(:chunk_ids)" in sql:
            return _Result(self._rows)
        raise AssertionError(f"未识别的 SQL: {sql[:100]}")

    async def scalar(self, stmt, params=None):  # noqa: ANN001
        sql = stmt.text if hasattr(stmt, "text") else str(stmt)
        assert "current_setting" in sql, f"scalar 只该用于读当前设置：{sql[:80]}"
        self.scalar_sql.append(sql)
        return self._current_timeout

    async def rollback(self) -> None:
        self.rollback_calls += 1
        if self._rollback_raises:
            raise ConnectionError("连接已断")


class FakeStore:
    def __init__(self, neighbors: list, *, raises: Exception | None = None,
                 delay: float = 0.0) -> None:
        self._neighbors = neighbors
        self._raises = raises
        self._delay = delay
        self.calls: list[dict] = []

    async def neighbors(self, session, **kwargs):  # noqa: ANN001, ARG002
        self.calls.append(kwargs)
        if self._delay:
            import asyncio

            await asyncio.sleep(self._delay)
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


async def _recall(session: FakeSession, *, timeout_ms: int = 800) -> GraphRecallOutcome:
    return await recall_by_graph(
        session, query="差旅费报销标准", pushdown=PUSHDOWN,
        timeout_ms=timeout_ms, profile=PROFILE,
    )


# ── 正常路径 ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_returns_chunks_with_zero_scores(monkeypatch):
    cid = uuid.uuid4()
    session = FakeSession([_ChunkRow(cid)])
    _patch(
        monkeypatch,
        seeds=[_Seed(uuid.uuid4(), "差旅费", 1.0), _Seed(uuid.uuid4(), "报销", 0.6)],
        store=FakeStore([GraphNeighbor(chunk_id=cid, hop=1, weight=3)]),
    )
    out = await _recall(session)

    assert out.degraded_reason == ""
    assert out.seed_entity_count == 2
    assert len(out.chunks) == 1
    chunk = out.chunks[0]
    assert chunk.is_graph_recall is True
    assert chunk.chunk_id == cid
    # ★ 分数必须全 0 / None：给非 0 分数会改写 retrieved[0] 的阈值语义
    assert chunk.final_score == 0.0
    assert chunk.vector_score == 0.0
    assert chunk.rrf_score == 0.0
    assert chunk.rerank_score is None
    assert chunk.keyword_score is None
    assert out.elapsed_ms >= 0


@pytest.mark.asyncio
async def test_no_entities_is_normal_state(monkeypatch):
    session = FakeSession()
    store = FakeStore([])
    _patch(monkeypatch, seeds=[], store=store)
    out = await _recall(session)

    assert out.degraded_reason == "no_entities"
    assert out.chunks == []
    assert store.calls == [], "没有种子实体就不该发图扩展查询"


@pytest.mark.asyncio
async def test_no_neighbors_is_normal_state(monkeypatch):
    session = FakeSession()
    _patch(monkeypatch, seeds=[_Seed(uuid.uuid4(), "孤立实体", 1.0)], store=FakeStore([]))
    out = await _recall(session)

    assert out.degraded_reason == "no_neighbors"
    assert out.seed_entity_count == 1
    assert out.chunks == []


@pytest.mark.asyncio
async def test_sorted_and_truncated_by_top_k(monkeypatch):
    """排序键与存储层逐字一致：(hop ASC, weight DESC, chunk_id ASC)；再截 TOP_K。"""
    ids = [uuid.UUID(int=i) for i in range(1, 15)]  # 14 > GRAPH_RETRIEVE_TOP_K
    rows = [_ChunkRow(cid, token_count=1) for cid in ids]
    # 故意打乱：hop=2 的排最前、weight 乱序
    shuffled = (
        [GraphNeighbor(chunk_id=ids[0], hop=2, weight=9)]
        + [GraphNeighbor(chunk_id=cid, hop=1, weight=1) for cid in reversed(ids[1:])]
    )
    session = FakeSession(rows)
    _patch(monkeypatch, seeds=[_Seed(uuid.uuid4(), "种子", 1.0)], store=FakeStore(shuffled))
    out = await _recall(session)

    assert len(out.neighbors) == decisions.GRAPH_RETRIEVE_TOP_K
    assert out.neighbors[0].hop == 1, "hop 小的必须排前面"
    hops = [n.hop for n in out.neighbors]
    assert hops == sorted(hops)
    weights = [n.weight for n in out.neighbors]
    assert weights == sorted(weights, reverse=True)


@pytest.mark.asyncio
async def test_chunk_count_and_token_budget_stop_without_skipping(monkeypatch):
    """超预算即停（不跳片）：跳过一片会让上下文出现语义断裂的片段。"""
    ids = [uuid.UUID(int=i) for i in range(1, 6)]
    rows = [_ChunkRow(cid, token_count=500) for cid in ids]
    session = FakeSession(rows)
    _patch(
        monkeypatch,
        seeds=[_Seed(uuid.uuid4(), "种子", 1.0)],
        store=FakeStore([GraphNeighbor(chunk_id=cid, hop=1, weight=1) for cid in ids]),
    )
    out = await _recall(session)

    # 500 + 500 = 1000 ≤ 1200，第三片会超 → 停在第 2 片
    assert len(out.chunks) == 2
    assert [c.chunk_id for c in out.chunks] == ids[:2]


@pytest.mark.asyncio
async def test_chunk_count_capped_by_max_chunks(monkeypatch):
    ids = [uuid.UUID(int=i) for i in range(1, 6)]
    rows = [_ChunkRow(cid, token_count=1) for cid in ids]
    session = FakeSession(rows)
    _patch(
        monkeypatch,
        seeds=[_Seed(uuid.uuid4(), "种子", 1.0)],
        store=FakeStore([GraphNeighbor(chunk_id=cid, hop=1, weight=1) for cid in ids]),
    )
    out = await _recall(session)
    assert len(out.chunks) == decisions.GRAPH_RECALL_MAX_CHUNKS


@pytest.mark.asyncio
async def test_missing_chunk_row_is_skipped(monkeypatch):
    """抽取之后被重解析 / 软删：图数据还在、chunk 行没了 —— 跳过而不是崩。"""
    alive, dead = uuid.UUID(int=1), uuid.UUID(int=2)
    session = FakeSession([_ChunkRow(alive)])
    _patch(
        monkeypatch,
        seeds=[_Seed(uuid.uuid4(), "种子", 1.0)],
        store=FakeStore([
            GraphNeighbor(chunk_id=dead, hop=1, weight=5),
            GraphNeighbor(chunk_id=alive, hop=1, weight=1),
        ]),
    )
    out = await _recall(session)
    assert [c.chunk_id for c in out.chunks] == [alive]


# ── 故障路径 ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_timeout_degrades_and_rolls_back(monkeypatch):
    """真实墙钟超时（不是伪造异常）：asyncio.timeout 取消 await 后必须清理事务。"""
    session = FakeSession()
    _patch(
        monkeypatch,
        seeds=[_Seed(uuid.uuid4(), "种子", 1.0)],
        store=FakeStore([], delay=0.2),
    )
    out = await _recall(session, timeout_ms=1)

    assert out.degraded_reason == "timeout"
    assert out.chunks == []
    assert session.rollback_calls == 1


@pytest.mark.asyncio
async def test_store_error_degrades_and_rolls_back(monkeypatch):
    session = FakeSession()
    _patch(
        monkeypatch,
        seeds=[_Seed(uuid.uuid4(), "种子", 1.0)],
        store=FakeStore([], raises=RuntimeError("图库抖动")),
    )
    out = await _recall(session)

    assert out.degraded_reason == "error"
    assert out.chunks == []
    assert session.rollback_calls == 1, "失败返回前必须 rollback，否则主链路 SQL 全部失效"


@pytest.mark.asyncio
async def test_rollback_failure_does_not_mask_reason(monkeypatch):
    """连接已断时 rollback 自己会抛 —— 它的失败不该盖住原始错因。"""
    session = FakeSession(rollback_raises=True)
    _patch(
        monkeypatch,
        seeds=[_Seed(uuid.uuid4(), "种子", 1.0)],
        store=FakeStore([], raises=RuntimeError("图库抖动")),
    )
    out = await _recall(session)
    assert out.degraded_reason == "error"


# ── SQL 契约 ────────────────────────────────────────────────
def test_chunk_sql_reuses_where_base_verbatim():
    """四闸门 MUST 与主召回同一字符串（复制一份到本文件必然漂移，且不报错）。"""
    sql = recall_mod._CHUNK_SQL_TEMPLATE.format(where_base=_WHERE_BASE)
    assert _WHERE_BASE in sql
    for gate in (
        "c.tenant_id = :tenant_id",
        "c.is_latest = true",
        "c.level_rank <= :clearance",
        "c.kb_id = ANY(:authorized_kb_ids)",
        "NOT (c.deny_subjects && :user_subjects)",
        "c.acl_tags && :user_subjects",
    ):
        assert gate in sql, f"取片 SQL 少了闸门：{gate}"


@pytest.mark.asyncio
async def test_sets_db_side_statement_timeout(monkeypatch):
    """墙钟预算要两端都设：只取消 await 时查询仍在 PG 里跑，还占着连接。"""
    session = FakeSession()
    _patch(monkeypatch, seeds=[], store=FakeStore([]))
    await _recall(session, timeout_ms=800)

    # 先读当前值（作为稍后还原的目标），再把预算设上去
    assert "current_setting" in session.scalar_sql[0]
    set_idx = next(i for i, sql in enumerate(session.sql) if "set_config" in sql)
    assert session.params[set_idx]["ms"] == "800"
    # SET 语句不接受绑定参数（asyncpg 走扩展协议会报 syntax error at or near "$1"），
    # 所以 MUST 用 set_config 函数形式
    assert "SET LOCAL" not in session.sql[set_idx].upper()


@pytest.mark.asyncio
async def test_restores_statement_timeout_as_last_sql(monkeypatch):
    """★ 事务级设置的泄漏防护：成功路径的最后一条 SQL 必须是还原。

    不还原 = 800ms 的图召回预算继续套在同一事务里的大表回捞 / 章节补全上，
    「慢一点但能成功」的回捞变成 QueryCanceled，章节补全（无 try 包裹）直接冒泡成
    VECTOR_STORE_DOWN —— 把本来会成功的请求变成失败。
    """
    cid = uuid.uuid4()
    session = FakeSession([_ChunkRow(cid)], current_timeout="30s")
    _patch(
        monkeypatch,
        seeds=[_Seed(uuid.uuid4(), "差旅费", 1.0)],
        store=FakeStore([GraphNeighbor(chunk_id=cid, hop=1, weight=1)]),
    )
    out = await _recall(session)

    assert out.degraded_reason == ""
    last_sql, last_params = session.sql[-1], session.params[-1]
    assert "set_config" in last_sql and "statement_timeout" in last_sql
    # 还原成**调用前的值**，不是硬编码 "0"：否则会悄悄放宽服务端配的全局超时
    assert last_params["prev"] == "30s"


@pytest.mark.parametrize("seed_kind", ["no_entities", "no_neighbors", "hit"])
@pytest.mark.asyncio
async def test_every_success_path_restores(monkeypatch, seed_kind):
    """空种子 / 空邻居也是成功路径 —— 它们最常见（否定路径），更该还原。

    若只在大功告成那条 return 里还原，最常见的两种情况恰恰把最短的超时留给后续 SQL。
    """
    cid = uuid.uuid4()
    if seed_kind == "no_entities":
        session = FakeSession(current_timeout="30s")
        _patch(monkeypatch, seeds=[], store=FakeStore([]))
    elif seed_kind == "no_neighbors":
        session = FakeSession(current_timeout="30s")
        _patch(monkeypatch, seeds=[_Seed(uuid.uuid4(), "孤立", 1.0)], store=FakeStore([]))
    else:
        session = FakeSession([_ChunkRow(cid)], current_timeout="30s")
        _patch(
            monkeypatch,
            seeds=[_Seed(uuid.uuid4(), "种子", 1.0)],
            store=FakeStore([GraphNeighbor(chunk_id=cid, hop=1, weight=1)]),
        )

    out = await _recall(session)
    assert out.degraded_reason in ("", "no_entities", "no_neighbors")
    assert session.params[-1].get("prev") == "30s", "成功路径最后一条 SQL 必须是还原"


@pytest.mark.asyncio
async def test_restore_failure_does_not_mask_success(monkeypatch):
    """收尾失败（连接已断）不许把一次成功召回判成 degraded。"""
    cid = uuid.uuid4()
    session = FakeSession([_ChunkRow(cid)], restore_raises=True)
    _patch(
        monkeypatch,
        seeds=[_Seed(uuid.uuid4(), "种子", 1.0)],
        store=FakeStore([GraphNeighbor(chunk_id=cid, hop=1, weight=1)]),
    )
    out = await _recall(session)

    assert out.degraded_reason == "", "还原失败是收尾问题，不是召回失败"
    assert [c.chunk_id for c in out.chunks] == [cid]
    assert session.rollback_calls == 0, "成功路径不该 rollback（那会丢掉已读到的结果）"


@pytest.mark.asyncio
async def test_failure_path_does_not_restore(monkeypatch):
    """失败路径已 rollback（事务级设置随事务消失），MUST NOT 再发还原 SQL。"""
    session = FakeSession()
    _patch(
        monkeypatch,
        seeds=[_Seed(uuid.uuid4(), "种子", 1.0)],
        store=FakeStore([], raises=RuntimeError("图库抖动")),
    )
    out = await _recall(session)

    assert out.degraded_reason == "error"
    assert session.rollback_calls == 1
    assert not [p for p in session.params if "prev" in p], "rollback 后再发 SQL 会开新事务"


def test_outcome_is_graph_recall_result():
    """端口契约不动：调用方拿到的是 GraphRecallResult（子类只多一个 chunks 字段）。"""
    from app.services.graph.base import GraphRecallResult

    assert issubclass(GraphRecallOutcome, GraphRecallResult)
    out = GraphRecallOutcome()
    assert out.chunks == []
