"""用户画像 + 上下文压缩 + 缓存重要信息。

职责：
  1. extract_facts — 从一轮 user/assistant 对话中提取关键事实，合并到用户画像
  2. compress_history — 历史消息过长时，用 LLM 把较早部分压缩成摘要
  3. build_memory_prompt — 把画像拼成 system prompt 里的记忆段

★ 不做 fallback：提取/压缩失败静默跳过（不阻塞主流程）。
"""
from __future__ import annotations

import json
import logging

from app.config import decisions
from app.services.llm import LLMError, get_llm_service

logger = logging.getLogger(__name__)

# 提取事实的 prompt（要求输出 JSON）
_EXTRACT_FACTS_PROMPT = """你是一个信息抽取器。从下面这轮对话中提取对用户画像有价值的关键事实。

只提取稳定的、跨对话有意义的信息（如：用户身份、偏好、常用技术领域、公司背景、关键决策）。
不要提取临时上下文（如："让我看看文档"、"好的"）。

输出严格 JSON：{"facts": ["事实1", "事实2", ...]}，数组为空则表示没有新事实。

对话：
用户：{user}
助手：{assistant}"""

# 压缩历史的 prompt
_COMPRESS_PROMPT = """请把下面的对话历史压缩成一段简洁摘要，保留关键信息点和讨论脉络。

摘要："""


def _merge_facts(existing: list[str], new_facts: list[str]) -> list[str]:
    """合并事实列表，去重 + 限长。"""
    merged: list[str] = list(existing)
    for f in new_facts:
        f = f.strip()
        if not f:
            continue
        # 简单去重：已存在相似前缀的跳过
        if not any(f[:20] in e or e[:20] in f for e in merged):
            merged.append(f)
    # 限长
    if len(merged) > decisions.MEMORY_MAX_FACTS:
        merged = merged[-decisions.MEMORY_MAX_FACTS:]
    return merged


def _truncate_profile(profile: str) -> str:
    """画像文本截断，保证不超限。"""
    if len(profile) <= decisions.MEMORY_MAX_PROFILE_CHARS:
        return profile
    return profile[:decisions.MEMORY_MAX_PROFILE_CHARS] + "..."


async def extract_facts(
    user_msg: str,
    assistant_msg: str,
    existing_memory: dict | None = None,
) -> dict:
    """从一轮对话提取事实，返回更新后的 memory dict。

    输入 memory 结构：{"profile": "画像文本", "facts": ["事实1", ...]}
    返回同结构。失败返回原始 memory（或空结构）。
    """
    base = existing_memory or {}
    facts: list[str] = list(base.get("facts", []))

    try:
        llm = get_llm_service()
        prompt = _EXTRACT_FACTS_PROMPT.format(
            user=user_msg[:500], assistant_msg=assistant_msg[:1000]
        )
        raw = await llm.chat(
            [{"role": "user", "content": prompt}],
            temperature=0.1,
        )
        # 尝试解析 JSON
        json_start = raw.find("{")
        json_end = raw.rfind("}") + 1
        if json_start >= 0 and json_end > json_start:
            parsed = json.loads(raw[json_start:json_end])
            new_facts = parsed.get("facts", [])
            facts = _merge_facts(facts, [str(f) for f in new_facts if str(f).strip()])
    except (LLMError, json.JSONDecodeError, Exception) as exc:
        logger.warning("画像提取失败，跳过: %s", exc)

    # 用 facts 拼接 profile 文本
    profile = ""
    if facts:
        profile = "用户背景：\n" + "\n".join(f"- {f}" for f in facts)

    return {"profile": _truncate_profile(profile), "facts": facts}


async def compress_history(
    history: list[dict],
    keep_recent: int,
) -> list[dict]:
    """把较早的历史压缩成一条 system 摘要，保留最近 keep_recent 条完整消息。

    history 必须按时间正序。
    """
    if len(history) <= keep_recent:
        return history

    early = history[:-keep_recent]
    recent = history[-keep_recent:]

    # 把早期历史拼成文本
    dialogue_text = "\n".join(
        f"{'用户' if m['role'] == 'user' else '助手'}: {m['content'][:300]}"
        for m in early
    )

    try:
        llm = get_llm_service()
        summary = await llm.chat(
            [
                {"role": "system", "content": "你是对话摘要器。只输出摘要文本。"},
                {
                    "role": "user",
                    "content": f"{_COMPRESS_PROMPT}\n\n历史：\n{dialogue_text}",
                },
            ],
            temperature=0.1,
        )
        summary = summary.strip()
        if summary:
            return [{"role": "system", "content": f"以下是之前对话的摘要：\n{summary}"}] + recent
    except LLMError as exc:
        logger.warning("上下文压缩失败，跳过: %s", exc)

    # 压缩失败就原样返回（会超限，但不阻塞）
    return history


def build_memory_prompt(memory: dict | None) -> str:
    """把用户画像拼成注入 system 的记忆段。画像为空则返回空字符串。"""
    if not memory:
        return ""
    profile = memory.get("profile", "")
    if not profile:
        return ""
    return f"\n\n<memory>\n{profile}\n</memory>\n（回答时可参考以上用户背景）"
