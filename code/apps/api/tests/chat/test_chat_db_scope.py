"""S6-3 开启态接口级用例（chat）：库位冲突 409 与数据面会话使用。

不连真库：库位解析用猴补丁替身，检索/生成/审计全部打桩，只验证 chat 的接线语义：
  1. 混合库位（默认 + 绑定）解析抛 DbScopeError → HTTP 409，
     detail 带 CROSS_DB_RETRIEVAL_UNSUPPORTED 与原始原因码；
  2. 单绑定库位 → 检索拿到的正是数据面会话（不是控制面会话），db_key 正确；
  3. D5：检索异常时 _persist_assistant 必须在数据面会话关闭之后调用
     （用进入/退出/持久化的顺序断言，防止数据面与控制面连接并存）。
"""
from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

import app.api.chat as chat_mod
from app.api.deps import CurrentUser, get_current_user, get_db
from app.api.rate_limit import check_chat_rate_limit
from app.main import app
from app.services.db_scope import DbScopeError
from app.services.generate import StreamDelta
from app.services.retrieve.base import RetrievalResult, RetrievedChunk

_TENANT_ID = uuid.uuid4()
_USER_ID = uuid.uuid4()
_KB_ID = uuid.uuid4()

_USER = CurrentUser(
    user_id=_USER_ID,
    tenant_id=_TENANT_ID,
    username="tester",
    display_name="tester",
    clearance=40,  # admin：豁免库级交集校验，聚焦库位接线
    dept_path="",
    subjects=[],
    authorized_kb_ids=[],
    acl_epoch=1,
)


class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self) -> _FakeResult:
        return self

    def all(self) -> list:
        return list(self._rows)

    def first(self):  # noqa: ANN201
        return self._rows[0] if self._rows else None


class _FakeSession:
    """KB 存在性查询返回指定行数；历史/画像（SessionLocal 开出的会话）返回空。"""

    def __init__(self, kb_count: int = 0) -> None:
        self._kb_rows = [object() for _ in range(kb_count)]

    async def execute(self, stmt):  # noqa: ANN001
        return _FakeResult(self._kb_rows)

    async def get(self, model, key):  # noqa: ANN001
        return None

    def add(self, obj) -> None:  # noqa: ANN001
        pass

    def add_all(self, objs) -> None:  # noqa: ANN001
        pass

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        pass

    async def refresh(self, obj) -> None:  # noqa: ANN001
        pass

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


def _chunk() -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        kb_id=_KB_ID,
        filename="费用报销制度.pdf",
        content="一线城市住宿报销标准为 500 元/晚",
        heading_path="",
        page_no=None,
        level_rank=1,
        vector_score=0.9,
        keyword_score=None,
        rrf_score=0.9,
        rerank_score=0.95,
        final_score=0.95,
    )


class _FakeRetrieval:
    def __init__(self) -> None:
        self.raise_on_retrieve = False
        self.seen_session = None
        self.result = RetrievalResult(chunks=[_chunk()], stage_ms={"retrieving": 1})

    async def retrieve_chain(self, session, query, tenant_id, kb_ids, clearance, subjects,
                             rewrite_score=None, *, policy=None):  # noqa: ANN001
        self.seen_session = session
        if self.raise_on_retrieve:
            raise RuntimeError("pgvector 不可用")
        self.result.chain_mode = "single"
        return self.result


class _FakeGeneration:
    def stream_generate(self, query, chunks, history=None, memory_prompt="",  # noqa: ANN001
                        tenant_id=None, rewrite_score=None):
        async def _gen():
            yield StreamDelta(type="delta", text="500 元/晚 [1]\n")
            yield StreamDelta(
                type="done",
                full_text="500 元/晚 [1]\n",
                suggestions=[],
                usage={"prompt_tokens": 1, "completion_tokens": 2},
            )

        return _gen()


class _Harness:
    def __init__(self) -> None:
        self.client: TestClient | None = None
        self.retrieval = _FakeRetrieval()
        self.order: list[str] = []


@pytest.fixture
def harness(monkeypatch):
    h = _Harness()

    app.dependency_overrides[get_current_user] = lambda: _USER

    async def _override_get_db():
        yield _FakeSession(kb_count=1)

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[check_chat_rate_limit] = lambda: None

    # 改写/记忆/压缩用 SessionLocal：历史与画像都返回空行
    monkeypatch.setattr(chat_mod, "SessionLocal", lambda: _FakeSession(kb_count=0))
    monkeypatch.setattr(chat_mod, "get_retrieval_service", lambda: h.retrieval)
    monkeypatch.setattr(
        chat_mod, "get_stream_generation_service", lambda: _FakeGeneration()
    )

    async def _fake_rewrite(query, history, tenant_id):  # noqa: ANN001
        return query, "skipped", 100

    monkeypatch.setattr(chat_mod, "rewrite_query", _fake_rewrite)

    async def _fake_system_prompt(*args, **kwargs):  # noqa: ANN001
        return "system"

    monkeypatch.setattr(chat_mod, "build_system_prompt", _fake_system_prompt)

    async def _fake_caliber(tenant_id, chain_mode=None):  # noqa: ANN001
        return {}

    monkeypatch.setattr(chat_mod, "snapshot_for_message", _fake_caliber)

    async def _fake_persist(*args, **kwargs):  # noqa: ANN001
        h.order.append("persist")

    monkeypatch.setattr(chat_mod, "_persist_assistant", _fake_persist)

    async def _fake_memory(user_id, user_msg, assistant_msg):  # noqa: ANN001
        return None

    monkeypatch.setattr(chat_mod, "_update_user_memory", _fake_memory)

    async def _fake_audit(*args, **kwargs):  # noqa: ANN001
        return None

    monkeypatch.setattr(chat_mod.audit, "record", _fake_audit)

    async def _flag_off(*args, **kwargs):  # noqa: ANN001
        return False

    monkeypatch.setattr(chat_mod.flag_service, "is_enabled", _flag_off)

    async def _noop_incr(*args, **kwargs):  # noqa: ANN001
        return None

    monkeypatch.setattr(chat_mod.quota_service, "_incr_usage", _noop_incr)

    # 不触发 lifespan：本文件所有外部依赖都已打桩（同 test_sse_retrieval_observation）
    h.client = TestClient(app)
    try:
        yield h
    finally:
        app.dependency_overrides.clear()


def _post(h) -> object:  # noqa: ANN001
    return h.client.post(
        "/api/chat/ask",
        json={"kb_ids": [str(_KB_ID)], "question": "住宿报销标准是多少？"},
    )


def test_chat_mixed_db_scope_returns_409(harness, monkeypatch):
    """混合库位解析失败 → 409，detail 带 CROSS_DB_RETRIEVAL_UNSUPPORTED 与原因码。"""

    async def _resolve_multi(session, tenant_id, kb_ids):  # noqa: ANN001
        raise DbScopeError(
            "DB_SCOPE_MULTI_KEY",
            {"kb_ids": [str(k) for k in kb_ids], "db_keys": ["default", "kb_finance"]},
        )

    monkeypatch.setattr(chat_mod, "resolve_kb_db_key", _resolve_multi)

    resp = _post(harness)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "CROSS_DB_RETRIEVAL_UNSUPPORTED" in detail
    # 原因明细必须透出真实库位集合（default 明确在其中），便于定位混合请求
    assert "default" in detail
    assert "kb_finance" in detail
    # 未进入检索：只查一半的行为必须被 409 挡在门外
    assert harness.retrieval.seen_session is None


def test_chat_single_bound_key_uses_data_plane_session(harness, monkeypatch):
    """单绑定库位 → 检索使用数据面会话，且 db_key 为解析出的键。"""
    data_plane_session = object()
    opened: dict[str, object] = {}

    async def _resolve_bound(session, tenant_id, kb_ids):  # noqa: ANN001
        return "kb_finance"

    @asynccontextmanager
    async def _fake_open(control_session, db_key):  # noqa: ANN001
        opened["db_key"] = db_key
        opened["control_session"] = control_session
        yield data_plane_session

    monkeypatch.setattr(chat_mod, "resolve_kb_db_key", _resolve_bound)
    monkeypatch.setattr(chat_mod, "open_data_session", _fake_open)

    resp = _post(harness)
    assert resp.status_code == 200
    assert opened["db_key"] == "kb_finance"
    assert harness.retrieval.seen_session is data_plane_session
    assert harness.retrieval.seen_session is not opened["control_session"]


def test_chat_retrieval_failure_persists_after_data_session_closed(harness, monkeypatch):
    """D5：检索异常分支的持久化必须在数据面会话退出之后执行。"""
    order = harness.order

    async def _resolve_bound(session, tenant_id, kb_ids):  # noqa: ANN001
        return "kb_finance"

    @asynccontextmanager
    async def _fake_open(control_session, db_key):  # noqa: ANN001
        order.append("data.enter")
        try:
            yield object()
        finally:
            order.append("data.exit")

    monkeypatch.setattr(chat_mod, "resolve_kb_db_key", _resolve_bound)
    monkeypatch.setattr(chat_mod, "open_data_session", _fake_open)
    harness.retrieval.raise_on_retrieve = True

    resp = _post(harness)
    assert resp.status_code == 200
    events = [json.loads(b.split("data: ", 1)[1])
              for b in resp.text.split("\n\n") if b.startswith("event: refused")]
    assert events and events[0]["reason"] == "VECTOR_STORE_DOWN"
    assert order == ["data.enter", "data.exit", "persist"]
