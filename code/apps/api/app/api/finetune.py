"""微调样本管理 API（admin）—— 查看采纳收集的问答数据，微调时按导出状态取出。

  · GET /api/finetune/samples          分页列表（exported 过滤：未导出/已导出/全部）
  · POST /api/finetune/samples/export  把当前未导出样本打上 exported_at（微调取出动作）

导出数据格式：items 原样返回 question/answer，打标后下次只取增量。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_db, require_admin
from app.models import FinetuneSample

router = APIRouter(prefix="/finetune", tags=["finetune"])


class FinetuneSampleItem(BaseModel):
    id: str
    conversation_id: str
    question: str
    answer: str
    adopted_by: str
    adopted_at: str
    exported_at: str | None


class FinetuneSamplePage(BaseModel):
    items: list[FinetuneSampleItem]
    total: int
    page: int
    page_size: int


class FinetuneExportResponse(BaseModel):
    exported: int
    exported_at: str


@router.get("/samples", response_model=FinetuneSamplePage)
async def list_finetune_samples(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    exported: bool | None = Query(None, description="null=全部 true=已导出 false=未导出"),
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> FinetuneSamplePage:
    conditions = [FinetuneSample.tenant_id == user.tenant_id]
    if exported is True:
        conditions.append(FinetuneSample.exported_at.is_not(None))
    elif exported is False:
        conditions.append(FinetuneSample.exported_at.is_(None))

    total = (
        await session.execute(select(func.count(FinetuneSample.id)).where(*conditions))
    ).scalar_one()
    rows = (
        await session.execute(
            select(FinetuneSample)
            .where(*conditions)
            .order_by(FinetuneSample.adopted_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return FinetuneSamplePage(
        items=[
            FinetuneSampleItem(
                id=str(r.id),
                conversation_id=str(r.conversation_id),
                question=r.question,
                answer=r.answer,
                adopted_by=str(r.adopted_by),
                adopted_at=r.adopted_at.isoformat(),
                exported_at=r.exported_at.isoformat() if r.exported_at else None,
            )
            for r in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("/samples/export", response_model=FinetuneExportResponse)
async def export_finetune_samples(
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> FinetuneExportResponse:
    """微调取出打标：把本租户所有未导出样本标记 exported_at。

    取数流程：先 GET /finetune/samples?exported=false 拿到全部未导出样本，
    再调本接口打标，之后同批样本不会再出现在未导出列表中（增量语义）。
    """
    now = datetime.now(timezone.utc)
    rows = (
        await session.execute(
            select(FinetuneSample).where(
                FinetuneSample.tenant_id == user.tenant_id,
                FinetuneSample.exported_at.is_(None),
            )
        )
    ).scalars().all()
    for r in rows:
        r.exported_at = now
    await session.commit()
    return FinetuneExportResponse(
        exported=len(rows),
        exported_at=now.isoformat(),
    )
