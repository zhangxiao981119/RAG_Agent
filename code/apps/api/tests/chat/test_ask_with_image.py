"""chat ask 带图提问端点测试（WU-M-07 / INV-M-03 / INV-M-04）。

覆盖：
  · 不传图回归：meta.modality=text，事件序 meta→stage→citations→delta→done
  · 带图 happy path：meta.modality=image；转录文本拼接进检索与生成；
    user 消息只存原始问题；无任何 Document 落库（INV-M-03 守卫）
  · 图片 > 3 张 → 400（MUST NOT 静默截断）
  · data_url 单图超 max_upload_mb → 400
  · 图中无可读内容 → SSE refused IMAGE_NO_CONTENT（INV-M-04，不降级猜测）
  · 视觉链路异常 → SSE refused VISION_UNAVAILABLE
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.api.chat as chat_mod
from app.api.deps import CurrentUser, get_current_user, get_db
from app.api.rate_limit import check_chat_rate_limit
from app.main import app
from app.services.generate import StreamDelta
from app.services.multimodal import VisionError, VisionNoContentError
from app.services.parse import ParsedBlock
from app.services.retrieve.base import RetrievalResult, RetrievedChunk

_TENANT_ID = uuid.uuid4()
_USER_ID = uuid.uuid4()
_KB_ID = uuid.uuid4()

_USER = CurrentUser(
    user_id=_USER_ID,
    tenant_id=_TENANT_ID,
    username="tester",
    display_name="tester",
    clearance=40,  # admin：豁免库级交集校验，聚焦本 WU 的模态行为
    dept_path="",
    subjects=[],
    authorized_kb_ids=[],
    acl_epoch=1,
)

_IMG_A = "data:image/png;base64,QUJD"
_IMG_B = "data:image/png;base64,QUJE"


# ── 替身：DB ────────────────────────────────────────────────────


class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self) -> _FakeResult:
        return self

    def all(self) -> list:
        return list(self._rows)


class _FakeSession:
    """替身 AsyncSession。

    kb_rows 非空时充当端点层的 KB 存在性查询（只关心行数）；
    为空时充当 SessionLocal 会话（历史 / 画像查询返回空）。
    add/add_all 记录落库对象，供 INV-M-03 守卫断言。
    """

    def __init__(self, kb_count: int = 0) -> None:
        self._kb_rows = [object() for _ in range(kb_count)]
        self.added: list = []

    async def execute(self, stmt) -> _FakeResult:  # noqa: ANN001
        return _FakeResult(self._kb_rows)

    async def get(self, model, key):  # noqa: ANN001
        return None

    def add(self, obj) -> None:  # noqa: ANN001
        self.added.append(obj)

    def add_all(self, objs) -> None:  # noqa: ANN001
        self.added.extend(objs)

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        pass

    async def refresh(self, obj) -> None:  # noqa: ANN001
        pass

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc) -> bool:  # noqa: ANN001
        return False


# ── 替身：检索 / 生成 ────────────────────────────────────────────


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
        self.queries: list[str] = []

    async def retrieve(self, session, query, tenant_id, kb_ids, clearance, subjects):  # noqa: ANN001
        self.queries.append(query)
        return RetrievalResult(
            chunks=[_chunk()],
            stage_ms={"retrieving": 1},
            path_stats={},
        )

    async def retrieve_chain(self, session, query, tenant_id, kb_ids, clearance, subjects,
                             rewrite_score=None):  # noqa: ANN001
        # 单趟语义替身：本 WU 关注模态行为，链式编排由 tests/retrieve/test_chain.py 覆盖
        result = await self.retrieve(session, query, tenant_id, kb_ids, clearance, subjects)
        result.chain_mode = "single"
        return result


class _FakeGeneration:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def stream_generate(self, query, chunks, history=None, memory_prompt="",  # noqa: ANN001
                        tenant_id=None, rewrite_score=None):
        self.queries.append(query)

        async def _gen():
            yield StreamDelta(type="delta", text="住宿报销标准为 500 元/晚 [1]\n")
            yield StreamDelta(
                type="done",
                full_text="住宿报销标准为 500 元/晚 [1]\n",
                suggestions=[],
                usage={"prompt_tokens": 1, "completion_tokens": 2},
            )

        return _gen()


# ── 夹具：依赖覆盖 + 模块级替身 ──────────────────────────────────


class _Harness:
    def __init__(self) -> None:
        self.client: TestClient | None = None
        self.retrieval = _FakeRetrieval()
        self.generation = _FakeGeneration()
        self.db_session = _FakeSession(kb_count=1)
        self.persisted: list[dict] = []
        self.audit_calls: list[dict] = []


@pytest.fixture
def harness(monkeypatch):
    h = _Harness()

    app.dependency_overrides[get_current_user] = lambda: _USER

    async def _override_get_db():
        yield h.db_session

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[check_chat_rate_limit] = lambda: None

    # SessionLocal 会话（历史 / 画像 / 压缩计数查询）：全部返回空
    monkeypatch.setattr(chat_mod, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(chat_mod, "get_retrieval_service", lambda: h.retrieval)
    monkeypatch.setattr(chat_mod, "get_stream_generation_service", lambda: h.generation)

    async def _fake_rewrite(query, history, tenant_id):  # noqa: ANN001
        return query, "skipped", 100

    monkeypatch.setattr(chat_mod, "rewrite_query", _fake_rewrite)

    async def _fake_system_prompt(*args, **kwargs):  # noqa: ANN001
        return "system"

    monkeypatch.setattr(chat_mod, "build_system_prompt", _fake_system_prompt)

    async def _fake_caliber(tenant_id, chain_mode=None):  # noqa: ANN001
        return {}

    monkeypatch.setattr(chat_mod, "snapshot_for_message", _fake_caliber)

    async def _fake_persist(conversation_id, user, message_id, text, refused, citations, meta, chain_mode=None):  # noqa: ANN001
        h.persisted.append({"text": text, "refused": refused, "meta": meta})

    monkeypatch.setattr(chat_mod, "_persist_assistant", _fake_persist)

    async def _fake_memory(user_id, user_msg, assistant_msg):  # noqa: ANN001
        return None

    monkeypatch.setattr(chat_mod, "_update_user_memory", _fake_memory)

    async def _fake_audit(*args, **kwargs):  # noqa: ANN001
        h.audit_calls.append({"args": args, "kwargs": kwargs})

    monkeypatch.setattr(chat_mod.audit, "record", _fake_audit)

    async def _flag_off(*args, **kwargs):  # noqa: ANN001
        return False

    monkeypatch.setattr(chat_mod.flag_service, "is_enabled", _flag_off)

    async def _noop_incr(*args, **kwargs):  # noqa: ANN001
        return None

    monkeypatch.setattr(chat_mod.quota_service, "_incr_usage", _noop_incr)

    with TestClient(app) as client:
        h.client = client
        yield h
    app.dependency_overrides.clear()


# ── 工具 ────────────────────────────────────────────────────────


def _events(body: str) -> list[tuple[str, dict]]:
    """把 SSE 响应体解析为 (event, data) 序列。"""
    out: list[tuple[str, dict]] = []
    for block in body.split("\n\n"):
        event = data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[len("event: "):]
            elif line.startswith("data: "):
                data = line[len("data: "):]
        if event:
            out.append((event, json.loads(data) if data else {}))
    return out


def _ask_body(**overrides) -> dict:
    body = {"kb_ids": [str(_KB_ID)], "question": "住宿报销标准是多少？"}
    body.update(overrides)
    return body


def _patch_extract(monkeypatch, fn) -> None:  # noqa: ANN001
    """把视觉抽取入口替换为指定行为（harness 之外的按需替换）。"""
    monkeypatch.setattr(chat_mod, "extract_text_from_image", fn)


# ── 用例 ────────────────────────────────────────────────────────


def test_ask_without_images_regression(harness):
    """不传图回归：modality=text，事件序不变，管线收到原始问题。"""
    resp = harness.client.post("/api/chat/ask", json=_ask_body())
    assert resp.status_code == 200

    events = _events(resp.text)
    kinds = [e for e, _ in events]
    # 事件顺序 MUST 保持 meta→stage→citations→delta...→done
    assert kinds[0] == "meta"
    assert kinds.index("citations") < kinds.index("delta") < kinds.index("done")
    assert events[0][1]["modality"] == "text"
    # 无图时管线收到的问题就是原始问题
    assert harness.retrieval.queries == ["住宿报销标准是多少？"]
    assert harness.generation.queries == ["住宿报销标准是多少？"]


def test_ask_with_images_happy_path(harness, monkeypatch):
    transcripts = {_IMG_A: "转录一：住宿标准表", _IMG_B: "转录二：差旅规则"}

    async def _fake_extract(ref):  # noqa: ANN001
        return ParsedBlock(text=transcripts[ref.data_url])

    _patch_extract(monkeypatch, _fake_extract)

    resp = harness.client.post(
        "/api/chat/ask",
        json=_ask_body(images=[{"data_url": _IMG_A}, {"data_url": _IMG_B}]),
    )
    assert resp.status_code == 200

    events = _events(resp.text)
    kinds = [e for e, _ in events]
    assert kinds[0] == "meta"
    assert events[0][1]["modality"] == "image"
    assert kinds.index("citations") < kinds.index("delta") < kinds.index("done")

    # 转录文本按图片顺序拼在原问题之后，同时进检索与生成
    expected = (
        "住宿报销标准是多少？\n\n"
        "[图片1转录]\n转录一：住宿标准表\n\n"
        "[图片2转录]\n转录二：差旅规则"
    )
    assert harness.retrieval.queries == [expected]
    assert harness.generation.queries == [expected]

    # INV-M-03 守卫：整条链路只允许新增会话与消息，MUST NOT 出现 Document
    assert not any(
        type(obj).__name__ in ("Document", "ParseJob")
        for obj in harness.db_session.added
    )
    # user 消息只存原始问题（转录文本不沉淀）
    user_msgs = [
        obj for obj in harness.db_session.added
        if type(obj).__name__ == "Message" and obj.role == "user"
    ]
    assert len(user_msgs) == 1
    assert user_msgs[0].content == "住宿报销标准是多少？"
    # 正常落库的 assistant 消息带 modality 之前的 meta 合并，refused=False
    assert harness.persisted[-1]["refused"] is False


def test_ask_too_many_images_400(harness):
    resp = harness.client.post(
        "/api/chat/ask",
        json=_ask_body(images=[{"data_url": _IMG_A}] * 4),
    )
    assert resp.status_code == 400
    assert "TOO_MANY_IMAGES" in resp.json()["detail"]


def test_ask_oversized_data_url_400(harness, monkeypatch):
    # 把单图上限压到 1MB，构造解码后 >1MB 的 base64 载荷
    monkeypatch.setattr(
        chat_mod, "get_settings",
        lambda: SimpleNamespace(max_upload_mb=1),
    )
    big_payload = "A" * (1024 * 1024 * 2)  # base64 解码后约 1.5MB
    resp = harness.client.post(
        "/api/chat/ask",
        json=_ask_body(images=[{"data_url": f"data:image/png;base64,{big_payload}"}]),
    )
    assert resp.status_code == 400
    assert "IMAGE_TOO_LARGE" in resp.json()["detail"]


def test_ask_image_no_content_refused(harness, monkeypatch):
    async def _raise_no_content(ref):  # noqa: ANN001
        raise VisionNoContentError("图中未识别到可读内容")

    _patch_extract(monkeypatch, _raise_no_content)

    resp = harness.client.post(
        "/api/chat/ask",
        json=_ask_body(images=[{"data_url": _IMG_A}]),
    )
    assert resp.status_code == 200

    events = _events(resp.text)
    kinds = [e for e, _ in events]
    assert kinds == ["meta", "refused"]
    assert events[0][1]["modality"] == "image"
    assert events[1][1]["reason"] == "IMAGE_NO_CONTENT"
    # 拒答也落库，保持对话完整性
    assert harness.persisted[-1]["refused"] is True
    assert harness.persisted[-1]["meta"]["reason"] == "IMAGE_NO_CONTENT"
    # 抽取失败 MUST NOT 进入检索（不降级为纯文本作答）
    assert harness.retrieval.queries == []


def test_ask_vision_error_refused(harness, monkeypatch):
    async def _raise_vision(ref):  # noqa: ANN001
        raise VisionError("视觉服务 503")

    _patch_extract(monkeypatch, _raise_vision)

    resp = harness.client.post(
        "/api/chat/ask",
        json=_ask_body(images=[{"data_url": _IMG_A}]),
    )
    assert resp.status_code == 200

    events = _events(resp.text)
    kinds = [e for e, _ in events]
    assert kinds == ["meta", "refused"]
    assert events[1][1]["reason"] == "VISION_UNAVAILABLE"
    assert harness.persisted[-1]["refused"] is True
    assert harness.retrieval.queries == []
