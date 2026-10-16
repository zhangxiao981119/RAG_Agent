"""WU-M-20 parse-preview 端点测试 —— 结构对齐规范 §3.5 / INV-M-17 同屏回显。

钉住七件事：
  1. 结构：document_id / modality / source / blocks 与 §3.5 一致
  2. raw_url 一律相对路径（INV-M-16），无对象存储直连地址
  3. modality 与模态路由同源（图片扩展 → image）
  4. blocks 从 chunks 表读、chunk_index 排序（核对对象 = 真实入库结果）
  5. 多页扫描件 PDF → page_count = max(page_no) + 逐页 page_raw_urls
  6. xlsx 文档 → is_table 提示 true；图片文档 → false
  7. 无权限 / 密级不足 → 403
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
    def __init__(self, doc: SimpleNamespace | None, chunks: list[SimpleNamespace]) -> None:
        self._doc = doc
        self._chunks = chunks

    async def execute(self, stmt) -> _FakeResult:  # noqa: ANN001
        # 模拟 DB 行为：按 chunk_index 排序（端点的 order_by 由 SQLAlchemy 保证）
        return _FakeResult(sorted(self._chunks, key=lambda ch: ch.chunk_index))

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
        storage_key=f"{_TENANT_ID}/{_KB_ID}/g1/v1/scan.{ext}",
        level_rank=level_rank,
    )


def _chunk(index: int, text: str, page_no: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        chunk_index=index,
        content=text,
        page_no=page_no,
        token_count=10,
    )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    """客户端工厂：按用例挂文档 / chunks / 用户，用例结束统一清 overrides。"""

    def _mount(doc: SimpleNamespace, chunks: list[SimpleNamespace], user: CurrentUser) -> TestClient:
        app.dependency_overrides[get_current_user] = lambda: user

        async def _override_get_db():
            yield _FakeSession(doc, chunks)

        app.dependency_overrides[get_db] = _override_get_db
        # 不进 lifespan（避免真实连 redis/pg）；端点不依赖 lifespan 资源
        return TestClient(app, raise_server_exceptions=False)

    yield _mount
    app.dependency_overrides.clear()


# ── 用例 ─────────────────────────────────────────────────────────


def test_image_preview_structure_matches_spec(client):
    """图片文档：modality=image、raw_url 相对路径、blocks 逐块。"""
    chunks = [_chunk(0, "物料编码 M-1002", page_no=1), _chunk(1, "数量 5")]
    c = client(_doc("png"), chunks, _user())
    resp = c.get(f"/api/documents/{_DOC_ID}/parse-preview")

    assert resp.status_code == 200
    body = resp.json()
    assert body["document_id"] == str(_DOC_ID)
    assert body["modality"] == "image"
    assert body["source"]["raw_url"] == f"/api/documents/{_DOC_ID}/raw"
    assert body["source"]["page_count"] == 1
    assert body["source"]["page_raw_urls"] == [f"/api/documents/{_DOC_ID}/raw"]
    assert [b["index"] for b in body["blocks"]] == [0, 1]
    assert body["blocks"][0]["text"] == "物料编码 M-1002"
    assert body["blocks"][0]["is_table"] is False


def test_text_doc_modality_is_text(client):
    chunks = [_chunk(0, "制度正文")]
    c = client(_doc("md"), chunks, _user())
    body = c.get(f"/api/documents/{_DOC_ID}/parse-preview").json()

    assert body["modality"] == "text"


def test_blocks_ordered_by_chunk_index(client):
    """乱序入库的 chunk MUST 按 chunk_index 输出（核对顺序 = 检索顺序）。"""
    chunks = [_chunk(2, "第三"), _chunk(0, "第一"), _chunk(1, "第二")]
    c = client(_doc("png"), chunks, _user())
    body = c.get(f"/api/documents/{_DOC_ID}/parse-preview").json()

    assert [b["text"] for b in body["blocks"]] == ["第一", "第二", "第三"]


def test_scanned_pdf_page_count_and_urls(client):
    """多页扫描件：page_count = max(page_no)，逐页 page_raw_urls。"""
    chunks = [
        _chunk(0, "第 1 页转录", page_no=1),
        _chunk(1, "第 2 页转录", page_no=2),
        _chunk(2, "第 3 页转录", page_no=3),
    ]
    c = client(_doc("pdf"), chunks, _user())
    body = c.get(f"/api/documents/{_DOC_ID}/parse-preview").json()

    assert body["source"]["page_count"] == 3
    assert body["source"]["page_raw_urls"] == [
        f"/api/documents/{_DOC_ID}/raw?page=1",
        f"/api/documents/{_DOC_ID}/raw?page=2",
        f"/api/documents/{_DOC_ID}/raw?page=3",
    ]


def test_xlsx_blocks_flagged_as_table(client):
    """xlsx 文档块 is_table 提示 true（预览样式用）。"""
    chunks = [_chunk(0, "编码\t数量")]
    c = client(_doc("xlsx"), chunks, _user())
    body = c.get(f"/api/documents/{_DOC_ID}/parse-preview").json()

    assert body["blocks"][0]["is_table"] is True


def test_empty_chunks_returns_empty_blocks(client):
    """解析中/无块的文档：blocks 空数组，结构仍完整（不发明字段）。"""
    c = client(_doc("png"), [], _user())
    body = c.get(f"/api/documents/{_DOC_ID}/parse-preview").json()

    assert body["blocks"] == []
    assert body["source"]["page_count"] == 1


def test_forbidden_without_kb_authorization(client):
    c = client(_doc("png"), [], _user(clearance=20, authorized=[]))
    resp = c.get(f"/api/documents/{_DOC_ID}/parse-preview")

    assert resp.status_code == 403


def test_forbidden_below_clearance(client):
    c = client(_doc("png", level_rank=20), [], _user(clearance=10, authorized=[_KB_ID]))
    resp = c.get(f"/api/documents/{_DOC_ID}/parse-preview")

    assert resp.status_code == 403
