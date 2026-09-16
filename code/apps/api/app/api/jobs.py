"""解析任务状态查询 API（手册 §5.1 GET /jobs/{job_id}）。"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user, get_db
from app.models import ParseJob
from app.schemas.jobs import JobOut

router = APIRouter(tags=["jobs"])


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job(
    job_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> JobOut:
    job = await session.get(ParseJob, job_id)
    if job is None or job.tenant_id != user.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="NOT_FOUND")
    return JobOut(
        id=job.id,
        document_id=job.document_id,
        status=job.status,
        attempts=job.attempts,
        max_attempts=job.max_attempts,
        last_error=job.last_error,
        started_at=job.started_at,
        finished_at=job.finished_at,
        created_at=job.created_at,
    )
