"""配额管理 Admin API（M6 续篇）。

  GET    /api/admin/quota                查当前租户配额
  PUT    /api/admin/quota                修改租户配额（admin only）
  GET    /api/admin/quota/usage          查当前用户今日用量

admin 判定：clearance >= 40（与 chat.py 一致）。
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user, get_db
from app.config.settings import get_settings
from app.models import TenantQuota
from app.schemas.quota import QuotaUsage, TenantQuotaOut, TenantQuotaUpdate
from app.services import quota as quota_service

router = APIRouter(prefix="/admin", tags=["admin"])


def _require_admin(user: CurrentUser) -> None:
    if user.clearance < 40:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="FORBIDDEN")


@router.get("/quota", response_model=TenantQuotaOut)
async def get_quota(
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> TenantQuotaOut:
    """查当前租户的配额。无记录返回 settings 默认值（让 admin 看清当前生效数值）。"""
    _require_admin(user)
    row = (
        await session.execute(
            select(TenantQuota).where(TenantQuota.tenant_id == user.tenant_id)
        )
    ).scalar_one_or_none()
    settings = get_settings()
    if row is None:
        # 不入库，直接返回默认值（让 admin 知道当前生效值）
        return TenantQuotaOut(
            tenant_id=user.tenant_id,
            daily_token_limit=settings.default_tenant_daily_token_limit,
            daily_message_limit=99999,  # 默认不限消息次数（仅 token 限额生效）
            monthly_token_limit=settings.default_tenant_monthly_token_limit,
            updated_at=datetime.now(timezone.utc),
        )
    return TenantQuotaOut(
        tenant_id=row.tenant_id,
        daily_token_limit=row.daily_token_limit,
        daily_message_limit=row.daily_message_limit,
        monthly_token_limit=row.monthly_token_limit,
        updated_at=row.updated_at,
    )


@router.put("/quota", response_model=TenantQuotaOut)
async def update_quota(
    payload: TenantQuotaUpdate,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> TenantQuotaOut:
    """修改租户配额（admin only）。无记录则新建，有记录则更新。"""
    _require_admin(user)
    row = (
        await session.execute(
            select(TenantQuota).where(TenantQuota.tenant_id == user.tenant_id)
        )
    ).scalar_one_or_none()
    if row is None:
        row = TenantQuota(
            tenant_id=user.tenant_id,
            daily_token_limit=payload.daily_token_limit,
            daily_message_limit=payload.daily_message_limit,
            monthly_token_limit=payload.monthly_token_limit,
        )
        session.add(row)
    else:
        row.daily_token_limit = payload.daily_token_limit
        row.daily_message_limit = payload.daily_message_limit
        row.monthly_token_limit = payload.monthly_token_limit
    await session.commit()
    await session.refresh(row)
    return TenantQuotaOut(
        tenant_id=row.tenant_id,
        daily_token_limit=row.daily_token_limit,
        daily_message_limit=row.daily_message_limit,
        monthly_token_limit=row.monthly_token_limit,
        updated_at=row.updated_at,
    )


@router.get("/quota/usage", response_model=QuotaUsage)
async def get_usage(
    user: CurrentUser = Depends(get_current_user),
) -> QuotaUsage:
    """查当前用户今日用量（普通用户也可查自己的，让用户感知剩余配额）。"""
    limits = await quota_service.get_limits(user.tenant_id)
    usage = await quota_service.get_usage(user.tenant_id, user.user_id)
    return QuotaUsage(
        user_id=user.user_id,
        user_tokens_today=usage.user_tokens_today,
        user_messages_today=usage.user_messages_today,
        tenant_tokens_today=usage.tenant_tokens_today,
        tenant_tokens_this_month=usage.tenant_tokens_this_month,
        user_daily_token_limit=limits.user_daily_token,
        user_daily_message_limit=limits.user_daily_msg,
        tenant_daily_token_limit=limits.tenant_daily_token,
        tenant_monthly_token_limit=limits.tenant_monthly_token,
    )
