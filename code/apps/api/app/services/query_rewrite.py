"""Query 改写 —— 检索前对用户问题做意图识别 + 改写。

设计要点：
  · 改写后的 query 只用于 retrieval（向量/关键词召回），原始 question 仍用于
    generation（prompt 组装里的问题字段保持原样，保证回答与用户原始意图对齐）
    —— 这是标准 HyDE / query reformulation 模式
  · 处理三类典型口语化问题：
      1. 指代消解 —— "它的核心优势" → "RAG Agent 的核心优势"（用历史上下文消解）
      2. 模糊意图 —— "那个怎么弄" → "飞书知识库文档怎么上传"
      3. 多义词 —— 结合领域上下文消歧
  · 超时 / LLM 异常 / JSON 解析失败 → 直接返回原始 question，静默跳过改写，
    不阻塞主流程（改写是 RAG 增强而非必须）

★ 不做 fallback 模板回答，但"改写失败回原始问题"是消除阻塞场景，不是兜底分支。
"""
from __future__ import annotations

import asyncio
import json
import logging

from app.config import decisions
from app.services.llm import LLMError, get_llm_service

logger = logging.getLogger(__name__)

# 改写 prompt：要求输出纯 JSON（避免 Markdown 代码围栏包裹）
# 历史上下文只取最近 3 轮（6 条），足够消解指代又不太长
_REWRITE_PROMPT = """你是一个检索优化器。请分析用户问题，结合对话历史，输出一个更适合向量检索和关键词检索的改写版本。

改写原则：
1. 补全指代 —— 用历史上下文把"它/那个/上面说的"替换成完整实体名
2. 提取关键词 —— 把口语化表达转换成领域术语（如"怎么弄"→"上传流程"）
3. 保留核心意图 —— 改写后的 query MUST 与用户原始意图等价，MUST NOT 引入新信息
4. 简洁聚焦 —— 去掉语气词和冗余修饰，让向量语义更明确

输出严格 JSON：{{"query": "改写后的检索友好问题", "intent": "意图标签（1-3个词）"}}

对话历史（按时间正序，用于指代消解）：
{history}

当前问题：{question}"""


def _format_history_for_rewrite(history: list[dict]) -> str:
    """把历史消息格式化成 prompt 可用的文本，只取最近 3 轮。"""
    if not history:
        return "（无历史对话）"
    recent = history[-6:]  # 最多最近 3 轮（user + assistant）
    lines = []
    for m in recent:
        role = "用户" if m["role"] == "user" else "助手"
        content = m.get("content", "")[:200]  # 每条截断 200 字防止过长
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


async def rewrite_query(
    question: str,
    history: list[dict] | None = None,
) -> tuple[str, str]:
    """对用户问题做意图识别 + 改写。

    返回 (改写后的 query, 意图标签)。
    改写失败时返回 (原始 question, "unknown")。

    改写后的 query 只用于 retrieval，原始 question 仍用于 generation。
    """
    if not decisions.QUERY_REWRITE_ENABLED:
        return question, "disabled"

    history_text = _format_history_for_rewrite(history or [])
    prompt = _REWRITE_PROMPT.format(history=history_text, question=question)

    try:
        llm = get_llm_service()
        # 用 asyncio.wait_for 做超时保护（llm.chat 内部没单独超时参数）
        raw = await asyncio.wait_for(
            llm.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.1,
            ),
            timeout=decisions.QUERY_REWRITE_TIMEOUT_SECONDS,
        )
        # 解析 JSON：容错处理 LLM 可能输出 Markdown 包裹
        json_start = raw.find("{")
        json_end = raw.rfind("}") + 1
        if json_start < 0 or json_end <= json_start:
            logger.info("Query 改写输出无 JSON，回退原始问题")
            return question, "parse_error"
        parsed = json.loads(raw[json_start:json_end])
        rewritten = str(parsed.get("query", "")).strip()
        intent = str(parsed.get("intent", "unknown")).strip()[:20]
        if rewritten and rewritten != question:
            logger.info("Query 改写: '%s' → '%s' (intent=%s)", question[:50], rewritten[:50], intent)
            return rewritten, intent
        # 改写结果和原问题一样 → 直接用原问题
        return question, intent or "same"
    except asyncio.TimeoutError:
        logger.info("Query 改写超时（%.1fs），回退原始问题", decisions.QUERY_REWRITE_TIMEOUT_SECONDS)
        return question, "timeout"
    except (LLMError, json.JSONDecodeError) as exc:
        logger.warning("Query 改写失败: %s，回退原始问题", exc)
        return question, "llm_error"
    except Exception as exc:
        logger.warning("Query 改写未预期错误: %s，回退原始问题", exc)
        return question, "error"
