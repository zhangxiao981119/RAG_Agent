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
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.config.settings import get_settings
from app.infra import metrics

logger = logging.getLogger(__name__)

ChatMessage = dict[str, Any]
"""OpenAI 兼容对话消息（M7 WU-M-05 放宽类型）。

`content` 允许两种形态，均按 OpenAI 兼容规范原样透传：
  · 纯文本：str
  · 多模态：list[{"type": "text", "text": ...},
                 {"type": "image_url", "image_url": {"url": ...}}]
MUST NOT 引入厂商私有参数（项目定位为"任意 OpenAI 兼容接口"）。"""


class LLMTimeout(Exception):
    """首 token 超时。"""


class LLMError(Exception):
    """生成中途报错。"""


class LLMService:
    async def stream_chat(
        self,
        messages: list[ChatMessage],
        temperature: float,
        *,
        model: str | None = None,
        timeout_seconds: float | None = None,
        max_tokens: int | None = None,
        require_content: bool = False,
    ) -> AsyncIterator[str]:
        """流式生成。yield 每个 delta 文本片段。

        首 token 超时 → LLMTimeout；中途报错 → LLMError。
        messages 支持纯文本与多模态 content 数组（WU-M-05），请求体原样透传。

        model / timeout_seconds / max_tokens 是**可选覆盖**，默认 None = 用 settings 的口径：
          · 图抽取（离线）要能用与主模型不同的模型（GRAPH_EXTRACTOR_MODEL），
            且它是批量长输出，用生成链路的 60s 总超时必然不够（见
            GRAPH_EXTRACT_TIMEOUT_SECONDS）。
          · 同理输出上限：生成链路按 4K 预留（decisions.CONTEXT_OUTPUT_RESERVE_TOKENS），
            而图抽取一次要吐一批实体+提及+关系，4K 会在列表中途被硬截断
            （见 GRAPH_EXTRACT_MAX_OUTPUT_TOKENS）。启动校验把**全局**值锁在 4K，
            所以只能按调用覆盖，不能改全局。
          · 覆盖值只影响本次调用，不写回 settings —— 口径真源仍在 config 层，
            这里只是「按调用方给的口径发一次请求」。

        ★ timeout_seconds 在**本方法**里是 httpx 的**每次 I/O 超时**，不是总墙钟：
          用户对话要能长流，只要还有字节在流就不该掐断。批量离线接口要的是总预算，
          那层语义在 chat()（见其 docstring）。

        require_content=True：流正常结束后若一个 content 字符都没产出 → LLMError。
          推理模型（如 qwen3.5-plus）会把预算全烧在 reasoning_content 上，content 一直
          为空；此时对上层而言"拿到了结果"，但拿到的是空串 —— 图抽取会把它误报成
          「输出非法 JSON」，而两者修法完全不同（换非推理模型 / 改 prompt）。
          默认 False，保证所有既有调用方行为逐字节不变。
        """
        settings = get_settings()
        if not settings.llm_base_url:
            metrics.incr("rag.llm.error", kind="not_configured")
            raise LLMError("LLM_BASE_URL 未配置")
        headers = {"Authorization": f"Bearer {settings.llm_api_key}"} if settings.llm_api_key else {}
        body = {
            "model": model or settings.llm_model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
            # ★ 显式设输出上限。不设时长度完全由服务端默认值决定，
            #   而 decisions.CONTEXT_OUTPUT_RESERVE_TOKENS 已按 4K 扣过预算 ——
            #   不显式限制，那部分"预留"就是空的（启动时校验两者关系）。
            #   传了 max_tokens 就用调用方口径（图抽取的批量长输出），
            #   不传时仍取 settings 全局值 —— 请求体与引入覆盖前逐字节相同。
            "max_tokens": max_tokens or settings.llm_max_output_tokens,
        }
        total_timeout = timeout_seconds or settings.llm_total_timeout_seconds

        first_token_seen = False
        # 只计数、不 yield：reasoning_content 是模型的思考过程，不该进答案，
        # 但它的字符数是「为什么 content 为空」的唯一证据（见 require_content）。
        content_chars = 0
        reasoning_chars = 0
        try:
            async with httpx.AsyncClient(timeout=total_timeout) as client:
                async with client.stream(
                    "POST",
                    f"{settings.llm_base_url.rstrip('/').removesuffix('/v1')}/v1/chat/completions",
                    headers=headers,
                    json=body,
                ) as response:
                    if response.status_code != 200:
                        text = await response.aread()
                        metrics.incr("rag.llm.error", kind=f"http_{response.status_code}")
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
                            delta_obj = choices[0].get("delta", {})
                            reasoning = delta_obj.get("reasoning_content")
                            if reasoning:
                                reasoning_chars += len(reasoning)
                            delta = delta_obj.get("content")
                            if delta:
                                first_token_seen = True
                                content_chars += len(delta)
                                yield delta
                    except (httpx.TimeoutException, asyncio.TimeoutError) as exc:
                        if not first_token_seen:
                            metrics.incr("rag.llm.first_token_timeout")
                            raise LLMTimeout(f"首 token 超时: {exc}") from exc
                        logger.warning("LLM 中途超时，已输出部分内容: %s", exc)
                        return
                    # 走到这里说明流正常结束（含 [DONE] 提前 break）
                    if require_content and content_chars == 0:
                        metrics.incr("rag.llm.error", kind="reasoning_only")
                        raise LLMError(
                            f"LLM 只产出了推理内容（reasoning_content {reasoning_chars} 字符），"
                            "content 为空：该模型是推理模型，不适合结构化抽取，"
                            "请换非推理模型或降低思考预算"
                        )
        except (httpx.TimeoutException, asyncio.TimeoutError) as exc:
            if not first_token_seen:
                metrics.incr("rag.llm.first_token_timeout")
                raise LLMTimeout(f"首 token 超时: {exc}") from exc
            logger.warning("LLM 总超时，已输出部分内容: %s", exc)
            return
        except httpx.HTTPError as exc:
            metrics.incr("rag.llm.error", kind="transport")
            raise LLMError(f"LLM 调用失败: {exc}") from exc

    async def chat(
        self,
        messages: list[ChatMessage],
        temperature: float,
        *,
        model: str | None = None,
        timeout_seconds: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """非流式调用（L3 grounding / 治理审查 / 图抽取等需要完整输出的场景）。

        ★ timeout_seconds 在本方法是**总墙钟预算**，与 stream_chat 的「每次 I/O 超时」
          是两个语义，别混：
          · stream_chat：只要还有字节在流就不掐断 —— 用户对话要能长流；
          · chat：`asyncio.timeout` 包住整条流 —— 它是批量离线调用
            （grounding / 审查 / 抽取），拿到半截结果没有意义，MUST 有上限。
          这个区别不是洁癖：qwen3.5-plus 这类推理模型会持续吐 reasoning_content，
          字节一直在流 → httpx 的 per-IO 超时永远不触发 → 进程无限等待
          （实测冻结 16 分钟，数据库连接 idle in transaction 11 分钟）。

        ★ 固定 require_content=True：批量接口拿到空串一定是失败，没有例外。
        """
        budget = timeout_seconds or get_settings().llm_total_timeout_seconds
        result: list[str] = []
        started = time.monotonic()
        try:
            async with asyncio.timeout(budget):
                async for delta in self.stream_chat(
                    messages,
                    temperature,
                    model=model,
                    timeout_seconds=timeout_seconds,
                    max_tokens=max_tokens,
                    require_content=True,
                ):
                    result.append(delta)
        except TimeoutError as exc:
            elapsed = time.monotonic() - started
            metrics.incr("rag.llm.error", kind="chat_wall_clock_timeout")
            raise LLMTimeout(
                f"LLM 调用超出总墙钟预算（已耗时 {elapsed:.1f}s，预算 {budget:g}s，"
                f"已收到 {sum(len(part) for part in result)} 字符）："
                "批量接口不接受无限流 —— 若模型是推理模型，它可能把预算全烧在 "
                "reasoning_content 上，建议换非推理模型或降低思考预算"
            ) from exc
        text = "".join(result)
        if not text:
            # 兜住「流在吐字前就结束」的路径（例如 read 超时后走了 stream_chat 的
            # early return）：require_content 的检查只在流正常结束时生效。
            metrics.incr("rag.llm.error", kind="empty_content")
            raise LLMError(
                "LLM 未产出任何 content（收到 0 字符）："
                "若模型是推理模型，它可能把预算全烧在 reasoning_content 上，"
                "请换非推理模型或降低思考预算"
            )
        return text


def get_llm_service() -> LLMService:
    return LLMService()
