"""评估门禁服务 —— 手册 §7.3 / M6 G5。

遍历 eval_cases 表，对每条 case 跑 retrieve + generate，
检查可答 case 是否回答了、不可答 case 是否拒答了。
"""
from __future__ import annotations

import hashlib
import logging
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EvalCase, EvalRun
from app.services.generate import get_generation_service
from app.services.retrieve import RetrievalService

logger = logging.getLogger(__name__)


@dataclass
class EvalCaseResult:
    """单条评估结果。"""
    question: str
    expected_answerable: bool
    # 实际是否拒答
    actual_refused: bool
    # 是否通过（可答没拒答 = 通过；不可答拒答了 = 通过）
    passed: bool
    # 检索到的 chunk 数
    chunks_count: int
    # 拒答原因（如果拒答）
    refuse_reason: str | None
    # 耗时（毫秒）
    elapsed_ms: int


@dataclass
class EvalReport:
    """评估报告。"""
    total: int = 0
    passed: int = 0
    failed: int = 0
    # 拒答率（不可答 case 中正确拒答的比例）
    refuse_accuracy: float = 0.0
    # 回答率（可答 case 中正确回答的比例）
    answer_accuracy: float = 0.0
    # 总耗时（秒）
    total_seconds: float = 0.0
    # 逐条结果
    cases: list[EvalCaseResult] = field(default_factory=list)


async def run_eval(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    subjects: list[str],
    clearance: int,
) -> EvalReport:
    """跑全量 eval_cases，返回评估报告。

    subjects / clearance MUST 由调用方（admin 接口）从 CurrentUser 传入 ——
    与 chat.py 走同一份主体解析结果。此前这里硬编码 `["role:admin"]`，
    漏掉了 `public`，被 G4 闸门（`acl_tags && user_subjects`）全量过滤，
    导致公开库文档一条都召不回、评估恒为「可答 0% / 拒答 100%」。
    """
    rows = (
        await session.execute(
            select(EvalCase)
            .where(EvalCase.tenant_id == tenant_id)
            .order_by(EvalCase.created_at)
        )
    ).scalars().all()

    report = EvalReport(total=len(rows))
    if not rows:
        return report

    retrieval = RetrievalService()
    generation = get_generation_service()
    t0 = time.monotonic()

    # 评估用的权限口径来自调用方传入的 admin 主体（含 public），
    # kb_id 来自 eval_case
    answerable_total = 0
    answerable_passed = 0
    refuse_total = 0
    refuse_passed = 0

    for case in rows:
        kb_ids = [case.kb_id] if case.kb_id else []
        case_t0 = time.monotonic()

        # 检索
        result = await retrieval.retrieve(
            session, case.question, tenant_id,
            authorized_kb_ids=kb_ids,
            clearance=clearance,
            user_subjects=subjects,
        )

        # 如果检索阶段就拒答
        if result.refused:
            elapsed_ms = int((time.monotonic() - case_t0) * 1000)
            cr = EvalCaseResult(
                question=case.question,
                expected_answerable=case.expected_answerable,
                actual_refused=True,
                passed=not case.expected_answerable,
                chunks_count=0,
                refuse_reason=result.refuse_reason,
                elapsed_ms=elapsed_ms,
            )
            report.cases.append(cr)
            if cr.passed:
                report.passed += 1
                if not case.expected_answerable:
                    refuse_passed += 1
            else:
                report.failed += 1
            if case.expected_answerable:
                answerable_total += 1
            else:
                refuse_total += 1
            continue

        # 生成
        gen_result = await generation.generate(
            case.question, result.chunks,
            history=[], memory_prompt="",
        )

        elapsed_ms = int((time.monotonic() - case_t0) * 1000)
        actual_refused = gen_result.refused

        cr = EvalCaseResult(
            question=case.question,
            expected_answerable=case.expected_answerable,
            actual_refused=actual_refused,
            passed=(not actual_refused) if case.expected_answerable else actual_refused,
            chunks_count=len(result.chunks),
            refuse_reason=gen_result.refuse_reason if actual_refused else None,
            elapsed_ms=elapsed_ms,
        )
        report.cases.append(cr)

        if cr.passed:
            report.passed += 1
            if case.expected_answerable:
                answerable_passed += 1
            else:
                refuse_passed += 1
        else:
            report.failed += 1

        if case.expected_answerable:
            answerable_total += 1
        else:
            refuse_total += 1

    report.total_seconds = round(time.monotonic() - t0, 1)
    report.answer_accuracy = round(answerable_passed / answerable_total * 100, 1) if answerable_total else 0.0
    report.refuse_accuracy = round(refuse_passed / refuse_total * 100, 1) if refuse_total else 0.0

    logger.info(
        "eval.complete",
        extra={
            "total": report.total,
            "passed": report.passed,
            "failed": report.failed,
            "answer_accuracy": report.answer_accuracy,
            "refuse_accuracy": report.refuse_accuracy,
            "total_seconds": report.total_seconds,
        },
    )

    return report


def case_set_fingerprint(cases: Sequence[EvalCase]) -> str:
    """用例集指纹 = sha1(排序后的用例身份集合)[:12]（项2：评估基线版本）。

    身份取「可答性 + 题面 + 期望文档」三者 —— 改题面或改标注都会换指纹，
    提示「与旧基线比对已经没有意义」。排序保证与 DB 返回顺序无关。
    """
    items = sorted(
        f"{c.expected_answerable}|{c.question}|"
        f"{','.join(sorted(str(d) for d in (c.expected_doc_ids or [])))}"
        for c in cases
    )
    raw = "\n".join(items)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


async def save_eval_run(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    metrics: dict,
    case_fingerprint: str,
    case_count: int,
    caliber: dict,
    passed: bool,
    note: str | None = None,
) -> EvalRun:
    """落一条评估基线运行记录。"""
    row = EvalRun(
        tenant_id=tenant_id,
        metrics=metrics,
        case_fingerprint=case_fingerprint,
        case_count=case_count,
        caliber=caliber,
        passed=passed,
        note=note,
    )
    session.add(row)
    await session.commit()
    return row


async def latest_eval_run(
    session: AsyncSession, tenant_id: uuid.UUID, case_fingerprint: str
) -> EvalRun | None:
    """取最近一条**同一用例集指纹**的运行，作为基线。

    指纹不一致的历史运行不纳入 —— 那代表另一批用例，比了会得到误导性结论。
    """
    return (
        await session.execute(
            select(EvalRun)
            .where(
                EvalRun.tenant_id == tenant_id,
                EvalRun.case_fingerprint == case_fingerprint,
            )
            .order_by(EvalRun.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
