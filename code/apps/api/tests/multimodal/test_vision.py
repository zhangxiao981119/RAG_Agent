"""视觉抽取服务单测（WU-M-06 / INV-M-04 / INV-M-15）。

覆盖：
  · data_url 透传 → 请求体是 OpenAI 兼容多模态 content 数组
  · storage_key → 从对象存储取字节、补 MIME、内联成 data URL
  · 模型回复哨兵「无可读内容」→ 抛 VisionNoContentError（不降级猜测）
  · 空输出 → 同上
  · ImageRef 二选一校验（恰有其一）
  · 提示词走注册表且锁定区含三条约束（INV-M-15）
"""
from __future__ import annotations

import base64

import pytest
from pydantic import ValidationError

from app.config.prompts import NO_ANSWER_TEXT, build_prompt, validate_prompt
from app.schemas.chat import ImageRef
from app.services.multimodal import VisionNoContentError, extract_text_from_image
from app.services.multimodal import vision as vision_mod


class _FakeLLM:
    """替身：记录收到的消息，返回预设文本。"""

    def __init__(self, raw: str) -> None:
        self.raw = raw
        self.calls: list[list[dict]] = []

    async def chat(self, messages, temperature):  # noqa: ANN001
        self.calls.append(messages)
        return self.raw


class _FakeStorage:
    """替身：返回预设字节，记录被读取的 key。"""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.keys: list[str] = []

    async def get_object(self, key: str) -> bytes:
        self.keys.append(key)
        return self.data


def _patch_llm(monkeypatch, raw: str) -> _FakeLLM:
    fake = _FakeLLM(raw)
    monkeypatch.setattr(vision_mod, "get_llm_service", lambda: fake)
    return fake


def _patch_storage(monkeypatch, data: bytes) -> _FakeStorage:
    fake = _FakeStorage(data)

    async def _get() -> _FakeStorage:
        return fake

    monkeypatch.setattr(vision_mod, "get_storage", _get)
    return fake


# ── ImageRef 二选一 ──────────────────────────────────────────────


def test_image_ref_requires_exactly_one_source():
    with pytest.raises(ValidationError):
        ImageRef()  # 两者都缺
    with pytest.raises(ValidationError):
        ImageRef(storage_key="k", data_url="d")  # 两者都给
    assert ImageRef(storage_key="k").storage_key == "k"
    assert ImageRef(data_url="d").data_url == "d"


# ── data_url：透传 + 消息体形状 ─────────────────────────────────


@pytest.mark.asyncio
async def test_data_url_passthrough_and_message_shape(monkeypatch):
    fake = _patch_llm(monkeypatch, "报销标准：一线城市 500 元")
    ref = ImageRef(data_url="data:image/png;base64,QUJD")

    block = await extract_text_from_image(ref)

    assert block.text == "报销标准：一线城市 500 元"
    assert len(fake.calls) == 1
    content = fake.calls[0][0]["content"]
    assert content[0]["type"] == "text"          # 先文本指令
    assert content[1] == {                        # 后图片
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,QUJD"},
    }


@pytest.mark.asyncio
async def test_prompt_instruction_comes_from_registry(monkeypatch):
    """文本部分 MUST 是注册表拼出的 vision_extract 模板。"""
    fake = _patch_llm(monkeypatch, "随便一段")
    await extract_text_from_image(ImageRef(data_url="data:image/png;base64,QUJD"))
    assert fake.calls[0][0]["content"][0]["text"] == build_prompt("vision_extract", None)


# ── storage_key：取字节 → 补 MIME → 内联 ────────────────────────


@pytest.mark.asyncio
async def test_storage_key_builds_data_url(monkeypatch):
    payload = b"\x89PNG\r\n\x1a\nfake-bytes"
    fake_llm = _patch_llm(monkeypatch, "转录出的文字")
    storage = _patch_storage(monkeypatch, payload)
    ref = ImageRef(storage_key="t/kb/group/v1/scan.png")

    await extract_text_from_image(ref)

    assert storage.keys == ["t/kb/group/v1/scan.png"]
    url = fake_llm.calls[0][0]["content"][1]["image_url"]["url"]
    assert url == "data:image/png;base64," + base64.b64encode(payload).decode()


@pytest.mark.asyncio
async def test_unknown_ext_falls_back_to_octet_stream(monkeypatch):
    fake_llm = _patch_llm(monkeypatch, "转录出的文字")
    _patch_storage(monkeypatch, b"raw")
    await extract_text_from_image(ImageRef(storage_key="t/kb/group/v1/noext"))
    url = fake_llm.calls[0][0]["content"][1]["image_url"]["url"]
    assert url.startswith("data:application/octet-stream;base64,")


# ── 无内容 → 抛错（不降级猜测，INV-M-04）─────────────────────────


@pytest.mark.asyncio
async def test_sentinel_raises_no_content(monkeypatch):
    _patch_llm(monkeypatch, NO_ANSWER_TEXT)
    with pytest.raises(VisionNoContentError):
        await extract_text_from_image(ImageRef(data_url="data:image/png;base64,QUJD"))


@pytest.mark.asyncio
async def test_quoted_sentinel_still_detected(monkeypatch):
    """模型常给哨兵加引号 —— 识别口径必须与 generate 层一致。"""
    _patch_llm(monkeypatch, f"“{NO_ANSWER_TEXT}”")
    with pytest.raises(VisionNoContentError):
        await extract_text_from_image(ImageRef(data_url="data:image/png;base64,QUJD"))


@pytest.mark.asyncio
async def test_empty_output_raises_no_content(monkeypatch):
    _patch_llm(monkeypatch, "   \n  ")
    with pytest.raises(VisionNoContentError):
        await extract_text_from_image(ImageRef(data_url="data:image/png;base64,QUJD"))


# ── 提示词注册与锁定区（INV-M-15）────────────────────────────────


def test_vision_prompt_locked_region_has_required_constraints():
    validate_prompt("vision_extract", None)  # 不合规会抛 PromptInvalidError
    locked = build_prompt("vision_extract", None)
    assert "仅依据给定图片内容" in locked   # 仅依据给定内容
    assert "编造" in locked                 # 不得臆造
    assert "改写原文" in locked             # 不得改写原文
    assert NO_ANSWER_TEXT in locked          # 无可读内容哨兵在锁定区
