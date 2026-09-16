"""生成服务 —— prompt 模板 + LLM 流式 + L3 校验 + 引用回填（手册 §3.1 L2 + §5.2）。

流程：
  1. 构造 prompt（<context> + 引用编号 + 四条约束）
  2. 调 LLM 流式生成（服务端缓冲完整文本）
  3. L3 出口校验（grounding.check_grounding）
  4. 引用回填（从 chunk 取 filename/heading_path/page_no）
  5. 返回 GenerationResult，由 API 层切成 deltas 推送

★ 不做 fallback：LLM 挂了抛错，不降级用模板。
★ 模拟流式：API 层把最终文本按 ~30 字符/秒切片推送（用户偏好）。
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from app.config import decisions
from app.services.grounding import check_grounding
from app.services.llm import LLMError, LLMTimeout, get_llm_service
from app.services.retrieve.base import RetrievedChunk

logger = logging.getLogger(__name__)

# 手册 §3.1 L2 四条约束 + 格式要求
_SYSTEM_PROMPT = """你是一个严格依据知识库回答问题的助手。

约束（MUST 遵守）：
1. 只能使用 <context> 中的内容回答问题
2. 每个结论后 MUST 附 [n] 引用编号，n 对应 <context> 中的编号
3. 上下文不足时 MUST 回答"知识库中未找到相关内容"
4. 禁止使用自身知识补充、禁止编造

格式要求：
- 用短句，每句不超过 30 字
- 关键点用数字编号（如 1. xxx [n]）
- 不同要点之间换行
- 引用编号紧跟结论，中间不加空格

如果 <context> 无法回答问题，直接回复"知识库中未找到相关内容"，不要附引用编号。"""


def _build_context(chunks: list[RetrievedChunk]) -> str:
    """构造 <context> 块。每个 chunk 一个编号 [n]。"""
    parts: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        location = f"{chunk.filename}"
        if chunk.heading_path:
            location += f" / {chunk.heading_path}"
        if chunk.page_no is not None:
            location += f" / 第 {chunk.page_no} 页"
        parts.append(
            f"[{index}] {location}\n内容：{chunk.content}"
        )
    return "\n\n".join(parts)


def _build_user_prompt(query: str, chunks: list[RetrievedChunk]) -> str:
    context = _build_context(chunks)
    return f"<context>\n{context}\n</context>\n\n问题：{query}\n\n请依据 <context> 回答，每个结论后附 [n] 引用编号。"


@dataclass
class Citation:
    """引用回填结构（手册 §5.2 citations 字段）。"""

    n: int
    chunk_id: uuid.UUID
    doc_id: uuid.UUID
    filename: str
    heading_path: str
    page_no: int | None
    score: float


@dataclass
class GenerationResult:
    text: str
    raw_text: str
    stripped_sentences: int
    refused: bool
    refuse_reason: str
    citations: list[Citation] = field(default_factory=list)
    usage: dict = field(default_factory=dict)


class GenerationService:
    async def generate(
        self, query: str, chunks: list[RetrievedChunk]
    ) -> GenerationResult:
        # 构造引用映射
        valid_ns: set[int] = set()
        citations: list[Citation] = []
        for index, chunk in enumerate(chunks, start=1):
            valid_ns.add(index)
            citations.append(
                Citation(
                    n=index,
                    chunk_id=chunk.chunk_id,
                    doc_id=chunk.document_id,
                    filename=chunk.filename,
                    heading_path=chunk.heading_path,
                    page_no=chunk.page_no,
                    score=chunk.display_score,
                )
            )

        # 构造 prompt
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(query, chunks)},
        ]

        # 调 LLM（流式生成，服务端缓冲完整文本）
        # 异常处理（手册 §3.4 故障 3/4）：
        #   · 首 token 超时 / 调用失败 + 无已输出 → refused=True
        #   · 中途报错 + 有已输出 → 保留部分文本（不 refused）
        llm = get_llm_service()
        raw_text_parts: list[str] = []
        llm_failed = False
        llm_fail_reason = ""
        try:
            async for delta in llm.stream_chat(messages, decisions.GENERATION_TEMPERATURE):
                raw_text_parts.append(delta)
        except LLMTimeout as exc:
            logger.warning("LLM 首 token 超时: %s", exc)
            llm_failed = True
            llm_fail_reason = "LLM_TIMEOUT"
        except LLMError as exc:
            logger.error("LLM 生成失败: %s", exc)
            llm_failed = True
            llm_fail_reason = "LLM_ERROR"

        raw_text = "".join(raw_text_parts)
        if llm_failed and not raw_text:
            return GenerationResult(
                text="",
                raw_text="",
                stripped_sentences=0,
                refused=True,
                refuse_reason=llm_fail_reason,
                citations=citations,
            )

        # L3 出口校验
        grounding_result = check_grounding(raw_text, valid_ns)
        if grounding_result.refused:
            return GenerationResult(
                text="",
                raw_text=raw_text,
                stripped_sentences=grounding_result.stripped_sentences,
                refused=True,
                refuse_reason=grounding_result.refuse_reason,
                citations=citations,
            )

        return GenerationResult(
            text=grounding_result.text,
            raw_text=raw_text,
            stripped_sentences=grounding_result.stripped_sentences,
            refused=False,
            refuse_reason="",
            citations=citations,
            usage={"prompt_tokens": 0, "completion_tokens": len(raw_text)},
        )


def get_generation_service() -> GenerationService:
    return GenerationService()
