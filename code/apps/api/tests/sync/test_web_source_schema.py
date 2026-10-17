"""WU-M-11 web 源 schema 与凭据加密测试 —— INV-M-07 凭据密文存储。

钉住六件事：
  1. web 源创建：凭据明文进请求 → DB 里 auth_payload 是 Fernet 密文（无明文）
  2. 加密往返：encrypt_credential → decrypt_credential 还原原 dict
  3. 解密 fail-closed：密文损坏 / 密钥不匹配 → ValueError（不静默给空值）
  4. 响应不回显：Out 只有 has_auth 布尔，无 auth/auth_payload 字段
  5. 校验拦截面：local 源带 web 字段 422；web 源缺 entry_urls 422
  6. 更新语义：auth 换新全量替换密文；clear_auth 显式清除并重置 auth_type
"""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_db, require_admin
from app.api.sync import router
from app.models import KnowledgeBase
from app.services.crypto import decrypt_credential, encrypt_credential

_TENANT_ID = uuid.uuid4()
_KB_ID = uuid.uuid4()
_ADMIN_ID = uuid.uuid4()


class _CaptureSession:
    """KB get 返回预设库；add 捕获对象供断言；commit/refresh no-op（id 补齐）。"""

    def __init__(self, kb: KnowledgeBase | None) -> None:
        self._kb = kb
        self.added: list[object] = []

    def add(self, obj) -> None:  # noqa: ANN001
        self.added.append(obj)

    async def commit(self) -> None:
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()
            if getattr(obj, "created_at", None) is None:
                from datetime import datetime, timezone

                obj.created_at = datetime.now(timezone.utc)
                obj.updated_at = obj.created_at
            # 模拟 DB server_default：last_sync_count 无 Python default
            if getattr(obj, "last_sync_count", None) is None:
                obj.last_sync_count = 0

    async def refresh(self, obj) -> None:  # noqa: ANN001
        pass

    async def get(self, model, key):  # noqa: ANN001
        if model is KnowledgeBase:
            return self._kb
        return None


def _kb() -> KnowledgeBase:
    return KnowledgeBase(id=_KB_ID, tenant_id=_TENANT_ID, name="测试库", description="")


def _admin() -> dict:
    return {"user_id": _ADMIN_ID, "tenant_id": _TENANT_ID, "clearance": 40}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.api.sync.audit.record", _fake_audit)
    session = _CaptureSession(_kb())
    test_app = FastAPI()
    test_app.include_router(router, prefix="/api")

    test_app.dependency_overrides[require_admin] = lambda: _SimpleUser(_admin())

    async def _override_get_db():
        yield session

    test_app.dependency_overrides[get_db] = _override_get_db
    return TestClient(test_app, raise_server_exceptions=False), session


async def _fake_audit(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
    return None


class _SimpleUser:
    """require_admin 依赖返回的最小用户形态（sync 端点只用到这两个字段）。"""

    def __init__(self, d: dict) -> None:
        self.user_id = d["user_id"]
        self.tenant_id = d["tenant_id"]
        self.clearance = d["clearance"]


# ── 加密原语 ─────────────────────────────────────────────────────


def test_credential_roundtrip():
    plain = json.dumps({"username": "u1", "password": "p@ss"}, ensure_ascii=False)
    ciphertext = encrypt_credential(plain)
    assert ciphertext != plain and "p@ss" not in ciphertext
    assert json.loads(decrypt_credential(ciphertext))["password"] == "p@ss"


def test_decrypt_invalid_ciphertext_fails_closed():
    with pytest.raises(ValueError, match="解密失败"):
        decrypt_credential(encrypt_credential("ok")[:-6] + "AAAAAA")


# ── 创建端点 ─────────────────────────────────────────────────────


def test_web_source_create_encrypts_payload(client):
    c, session = client
    resp = c.post(
        f"/api/kbs/{_KB_ID}/sync-sources",
        json={
            "name": "内网 wiki",
            "source_type": "web",
            "path": "https://wiki.internal",
            "auth_type": "basic",
            "auth": {"username": "alice", "password": "s3cret"},
            "entry_urls": ["https://wiki.internal/docs/"],
            "scope_rules": {"allow_prefixes": ["/docs/"]},
        },
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["auth_type"] == "basic"
    assert body["has_auth"] is True
    assert "auth" not in body and "auth_payload" not in body

    source = session.added[0]
    # INV-M-07 核心断言：落库的是密文，且不含明文
    assert source.auth_payload is not None
    assert "alice" not in source.auth_payload and "s3cret" not in source.auth_payload
    assert json.loads(decrypt_credential(source.auth_payload))["username"] == "alice"


def test_local_source_rejects_web_fields(client):
    c, _ = client
    resp = c.post(
        f"/api/kbs/{_KB_ID}/sync-sources",
        json={"name": "本地源", "source_type": "local", "path": "docs",
              "auth_type": "basic", "auth": {"username": "u", "password": "p"}},
    )
    assert resp.status_code == 422


def test_web_source_requires_entry_urls(client):
    c, _ = client
    resp = c.post(
        f"/api/kbs/{_KB_ID}/sync-sources",
        json={"name": "内网", "source_type": "web", "path": "https://wiki.internal"},
    )
    assert resp.status_code == 422


def test_web_source_auth_without_credentials_rejected(client):
    c, _ = client
    resp = c.post(
        f"/api/kbs/{_KB_ID}/sync-sources",
        json={"name": "内网", "source_type": "web", "path": "https://wiki.internal",
              "auth_type": "bearer", "entry_urls": ["https://wiki.internal/"]},
    )
    assert resp.status_code == 422


# ── 更新端点 ─────────────────────────────────────────────────────


class _ExistingSource:
    """模拟一个已存在的 web 源（sync.py 只读属性，够用即可）。"""

    def __init__(self) -> None:
        import datetime as _dt

        self.id = uuid.uuid4()
        self.tenant_id = _TENANT_ID
        self.kb_id = _KB_ID
        self.name = "旧源"
        self.source_type = "web"
        self.path = "https://wiki.internal"
        self.branch = None
        self.file_patterns = "*.md"
        self.level_rank = 20
        self.status = "active"
        self.synced_files = {}
        self.last_synced_at = None
        self.last_sync_count = 0
        self.last_error = None
        self.created_at = _dt.datetime.now(_dt.timezone.utc)
        self.updated_at = self.created_at
        self.auth_type = "basic"
        self.auth_payload = encrypt_credential(json.dumps({"username": "old", "password": "old"}))
        self.entry_urls = ["https://wiki.internal/"]
        self.scope_rules = {}


@pytest.fixture
def update_client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.api.sync.audit.record", _fake_audit)
    source = _ExistingSource()
    session = _CaptureSession(None)

    async def _get(model, key):  # noqa: ANN001
        if model.__name__ == "SyncSource":
            return source
        return None

    session.get = _get
    test_app = FastAPI()
    test_app.include_router(router, prefix="/api")
    test_app.dependency_overrides[require_admin] = lambda: _SimpleUser(_admin())

    async def _override_get_db():
        yield session

    test_app.dependency_overrides[get_db] = _override_get_db
    return TestClient(test_app, raise_server_exceptions=False), source


def test_update_replaces_ciphertext_fully(update_client):
    c, source = update_client
    resp = c.patch(
        f"/api/sync-sources/{source.id}",
        json={"auth_type": "bearer", "auth": {"token": "new-token"}},
    )
    assert resp.status_code == 200
    assert source.auth_type == "bearer"
    restored = json.loads(decrypt_credential(source.auth_payload))
    assert restored == {"token": "new-token"}, "新凭据必须全量替换而非合并"


def test_update_clear_auth_resets_type(update_client):
    c, source = update_client
    resp = c.patch(f"/api/sync-sources/{source.id}", json={"clear_auth": True})
    assert resp.status_code == 200
    assert source.auth_payload is None
    assert source.auth_type == "none"
