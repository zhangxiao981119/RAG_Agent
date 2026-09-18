"""特性开关 Admin API（M6 续篇）。

  GET    /api/admin/feature-flags           列出所有规则
  POST   /api/admin/feature-flags           新增灰度规则
  PATCH  /api/admin/feature-flags/{id}      修改 enabled / rollout_percent / pattern
  DELETE /api/admin/feature-flags/{id}      删除规则

写操作后清 Redis 缓存，立即生效（缓存 TTL 60s 内）。
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user, get_db
from app.models import FeatureFlag
from app.schemas.feature_flag import FeatureFlagCreate, FeatureFlagOut, FeatureFlagUpdate
from app.services import feature_flag as flag_service

router = APIRouter(prefix="/admin", tags=["admin"])


def _require_admin(user: CurrentUser) -> None:
    if user.clearance < 40:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="FORBIDDEN")


@router.get("/feature-flags", response_model=list[FeatureFlagOut])
async def list_feature_flags(
    feature_key: str | None = None,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> list[FeatureFlagOut]:
    """列出全部规则。可按 feature_key 过滤。"""
    _require_admin(user)
    stmt = select(FeatureFlag).where(FeatureFlag.tenant_id == user.tenant_id)
    if feature_key:
        stmt = stmt.where(FeatureFlag.feature_key == feature_key)
    stmt = stmt.order_by(FeatureFlag.feature_key, FeatureFlag.dept_path_pattern.desc())
    rows = (await session.execute(stmt)).scalars().all()
    return [
        FeatureFlagOut(
            id=r.id,
            tenant_id=r.tenant_id,
            feature_key=r.feature_key,
            dept_path_pattern=r.dept_path_pattern,
            enabled=r.enabled,
            rollout_percent=r.rollout_percent,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )
        for r in rows
    ]


@router.post("/feature-flags", response_model=FeatureFlagOut, status_code=status.HTTP_201_CREATED)
async def create_feature_flag(
    payload: FeatureFlagCreate,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> FeatureFlagOut:
    """新增灰度规则。(tenant, feature_key, dept_path_pattern) 唯一。"""
    _require_admin(user)
    row = FeatureFlag(
        tenant_id=user.tenant_id,
        feature_key=payload.feature_key,
        dept_path_pattern=payload.dept_path_pattern,
        enabled=payload.enabled,
        rollout_percent=payload.rollout_percent,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "规则已存在（同 feature_key + dept_path_pattern）") from exc
    await session.refresh(row)
    await flag_service._invalidate(user.tenant_id, payload.feature_key)
    return FeatureFlagOut(
        id=row.id,
        tenant_id=row.tenant_id,
        feature_key=row.feature_key,
        dept_path_pattern=row.dept_path_pattern,
        enabled=row.enabled,
        rollout_percent=row.rollout_percent,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.patch("/feature-flags/{flag_id}", response_model=FeatureFlagOut)
async def update_feature_flag(
    flag_id: uuid.UUID,
    payload: FeatureFlagUpdate,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> FeatureFlagOut:
    """修改规则。只取客户端实际传入的字段（model_dump(exclude_unset=True)）。"""
    _require_admin(user)
    row = await session.get(FeatureFlag, flag_id)
    if row is None or row.tenant_id != user.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="NOT_FOUND")
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "无变更")
    for k, v in changes.items():
        setattr(row, k, v)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "规则冲突（同 feature_key + dept_path_pattern 已存在）") from exc
    await session.refresh(row)
    await flag_service._invalidate(user.tenant_id, row.feature_key)
    return FeatureFlagOut(
        id=row.id,
        tenant_id=row.tenant_id,
        feature_key=row.feature_key,
        dept_path_pattern=row.dept_path_pattern,
        enabled=row.enabled,
        rollout_percent=row.rollout_percent,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.delete("/feature-flags/{flag_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_feature_flag(
    flag_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> None:
    """删除规则。"""
    _require_admin(user)
    row = await session.get(FeatureFlag, flag_id)
    if row is None or row.tenant_id != user.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="NOT_FOUND")
    feature_key = row.feature_key
    await session.delete(row)
    await session.commit()
    await flag_service._invalidate(user.tenant_id, feature_key)
