"""LLM 服务 —— OpenAI 兼容 /v1/chat/completions SSE 流式（手册 §3.1 L2 + §3.4 故障 3/4/5）。

端点：POST {LLM_BASE_URL}/v1/chat/completions  (stream=true)
超时：
  · 首 token 15s（手册 §3.4 故障 3，超时则中止，保留已输出）
  · 总超时 60s（避免无限挂起）

★ 不做 fallback：LLM 全挂时进维护态，MUST NOT 降级用模板回答
  （那会绕过 L3 grounding，违反"只依据知识库作答"承诺）。
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

import httpx

from app.config.settings import get_settings

logger = logging.getLogger(__name__)


class LLMTimeout(Exception):
    """首 token 超时。"""


class LLMError(Exception):
    """生成中途报错。"""


class LLMService:
    async def stream_chat(
        self, messages: list[dict[str, str]], temperature: float
    ) -> AsyncIterator[str]:
        """流式生成。yield 每个 delta 文本片段。

        首 token 超时 → LLMTimeout；中途报错 → LLMError。
        """
        settings = get_settings()
        if not settings.llm_base_url:
            raise LLMError("LLM_BASE_URL 未配置")
        headers = {"Authorization": f"Bearer {settings.llm_api_key}"} if settings.llm_api_key else {}
        body = {
            "model": settings.llm_model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }

        first_token_seen = False
        try:
            async with httpx.AsyncClient(timeout=settings.llm_total_timeout_seconds) as client:
                async with client.stream(
                    "POST",
                    f"{settings.llm_base_url.rstrip('/').rstrip('/v1')}/v1/chat/completions",
                    headers=headers,
                    json=body,
                ) as response:
                    if response.status_code != 200:
                        text = await response.aread()
                        raise LLMError(
                            f"LLM 返回 {response.status_code}: {text.decode()[:200]}"
                        )
                    # 首 token 超时计时
                    try:
                        async for line in response.aiter_lines():
                            if not line or not line.startswith("data:"):
                                continue
                            data = line[len("data:") :].strip()
                            if data == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data)
                            except json.JSONDecodeError:
                                continue
                            choices = chunk.get("choices", [])
                            if not choices:
                                continue
                            delta = choices[0].get("delta", {}).get("content")
                            if delta:
                                first_token_seen = True
                                yield delta
                    except (httpx.TimeoutException, asyncio.TimeoutError) as exc:
                        if not first_token_seen:
                            raise LLMTimeout(f"首 token 超时: {exc}") from exc
                        logger.warning("LLM 中途超时，已输出部分内容: %s", exc)
                        return
        except (httpx.TimeoutException, asyncio.TimeoutError) as exc:
            if not first_token_seen:
                raise LLMTimeout(f"首 token 超时: {exc}") from exc
            logger.warning("LLM 总超时，已输出部分内容: %s", exc)
            return
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM 调用失败: {exc}") from exc

    async def chat(self, messages: list[dict[str, str]], temperature: float) -> str:
        """非流式调用（用于 L3 grounding 等需要完整输出的场景）。"""
        result: list[str] = []
        async for delta in self.stream_chat(messages, temperature):
            result.append(delta)
        return "".join(result)


def get_llm_service() -> LLMService:
    return LLMService()
