"""知识治理 —— 四维审查（M7 B7-4 / WU-M-15）。

四维判据（各有可判定判据）：
  · freshness（时效性）—— 纯规则：expire_at 过期 0 分 / effective_at 未到低分 /
    created_at 距今按 GOVERNANCE_FRESHNESS_TIERS 分档衰减。确定性输入不花 LLM token。
  · authority / consistency / completeness —— 一次 LLM 调用
    （governance_review 提示词，WU-M-16 注册，锁定区含 INV-M-15 三类禁止约束）。

不变量：
  · INV-M-10 审查独立任务 —— run_parse_job 主路径零调用；本模块的
    execute_governance_job / scan_and_enqueue 由 parse_job.py 的 WorkerSettings
    注册为独立 arq 任务 + cron 扫描。
  · INV-M-11 只写治理字段 —— 结论只落 documents 治理列 + governance_report，
    并同步 chunks.governance_weight；MUST NOT 触碰 content / filename 等原文。
  · INV-M-12 失败降级「未审查」—— 审查失败不写治理分（weight 保持 0），
    检索按基础分照常工作；达到 max_attempts 置 failed，由 rerun 手动重跑。
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from arq import Retry
from sqlalchemy import select, update

from app.config import decisions
from app.config.prompts import NO_ANSWER_TEXT, render_prompt
from app.database import SessionLocal
from app.infra import metrics, trace
from app.infra.arq_pool import get_arq_pool
from app.models import Chunk, Document, GovernanceJob
from app.services.llm import get_llm_service
from app.services.prompt_registry import resolve_prompt

logger = logging.getLogger(__name__)

# 四维中的 LLM 判定维度（freshness 走规则，不在其中）
_LLM_DIMS: tuple[str, ...] = ("authority", "consistency", "completeness")


class GovernanceError(Exception):
    """审查过程失败（LLM 输出契约违反 / 文档缺失等），触发任务重试。"""


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


def freshness_score(
    effective_at: datetime | None,
    expire_at: datetime | None,
    created_at: datetime | None,
    now: datetime,
) -> tuple[int, str]:
    """规则判定时效性，返回 (分数, 依据)。

    判定顺序（互斥）：已废止 > 未生效 > 按 created_at 分档。
    """
    if expire_at is not None and expire_at <= now:
        return 0, f"已过失效时间（expire_at={expire_at:%Y-%m-%d}），判定废止"
    if effective_at is not None and effective_at > now:
        return (
            decisions.GOVERNANCE_FRESHNESS_FUTURE_SCORE,
            f"尚未生效（effective_at={effective_at:%Y-%m-%d}）",
        )
    if created_at is None:
        return (
            decisions.GOVERNANCE_FRESHNESS_STALE_SCORE,
            "无创建时间，按最保守分档计",
        )
    age_days = (now - created_at).days
    for max_days, score in decisions.GOVERNANCE_FRESHNESS_TIERS:
        if age_days <= max_days:
            return score, f"创建至今 {age_days} 天（{max_days} 天内分档）"
    return (
        decisions.GOVERNANCE_FRESHNESS_STALE_SCORE,
        f"创建至今 {age_days} 天，超过最长分档（{decisions.GOVERNANCE_FRESHNESS_TIERS[-1][0]} 天）",
    )


def compose_weight(freshness: int, authority: int, consistency: int, completeness: int) -> int:
    """四维加权合成 governance_weight ∈ [-50, +50]（0 = 中性）。

    系数读 decisions.GOVERNANCE_DIM_WEIGHTS（口径真源，测试可 monkeypatch）。
    """
    scores = {
        "freshness": freshness,
        "authority": authority,
        "consistency": consistency,
        "completeness": completeness,
    }
    avg = sum(scores[name] * weight for name, weight in decisions.GOVERNANCE_DIM_WEIGHTS.items())
    return round(avg - 50)


def _normalize_llm_scores(parsed: dict) -> dict[str, dict[str, Any]]:
    """校验并规范化 LLM 三维输出；结构不符抛 GovernanceError（进任务重试）。"""
    dims: dict[str, dict[str, Any]] = {}
    for key in _LLM_DIMS:
        item = parsed.get(key)
        if not isinstance(item, dict) or "score" not in item:
            raise GovernanceError(f"LLM 输出缺少 {key} 维度或结构不符")
        try:
            score = int(item["score"])
        except (TypeError, ValueError) as exc:
            raise GovernanceError(f"LLM 输出 {key}.score 非整数: {item['score']!r}") from exc
        dims[key] = {
            "score": max(0, min(100, score)),
            "basis": str(item.get("basis", "")).strip()[:200],
        }
    return dims


async def review_document(document_id: uuid.UUID, tenant_id: uuid.UUID) -> dict:
    """对单文档执行四维审查并落库，返回 governance_report。

    分两段 session：读文档/chunks（提取标量）→ 审查 → 写治理字段。
    审查文本从 chunks 拼接（带 heading_path 前缀），超出上限截断。
    """
    now = datetime.now(timezone.utc)

    # ── 第一段：读文档与 chunks（提取标量，session 外使用）──
    async with SessionLocal() as session:
        doc = await session.get(Document, document_id)
        if doc is None:
            raise GovernanceError("document not found")
        doc_created_at = doc.created_at
        doc_effective_at = doc.effective_at
        doc_expire_at = doc.expire_at
        rows = (
            await session.execute(
                select(Chunk.content, Chunk.heading_path)
                .where(Chunk.document_id == document_id, Chunk.is_latest.is_(True))
                .order_by(Chunk.chunk_index)
            )
        ).all()

    # ── 审查（无 session；LLM 调用可能十几秒）──────────────
    parts = [f"[{heading}] {content}" if heading else content for content, heading in rows]
    review_text = "\n\n".join(parts)[: decisions.GOVERNANCE_PROMPT_MAX_CHARS]

    fresh_score, fresh_basis = freshness_score(doc_effective_at, doc_expire_at, doc_created_at, now)

    if not review_text.strip():
        # 无可审查内容：代码层短路（提示词锁定区第 4 条由代码执行），省一次无意义 LLM 调用
        dims: dict[str, dict[str, Any]] = {
            "authority": {"score": 0, "basis": NO_ANSWER_TEXT},
            "consistency": {"score": 0, "basis": NO_ANSWER_TEXT},
            "completeness": {"score": 0, "basis": NO_ANSWER_TEXT},
        }
    else:
        template = await resolve_prompt("governance_review", tenant_id)
        prompt = render_prompt(template, {"content": review_text})
        raw = await get_llm_service().chat([{"role": "user", "content": prompt}], temperature=0.1)
        parsed = _extract_json(raw)
        if parsed is None:
            raise GovernanceError(f"LLM 输出无 JSON: {raw[:200]}")
        dims = _normalize_llm_scores(parsed)

    dims["freshness"] = {"score": fresh_score, "basis": fresh_basis}
    weight = compose_weight(
        fresh_score,
        dims["authority"]["score"],
        dims["consistency"]["score"],
        dims["completeness"]["score"],
    )
    report: dict[str, Any] = {
        "dims": dims,
        "governance_weight": weight,
        "reviewed_at": now.isoformat(),
        "content_chars": len(review_text),
    }

    # ── 第二段：写治理字段（INV-M-11：只写治理列，不改原文）──
    async with SessionLocal() as session:
        doc = await session.get(Document, document_id)
        if doc is None:
            raise GovernanceError("document not found")
        doc.freshness_score = dims["freshness"]["score"]
        doc.authority_score = dims["authority"]["score"]
        doc.consistency_score = dims["consistency"]["score"]
        doc.completeness_score = dims["completeness"]["score"]
        doc.governance_report = report
        doc.governance_status = "done"
        # chunks.governance_weight 同步（检索期免 join documents）
        await session.execute(
            update(Chunk)
            .where(Chunk.document_id == document_id)
            .values(governance_weight=weight)
        )
        await session.commit()

    metrics.incr("governance.review.done")
    logger.info(
        "governance.review.done",
        extra={"document_id": str(document_id), "weight": weight},
    )
    return report


async def scan_and_enqueue(ctx: dict[str, Any]) -> int:
    """cron 扫描：pending 且解析完成的文档批量建 GovernanceJob + 入队。

    enqueue 在 commit 之前执行 —— 任一入队失败整体回滚，无 zombie job（口径同 _sync_one_file）。
    返回本轮建的任务数。
    """
    trace.set_trace_id(trace.new_trace_id())
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(Document.id, Document.tenant_id)
                .where(
                    Document.governance_status == "pending",
                    Document.status == "indexed",
                    Document.deleted_at.is_(None),
                )
                .order_by(Document.created_at)
                .limit(decisions.GOVERNANCE_SCAN_BATCH)
            )
        ).all()
        if not rows:
            return 0

        jobs: list[GovernanceJob] = []
        for doc_id, tenant_id in rows:
            job = GovernanceJob(tenant_id=tenant_id, document_id=doc_id)
            session.add(job)
            jobs.append(job)
        await session.flush()

        pool = await get_arq_pool()
        for job in jobs:
            await pool.enqueue_job(
                "execute_governance_job", str(job.id), trace.get_trace_id()
            )
        await session.commit()

    metrics.incr("governance.scan.enqueued", len(jobs))
    logger.info("governance.scan.enqueued", extra={"count": len(jobs)})
    return len(jobs)


async def execute_governance_job(
    ctx: dict[str, Any], job_id: str, trace_id: str | None = None
) -> str:
    """arq 任务入口。job_id 是 governance_jobs.id 字符串；trace_id 由入队方透传。

    分三段 session：锁任务 → 审查（网络调用，无 session）→ 写完成状态。
    失败重试 max_attempts 次后进死信（status=dead），文档治理状态置 failed
    （降级为「未审查」——INV-M-12：知识仍按基础分可检索）。
    """
    trace.set_trace_id(trace_id or trace.new_trace_id())
    gov_job_id = uuid.UUID(job_id)

    # ── 第一段：锁任务 + 置 running ───────────────────────
    async with SessionLocal() as session:
        job = await session.get(GovernanceJob, gov_job_id)
        if job is None:
            logger.warning("governance_job not found: %s", job_id)
            return "dead"
        if job.status in ("succeeded", "dead"):
            return job.status

        job.status = "running"
        job.started_at = datetime.now(timezone.utc)
        job.attempts += 1

        document = await session.get(Document, job.document_id)
        if document is None:
            job.status = "dead"
            job.last_error = "document not found"
            job.finished_at = datetime.now(timezone.utc)
            await session.commit()
            return "dead"
        if document.governance_status == "done":
            # 幂等：文档已被其它 job 审查完（如崩溃后 cron 重建了任务），直接成功
            job.status = "succeeded"
            job.finished_at = datetime.now(timezone.utc)
            await session.commit()
            return "succeeded"
        document.governance_status = "running"
        await session.commit()
        document_id = document.id
        tenant_id = document.tenant_id

    # ── 第二段：审查（无 session，LLM 调用可能十几秒）──────
    try:
        await review_document(document_id, tenant_id)
    except Exception as exc:
        # 审查异常 —— 开新 session 写回失败状态 + 重试计数
        async with SessionLocal() as fail_session:
            retry_job = await fail_session.get(GovernanceJob, gov_job_id)
            if retry_job is None:
                raise
            retry_job.last_error = f"{type(exc).__name__}: {exc}"
            if retry_job.attempts >= retry_job.max_attempts:
                retry_job.status = "dead"
                retry_job.finished_at = datetime.now(timezone.utc)
                doc = await fail_session.get(Document, retry_job.document_id)
                if doc is not None:
                    doc.governance_status = "failed"
                await fail_session.commit()
                metrics.incr("governance.review.dead")
                return "dead"
            retry_job.status = "queued"
            await fail_session.commit()
            raise Retry(defer=2 ** retry_job.attempts) from exc

    # ── 第三段：任务完成 ─────────────────────────────────
    async with SessionLocal() as session:
        job = await session.get(GovernanceJob, gov_job_id)
        job.status = "succeeded"
        job.finished_at = datetime.now(timezone.utc)
        await session.commit()
    return "succeeded"
