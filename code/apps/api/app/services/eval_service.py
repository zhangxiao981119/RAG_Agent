"""评估门禁服务 —— 手册 §7.3 / M6 G5。

遍历 eval_cases 表，对每条 case 跑 retrieve + generate，
检查可答 case 是否回答了、不可答 case 是否拒答了。

★ 双口径并存（`with_recall_chunks`）：
  生产 chat 喂给模型的是**四类来源**（命中片 + 表格回捞 + 章节补全 + 图召回），
  而本服务历史上只喂 `result.chunks` —— 于是「评测测的不是生产行为」。
  两者都不能丢：
    · False（默认）= 与引入补充片之前逐字节相同 → 旧基线仍然可比；
    · True        = 走 `retrieve_chain()` + `assemble_context_chunks()`，与生产同构。
  报告里必须带 `context_caliber`：两份分数放一起时，人要能看出它们能不能比。

★ 即使 True 也不等于「与生产完全同构」，剩一处差异必须写在报告里：
  生产在检索前做 **query 改写**（`chat.py` 传的是 `retrieve_query` = 改写后的问句），
  评测直接用原问题。改写会显著影响召回，所以「对齐了拼装」只解决上下文那一半。
"""
from __future__ import annotations

import hashlib
import logging
import time
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EvalCase, EvalRun, KnowledgeBase
from app.services.generate import get_generation_service
from app.services.retrieve import RetrievalService
from app.services.retrieve.base import (
    RetrievalResult,
    assemble_context_chunks,
    recall_chunks_count,
)

logger = logging.getLogger(__name__)

CONTEXT_CALIBER_CHUNKS_ONLY = "chunks_only"
CONTEXT_CALIBER_WITH_RECALL = "with_recall_chunks"

# 报告自带的声明句 —— 措辞随口径翻转。
# ★ 为什么必须写进报告而不是只写在命令行历史里：报告会被人单独转发、被存档、
#   被拿去和另一份报告比。口径与限制不在报告里，就等于没记录。
_NOTE_CHUNKS_ONLY = (
    "本次评测未将图召回片注入生成上下文（with_recall_chunks=False），"
    "故本报告不覆盖图召回对答案质量的影响；graph_covered / graph_unique 只是召回侧计数，"
    "不能据此宣称「图无副作用」。"
)
_NOTE_WITH_RECALL = (
    "本次评测已将表格回捞 / 章节补全 / 图召回片一并注入生成上下文"
    "（with_recall_chunks=True），拼装口径与生产 chat 一致；"
    "仍有一处差异：评测不做 query 改写，直接用原问题检索。"
)


@dataclass
class EvalCaseResult:
    """单条评估结果。"""
    question: str
    expected_answerable: bool
    # 实际是否拒答
    actual_refused: bool
    # 是否通过（可答没拒答 = 通过；不可答拒答了 = 通过）
    passed: bool
    # 实际喂给生成的 chunk 条数（口径相关：chunks_only 时等于命中片条数）
    chunks_count: int
    # 拒答原因（如果拒答）
    refuse_reason: str | None
    # 耗时（毫秒）
    elapsed_ms: int
    # 上面 chunks_count 里属于「补充片」的条数（表格/章节/图）。
    # ★ 必须单独记：否则报告只显示「上下文变多了」，分不清是命中片多了
    #   （检索质量变化）还是补充片多了（拼装口径变化）——这是两件完全不同的事。
    recall_chunks_count: int = 0


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
    # 本次报告是在哪套上下文口径下得到的（两份分数能不能比，取决于这一行）
    context_caliber: str = CONTEXT_CALIBER_CHUNKS_ONLY
    # 报告自带的声明句（措辞随 context_caliber 翻转，见模块 docstring）
    context_note: str = _NOTE_CHUNKS_ONLY
    # 图召回的召回侧观测（无论开关都聚合；未开图时恒 0 / 空）
    graph_covered: int = 0
    graph_unique: int = 0
    graph_degraded_reasons: dict[str, int] = field(default_factory=dict)
    # 逐条结果
    cases: list[EvalCaseResult] = field(default_factory=list)


def _accumulate_graph_stats(
    report: EvalReport, result: RetrievalResult, reasons: Counter[str]
) -> None:
    """把单条检索的图召回观测并进报告（召回侧，与生成质量无关）。"""
    report.graph_covered += int(result.path_stats.get("graph_covered", 0))
    report.graph_unique += int(result.path_stats.get("graph_unique", 0))
    if result.graph_degraded_reason:
        reasons[result.graph_degraded_reason] += 1


async def run_eval(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    subjects: list[str],
    clearance: int,
    *,
    with_recall_chunks: bool = False,
) -> EvalReport:
    """跑全量 eval_cases，返回评估报告。

    subjects / clearance MUST 由调用方（admin 接口）从 CurrentUser 传入 ——
    与 chat.py 走同一份主体解析结果。此前这里硬编码 `["role:admin"]`，
    漏掉了 `public`，被 G4 闸门（`acl_tags && user_subjects`）全量过滤，
    导致公开库文档一条都召不回、评估恒为「可答 0% / 拒答 100%」。

    `with_recall_chunks`：
      · False（默认）→ `retrieve()` + 只喂 `result.chunks`，与历史口径逐字节相同，
        因此旧基线仍然可比。**这是默认值的唯一理由**，不是"懒得改"。
      · True → `retrieve_chain()` + `assemble_context_chunks()`（四类全喂）。
        `rewrite_score` 传 `None`：评测没有改写阶段，复杂度信号本来就未知 ——
        内部会按"信号不满足"降级为单趟。MUST NOT 伪造一个分数，那会把"单趟"
        伪装成"按复杂度决定"，让 `chain_mode` 这个审计字段直接说谎。
    """
    rows = (
        await session.execute(
            select(EvalCase)
            .where(EvalCase.tenant_id == tenant_id)
            .order_by(EvalCase.created_at)
        )
    ).scalars().all()

    report = EvalReport(total=len(rows))
    # 口径与声明句在**取到用例之后就写进报告**：空用例集跑出来的报告同样要能
    # 说清"这是哪套口径"，否则一份空报告会被当成"某套口径的 0 分"。
    if with_recall_chunks:
        report.context_caliber = CONTEXT_CALIBER_WITH_RECALL
        report.context_note = _NOTE_WITH_RECALL
    if not rows:
        return report

    retrieval = RetrievalService()
    generation = get_generation_service()
    t0 = time.monotonic()
    graph_reasons: Counter[str] = Counter()

    # 评估用的权限口径来自调用方传入的 admin 主体（含 public），
    # kb_id 来自 eval_case
    answerable_total = 0
    answerable_passed = 0
    refuse_total = 0
    refuse_passed = 0

    for case in rows:
        # kb_id 为空 = 跨库用例，检索用租户下全部知识库。
        # ★ 与 scripts/run_eval.py「全租户 kb 列表」的口径保持一致：
        #   两套调用方（门禁服务 / CLI 评测）必须检索同一个范围，否则同一批
        #   用例在两边跑出不同分数。此前 case.kb_id 全指向空库 p1-restricted，
        #   导致本服务只检索 0 个库、而 CLI 检索全库，分数无法对齐。
        if case.kb_id:
            kb_ids: list[uuid.UUID] = [case.kb_id]
        else:
            kb_ids = list(
                (
                    await session.scalars(
                        select(KnowledgeBase.id).where(KnowledgeBase.tenant_id == tenant_id)
                    )
                ).all()
            )
        case_t0 = time.monotonic()

        # 检索：口径决定走哪个入口（两个入口返回同一个 RetrievalResult 契约）
        if with_recall_chunks:
            # 与 chat.py 的调用形态对齐（位置参数顺序一致），但 rewrite_score 传 None：
            # 评测没有改写阶段 —— 不能伪造分数，见本函数 docstring
            result = await retrieval.retrieve_chain(
                session, case.question, tenant_id, kb_ids, clearance, subjects,
                rewrite_score=None,
            )
        else:
            result = await retrieval.retrieve(
                session, case.question, tenant_id,
                authorized_kb_ids=kb_ids,
                clearance=clearance,
                user_subjects=subjects,
            )

        # 图召回是召回侧观测，与"喂不喂给模型"无关 —— 无论开关都收
        _accumulate_graph_stats(report, result, graph_reasons)

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

        # 生成。context_chunks 的取法就是本报告的核心口径差异 ——
        # 关闭态保持 `result.chunks`（与历史逐字节相同），开启态走共享拼装函数。
        context_chunks = (
            assemble_context_chunks(result) if with_recall_chunks else result.chunks
        )
        gen_result = await generation.generate(
            case.question, context_chunks,
            history=[], memory_prompt="",
        )

        elapsed_ms = int((time.monotonic() - case_t0) * 1000)
        actual_refused = gen_result.refused

        cr = EvalCaseResult(
            question=case.question,
            expected_answerable=case.expected_answerable,
            actual_refused=actual_refused,
            passed=(not actual_refused) if case.expected_answerable else actual_refused,
            chunks_count=len(context_chunks),
            refuse_reason=gen_result.refuse_reason if actual_refused else None,
            elapsed_ms=elapsed_ms,
            recall_chunks_count=(
                recall_chunks_count(result) if with_recall_chunks else 0
            ),
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
    report.graph_degraded_reasons = dict(graph_reasons)

    logger.info(
        "eval.complete",
        extra={
            "total": report.total,
            "passed": report.passed,
            "failed": report.failed,
            "answer_accuracy": report.answer_accuracy,
            "refuse_accuracy": report.refuse_accuracy,
            "total_seconds": report.total_seconds,
            # 口径进日志：同一批用例两套口径的分数会不一样，日志里必须先说口径
            "context_caliber": report.context_caliber,
            "graph_covered": report.graph_covered,
            "graph_unique": report.graph_unique,
            "graph_degraded_reasons": report.graph_degraded_reasons,
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
