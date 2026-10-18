"""检索侧链式推理 —— 问题拆解 / 多轮检索编排 / 证据合并。

设计（方案定稿）：
  · 只做「拆解 → 多轮检索 → 证据合并」，不做 HyDE / IRCoT 迭代 / 多跳图谱
  · 拆解失败（超时 / 异常 / JSON 非法）→ 降级单趟检索并打点，不阻断主流程
  · 多轮 MUST 串行复用同一 AsyncSession（SQLAlchemy AsyncSession 非并发安全）
  · 复杂度信号复用 Query 改写分（rewrite_score < 阈值才拆解，见 should_use_chain）
  · 合并后条数上限仍是 TOP_K_RERANK，32K 窗口估算 / 配额降级 / citations 编号零改动

★ 跨轮融合（_build_rrf_scores）只作**排序键**：RetrievedChunk.final_score 保留各轮
  原值取 max —— MUST NOT 把 RRF 分写回 final_score。RRF 分与已标定的
  RELEVANCE_THRESHOLD_* 口径不同量纲，写回会让阈值判定与 eval 基线同时失效
  （与既有注释「治理加权 MUST NOT 进 RRF」同构）。
"""
from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterable

from app.config import decisions
from app.config.prompts import render_prompt
from app.services import prompt_registry as prompt_registry_service
from app.services.llm import get_llm_service
from app.services.retrieve.base import RetrievalResult, RetrievedChunk


class ChainDecomposeError(Exception):
    """拆解 LLM 输出不可用（超时 / 异常 / JSON 非法 / 结构缺失）。

    调用方据此降级为单趟检索。★ 与「拆解后只有一个子问题」区分开：后者是拆解
    判定问题已足够聚焦（正常态），前者是功能不可用（要打降级指标）。
    """


def _extract_json(raw: str) -> dict | None:
    """从 LLM 输出里提取 JSON 对象（容错 Markdown 包裹），失败返回 None。"""
    json_start = raw.find("{")
    json_end = raw.rfind("}") + 1
    if json_start < 0 or json_end <= json_start:
        return None
    try:
        return json.loads(raw[json_start:json_end])
    except json.JSONDecodeError:
        return None


def _normalize(text: str) -> str:
    """折叠空白，得到单行子问题文本。"""
    return " ".join(text.split())


def normalize_subquestions(candidates: Iterable[str], original: str) -> list[str]:
    """清洗拆解结果：去空白 / 长度过滤 / 去重 / 上限截断，原始问题恒在首位。

    规则（参数全部取自 decisions）：
      · 原始问题不受 CHAIN_SUBQUESTION_MAX_CHARS 约束（它是既定输入，截断会变语义）
      · 拆解产出的子问题为空 / 超长 → 丢弃；与已保留项重复（含与原始问题相同）→ 丢弃
      · 最终上限 CHAIN_MAX_SUBQUESTIONS（含原始问题）
    返回列表；至少含 original（拆解产出全不可用时退化为单趟）。
    """
    kept: list[str] = [original]
    seen: set[str] = {original}
    for cand in candidates:
        if len(kept) >= decisions.CHAIN_MAX_SUBQUESTIONS:
            break
        text = _normalize(str(cand))
        if not text or len(text) > decisions.CHAIN_SUBQUESTION_MAX_CHARS:
            continue
        if text in seen:
            continue
        seen.add(text)
        kept.append(text)
    return kept


async def decompose_question(
    question: str,
    tenant_id: uuid.UUID | None = None,
) -> list[str]:
    """调 LLM 把问题拆成子问题列表；不可用一律抛 ChainDecomposeError。

    调用方（RetrievalService.retrieve_chain）捕获后降级单趟检索 —— 拆解是增强项，
    不可用不得阻断问答主链路。
    """
    template = await prompt_registry_service.resolve_prompt("qa_decompose", tenant_id)
    prompt = render_prompt(template, {
        "question": question,
        "max": decisions.CHAIN_MAX_SUBQUESTIONS,
        "max_chars": decisions.CHAIN_SUBQUESTION_MAX_CHARS,
    })
    try:
        llm = get_llm_service()
        raw = await asyncio.wait_for(
            llm.chat([{"role": "user", "content": prompt}], temperature=0.1),
            timeout=decisions.CHAIN_DECOMPOSE_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        # 超时 / 网络 / LLM 异常统一归入「拆解不可用」，由调用方降级
        raise ChainDecomposeError(f"拆解 LLM 调用失败: {exc}") from exc

    parsed = _extract_json(raw)
    if parsed is None:
        raise ChainDecomposeError("拆解 LLM 输出无 JSON")
    subs = parsed.get("subquestions")
    if not isinstance(subs, list):
        raise ChainDecomposeError("拆解 LLM 输出缺少 subquestions 列表")
    return normalize_subquestions([str(s) for s in subs], question)


def merge_rounds(rounds: list[RetrievalResult]) -> RetrievalResult:
    """跨轮证据合并（纯函数：无 IO / 无打点，便于单测）。

    口径：
      · 候选集 = 各轮 chunks 按 chunk_id 去重（首轮出现者保留字段）
      · 排序键 = 对各轮「轮内顺序」跑既有 _build_rrf_scores（k=RRF_K），
        跨轮命中越多排越前 —— 与单趟内多路召回同款融合，不新造算法
      · final_score = 各轮原值取 max（MUST NOT 写回 RRF 分，见模块 docstring）
      · 合并后条数上限 = TOP_K_RERANK（与单趟一致，下游窗口估算/配额/编号零改动）
      · 闸门：合并集 max(final_score) < active_threshold(use_rerank) → 拒答
      · use_rerank 任一轮为 False → 整体按降级口径（vector 阈值）判定（保守）
      · table_recall_chunks 按 chunk_id 去重合并，仍排命中片之后、分数为 0
      · stage_ms / path_stats 按轮累加；chain_decompose 由调用方注入，不在此处
    """
    # 延迟导入：_build_rrf_scores 定义在包 __init__，而本模块被该 __init__ 导入，
    # 顶层导入会命中「父包尚未执行完」的部分初始化状态。
    from app.services.retrieve import _build_rrf_scores

    stage_ms: dict[str, int] = {}
    path_stats: dict[str, int] = {}
    merged: dict[uuid.UUID, RetrievedChunk] = {}
    recall: dict[uuid.UUID, RetrievedChunk] = {}
    rank_lists: list[list[uuid.UUID]] = []
    use_rerank = True

    for result in rounds:
        for key, ms in result.stage_ms.items():
            stage_ms[key] = stage_ms.get(key, 0) + ms
        for key, val in result.path_stats.items():
            path_stats[key] = path_stats.get(key, 0) + val
        rank_lists.append([c.chunk_id for c in result.chunks])
        for chunk in result.chunks:
            prev = merged.get(chunk.chunk_id)
            if prev is None:
                merged[chunk.chunk_id] = chunk
            elif chunk.final_score > prev.final_score:
                # 跨轮同一 chunk：保留先到者字段，只把 final_score 抬到各轮最大值
                prev.final_score = chunk.final_score
        for chunk in result.table_recall_chunks:
            recall.setdefault(chunk.chunk_id, chunk)
        if not result.use_rerank:
            use_rerank = False

    score_source = "rerank" if use_rerank else "vector"

    if not merged:
        return RetrievalResult(
            refused=True, refuse_reason="NO_RELEVANT_CONTENT",
            stage_ms=stage_ms, score_source=score_source, use_rerank=use_rerank,
        )

    threshold = decisions.active_threshold(use_rerank)
    if max(c.final_score for c in merged.values()) < threshold:
        return RetrievalResult(
            refused=True, refuse_reason="NO_RELEVANT_CONTENT",
            stage_ms=stage_ms, score_source=score_source, use_rerank=use_rerank,
        )

    rrf_scores = _build_rrf_scores(*rank_lists)
    ordered = sorted(
        merged.values(), key=lambda c: rrf_scores.get(c.chunk_id, 0.0), reverse=True
    )
    return RetrievalResult(
        chunks=ordered[: decisions.TOP_K_RERANK],
        stage_ms=stage_ms,
        score_source=score_source,
        use_rerank=use_rerank,
        path_stats=path_stats,
        table_recall_chunks=list(recall.values()),
    )
