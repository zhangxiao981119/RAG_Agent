"""三级记忆层 —— 短期（会话）/ 长期（用户偏好）/ 永久（口径版本）。

职责：
  1. extract_facts — 从一轮 user/assistant 对话中提取关键事实，经**写入过滤器**后合并到画像
  2. compress_history — 历史消息过长时，用 LLM 把较早部分压缩成摘要（短期记忆）
  3. build_memory_prompt — 把画像拼成 system prompt 里的记忆段（**读取白名单**）
  4. purge_facts_by_trace_id — 权限变更 / 文档删除时按来源清除记忆项
  5. build_caliber_snapshot — 永久记忆：口径版本快照

★ 不做 fallback：提取/压缩失败静默跳过（不阻塞主流程）。

★ 长期记忆的合规约束（见《技术开发文档》D-21 / 4.20）：
  只放**用户偏好**，MUST NOT 放文档内容 —— 那是权限副本，
  会绕过四层守卫的实时判定（权限收回了，副本仍在）。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from app.config import decisions
from app.services.llm import LLMError, get_llm_service

logger = logging.getLogger(__name__)

# 提取事实的 prompt（要求输出 JSON）
_EXTRACT_FACTS_PROMPT = """你是一个信息抽取器。从下面这轮对话中提取对用户画像有价值的关键事实。

只提取稳定的、跨对话有意义的信息（如：用户身份、偏好、常用技术领域、公司背景、关键决策）。
不要提取临时上下文（如："让我看看文档"、"好的"）。
★ 不要提取文档正文内容、引用片段、或具体的资料原文 —— 只提炼「偏好」与「背景」。
★ 单条事实 MUST 简短（不超过 {max_chars} 字）。

输出严格 JSON：{"facts": ["事实1", "事实2", ...]}，数组为空则表示没有新事实。

对话：
用户：{user}
助手：{assistant}"""

# 压缩历史的 prompt
_COMPRESS_PROMPT = """请把下面的对话历史压缩成一段简洁摘要，保留关键信息点和讨论脉络。

摘要："""


# ── 记忆项契约（D-21 / 4.20）────────────────────────────


@dataclass
class MemoryItem:
    """长期记忆项。

    历史上 facts 是 `list[str]`；自 v1.1 起升级为带元数据的结构，
    以支持「按来源反查清除」与「置信度门槛」。旧数据由 normalize_facts 兼容。
    """

    content: str
    kind: str = "preference"          # preference / caliber / fact
    confidence: float = 1.0
    source_trace_id: str = ""

    def to_dict(self) -> dict:
        return {
            "content": self.content,
            "kind": self.kind,
            "confidence": round(float(self.confidence), 3),
            "source_trace_id": self.source_trace_id,
        }

    @classmethod
    def from_raw(cls, raw) -> "MemoryItem | None":
        """从 dict（新格式）或 str（旧格式）构造。无法解析返回 None。"""
        if isinstance(raw, str):
            text = raw.strip()
            return cls(content=text) if text else None
        if isinstance(raw, dict):
            text = str(raw.get("content") or "").strip()
            if not text:
                return None
            try:
                conf = float(raw.get("confidence", 1.0))
            except (TypeError, ValueError):
                conf = 1.0
            return cls(
                content=text,
                kind=str(raw.get("kind") or "preference"),
                confidence=conf,
                source_trace_id=str(raw.get("source_trace_id") or ""),
            )
        return None


def normalize_facts(raw_facts: list | None) -> list[MemoryItem]:
    """把历史 `list[str]` 与新 `list[dict]` 统一成 `list[MemoryItem]`。

    旧格式项无 source_trace_id —— 这是历史遗留，无法追溯来源，
    但至少要能读出来（不能因为升级格式就丢掉用户已有画像）。
    """
    out: list[MemoryItem] = []
    for raw in raw_facts or []:
        item = MemoryItem.from_raw(raw)
        if item:
            out.append(item)
    return out


_BLOCKED_RE = re.compile("|".join(decisions.BLOCKED_MEMORY_PATTERNS))


def blocked_reason(text: str) -> str | None:
    """返回命中拦截的原因；未命中返回 None。

    拦截规则（见 decisions.BLOCKED_MEMORY_PATTERNS）：
      too_short              过短，无信息量
      too_long_suspected_fragment  过长，疑似文档片段而非偏好
      blocked_pattern        命中黑名单（引用编号 / 拒答文案 / 密级 / 内部字段名）
    """
    s = (text or "").strip()
    if len(s) < decisions.MEMORY_MIN_FACT_CHARS:
        return "too_short"
    if len(s) > decisions.MEMORY_MAX_FACT_CHARS:
        return "too_long_suspected_fragment"
    if _BLOCKED_RE.search(s):
        return "blocked_pattern"
    return None


def filter_facts(
    candidates: list[str] | list[MemoryItem],
    *,
    trace_id: str = "",
    confidence: float = 1.0,
) -> tuple[list[MemoryItem], list[tuple[str, str]]]:
    """写入过滤器。返回 (通过的项, [(被拒文本, 原因)])。

    这是长期记忆的**合规底线**：宁可少记，不可错记。
    被拒项由调用方决定是否记合规事件。
    """
    passed: list[MemoryItem] = []
    rejected: list[tuple[str, str]] = []
    for raw in candidates:
        text = raw.content if isinstance(raw, MemoryItem) else str(raw)
        text = (text or "").strip()
        reason = blocked_reason(text)
        if reason:
            rejected.append((text[:80], reason))
            continue
        passed.append(
            MemoryItem(
                content=text,
                kind=raw.kind if isinstance(raw, MemoryItem) else "preference",
                confidence=confidence,
                source_trace_id=trace_id,
            )
        )
    return passed, rejected


def purge_facts_by_trace_id(
    memory: dict | None, trace_ids: set[str]
) -> tuple[dict, int]:
    """按来源 trace_id 清除记忆项。返回 (新 memory, 清除条数)。

    用途：权限被收回 / 文档被删除时，清除由那次检索沉淀下来的偏好项。
    ★ 旧格式（无 source_trace_id）无法匹配，不会被清除 —— 这也是
      为什么 MUST 要求新写入项带 source_trace_id。
    """
    base = dict(memory or {})
    if not trace_ids:
        return base, 0

    items = normalize_facts(base.get("facts"))
    kept = [
        it for it in items
        if not (it.source_trace_id and it.source_trace_id in trace_ids)
    ]
    removed = len(items) - len(kept)

    base["facts"] = [it.to_dict() for it in kept]
    if kept:
        base["profile"] = _truncate_profile(
            "用户背景：\n" + "\n".join(f"- {it.content}" for it in kept)
        )
    else:
        base["profile"] = ""
    return base, removed


def build_caliber_snapshot() -> dict:
    """永久记忆：口径版本快照。

    永久记忆**不含任何用户内容** —— 只记「系统按什么口径运行」，
    供申诉与问题回溯（「这条答案是哪个阈值/提示词版本产生的」）。
    """
    return {
        "contract_version": decisions.CONTRACT_VERSION,
        "relevance_threshold_vector": decisions.RELEVANCE_THRESHOLD_VECTOR,
        "relevance_threshold_rerank": decisions.RELEVANCE_THRESHOLD_RERANK,
        "rrf_k": decisions.RRF_K,
        "top_k_recall": decisions.TOP_K_RECALL,
        "top_k_rerank": decisions.TOP_K_RERANK,
        "chunk_target_tokens": decisions.CHUNK_TARGET_TOKENS,
        "cot_mode": decisions.COT_MODE,
        "grounding_check_enabled": decisions.GROUNDING_CHECK_ENABLED,
        "snapshot_at": datetime.now(UTC).isoformat(),
    }


def _merge_facts(
    existing: list[MemoryItem], new_facts: list[MemoryItem]
) -> list[MemoryItem]:
    """合并记忆项，去重 + 限长。

    去重按内容前缀比对（不做语义去重 —— 成本高且引入额外 LLM 调用）。
    新项追加在尾部；超出上限时保留最新的 MEMORY_MAX_FACTS 条。
    """
    merged: list[MemoryItem] = list(existing)
    for item in new_facts:
        text = item.content.strip()
        if not text:
            continue
        if not any(text[:20] in e.content or e.content[:20] in text for e in merged):
            merged.append(item)
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
    trace_id: str = "",
) -> dict:
    """从一轮对话提取事实，**经写入过滤器**后返回更新后的 memory dict。

    memory 结构：{"profile": "画像文本", "facts": [{content, kind, confidence, source_trace_id}, ...]}
    （旧格式 facts 为 list[str]，由 normalize_facts 兼容读出）

    trace_id 用于给新记忆项打来源标记 —— 权限变更时按它反查清除。

    ★ 无论 LLM 提取出什么，一律先过 filter_facts：
      只放行「偏好」类短句，拒绝文档片段、引用痕迹、拒答文案、密级标识。
      提取失败则静默跳过（不阻塞主流程）。
    """
    base = existing_memory or {}
    items = normalize_facts(base.get("facts"))

    # ── ③ 置信度门槛：低置信不升长期（这里由调用方传入，默认 1.0 表示最高置信）──
    confidence = 1.0
    if len(user_msg.strip()) < 10:
        # 用户输入过短，提炼出的"偏好"可靠度低 → 降置信，写短期即可
        confidence = 0.4

    candidates: list[str] = []
    try:
        llm = get_llm_service()
        prompt = _EXTRACT_FACTS_PROMPT.format(
            user=user_msg[:500],
            assistant_msg=assistant_msg[:1000],
            max_chars=decisions.MEMORY_MAX_FACT_CHARS,
        )
        raw = await llm.chat(
            [{"role": "user", "content": prompt}],
            temperature=0.1,
        )
        json_start = raw.find("{")
        json_end = raw.rfind("}") + 1
        if json_start >= 0 and json_end > json_start:
            parsed = json.loads(raw[json_start:json_end])
            candidates = [str(f) for f in parsed.get("facts", []) if str(f).strip()]
    except (LLMError, json.JSONDecodeError, Exception) as exc:
        logger.warning("画像提取失败，跳过: %s", exc)

    # ── ② 过滤器（合规底线）──
    passed, rejected = filter_facts(
        candidates, trace_id=trace_id, confidence=confidence
    )
    if rejected:
        logger.info(
            "记忆写入过滤器拦截 %d 条",
            len(rejected),
            extra={
                "rejected": [{"text": txt, "reason": r} for txt, r in rejected],
                "trace_id": trace_id,
            },
        )
        # 低置信的通过项也不升长期
    accepted = [
        it for it in passed
        if it.confidence >= decisions.MEMORY_WRITE_CONFIDENCE_THRESHOLD
    ]
    dropped_low_conf = len(passed) - len(accepted)

    # ── ④ 留痕：合并时保留 source_trace_id ──
    items = _merge_facts(items, accepted)

    profile = ""
    if items:
        profile = "用户背景：\n" + "\n".join(f"- {it.content}" for it in items)

    return {
        "profile": _truncate_profile(profile),
        "facts": [it.to_dict() for it in items],
        "extract_stats": {
            "candidates": len(candidates),
            "accepted": len(accepted),
            "rejected": len(rejected),
            "low_confidence_dropped": dropped_low_conf,
        },
    }


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
    """把长期记忆拼成注入 system 的记忆段。

    ★ 读取侧白名单（与写入过滤器构成双保险）：
      只召回 `kind ∈ MEMORY_LONG_TERM_KINDS` 的项。
      即便写入过滤器被绕过（模型换种说法把文档片段写成了"偏好"），
      读取侧仍会按 kind 过滤 —— 两道门任一失守都不至于泄漏。

    ★ 不直接使用 memory["profile"] 字段：那是写入时的拼接快照，
      无法按 kind 过滤。这里从 facts 重建，保证过滤一定生效。
    """
    if not memory:
        return ""
    items = normalize_facts(memory.get("facts"))
    allowed = [
        it for it in items
        if it.kind in decisions.MEMORY_LONG_TERM_KINDS
        and it.confidence >= decisions.MEMORY_WRITE_CONFIDENCE_THRESHOLD
    ]
    if not allowed:
        return ""
    body = "\n".join(f"- {it.content}" for it in allowed)
    return (
        f"\n\n<memory>\n用户背景：\n{body}\n</memory>\n"
        "（回答时可参考以上用户偏好）"
    )


# ── 上下文窗口管理（128K token 限制）──────────────────────────

def _estimate_tokens(text: str) -> int:
    """估算文本 token 数（中文保守按 2 字符/token）。

    不复用 quota.estimate_tokens 避免循环引用（quota.py import 本模块）。
    逻辑完全同 quota.estimate_tokens，是轻量工具函数。
    """
    if not text:
        return 0
    return max(1, len(text) // decisions.QUOTA_TOKEN_ESTIMATE_CHARS_PER_TOKEN)


def estimate_context_tokens(
    system_prompt: str,
    memory_prompt: str,
    history: list[dict],
    chunks_text: str,
    question: str,
) -> int:
    """估算单次请求 prompt 总 token 数。

    构成：system + memory + history + chunks(context) + user(question) + 预期输出
    与 quota._estimate_request_tokens 口径一致，但参数更完整（含 system_prompt）。
    """
    total = _estimate_tokens(system_prompt)
    total += _estimate_tokens(memory_prompt)
    for m in history:
        total += _estimate_tokens(m.get("content", ""))
    total += _estimate_tokens(chunks_text)
    total += _estimate_tokens(question)
    # 预期输出预留（与 quota 保持一致：1024 token）
    total += 1024
    return total


async def ensure_context_within_limit(
    system_prompt: str,
    memory_prompt: str,
    history: list[dict],
    chunks_text: str,
    question: str,
    chunks: list,
) -> tuple[list[dict], list]:
    """确保 context 在窗口上限内。超限则按 history 压缩 → chunks 裁剪顺序处理。

    策略（与 quota 四级降级对齐，但此处不降级 memory_prompt，
    因为 context 窗口限制是模型硬约束，必须保证 system prompt 完整）：
      1. history 太长 → 调 compress_history 压缩早期历史（保留最近轮数逐轮减少）
      2. 压缩后仍超限 → 裁剪 chunks（从尾部丢，保留 top-K_RERANK 不变）
      3. 到达极限仍超限 → 只保留 system prompt + 压缩 history + top-2 chunks

    返回 (最终 history, 最终 chunks)。原始参数不修改。
    """
    limit = decisions.CONTEXT_WINDOW_LIMIT_TOKENS - decisions.CONTEXT_OUTPUT_RESERVE_TOKENS
    current_history = list(history)
    current_chunks = list(chunks)

    def _make_chunks_text(cs: list) -> str:
        """从 chunks 列表拼 context 文本（用于 token 估算）。"""
        try:
            # 运行时导入避免循环引用（generate 不依赖 memory，memory 在运行时才需要 generate 的工具函数）
            from app.services.generate import _build_context
            return _build_context(cs)
        except Exception:
            return "\n\n".join(f"[{i+1}] {c.content}" for i, c in enumerate(cs))

    # 第一轮：逐轮压缩 history（从保留 6 条 → 4 条 → 2 条）
    for keep in (decisions.MAX_HISTORY_TURNS, 4, 2, 0):
        if len(current_history) <= keep:
            break
        compressed = await compress_history(current_history, keep_recent=keep)
        est = estimate_context_tokens(
            system_prompt, memory_prompt, compressed,
            _make_chunks_text(current_chunks), question,
        )
        if est <= limit:
            logger.info("context 压缩后达标: history→%d 条, est=%d tokens", len(compressed), est)
            return compressed, current_chunks
        current_history = compressed

    # 第二轮：裁剪 chunks（从尾部开始丢，保留前 N 个）
    # 逐次减 2 个直到达标或只剩 2 个
    while len(current_chunks) > 2:
        current_chunks = current_chunks[:-2]
        est = estimate_context_tokens(
            system_prompt, memory_prompt, current_history,
            _make_chunks_text(current_chunks), question,
        )
        if est <= limit:
            logger.info("context 裁剪 chunks 后达标: chunks→%d 条, est=%d tokens", len(current_chunks), est)
            return current_history, current_chunks

    # 极限：只剩压缩 history + top-2 chunks
    est = estimate_context_tokens(
        system_prompt, memory_prompt, current_history,
        _make_chunks_text(current_chunks), question,
    )
    logger.warning("context 到达极限: history=%d, chunks=%d, est=%d (limit=%d)",
                   len(current_history), len(current_chunks), est, limit)
    return current_history, current_chunks
