"""企业应用集成 API 的认证、权限、配额、审计与会话隔离测试。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

import app.api.chat as chat_mod
import app.api.integrations as integrations_mod
import app.api.rate_limit as rate_limit_mod
from app.api.deps import CurrentUser
from app.models import (
    IntegrationApplication,
    IntegrationConversation,
    User,
)
from app.schemas.integration import (
    IntegrationApplicationCreate,
    IntegrationAskRequest,
    IntegrationSearchRequest,
)
from app.services import quota as quota_service
from app.services.generate import StreamDelta
from app.services.quota import DegradationPlan
from app.services.retrieve.base import RetrievalResult, RetrievedChunk

_TENANT_ID = uuid.uuid4()
_USER_ID = uuid.uuid4()
_APPLICATION_ID = uuid.uuid4()
_KB_ALLOWED = uuid.uuid4()
_KB_USER_ONLY = uuid.uuid4()
_KB_APP_ONLY = uuid.uuid4()


def _user() -> CurrentUser:
    return CurrentUser(
        user_id=_USER_ID,
        tenant_id=_TENANT_ID,
        username="real-user",
        display_name="真实用户",
        clearance=40,
        dept_path="/总部/",
        subjects=["user:" + str(_USER_ID), "dept:/总部/"],
        authorized_kb_ids=[_KB_ALLOWED, _KB_USER_ONLY],
        acl_epoch=1,
    )


def _application(**changes) -> IntegrationApplication:
    values = {
        "id": _APPLICATION_ID,
        "tenant_id": _TENANT_ID,
        "name": "企业搜索",
        "key_hash": integrations_mod._application_key_hash("correct-key"),
        "kb_ids": [str(_KB_ALLOWED), str(_KB_APP_ONLY)],
        "status": "active",
        "rate_limit_per_minute": 60,
        "daily_token_limit": 200_000,
        "daily_message_limit": 1_000,
        "created_by": _USER_ID,
        "created_at": datetime.now(timezone.utc),
        "disabled_at": None,
    }
    values.update(changes)
    return IntegrationApplication(**values)


class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self):
        return self

    def all(self) -> list:
        return list(self._rows)


class _FakeSession:
    def __init__(self, kb_rows: list | None = None, binding=None) -> None:
        self.kb_rows = kb_rows or []
        self.binding = binding
        self.added: list = []
        self.get_models: list = []

    async def execute(self, stmt):
        return _FakeResult(self.kb_rows)

    async def get(self, model, key):
        self.get_models.append(model)
        if model is IntegrationConversation and self.binding is not None:
            return self.binding
        return None

    def add(self, obj) -> None:
        self.added.append(obj)

    def add_all(self, objs) -> None:
        self.added.extend(objs)

    async def flush(self) -> None:
        for obj in self.added:
            if getattr(obj, "id", object()) is None:
                obj.id = uuid.uuid4()

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass

    async def refresh(self, obj) -> None:
        if getattr(obj, "id", object()) is None:
            obj.id = uuid.uuid4()
        if getattr(obj, "created_at", object()) is None:
            obj.created_at = datetime.now(timezone.utc)
        if isinstance(obj, IntegrationApplication) and obj.status is None:
            obj.status = "active"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


def _chunk() -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        kb_id=_KB_ALLOWED,
        filename="制度.pdf",
        content="住宿报销标准为 500 元",
        heading_path="费用/住宿",
        page_no=3,
        level_rank=20,
        vector_score=0.8,
        keyword_score=None,
        rrf_score=0.8,
        rerank_score=0.9,
        final_score=0.9,
    )


def test_kb_scope_is_application_user_request_intersection_even_for_admin():
    requested = [_KB_ALLOWED, _KB_USER_ONLY, _KB_APP_ONLY, uuid.uuid4()]
    assert chat_mod._integration_kb_ids(requested, _user(), _application()) == [
        _KB_ALLOWED
    ]


def test_ask_request_forbids_caller_supplied_user_id():
    with pytest.raises(ValidationError):
        IntegrationAskRequest.model_validate(
            {
                "question": "报销标准",
                "kb_ids": [str(_KB_ALLOWED)],
                "user_id": str(uuid.uuid4()),
            }
        )


@pytest.mark.asyncio
async def test_application_authentication_binds_real_user_and_rejects_bad_key():
    session = _FakeSession()
    session.application = _application()

    async def _get(model, key):
        return session.application if model is IntegrationApplication else None

    session.get = _get
    principal = await integrations_mod.get_integration_principal(
        _user(), session, str(_APPLICATION_ID), "correct-key"
    )
    assert principal.user.user_id == _USER_ID
    assert principal.application.id == _APPLICATION_ID

    with pytest.raises(HTTPException) as exc_info:
        await integrations_mod.get_integration_principal(
            _user(), session, str(_APPLICATION_ID), "wrong-key"
        )
    assert exc_info.value.status_code == 401

    session.application = _application(status="disabled")
    with pytest.raises(HTTPException) as exc_info:
        await integrations_mod.get_integration_principal(
            _user(), session, str(_APPLICATION_ID), "correct-key"
        )
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_create_application_stores_only_hash_and_returns_key_once(monkeypatch):
    session = _FakeSession(kb_rows=[_KB_ALLOWED])
    audited = []

    async def _audit(*args, **kwargs):
        audited.append((args, kwargs))

    monkeypatch.setattr(integrations_mod.audit, "record", _audit)
    result = await integrations_mod.create_application(
        IntegrationApplicationCreate(name="新项目", kb_ids=[_KB_ALLOWED]),
        _user(),
        session,
    )
    stored = next(obj for obj in session.added if isinstance(obj, IntegrationApplication))
    assert result.application_key
    assert stored.key_hash != result.application_key
    assert stored.key_hash == integrations_mod._application_key_hash(result.application_key)
    assert "application_key" not in integrations_mod.IntegrationApplicationOut.model_fields
    assert result.application_key not in str(audited)


@pytest.mark.asyncio
async def test_sync_ask_consumes_shared_stream_and_uses_principal(monkeypatch):
    captured = {}

    async def _rate_limit(user, application):
        captured["rate_limit"] = (user, application)

    async def _ask_impl(payload, user, session, integration=None):
        captured["ask"] = (payload, user, session, integration)

        async def _events():
            yield ": ping\n\n"
            conversation_id = uuid.uuid4()
            message_id = uuid.uuid4()
            yield chat_mod._sse("meta", {
                "conversation_id": str(conversation_id),
                "message_id": str(message_id),
            })
            yield chat_mod._sse("citations", {"citations": [{"n": 1}]})
            yield chat_mod._sse("delta", {"text": "报销"})
            yield chat_mod._sse("delta", {"text": "标准"})
            yield chat_mod._sse("done", {
                "usage": {"prompt_tokens": 3},
                "suggestions": ["追问"],
            })

        return StreamingResponse(_events())

    monkeypatch.setattr(integrations_mod, "check_integration_rate_limit", _rate_limit)
    monkeypatch.setattr(integrations_mod, "_chat_ask_impl", _ask_impl)
    session = _FakeSession()
    principal = integrations_mod.IntegrationPrincipal(_user(), _application())
    result = await integrations_mod.integration_ask(
        IntegrationAskRequest(question="报销标准", kb_ids=[_KB_ALLOWED]),
        principal,
        session,
    )
    payload, asked_user, asked_session, application = captured["ask"]
    assert payload.question == "报销标准"
    assert asked_user is principal.user
    assert asked_session is session
    assert application is principal.application
    assert captured["rate_limit"] == (principal.user, principal.application)
    assert result.answer == "报销标准"
    assert result.citations == [{"n": 1}]
    assert result.suggestions == ["追问"]
    assert result.refused is False


@pytest.mark.asyncio
async def test_consume_ask_response_decodes_utf8_split_across_chunks():
    """增量解码：中文字符被切在两个 chunk 边界时不能抛 UnicodeDecodeError。"""
    body = (
        'event: meta\ndata: {"conversation_id": null, "message_id": null}\n\n'
        'event: delta\ndata: {"text": "报销标准"}\n\n'
        'event: done\ndata: {"usage": {}, "suggestions": []}\n\n'
    ).encode("utf-8")
    # 在"标"字三字节序列的内部切开，强制多字节字符跨 chunk
    cut = body.index("标".encode("utf-8")) + 1

    class _FakeResponse:
        def __init__(self, parts: list[bytes]) -> None:
            self.body_iterator = self._iter(parts)

        @staticmethod
        async def _iter(parts: list[bytes]):
            for part in parts:
                yield part

    result = await integrations_mod._consume_ask_response(
        _FakeResponse([body[:cut], body[cut:]])
    )

    assert result.answer == "报销标准"


@pytest.mark.asyncio
async def test_search_uses_real_acl_and_records_application(monkeypatch):
    captured = {}
    audited = []

    class _Retrieval:
        async def retrieve(self, session, query, tenant_id, kb_ids, clearance, subjects):
            captured["retrieve"] = (query, tenant_id, kb_ids, clearance, subjects)
            return RetrievalResult(chunks=[_chunk()])

    async def _rate_limit(user, application):
        pass

    async def _audit(*args, **kwargs):
        audited.append((args, kwargs))

    monkeypatch.setattr(integrations_mod, "check_integration_rate_limit", _rate_limit)
    monkeypatch.setattr(integrations_mod, "get_retrieval_service", lambda: _Retrieval())
    monkeypatch.setattr(integrations_mod.audit, "record", _audit)
    principal = integrations_mod.IntegrationPrincipal(_user(), _application())
    result = await integrations_mod.integration_search(
        IntegrationSearchRequest(
            query="住宿标准",
            kb_ids=[_KB_ALLOWED, _KB_USER_ONLY, _KB_APP_ONLY],
        ),
        principal,
        _FakeSession(),
    )
    assert captured["retrieve"][2] == [_KB_ALLOWED]
    assert captured["retrieve"][3] == principal.user.clearance
    assert captured["retrieve"][4] == principal.user.subjects
    assert len(result.items) == 1
    assert audited[0][0][1] == _USER_ID
    assert audited[0][1]["detail"]["application_id"] == str(_APPLICATION_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["other_app", "other_user", "web"])
async def test_conversation_binding_rejects_cross_application_user_and_web(scenario):
    conversation_id = uuid.uuid4()
    binding = IntegrationConversation(
        conversation_id=conversation_id,
        tenant_id=_TENANT_ID,
        application_id=_APPLICATION_ID,
        user_id=_USER_ID,
    )
    application = _application()
    integration = application
    if scenario == "other_app":
        integration = _application(id=uuid.uuid4())
    elif scenario == "other_user":
        binding.user_id = uuid.uuid4()
    elif scenario == "web":
        integration = None

    with pytest.raises(HTTPException) as exc_info:
        await chat_mod._chat_ask_impl(
            chat_mod.ChatAskRequest(
                question="继续提问",
                kb_ids=[_KB_ALLOWED],
                conversation_id=conversation_id,
            ),
            _user(),
            _FakeSession(kb_rows=[object()], binding=binding),
            integration=integration,
        )
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_integration_flow_isolates_memory_and_applies_dual_quota_and_audit(
    monkeypatch,
):
    session = _FakeSession(kb_rows=[object()])
    history_session = _FakeSession()
    monkeypatch.setattr(chat_mod, "SessionLocal", lambda: history_session)

    async def _rewrite(query, history, tenant_id):
        return query, "skipped", 100

    class _Retrieval:
        async def retrieve_chain(
            self, session, query, tenant_id, kb_ids, clearance, subjects, rewrite_score=None
        ):
            return RetrievalResult(chunks=[_chunk()], chain_mode="single")

    class _Generation:
        def stream_generate(self, *args, **kwargs):
            async def _events():
                yield StreamDelta(type="delta", text="500 元")
                yield StreamDelta(
                    type="done",
                    full_text="500 元",
                    usage={"prompt_tokens": 2, "completion_tokens": 1},
                    suggestions=[],
                )

            return _events()

    async def _system_prompt(*args, **kwargs):
        return "system"

    async def _caliber(*args, **kwargs):
        return {}

    async def _flag_off(*args, **kwargs):
        return False

    calls = {"audit": [], "persist": []}

    async def _application_quota(application, *args):
        calls["application_quota"] = application
        return None

    async def _user_quota(tenant_id, user_id, question, history, memory_prompt, chunks):
        calls["user_quota"] = user_id
        return DegradationPlan(history=history, memory_prompt=memory_prompt, chunks=chunks)

    async def _incr_usage(*args, **kwargs):
        calls["usage"] = kwargs

    async def _audit(*args, **kwargs):
        calls["audit"].append((args, kwargs))

    async def _persist(*args, **kwargs):
        calls["persist"].append(kwargs)

    async def _unexpected_memory(*args, **kwargs):
        raise AssertionError("集成调用不得读写长期用户画像")

    monkeypatch.setattr(chat_mod, "rewrite_query", _rewrite)
    monkeypatch.setattr(chat_mod, "get_retrieval_service", lambda: _Retrieval())
    monkeypatch.setattr(chat_mod, "get_stream_generation_service", lambda: _Generation())
    monkeypatch.setattr(chat_mod, "build_system_prompt", _system_prompt)
    monkeypatch.setattr(chat_mod, "snapshot_for_message", _caliber)
    monkeypatch.setattr(chat_mod.flag_service, "is_enabled", _flag_off)
    monkeypatch.setattr(chat_mod.quota_service, "check_application_quota", _application_quota)
    monkeypatch.setattr(chat_mod.quota_service, "check_and_degrade", _user_quota)
    monkeypatch.setattr(chat_mod.quota_service, "_incr_usage", _incr_usage)
    monkeypatch.setattr(chat_mod.audit, "record", _audit)
    monkeypatch.setattr(chat_mod, "_persist_assistant", _persist)
    monkeypatch.setattr(chat_mod, "_update_user_memory", _unexpected_memory)

    application = _application()
    response = await chat_mod._chat_ask_impl(
        chat_mod.ChatAskRequest(question="住宿标准", kb_ids=[_KB_ALLOWED]),
        _user(),
        session,
        integration=application,
    )
    result = await integrations_mod._consume_ask_response(response)
    assert result.answer == "500 元"
    binding = next(obj for obj in session.added if isinstance(obj, IntegrationConversation))
    assert binding.application_id == application.id
    assert binding.user_id == _USER_ID
    assert User not in history_session.get_models
    assert calls["application_quota"] is application
    assert calls["user_quota"] == _USER_ID
    assert calls["usage"]["application_id"] == application.id
    assert calls["audit"]
    assert all(
        kwargs["detail"]["application_id"] == str(application.id)
        for _, kwargs in calls["audit"]
    )


class _QuotaPipeline:
    def __init__(self, values):
        self.values = values

    def get(self, key):
        return self

    async def execute(self):
        return self.values


class _QuotaRedis:
    def __init__(self, values):
        self.pipeline_instance = _QuotaPipeline(values)

    def pipeline(self):
        return self.pipeline_instance

    async def aclose(self):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("values", "limits", "expected"),
    [
        (["199000", "0"], {}, "QUOTA_EXCEEDED_APPLICATION_DAILY_TOKEN"),
        (["0", "1000"], {}, "QUOTA_EXCEEDED_APPLICATION_DAILY_MSG"),
        (["0", "0"], {}, None),
    ],
)
async def test_application_quota_limits(monkeypatch, values, limits, expected):
    redis = _QuotaRedis(values)

    async def _get_redis():
        return redis

    monkeypatch.setattr(quota_service, "_get_redis", _get_redis)
    reason = await quota_service.check_application_quota(
        _application(**limits), "问题", [], "", [_chunk()]
    )
    assert reason == expected


class _RatePipeline:
    def __init__(self, counts=None, error=None):
        self.counts = counts
        self.error = error

    def incr(self, key):
        return self

    async def execute(self):
        if self.error:
            raise self.error
        return self.counts


class _RateRedis:
    def __init__(self, counts=None, error=None):
        self.pipeline_instance = _RatePipeline(counts, error)

    def pipeline(self):
        return self.pipeline_instance

    async def expire(self, key, seconds):
        return None

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_integration_rate_limit_checks_application_and_user(monkeypatch):
    monkeypatch.setattr(
        rate_limit_mod, "get_settings", lambda: SimpleNamespace(chat_rate_limit_per_minute=1)
    )
    redis = _RateRedis(counts=[2, 1])

    async def _get_redis():
        return redis

    monkeypatch.setattr(rate_limit_mod, "get_redis", _get_redis)
    with pytest.raises(HTTPException) as exc_info:
        await rate_limit_mod.check_integration_rate_limit(
            _user(), _application(rate_limit_per_minute=1)
        )
    assert exc_info.value.status_code == 429

    redis.pipeline_instance = _RatePipeline(counts=[1, 2])
    with pytest.raises(HTTPException) as exc_info:
        await rate_limit_mod.check_integration_rate_limit(
            _user(), _application(rate_limit_per_minute=60)
        )
    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_integration_rate_limit_does_not_fail_open(monkeypatch):
    redis = _RateRedis(error=RuntimeError("redis down"))

    async def _get_redis():
        return redis

    monkeypatch.setattr(rate_limit_mod, "get_redis", _get_redis)
    with pytest.raises(HTTPException) as exc_info:
        await rate_limit_mod.check_integration_rate_limit(_user(), _application())
    assert exc_info.value.status_code == 503
