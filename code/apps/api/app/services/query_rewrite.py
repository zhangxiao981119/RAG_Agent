"""Query 改写 —— 检索前先打分再决定是否改写。

设计要点：
  · 一次 LLM 调用完成「评分 → 决策 → 可选改写」，省掉两次调用的首字延迟
  · 百分制打分（0-100），≥ 阈值直接用原始 query，< 阈值才改写
  · 改写后的 query 只用于 retrieval（HyDE 模式），原始 question 仍用于 generation
  · 超时 / 异常 / JSON 解析失败 → 静默回原始 question，不阻塞主流程

评分维度（百分制）：
  · 明确度（clarity）：问题自包含、无歧义、实体完整
  · 检索友好度（retrieval_friendliness）：关键词/术语/实体名是否充分
  · 指代依赖（reference_dependency）：是否依赖历史上下文才能理解（越高越需要改写）
  综合分 = clarity 和 retrieval_friendliness 加权（reference_dependency 作减分项）
"""
from __future__ import annotations

import asyncio
import json
import logging

from app.config import decisions
from app.services.llm import LLMError, get_llm_service

logger = logging.getLogger(__name__)

# 评分 + 改写 prompt：一次调用完成评分和可选改写
# 输出 JSON 包含 score + need_rewrite + (可选) query/intent
_SCORE_AND_REWRITE_PROMPT = """你是一个检索优化器。请对用户问题进行打分，判断是否需要改写以提升检索效果。

评分标准（百分制，0-100）：
  90-100 分：问题自包含、实体完整、关键词充分、无歧义（如"飞书知识库支持哪些文件格式？"）
  70-89 分：有小瑕疵但基本可检索（如"怎么上传文档？"——缺少知识库限定词）
  50-69 分：需要结合历史才能理解（如"它的核心优势是什么？"——"它"指代不明）
  0-49 分：口语化严重或过于模糊（如"那个东西怎么弄？"——完全无法独立检索）

打分维度：
  - 明确度（clarity）：问题是否自包含、实体/领域是否清晰
  - 检索友好度（retrieval_friendliness）：是否含关键词/术语/实体名
  - 指代依赖度（reference_dependency）：是否依赖历史上下文才能理解（越高越需改写）
  综合分 = max(0, clarity + retrieval_friendliness - reference_dependency // 2)，clamp 到 [0, 100]

改写规则（仅 need_rewrite=true 时执行）：
  1. 补全指代 —— 用历史上下文把"它/那个"替换成完整实体名
  2. 提取关键词 —— 口语化转领域术语（"怎么弄"→"上传流程"）
  3. 保留核心意图 —— 改写后 MUST 与原问题等价，MUST NOT 引入新信息
  4. 简洁聚焦 —— 去掉语气词，让向量语义更明确

输出严格 JSON（不要 Markdown 代码围栏）：
{{"score": 85, "need_rewrite": true, "query": "改写后的检索友好问题", "intent": "意图标签"}}

对话历史（用于指代消解）：
{history}

当前问题：{question}"""


def _estimate_tokens(text: str) -> int:
    """估算文本 token 数（中文 2 字符 ≈ 1 token）。"""
    if not text:
        return 0
    return max(1, len(text) // 2)


def _format_history(history: list[dict]) -> str:
    """把历史消息格式化成 prompt 可用的文本，按 token budget 从最新往前截断。

    DB 层已放开到 500 行，这里不再按条数硬限制。
    策略：从 history 末尾（最新的）开始逐条累积 token，
    超过 QUERY_REWRITE_HISTORY_BUDGET_TOKENS 就停止，保证最新的对话优先进入改写 prompt。
    """
    if not history:
        return "（无历史对话）"

    # 从最新往前累积，保证最新对话一定在 budget 内
    budget = decisions.QUERY_REWRITE_HISTORY_BUDGET_TOKENS
    accumulated: list[str] = []
    total_tokens = 0

    for m in reversed(history):
        role = "用户" if m["role"] == "user" else "助手"
        content = m.get("content", "")
        # 每条最多 500 字，避免单条过长挤占 budget
        if len(content) > 500:
            content = content[:500] + "..."
        line = f"{role}: {content}"
        line_tokens = _estimate_tokens(line)

        # 加这条会超 budget → 停止（这条更早，丢掉）
        if total_tokens + line_tokens > budget and accumulated:
            break

        accumulated.append(line)
        total_tokens += line_tokens

    # reversed 后 accumulated 是从新到旧的，再反回来成时间正序
    accumulated.reverse()
    return "\n".join(accumulated)


def _extract_json(raw: str) -> dict | None:
    """从 LLM 输出里提取 JSON 对象（容错 Markdown 包裹）。"""
    json_start = raw.find("{")
    json_end = raw.rfind("}") + 1
    if json_start < 0 or json_end <= json_start:
        return None
    try:
        return json.loads(raw[json_start:json_end])
    except json.JSONDecodeError:
        return None


def _compute_binary_score(question: str, has_history: bool) -> int | None:
    """当 LLM 打分失败时，用规则做快速兜底评分（不调 LLM，同步返回）。

    规则是"宁可多改写也不误改"：
      · 过短问题（≤5字）→ 50 分（大概率需要改写）
      · 含指代代词（它/那个/这个/上面说的/那个东西）且有历史 → 55 分
      · 纯口语化疑问词（怎么弄/咋办/咋搞）→ 60 分
      · 其它 → 直接返回 None（表示不做兜底，回原始问题）
    """
    q = question.strip()
    if len(q) <= 5:
        return 50
    if has_history and any(p in q for p in ("它", "那个", "这个", "上面说的", "那东西")):
        return 55
    if any(p in q for p in ("怎么弄", "咋办", "咋搞", "咋弄")):
        return 60
    return None


async def rewrite_query(
    question: str,
    history: list[dict] | None = None,
) -> tuple[str, str, int]:
    """对用户问题先打分，再决定是否改写。

    返回 (最终 query, 状态标签, 打分)。
    最终 query 可能是原始问题（score >= 阈值 或 不需要改写 或 LLM 失败），
    也可能是改写后的检索友好版本。

    改写后的 query 只用于 retrieval，原始 question 仍用于 generation。
    """
    if not decisions.QUERY_REWRITE_ENABLED:
        return question, "disabled", 100  # 关闭时假装满分，跳过改写

    history_text = _format_history(history or [])
    has_history = bool(history)
    prompt = _SCORE_AND_REWRITE_PROMPT.format(history=history_text, question=question)

    try:
        llm = get_llm_service()
        raw = await asyncio.wait_for(
            llm.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.1,
            ),
            timeout=decisions.QUERY_REWRITE_TIMEOUT_SECONDS,
        )
        parsed = _extract_json(raw)
        if parsed is None:
            logger.info("Query 评分改写: LLM 输出无 JSON，尝试规则兜底")
            rule_score = _compute_binary_score(question, has_history)
            if rule_score is not None and rule_score < decisions.QUERY_REWRITE_SCORE_THRESHOLD:
                logger.info("Query 评分改写: 规则兜底 score=%d < 阈值=%d，但 LLM 输出无改写内容，跳过",
                            rule_score, decisions.QUERY_REWRITE_SCORE_THRESHOLD)
            return question, "parse_error", rule_score if rule_score is not None else 100

        score_raw = parsed.get("score", 100)
        try:
            score = max(0, min(100, int(score_raw)))
        except (ValueError, TypeError):
            score = 100
        need_rewrite = bool(parsed.get("need_rewrite", False))

        # 分数已够高 → 直接用原始 query，不改写
        if score >= decisions.QUERY_REWRITE_SCORE_THRESHOLD:
            logger.info("Query 评分改写: score=%d >= 阈值=%d，跳过改写",
                        score, decisions.QUERY_REWRITE_SCORE_THRESHOLD)
            return question, "skip_high_score", score

        # 分数低但 LLM 判断不需要改写 → 信任 score（分低就该改写）
        # 或者 need_rewrite=true → 都走改写分支
        rewritten = str(parsed.get("query", "")).strip()
        intent = str(parsed.get("intent", "unknown")).strip()[:20]

        if rewritten and rewritten != question:
            logger.info("Query 评分改写: score=%d < 阈值=%d，'%s' → '%s' (intent=%s)",
                        score, decisions.QUERY_REWRITE_SCORE_THRESHOLD,
                        question[:50], rewritten[:50], intent)
            return rewritten, "rewritten", score

        # 分低但改写结果和原问题一样 → 记录日志，还是用原问题
        logger.info("Query 评分改写: score=%d < 阈值=%d，但改写结果未变化，跳过",
                    score, decisions.QUERY_REWRITE_SCORE_THRESHOLD)
        return question, "rewrite_no_change", score

    except asyncio.TimeoutError:
        logger.info("Query 评分改写超时（%.1fs），回退原始问题", decisions.QUERY_REWRITE_TIMEOUT_SECONDS)
        return question, "timeout", 100
    except (LLMError, json.JSONDecodeError) as exc:
        logger.warning("Query 评分改写 LLM 失败: %s，回退原始问题", exc)
        return question, "llm_error", 100
    except Exception as exc:
        logger.warning("Query 评分改写未预期错误: %s，回退原始问题", exc)
        return question, "error", 100
