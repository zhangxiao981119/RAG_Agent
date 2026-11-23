"""S6-3 开启态接口级用例（kbs）：删除门禁与批量上传的库位错误映射。

不连真库：用猴补丁替换 `resolve_kb_db_key`，验证库里已接线后的对外语义：
  1. delete_kb：kb 绑定非默认库位 → 409 KB_IN_BOUND_SCENE，且不进入删除逻辑；
  2. delete_kb：解析抛 DbScopeError（同一 kb 命中多个库）→ 409 KB_SCOPE_CONFLICT，
     detail 保留原始原因码（不得裸 500、不得丢 code）；
  3. 批量上传：单文件库位解析冲突 → 该文件进入 failed 且 detail 含 code，
     其余文件不受影响（不是整批 500）。
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.kbs import router
from app.api.deps import CurrentUser, get_current_user, get_db
from app.models import KnowledgeBase
from app.services.db_scope import DbScopeError

_TENANT_ID = uuid.uuid4()
_KB_ID = uuid.uuid4()


def _user() -> CurrentUser:
    return CurrentUser(
        user_id=uuid.uuid4(),
        tenant_id=_TENANT_ID,
        username="tester",
        display_name="tester",
        clearance=40,  # admin：库管理权
        dept_path="研发中心",
        subjects=[],
        authorized_kb_ids=[_KB_ID],
        acl_epoch=1,
    )


def _kb() -> KnowledgeBase:
    return KnowledgeBase(
        id=_KB_ID,
        tenant_id=_TENANT_ID,
        name="测试库",
        description="",
    )


class _FakeSession:
    def __init__(self, kb: KnowledgeBase | None) -> None:
        self._kb = kb

    def add(self, obj) -> None:  # noqa: ANN001
        pass

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        pass

    async def refresh(self, obj) -> None:  # noqa: ANN001
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        if getattr(obj, "created_at", None) is None:
            from datetime import datetime, timezone

            obj.created_at = datetime.now(timezone.utc)

    async def get(self, model, key):  # noqa: ANN001
        if model is KnowledgeBase:
            return self._kb
        return None

    async def delete(self, obj) -> None:  # noqa: ANN001
        raise AssertionError("库位判定失败时 MUST NOT 进入删除逻辑")


class _FakeStorage:
    def __init__(self) -> None:
        self.put_keys: list[str] = []

    async def put_object(self, key: str, data: bytes, content_type: str) -> None:
        self.put_keys.append(key)


class _FakeArqPool:
    async def enqueue_job(self, name: str, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        pass


@pytest.fixture
def client(monkeypatch):
    storage = _FakeStorage()

    async def _async_of(value):
        return value

    monkeypatch.setattr("app.api.kbs.get_storage", lambda: _async_of(storage))

    async def _fake_audit(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
        return None

    monkeypatch.setattr("app.api.kbs.audit.record", _fake_audit)

    test_app = FastAPI()
    test_app.include_router(router, prefix="/api")
    test_app.state.arq_pool = _FakeArqPool()
    test_app.state.test_storage = storage
    test_app.dependency_overrides[get_current_user] = lambda: _user()

    async def _override_get_db():
        yield _FakeSession(_kb())

    test_app.dependency_overrides[get_db] = _override_get_db
    return TestClient(test_app, raise_server_exceptions=False)


def test_delete_kb_bound_key_returns_409(client, monkeypatch):
    async def _resolve_bound(session, tenant_id, kb_ids):  # noqa: ANN001
        return "kb_finance"

    monkeypatch.setattr("app.api.kbs.resolve_kb_db_key", _resolve_bound)

    resp = client.delete(f"/api/kbs/{_KB_ID}")
    assert resp.status_code == 409
    assert "KB_IN_BOUND_SCENE" in resp.json()["detail"]


def test_delete_kb_scope_conflict_returns_409_with_code(client, monkeypatch):
    async def _resolve_conflict(session, tenant_id, kb_ids):  # noqa: ANN001
        raise DbScopeError(
            "DB_SCOPE_MULTI_KEY",
            {"conflicts": {str(_KB_ID): ["kb_finance", "kb_hr"]}},
        )

    monkeypatch.setattr("app.api.kbs.resolve_kb_db_key", _resolve_conflict)

    resp = client.delete(f"/api/kbs/{_KB_ID}")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "KB_SCOPE_CONFLICT" in detail
    assert "DB_SCOPE_MULTI_KEY" in detail


def test_batch_upload_scope_conflict_reports_per_file(client, monkeypatch):
    async def _resolve_conflict(session, tenant_id, kb_ids):  # noqa: ANN001
        raise DbScopeError(
            "DB_SCOPE_MULTI_KEY",
            {"conflicts": {str(_KB_ID): ["kb_finance", "kb_hr"]}},
        )

    monkeypatch.setattr("app.api.kbs.resolve_kb_db_key", _resolve_conflict)

    resp = client.post(
        f"/api/kbs/{_KB_ID}/documents/batch",
        files=[("files", ("制度.pdf", b"%PDF-1.4", "application/pdf"))],
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["succeeded"] == []
    assert len(body["failed"]) == 1
    assert body["failed"][0]["filename"] == "制度.pdf"
    assert "DB_SCOPE_MULTI_KEY" in body["failed"][0]["detail"]


def test_single_upload_scope_conflict_returns_409_with_code(client, monkeypatch):
    """单文件上传：库位解析冲突 → 409 KB_SCOPE_CONFLICT 且保留原因码，不落存储。"""
    storage = client.app.state.test_storage

    async def _resolve_conflict(session, tenant_id, kb_ids):  # noqa: ANN001
        raise DbScopeError(
            "DB_SCOPE_MULTI_KEY",
            {"conflicts": {str(_KB_ID): ["kb_finance", "kb_hr"]}},
        )

    monkeypatch.setattr("app.api.kbs.resolve_kb_db_key", _resolve_conflict)

    resp = client.post(
        f"/api/kbs/{_KB_ID}/documents",
        files={"file": ("制度.pdf", b"%PDF-1.4", "application/pdf")},
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "KB_SCOPE_CONFLICT" in detail
    assert "DB_SCOPE_MULTI_KEY" in detail
    assert storage.put_keys == []
