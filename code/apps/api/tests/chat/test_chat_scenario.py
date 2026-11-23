"""Web `/chat/ask` 可选场景（`scenario_id`）接口级用例。

模板来自 `tests/chat/test_chat_db_scope.py`（同一套 TestClient harness：override
鉴权 / 限流，打桩检索、生成、改写、持久化、审计、配额），只验证 chat 的接线语义：
  1. 带 scenario_id → 解析出的 policy / caps 原样进检索（caps 对 Web 生效）；
  2. 不带 scenario_id → 完全不调用场景解析，policy / caps 均为 None（等价性）；
  3. 解析失败（不存在 / 跨租户 / 停用）→ 4xx 且未进入检索，绝不回退默认场景。
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.api.chat as chat_mod
from app.api.deps import CurrentUser, get_current_user, get_db
from app.api.rate_limit import check_chat_rate_limit
from app.main import app
from app.schemas.chat import ChatAskRequest
from app.schemas.integration import IntegrationAskRequest
from app.services.generate import StreamDelta
from app.services.retrieve.base import RetrievalResult, RetrievedChunk
from app.services.scenario import ScenarioCode, ScenarioError
from app.services.scenario.snapshot import ScenarioPolicy
from app.services.tenant.governance import TenantPolicyCaps

_TENANT_ID = uuid.uuid4()
_USER_ID = uuid.uuid4()
_KB_ID = uuid.uuid4()
_KB_ID_2 = uuid.uuid4()
_SCENARIO_ID = uuid.uuid4()

_USER = CurrentUser(
    user_id=_USER_ID,
    tenant_id=_TENANT_ID,
    username="tester",
    display_name="tester",
    clearance=40,  # admin：豁免库级交集校验，聚焦场景接线
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
        self.seen_session = None
        self.seen_policy = None
        self.seen_caps = None
        self.seen_kb_ids = None
        self.result = RetrievalResult(chunks=[_chunk()], stage_ms={"retrieving": 1})

    async def retrieve_chain(self, session, query, tenant_id, kb_ids, clearance, subjects,
                             rewrite_score=None, *, policy=None,
                             policy_caps=None):  # noqa: ANN001
        self.seen_session = session
        # 记录主链路实际传下来的场景口径：caps 是否生效只能在这里观测
        self.seen_policy = policy
        self.seen_caps = policy_caps
        # 记录检索范围：证明场景快照的 kb_ids 不会覆盖已过授权交集的 effective_kb_ids
        self.seen_kb_ids = list(kb_ids)
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


@pytest.fixture
def harness(monkeypatch):
    h = _Harness()

    app.dependency_overrides[get_current_user] = lambda: _USER

    async def _override_get_db():
        yield _FakeSession(kb_count=1)

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[check_chat_rate_limit] = lambda: None

    # 库位解析不影响场景接线：固定默认库（open_data_session 原样交出控制面会话）
    async def _resolve_default(session, tenant_id, kb_ids):  # noqa: ANN001
        return "default"

    monkeypatch.setattr(chat_mod, "resolve_kb_db_key", _resolve_default)

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
        return None

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

    # 不触发 lifespan：本文件所有外部依赖都已打桩（同 test_chat_db_scope）
    h.client = TestClient(app)
    try:
        yield h
    finally:
        app.dependency_overrides.clear()


def _post(h, *, scenario_id: uuid.UUID | None = None) -> object:  # noqa: ANN001
    body = {"kb_ids": [str(_KB_ID)], "question": "住宿报销标准是多少？"}
    if scenario_id is not None:
        body["scenario_id"] = str(scenario_id)
    return h.client.post("/api/chat/ask", json=body)


def test_scenario_policy_and_caps_reach_retrieval(harness, monkeypatch):
    """带 scenario_id → 解析出的 policy / caps 原样进检索（caps 对 Web 生效）。"""
    captured: dict[str, object] = {}
    policy = ScenarioPolicy(recall_top_k=8)
    caps = TenantPolicyCaps(recall_top_k_max=5)

    async def _fake_resolve(session, *, tenant_id, scenario_id, requested_kb_ids):  # noqa: ANN001
        captured["tenant_id"] = tenant_id
        captured["scenario_id"] = scenario_id
        captured["requested_kb_ids"] = list(requested_kb_ids)
        return SimpleNamespace(policy=policy, caps=caps)

    monkeypatch.setattr(chat_mod, "resolve_scenario_by_id", _fake_resolve)

    resp = _post(harness, scenario_id=_SCENARIO_ID)
    assert resp.status_code == 200
    assert captured["tenant_id"] == _TENANT_ID
    assert captured["scenario_id"] == _SCENARIO_ID
    # 解析入参必须是请求选的 kb（已过授权交集）
    assert captured["requested_kb_ids"] == [_KB_ID]
    assert harness.retrieval.seen_policy is policy
    assert harness.retrieval.seen_policy.recall_top_k == 8
    assert harness.retrieval.seen_caps is caps
    assert harness.retrieval.seen_caps.recall_top_k_max == 5


def test_without_scenario_id_skips_resolution(harness, monkeypatch):
    """不带 scenario_id → 完全不解析场景，policy / caps 均为 None（等价性）。"""

    async def _must_not_call(*args, **kwargs):  # noqa: ANN001
        raise AssertionError("不应解析场景")

    monkeypatch.setattr(chat_mod, "resolve_scenario_by_id", _must_not_call)

    resp = _post(harness)
    assert resp.status_code == 200
    assert harness.retrieval.seen_session is not None
    assert harness.retrieval.seen_policy is None
    assert harness.retrieval.seen_caps is None


@pytest.mark.parametrize(
    "diagnostic",
    [
        {"reason": "scenario_not_found", "tenant_id": str(_TENANT_ID),
         "scenario_id": str(_SCENARIO_ID)},
        # 跨租户与不存在在解析层落成同一个 NOT_FOUND：端点层同样 404，
        # 状态码差异不得成为场景存在性探针。
        {"reason": "scenario_not_found", "tenant_id": str(uuid.uuid4()),
         "scenario_id": str(_SCENARIO_ID)},
    ],
)
def test_scenario_not_found_returns_404_before_retrieval(
    harness, monkeypatch, diagnostic
):
    """解析抛 NOT_FOUND（含跨租户）→ 404，且未进入检索。"""

    async def _raise_not_found(*args, **kwargs):  # noqa: ANN001
        raise ScenarioError(ScenarioCode.NOT_FOUND, diagnostic=diagnostic)

    monkeypatch.setattr(chat_mod, "resolve_scenario_by_id", _raise_not_found)

    resp = _post(harness, scenario_id=_SCENARIO_ID)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "场景不存在"
    assert harness.retrieval.seen_session is None


def test_scenario_not_allowed_returns_400_before_retrieval(harness, monkeypatch):
    """停用场景 → 400，且未进入检索（fail-closed，不回落默认场景）。"""

    async def _raise_not_allowed(*args, **kwargs):  # noqa: ANN001
        raise ScenarioError(
            ScenarioCode.NOT_ALLOWED,
            diagnostic={"reason": "scenario_not_active",
                        "tenant_id": str(_TENANT_ID),
                        "scenario_id": str(_SCENARIO_ID)},
        )

    monkeypatch.setattr(chat_mod, "resolve_scenario_by_id", _raise_not_allowed)

    resp = _post(harness, scenario_id=_SCENARIO_ID)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "场景不可用"
    assert harness.retrieval.seen_session is None


def test_scenario_mismatch_returns_400_before_retrieval(harness, monkeypatch):
    """请求 kb 超出场景声明 → 400，且未进入检索（不得放宽为「按交集继续跑」）。"""

    async def _raise_mismatch(*args, **kwargs):  # noqa: ANN001
        raise ScenarioError(
            ScenarioCode.MISMATCH,
            diagnostic={"reason": "requested_outside_scenario_scope",
                        "tenant_id": str(_TENANT_ID),
                        "scenario_id": str(_SCENARIO_ID),
                        "requested": "1", "outside": "1"},
        )

    monkeypatch.setattr(chat_mod, "resolve_scenario_by_id", _raise_mismatch)

    resp = _post(harness, scenario_id=_SCENARIO_ID)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "请求的知识库不在场景范围内"
    assert harness.retrieval.seen_session is None


def test_scenario_unresolvable_returns_500_before_retrieval(harness, monkeypatch):
    """解析不出已发布版本（数据损坏）→ 500，且不回落默认场景继续检索。"""

    async def _raise_unresolvable(*args, **kwargs):  # noqa: ANN001
        raise ScenarioError(
            ScenarioCode.UNRESOLVABLE,
            diagnostic={"reason": "scenario_not_found",
                        "tenant_id": str(_TENANT_ID),
                        "scenario_id": str(_SCENARIO_ID)},
        )

    monkeypatch.setattr(chat_mod, "resolve_scenario_by_id", _raise_unresolvable)

    resp = _post(harness, scenario_id=_SCENARIO_ID)
    assert resp.status_code == 500
    assert resp.json()["detail"] == "场景配置不可解析"
    assert harness.retrieval.seen_session is None


def test_resolution_uses_authorized_intersection(harness, monkeypatch):
    """非管理员请求含未授权库时，解析入参只能是授权交集后的 kb 集合。"""
    limited = CurrentUser(
        user_id=_USER_ID,
        tenant_id=_TENANT_ID,
        username="limited",
        display_name="limited",
        clearance=10,  # 非管理员：kb 需过 authorized_kb_ids 交集
        dept_path="",
        subjects=[],
        authorized_kb_ids=[_KB_ID],
        acl_epoch=1,
    )
    app.dependency_overrides[get_current_user] = lambda: limited

    captured: dict[str, object] = {}

    async def _fake_resolve(session, *, tenant_id, scenario_id, requested_kb_ids):  # noqa: ANN001
        captured["requested_kb_ids"] = list(requested_kb_ids)
        return SimpleNamespace(
            policy=ScenarioPolicy(), caps=TenantPolicyCaps(), kb_ids=(),
        )

    monkeypatch.setattr(chat_mod, "resolve_scenario_by_id", _fake_resolve)

    resp = harness.client.post(
        "/api/chat/ask",
        json={
            "kb_ids": [str(_KB_ID), str(_KB_ID_2)],
            "question": "住宿报销标准是多少？",
            "scenario_id": str(_SCENARIO_ID),
        },
    )
    assert resp.status_code == 200
    # 未授权的 _KB_ID_2 必须在解析前就被交集剔除
    assert captured["requested_kb_ids"] == [_KB_ID]
    assert harness.retrieval.seen_kb_ids == [_KB_ID]


def test_snapshot_kb_ids_cannot_widen_retrieval_scope(harness, monkeypatch):
    """场景快照自带的 kb_ids 不得覆盖请求的授权范围（只取 policy / caps）。"""

    async def _fake_resolve(session, *, tenant_id, scenario_id, requested_kb_ids):  # noqa: ANN001
        # 恶意/错误的替身：快照 kb_ids 指向请求里没有的库
        return SimpleNamespace(
            policy=ScenarioPolicy(recall_top_k=8),
            caps=TenantPolicyCaps(recall_top_k_max=5),
            kb_ids=(_KB_ID_2,),
        )

    monkeypatch.setattr(chat_mod, "resolve_scenario_by_id", _fake_resolve)

    resp = _post(harness, scenario_id=_SCENARIO_ID)
    assert resp.status_code == 200
    # 检索范围必须仍是请求的库，而不是快照里的 kb_ids
    assert harness.retrieval.seen_kb_ids == [_KB_ID]


def test_integration_ask_request_forbids_scenario_id() -> None:
    """/integrations/ask 不接受请求体场景字段（extra=forbid），场景只能由应用绑定决定。"""
    assert "scenario_id" not in IntegrationAskRequest.model_fields
    with pytest.raises(ValidationError):
        IntegrationAskRequest.model_validate(
            {
                "question": "报销标准",
                "kb_ids": [str(_KB_ID)],
                "scenario_id": str(_SCENARIO_ID),
            }
        )


def test_chat_ask_request_scenario_id_defaults_to_none() -> None:
    """不传 scenario_id 时请求模型为 None（等价性入口）。"""
    payload = ChatAskRequest(kb_ids=[_KB_ID], question="报销标准")
    assert payload.scenario_id is None
