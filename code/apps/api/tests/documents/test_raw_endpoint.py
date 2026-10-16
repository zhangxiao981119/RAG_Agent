"""WU-M-19 /raw 端点测试 —— 图片回链、逐页渲染、鉴权与 INV-M-16 红线。

钉住六件事：
  1. 图片文档 → 流式转发原字节，Content-Type 用对象存储记录值
  2. 文本系文档 → 维持提取纯文本（列表预览现行行为不破坏）
  3. PDF ?page=N → 渲染该页 PNG（INV-M-05 逐页回链）；越界 400
  4. 无权限 / 密级不足 → 403（走既有 _get_visible_document 链）
  5. 响应中 MUST NOT 出现 S3_ENDPOINT / 签名串（INV-M-16）
  6. 图片请求带 ?page → page 对图片无意义，仍返回图片字节
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.deps import CurrentUser, get_current_user, get_db
from app.main import app

_TENANT_ID = uuid.uuid4()
_DOC_ID = uuid.uuid4()
_KB_ID = uuid.uuid4()
_STORAGE_KEY = f"{_TENANT_ID}/{_KB_ID}/g1/v1/scan.png"
_S3_ENDPOINT_VALUE = "http://minio:9000"

_PNG_MAGIC = b"\x89PNG-fake-image-bytes"


def _user(
    clearance: int = 40,
    authorized: list[uuid.UUID] | None = None,
) -> CurrentUser:
    return CurrentUser(
        user_id=uuid.uuid4(),
        tenant_id=_TENANT_ID,
        username="tester",
        display_name="tester",
        clearance=clearance,
        dept_path="",
        subjects=[],
        authorized_kb_ids=authorized or [],
        acl_epoch=1,
    )


class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self) -> _FakeResult:
        return self

    def all(self) -> list:
        return list(self._rows)


class _FakeSession:
    """get(Document, id) 返回预设文档；其余查询空。"""

    def __init__(self, doc: SimpleNamespace | None) -> None:
        self._doc = doc

    async def execute(self, stmt) -> _FakeResult:  # noqa: ANN001
        return _FakeResult([])

    async def get(self, model, key):  # noqa: ANN001
        return self._doc


def _doc(ext: str, level_rank: int = 20) -> SimpleNamespace:
    return SimpleNamespace(
        id=_DOC_ID,
        tenant_id=_TENANT_ID,
        kb_id=_KB_ID,
        deleted_at=None,
        ext=ext,
        filename=f"scan.{ext}",
        storage_key=_STORAGE_KEY,
        level_rank=level_rank,
    )


class _FakeStorage:
    """open_object 返回 (异步字节流, 记录的 MIME)；get_object 返回全量字节。"""

    def __init__(self, content_type: str, data: bytes) -> None:
        self._content_type = content_type
        self._data = data

    async def open_object(self, key: str):
        async def _iter():
            yield self._data

        return _iter(), self._content_type

    async def get_object(self, key: str) -> bytes:
        return self._data


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    """客户端工厂：按用例挂文档 / 存储 / 用户，用例结束统一清 overrides。"""

    def _mount(doc: SimpleNamespace, storage: _FakeStorage, user: CurrentUser) -> TestClient:
        app.dependency_overrides[get_current_user] = lambda: user

        async def _override_get_db():
            yield _FakeSession(doc)

        app.dependency_overrides[get_db] = _override_get_db
        monkeypatch.setattr(
            "app.api.documents.get_storage", lambda: _async_of(storage)
        )
        # 不进 lifespan（避免真实连 redis/pg）；端点不依赖 lifespan 资源
        return TestClient(app, raise_server_exceptions=False)

    yield _mount
    app.dependency_overrides.clear()


async def _async_of(value):
    return value


# ── 用例 ─────────────────────────────────────────────────────────


def test_image_raw_streams_bytes_with_recorded_content_type(client):
    """图片 → 原字节 + 存储 MIME + inline 文件名（前端相对路径可直接 <img>）。"""
    storage = _FakeStorage("image/png", _PNG_MAGIC)
    c = client(_doc("png"), storage, _user())
    resp = c.get(f"/api/documents/{_DOC_ID}/raw")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/png")
    assert resp.content == _PNG_MAGIC
    assert "inline" in resp.headers["content-disposition"]
    assert "scan.png" in resp.headers["content-disposition"]


def test_image_raw_ignores_page_param(client):
    """图片带 ?page → page 无意义，仍返回图片字节（显式分流：图片分支优先）。"""
    storage = _FakeStorage("image/jpeg", _PNG_MAGIC)
    c = client(_doc("jpg"), storage, _user())
    resp = c.get(f"/api/documents/{_DOC_ID}/raw?page=3")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/jpeg")


def test_text_doc_still_returns_extracted_text(client):
    """文本系 → 提取纯文本（列表预览现行行为不变）。"""
    storage = _FakeStorage("text/markdown", "# 费用制度\n住宿 500 元/晚".encode("utf-8"))
    c = client(_doc("md"), storage, _user())
    resp = c.get(f"/api/documents/{_DOC_ID}/raw")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "住宿 500 元/晚" in resp.text


def test_pdf_page_renders_png(client):
    """PDF ?page=N → 该页渲染为 PNG（逐页回链）。"""
    import pymupdf

    pdf_doc = pymupdf.open()
    page = pdf_doc.new_page()
    page.insert_text((60, 60), "hello raw page")
    data = pdf_doc.tobytes()
    pdf_doc.close()

    storage = _FakeStorage("application/pdf", data)
    c = client(_doc("pdf"), storage, _user())
    resp = c.get(f"/api/documents/{_DOC_ID}/raw?page=1")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/png")
    assert resp.content[:4] == b"\x89PNG"


def test_pdf_page_out_of_range_400(client):
    import pymupdf

    pdf_doc = pymupdf.open()
    pdf_doc.new_page()
    data = pdf_doc.tobytes()
    pdf_doc.close()

    storage = _FakeStorage("application/pdf", data)
    c = client(_doc("pdf"), storage, _user())
    resp = c.get(f"/api/documents/{_DOC_ID}/raw?page=99")

    assert resp.status_code == 400
    assert resp.json()["detail"] == "PAGE_OUT_OF_RANGE"


def test_forbidden_without_kb_authorization(client):
    """无权限用户取他人文档 raw 必 403（验收口径）。"""
    storage = _FakeStorage("image/png", _PNG_MAGIC)
    outsider = _user(clearance=20, authorized=[])  # 非 admin 且未授权该库
    c = client(_doc("png"), storage, outsider)
    resp = c.get(f"/api/documents/{_DOC_ID}/raw")

    assert resp.status_code == 403


def test_forbidden_below_clearance(client):
    """密级不足（文档 level_rank > 用户 clearance）→ 403。"""
    storage = _FakeStorage("image/png", _PNG_MAGIC)
    low = _user(clearance=10, authorized=[_KB_ID])  # 库已授权但密级不够
    c = client(_doc("png", level_rank=20), storage, low)
    resp = c.get(f"/api/documents/{_DOC_ID}/raw")

    assert resp.status_code == 403


def test_no_object_storage_address_leaks(client):
    """INV-M-16 红线：响应头 MUST NOT 含对象存储地址 / 签名串（响应体是
    对象自己的字节，原样转发即忠实回链，不在断言范围）。"""
    storage = _FakeStorage("image/png", _PNG_MAGIC)
    c = client(_doc("png"), storage, _user())
    resp = c.get(f"/api/documents/{_DOC_ID}/raw")

    assert resp.content == _PNG_MAGIC, "响应体就是对象字节本身"
    for name, value in resp.headers.items():
        assert _S3_ENDPOINT_VALUE not in value, f"响应头 {name} 泄露对象存储地址"
        assert "X-Amz-Signature" not in value
    assert not [k for k in resp.headers if k.lower().startswith("x-amz-")]
