"""LLMService 多模态签名放宽测试（WU-M-05）。

覆盖三点：
  1. 纯文本 messages 请求体与既有行为完全一致（回归）；
  2. OpenAI 兼容 content 数组（text + image_url）原样进请求体，不被改写；
  3. SSE 仍按 delta.content 逐段 yield。
用 httpx.MockTransport 拦截请求，不触网。
"""
from __future__ import annotations

import json

import httpx
import pytest

import app.services.llm as llm_mod
from app.services.llm import LLMService

_SSE = (
    'data: {"choices":[{"delta":{"content":"你好"}}]}\n\n'
    'data: {"choices":[{"delta":{"content":"，世界"}}]}\n\n'
    "data: [DONE]\n\n"
).encode("utf-8")


class _FakeSettings:
    """替身配置：仅提供 stream_chat 会读取的字段。"""

    llm_base_url = "http://llm.test/v1"
    llm_api_key = "secret"
    llm_model = "deepseek-v4-flash"
    llm_max_output_tokens = 4000
    llm_total_timeout_seconds = 60.0


@pytest.fixture(autouse=True)
def _patch_settings(monkeypatch):
    monkeypatch.setattr(llm_mod, "get_settings", lambda: _FakeSettings())


def _install_transport(monkeypatch, captured: dict) -> None:
    """把 LLMService 内部自建的 AsyncClient 换成带 MockTransport 的实例。"""
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=_SSE
        )

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", factory)


async def _drain(messages, **overrides):
    """消费流式输出，返回 delta 列表。"""
    return [delta async for delta in LLMService().stream_chat(messages, 0.1, **overrides)]


@pytest.mark.asyncio
async def test_text_only_body_unchanged(monkeypatch):
    """纯文本调用：请求体字段与改造前一致（回归）。"""
    captured: dict = {}
    _install_transport(monkeypatch, captured)
    messages = [{"role": "user", "content": "你好"}]

    deltas = await _drain(messages)

    assert "".join(deltas) == "你好，世界"
    body = captured["body"]
    assert body["messages"] == messages
    assert body["temperature"] == 0.1
    assert body["stream"] is True
    assert body["max_tokens"] == 4000
    assert body["model"] == "deepseek-v4-flash"
    # 未额外引入厂商私有参数：请求体就是这 5 个键
    assert set(body.keys()) == {"model", "messages", "temperature", "stream", "max_tokens"}
    assert captured["url"] == "http://llm.test/v1/chat/completions"


@pytest.mark.asyncio
async def test_multimodal_content_array_passthrough(monkeypatch):
    """多模态 content 数组原样透传，不做任何改写。"""
    captured: dict = {}
    _install_transport(monkeypatch, captured)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "这张图里写了什么？"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,AAAA"},
                },
            ],
        }
    ]

    deltas = await _drain(messages)

    assert "".join(deltas) == "你好，世界"
    assert captured["body"]["messages"] == messages


@pytest.mark.asyncio
async def test_max_tokens_override_only_affects_this_call(monkeypatch):
    """max_tokens 覆盖只影响本次请求体，且不写回 settings（下一次调用回到 4K）。

    图抽取用它绕开「全局 4K 被 CONTEXT_OUTPUT_RESERVE_TOKENS 锁死」的限制：
    批量长输出（4 分片/请求）在 4K 下会在实体列表中途被硬截断，
    截断后是合法 JSON 的前缀 → 被误报成「模型输出非法 JSON」。
    """
    captured: dict = {}
    _install_transport(monkeypatch, captured)
    messages = [{"role": "user", "content": "抽取实体"}]

    await _drain(messages, max_tokens=8000)
    assert captured["body"]["max_tokens"] == 8000
    # 请求体键集不变 —— 覆盖值只换值，不引入新字段
    assert set(captured["body"].keys()) == {
        "model",
        "messages",
        "temperature",
        "stream",
        "max_tokens",
    }

    # 同进程内紧接着的普通调用必须回到全局值：证明覆盖没有泄漏成「新默认」
    await _drain(messages)
    assert captured["body"]["max_tokens"] == 4000
