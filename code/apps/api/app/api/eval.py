"""评估门禁 API（admin）—— 手册 M6 G5。

POST /api/admin/eval/run  触发评估，返回逐条结果
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user, get_db
from app.services import audit
from app.services.eval_service import run_eval

router = APIRouter(prefix="/admin", tags=["admin"])


class EvalCaseResultItem(BaseModel):
    question: str
    expected_answerable: bool
    actual_refused: bool
    passed: bool
    chunks_count: int
    refuse_reason: str | None
    elapsed_ms: int


class EvalReportResponse(BaseModel):
    total: int
    passed: int
    failed: int
    answer_accuracy: float
    refuse_accuracy: float
    total_seconds: float
    cases: list[EvalCaseResultItem]


def _require_admin(user: CurrentUser) -> None:
    if "role:admin" not in user.subjects:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="FORBIDDEN")


@router.post("/eval/run", response_model=EvalReportResponse)
async def eval_run(
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> EvalReportResponse:
    """触发全量评估，返回逐条结果。"""
    _require_admin(user)
    report = await run_eval(session, user.tenant_id)

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
        },
    )

    return EvalReportResponse(
        total=report.total,
        passed=report.passed,
        failed=report.failed,
        answer_accuracy=report.answer_accuracy,
        refuse_accuracy=report.refuse_accuracy,
        total_seconds=report.total_seconds,
        cases=[
            EvalCaseResultItem(
                question=c.question,
                expected_answerable=c.expected_answerable,
                actual_refused=c.actual_refused,
                passed=c.passed,
                chunks_count=c.chunks_count,
                refuse_reason=c.refuse_reason,
                elapsed_ms=c.elapsed_ms,
            )
            for c in report.cases
        ],
    )
