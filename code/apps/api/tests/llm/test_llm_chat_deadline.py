"""chat() 的总墙钟死线 + 「只吐 reasoning 不吐 content」的语义区分（不连真 LLM）。

背景（真实故障，2026-11）：`backfill_graph --all` 跑到第 4 分钟彻底冻住 —— `done`
卡在 11、实体卡在 132，连续 16 分钟零变化；进程还在，数据库里一条会话
`idle in transaction` 挂了 11 分钟。根因是 `httpx.AsyncClient(timeout=...)` 只是
**每次 I/O 超时**，而推理模型（qwen3.5-plus）会持续吐 `reasoning_content`：
字节一直在流 → read 超时永远不触发 → 永远等下去。

本文件钉住三件事：
  1. `chat()` 有**总墙钟**上限，到点抛 LLMTimeout（不再无限等）；
  2. 只有 reasoning_content、content 为空 → LLMError，message 指向「换非推理模型」，
     不再被下游误报成「输出非法 JSON」（两者修法完全不同）；
  3. `stream_chat` 默认不检查 content（用户对话可以只吐思考、也可以长流），
     保证既有调用方行为不变。
"""
from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

import app.services.llm as llm_mod
from app.services.llm import LLMError, LLMService, LLMTimeout


class _FakeSettings:
    """替身配置：只提供 stream_chat / chat 会读取的字段。"""

    llm_base_url = "http://llm.test/v1"
    llm_api_key = "secret"
    llm_model = "deepseek-v4-flash"
    llm_max_output_tokens = 4000
    llm_total_timeout_seconds = 60.0


@pytest.fixture(autouse=True)
def _patch_settings(monkeypatch):
    monkeypatch.setattr(llm_mod, "get_settings", lambda: _FakeSettings())


def _install_transport(monkeypatch, sse: bytes, captured: dict | None = None) -> None:
    """把 LLMService 内部自建的 AsyncClient 换成带 MockTransport 的实例。"""
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse
        )

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", factory)


_REASONING_ONLY_SSE = (
    'data: {"choices":[{"delta":{"reasoning_content":"先想想用户要什么……"}}]}\n\n'
    'data: {"choices":[{"delta":{"reasoning_content":"再想想实体类型……"}}]}\n\n'
    "data: [DONE]\n\n"
).encode("utf-8")


@pytest.mark.asyncio
async def test_chat_enforces_wall_clock_deadline(monkeypatch):
    """流永远不结束 → chat 必须按总墙钟预算抛 LLMTimeout，而不是无限等。"""
    service = LLMService()

    async def _endless_stream(messages, temperature, **kwargs):
        # 模拟「字节一直在流」：每次都有输出，per-IO 超时永远不会触发
        while True:
            yield "字"
            await asyncio.sleep(0.01)

    monkeypatch.setattr(service, "stream_chat", _endless_stream)

    started = time.monotonic()
    # 外层 wait_for 是给测试自己的护栏：实现若没加死线，这里会干净地失败而不是挂死
    with pytest.raises(LLMTimeout) as excinfo:
        await asyncio.wait_for(
            service.chat([{"role": "user", "content": "抽取实体"}], 0.1, timeout_seconds=0.2),
            timeout=1.5,
        )
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, f"应在 0.2s 预算附近返回，实际 {elapsed:.2f}s"
    message = str(excinfo.value)
    assert "总墙钟预算" in message
    assert "预算 0.2s" in message  # 预算取自传入的 timeout_seconds，不是全局 60s
    # 报错要能自解释：说明它可能把预算烧在推理内容上
    assert "reasoning_content" in message


@pytest.mark.asyncio
async def test_reasoning_only_output_raises_actionable_error(monkeypatch):
    """只有 reasoning_content、content 为空 → LLMError，message 指向换非推理模型。"""
    _install_transport(monkeypatch, _REASONING_ONLY_SSE)

    with pytest.raises(LLMError) as excinfo:
        await LLMService().chat([{"role": "user", "content": "抽取实体"}], 0.1)

    message = str(excinfo.value)
    assert "推理内容" in message
    assert "content 为空" in message
    assert "非推理模型" in message
    # 关键：不能被说成「非法 JSON」——那是格式错，修法完全不同
    assert "非法 JSON" not in message


@pytest.mark.asyncio
async def test_stream_chat_default_keeps_content_optional(monkeypatch):
    """stream_chat 默认不检查 content：既有调用方（用户对话）行为不变。"""
    _install_transport(monkeypatch, _REASONING_ONLY_SSE)

    deltas = [delta async for delta in LLMService().stream_chat(
        [{"role": "user", "content": "你好"}], 0.1
    )]

    assert deltas == []  # 只有 reasoning，不往外 yield
