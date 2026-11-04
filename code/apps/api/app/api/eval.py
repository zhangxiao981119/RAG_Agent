"""评估门禁 API（admin）—— 手册 M6 G5。

POST /api/admin/eval/run  触发评估，返回逐条结果

★ 上下文口径由查询参数 `with_recall_chunks` 决定（默认 false = 旧口径）：
  两种口径的分数**不可混比**，所以响应里必带 `context_caliber` + `context_note`。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_db, require_admin
from app.services import audit
from app.services.eval_service import run_eval

router = APIRouter(prefix="/admin", tags=["admin"])


class EvalCaseResultItem(BaseModel):
    question: str
    expected_answerable: bool
    actual_refused: bool
    passed: bool
    chunks_count: int
    # chunks_count 里属于补充片（表格/章节/图）的条数；0 = 本次未注入补充片
    recall_chunks_count: int = 0
    refuse_reason: str | None
    elapsed_ms: int


class EvalReportResponse(BaseModel):
    total: int
    passed: int
    failed: int
    answer_accuracy: float
    refuse_accuracy: float
    total_seconds: float
    # 口径留痕：这两份报告能不能比，取决于这两行（见 EvalReport 注释）
    context_caliber: str = ""
    context_note: str = ""
    # 图召回召回侧观测（未开图 / 未开补充片时为 0，但**不能**据此宣称图无副作用）
    graph_covered: int = 0
    graph_unique: int = 0
    graph_degraded_reasons: dict[str, int] = {}
    cases: list[EvalCaseResultItem]


@router.post("/eval/run", response_model=EvalReportResponse)
async def eval_run(
    with_recall_chunks: bool = Query(
        False,
        description=(
            "true = 与生产同构地注入四类上下文（命中片+表格回捞+章节补全+图召回）；"
            "false（默认）= 只喂命中片，与历史基线可比。两套口径的分数不可混比。"
        ),
    ),
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> EvalReportResponse:
    """触发全量评估，返回逐条结果。"""
    # 传 admin 自身的主体与密级：评估必须与真实问答走同一份权限口径
    report = await run_eval(
        session, user.tenant_id, user.subjects, user.clearance,
        with_recall_chunks=with_recall_chunks,
    )

    # 审计记录
    await audit.record(
        user.tenant_id, user.user_id, "eval.run",
        object_type=None, object_id=None,
        detail={
            "total": report.total,
            "passed": report.passed,
            "failed": report.failed,
            "answer_accuracy": report.answer_accuracy,
            "refuse_accuracy": report.refuse_accuracy,
            "total_seconds": report.total_seconds,
            # 口径必须进审计：同一批用例两套口径分数不同，事后比的时候要知道
            "context_caliber": report.context_caliber,
            "graph_covered": report.graph_covered,
            "graph_unique": report.graph_unique,
        },
    )

    return EvalReportResponse(
        total=report.total,
        passed=report.passed,
        failed=report.failed,
        answer_accuracy=report.answer_accuracy,
        refuse_accuracy=report.refuse_accuracy,
        total_seconds=report.total_seconds,
        context_caliber=report.context_caliber,
        context_note=report.context_note,
        graph_covered=report.graph_covered,
        graph_unique=report.graph_unique,
        graph_degraded_reasons=report.graph_degraded_reasons,
        cases=[
            EvalCaseResultItem(
                question=c.question,
                expected_answerable=c.expected_answerable,
                actual_refused=c.actual_refused,
                passed=c.passed,
                chunks_count=c.chunks_count,
                recall_chunks_count=c.recall_chunks_count,
                refuse_reason=c.refuse_reason,
                elapsed_ms=c.elapsed_ms,
            )
            for c in report.cases
        ],
    )
