"""M3 任务 6 角色管理 API —— list / create / update / delete。

手册 §6 L1297 要求：
  · 列表（GET /api/roles）
  · 新建（POST /api/roles）
  · 改名（PATCH /api/roles/{id}，同步清理 user.role_names）
  · 删除（DELETE /api/roles/{id}，从 user.role_names 移除）

写操作后由 services/role 自动触发 tenant_acl_epoch+1。
用户分配角色走 PATCH /api/users/{id}（任务 4 已实现）。
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user, get_db
from app.config.settings import get_settings
from app.models import Role
from app.schemas.role import (
    RoleCreateRequest,
    RoleDeleteResponse,
    RoleNode,
    RoleResponse,
    RoleUpdateRequest,
)
from app.services import role as role_service

router = APIRouter(prefix="/roles", tags=["roles"])


def _row_to_node(r: role_service.RoleRow) -> RoleNode:
    return RoleNode(id=r.id, name=r.name, user_count=r.user_count)


def _role_to_response(r: Role) -> RoleResponse:
    return RoleResponse(id=r.id, name=r.name)


@router.get("", response_model=list[RoleNode])
async def list_roles(
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> list[RoleNode]:
    """列出所有角色（含 user_count）。"""
    rows = await role_service.list_roles(session, user.tenant_id)
    return [_row_to_node(r) for r in rows]


@router.post("", response_model=RoleResponse, status_code=status.HTTP_201_CREATED)
async def create_role(
    payload: RoleCreateRequest,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> RoleResponse:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    try:
        try:
            role = await role_service.create_role(
                session, redis, user.tenant_id, name=payload.name
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
        await session.commit()
        await session.refresh(role)
        return _role_to_response(role)
    finally:
        await redis.aclose()


@router.patch("/{role_id}", response_model=RoleResponse)
async def update_role(
    role_id: uuid.UUID,
    payload: RoleUpdateRequest,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> RoleResponse:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    try:
        try:
            role = await role_service.update_role(
                session, redis, user.tenant_id, role_id, payload.name
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
        await session.commit()
        await session.refresh(role)
        return _role_to_response(role)
    finally:
        await redis.aclose()


@router.delete("/{role_id}", response_model=RoleDeleteResponse)
async def delete_role(
    role_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> RoleDeleteResponse:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    try:
        try:
            affected = await role_service.delete_role(session, redis, user.tenant_id, role_id)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
        await session.commit()
        return RoleDeleteResponse(deleted=True, affected_users=affected)
    finally:
        await redis.aclose()
