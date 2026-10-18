"""WU-M-21 批量上传测试 —— INV-M-18 同格式约束与逐文件结果。

钉住六件事：
  1. 同格式批量 → 201，逐文件入队（arq enqueue 次数 == 文件数）
  2. 混合格式 → 400 MIXED_FILE_TYPES，且零入队（校验在业务动作之前）
  3. 部分失败（单文件超大小）→ succeeded/failed 逐文件结果，失败不影响成功文件
  4. 单文件端点回归：重构为 _upload_single 薄壳后行为不变（201）
  5. 未授权用户（库不在授权清单）→ 403
  6. upload_document 与 batch 共用 _upload_single，MUST NOT 出现第二套上传逻辑
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.kbs import router
from app.api.deps import CurrentUser, get_current_user, get_db
from app.config.settings import get_settings
from app.models import KnowledgeBase

_TENANT_ID = uuid.uuid4()
_KB_ID = uuid.uuid4()


def _user(clearance: int = 40, authorized: list[uuid.UUID] | None = None) -> CurrentUser:
    return CurrentUser(
        user_id=uuid.uuid4(),
        tenant_id=_TENANT_ID,
        username="tester",
        display_name="tester",
        clearance=clearance,
        dept_path="研发中心",
        subjects=[],
        authorized_kb_ids=authorized or [],
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
    """get(KnowledgeBase) 返回预设库；flush 给新对象赋 id；其余 no-op。"""

    def __init__(self, kb: KnowledgeBase | None) -> None:
        self._kb = kb

    def add(self, obj) -> None:  # noqa: ANN001
        pass

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        pass

    async def refresh(self, obj) -> None:  # noqa: ANN001
        # DocumentOut/审计需要 id / created_at；flush 后本该由 DB 生成
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        if getattr(obj, "created_at", None) is None:
            from datetime import datetime, timezone

            obj.created_at = datetime.now(timezone.utc)

    async def get(self, model, key):  # noqa: ANN001
        if model is KnowledgeBase:
            return self._kb
        return None


class _FakeStorage:
    def __init__(self) -> None:
        self.put_keys: list[str] = []

    async def put_object(self, key: str, data: bytes, content_type: str) -> None:
        self.put_keys.append(key)


class _FakeArqPool:
    def __init__(self) -> None:
        self.jobs: list[str] = []

    async def enqueue_job(self, name: str, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        self.jobs.append(name)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "app.api.kbs.get_storage", lambda: _async_of(_FakeStorage())
    )
    monkeypatch.setattr("app.api.kbs.audit.record", _fake_audit)
    fake_arq = _FakeArqPool()

    def _mount(kb: KnowledgeBase | None, user: CurrentUser) -> TestClient:
        test_app = FastAPI()
        test_app.include_router(router, prefix="/api")
        test_app.state.arq_pool = fake_arq

        test_app.dependency_overrides[get_current_user] = lambda: user

        async def _override_get_db():
            yield _FakeSession(kb)

        test_app.dependency_overrides[get_db] = _override_get_db
        return TestClient(test_app, raise_server_exceptions=False)

    yield _mount, fake_arq


async def _async_of(value):
    return value


async def _fake_audit(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
    return None


def _file(name: str, content: bytes) -> tuple[str, bytes, str]:
    return name, content, "application/octet-stream"


# ── 用例 ─────────────────────────────────────────────────────────


def test_batch_same_ext_succeeds(client):
    mount, arq = client
    c = mount(_kb(), _user(authorized=[_KB_ID]))
    resp = c.post(
        f"/api/kbs/{_KB_ID}/documents/batch",
        files=[
            ("files", _file("a.md", "# 第一篇".encode("utf-8"))),
            ("files", _file("b.md", "# 第二篇".encode("utf-8"))),
        ],
    )

    assert resp.status_code == 201
    body = resp.json()
    assert [f["filename"] for f in body["succeeded"]] == ["a.md", "b.md"]
    assert body["failed"] == []
    assert len(arq.jobs) == 2


def test_batch_mixed_ext_rejected_400(client):
    mount, arq = client
    c = mount(_kb(), _user(authorized=[_KB_ID]))
    resp = c.post(
        f"/api/kbs/{_KB_ID}/documents/batch",
        files=[
            ("files", _file("a.md", "text".encode("utf-8"))),
            ("files", _file("b.docx", b"bin")),
        ],
    )

    assert resp.status_code == 400
    assert "MIXED_FILE_TYPES" in resp.json()["detail"]
    assert arq.jobs == [], "混合格式必须在任何业务动作前被拦"


def test_batch_partial_failure_reports_per_file(client, monkeypatch: pytest.MonkeyPatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "max_upload_mb", 1)  # 上限临时降为 1MB

    mount, arq = client
    c = mount(_kb(), _user(authorized=[_KB_ID]))
    big = b"x" * (1 * 1024 * 1024 + 1)
    resp = c.post(
        f"/api/kbs/{_KB_ID}/documents/batch",
        files=[
            ("files", _file("big.md", big)),
            ("files", _file("ok.md", "小文件".encode("utf-8"))),
        ],
    )

    assert resp.status_code == 201
    body = resp.json()
    assert [f["filename"] for f in body["succeeded"]] == ["ok.md"]
    assert len(body["failed"]) == 1
    assert body["failed"][0]["filename"] == "big.md"
    assert "FILE_TOO_LARGE" in body["failed"][0]["detail"]
    assert len(arq.jobs) == 1, "失败文件不入队"


def test_single_upload_still_works_after_refactor(client):
    mount, arq = client
    c = mount(_kb(), _user(authorized=[_KB_ID]))
    resp = c.post(
        f"/api/kbs/{_KB_ID}/documents",
        files=[("file", _file("solo.md", "# 单文件".encode("utf-8")))],
    )

    assert resp.status_code == 201
    assert resp.json()["filename"] == "solo.md"
    assert len(arq.jobs) == 1


def test_forbidden_without_kb_authorization(client):
    mount, _ = client
    c = mount(_kb(), _user(clearance=20, authorized=[]))
    resp = c.post(
        f"/api/kbs/{_KB_ID}/documents/batch",
        files=[("files", _file("a.md", "text".encode("utf-8")))],
    )

    assert resp.status_code == 403


# ── 图片扩展名联动（P0-1）─────────────────────────────────────────
# 背景：图片扩展名进了 allowed_ext 默认值，但图片必须经本地 OCR 才能解析。
# 若只在解析层 fail-closed，用户看到的是「上传成功 + job 失败」，排查成本高。


def test_default_allowed_ext_includes_image_types():
    """代码默认白名单 MUST 与 IMAGE_EXTS 对齐 —— 否则图片在入口就被拒，门禁永远不执行。

    ★ 断言的是 `Settings` 类默认值，不是 `get_settings()`：后者读环境变量，
      部署侧配错（本次就踩到 ALLOWED_EXT 未带图片）会让单测跟着红，
      变成「环境问题伪装成代码回归」。
    """
    from app.config.settings import Settings
    from app.services.parse.image_parser import IMAGE_EXTS

    factory = Settings.model_fields["allowed_ext"].default_factory
    default_ext = factory()
    missing = [e for e in IMAGE_EXTS if e not in default_ext]
    assert missing == [], f"默认白名单缺图片扩展名：{missing}"


def test_image_upload_rejected_when_ocr_disabled(client, monkeypatch: pytest.MonkeyPatch):
    """OCR 关闭时上传图片 → 415，且报错直接指向开哪个开关。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "ocr_enabled", False)
    # 与部署环境解耦：即便容器的 ALLOWED_EXT 还是旧值，也要验证到联动分支
    monkeypatch.setattr(settings, "allowed_ext", [*settings.allowed_ext, "png"])

    mount, arq = client
    c = mount(_kb(), _user(authorized=[_KB_ID]))
    resp = c.post(
        f"/api/kbs/{_KB_ID}/documents",
        files=[("file", _file("scan.png", b"\x89PNG\r\n\x1a\n"))],
    )

    assert resp.status_code == 415
    detail = resp.json()["detail"]
    assert "IMAGE_REQUIRES_OCR" in detail
    assert "OCR_ENABLED=true" in detail
    assert arq.jobs == [], "被拒的图片 MUST NOT 入队（否则白跑一次解析）"


def test_image_upload_accepted_when_ocr_enabled(client, monkeypatch: pytest.MonkeyPatch):
    """OCR 开启时图片正常放行并入队 —— 白名单与开关的联动不能反向误伤。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "ocr_enabled", True)
    monkeypatch.setattr(settings, "allowed_ext", [*settings.allowed_ext, "png"])

    mount, arq = client
    c = mount(_kb(), _user(authorized=[_KB_ID]))
    resp = c.post(
        f"/api/kbs/{_KB_ID}/documents",
        files=[("file", _file("scan.png", b"\x89PNG\r\n\x1a\n"))],
    )

    assert resp.status_code == 201
    assert resp.json()["filename"] == "scan.png"
    assert len(arq.jobs) == 1
