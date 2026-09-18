"""审计日志查询 API（admin）—— 手册 §5.1 GET /api/admin/audit-logs。

action 支持前缀过滤（传 doc.* 可筛全部文档类动作）；
user_id 支持按操作人过滤。服务端分页。
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user, get_db
from app.services import audit

router = APIRouter(prefix="/admin", tags=["admin"])


class AuditLogItem(BaseModel):
    id: int
    user_id: str | None
    user_label: str
    action: str
    object_type: str | None
    object_id: str | None
    detail: dict
    ip: str | None
    created_at: str


class AuditLogPage(BaseModel):
    items: list[AuditLogItem]
    total: int
    page: int
    page_size: int


def _require_admin(user: CurrentUser) -> None:
    if "role:admin" not in user.subjects:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="FORBIDDEN")


@router.get("/audit-logs", response_model=AuditLogPage)
async def list_audit_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    action: str | None = Query(None, max_length=50, description="动作前缀过滤，如 doc.* 传 doc"),
    user_id: uuid.UUID | None = Query(None, description="按操作人过滤"),
    start_date: str | None = Query(None, description="起始日期 YYYY-MM-DD"),
    end_date: str | None = Query(None, description="结束日期 YYYY-MM-DD"),
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> AuditLogPage:
    _require_admin(user)
    items, total = await audit.list_logs(
        session, user.tenant_id, page, page_size,
        action=action, user_id=user_id, start_date=start_date, end_date=end_date,
    )
    return AuditLogPage(
        items=[AuditLogItem(**item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
    )
