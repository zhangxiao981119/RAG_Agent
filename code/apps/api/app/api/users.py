"""M3 任务 4 用户管理 API —— list / create / update / delete / reset-password。

手册 §6 L1295 要求：
  · 列表（GET /api/users）
  · 新建（POST /api/users）
  · 改部门 / 改密级 / 禁用（PATCH /api/users/{id}）
  · 删除（DELETE /api/users/{id}）
  · 重置密码（POST /api/users/{id}/reset-password）

写操作后由 services/user 自动触发 tenant_acl_epoch+1。
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_db, require_admin
from app.config.settings import get_settings
from app.models import User
from app.schemas.user import (
    UserCreateRequest,
    UserDeleteResponse,
    UserNode,
    UserPageResponse,
    UserResetPasswordRequest,
    UserResponse,
    UserUpdateRequest,
)
from app.services import user as user_service

router = APIRouter(prefix="/users", tags=["users"])


def _row_to_node(r: user_service.UserRow) -> UserNode:
    return UserNode(
        id=r.id,
        username=r.username,
        display_name=r.display_name,
        email=r.email,
        dept_id=r.dept_id,
        dept_path=r.dept_path,
        clearance=r.clearance,
        role_names=r.role_names,
        status=r.status,
    )


def _user_to_response(u: User, dept_path: str | None) -> UserResponse:
    return UserResponse(
        id=u.id,
        username=u.username,
        display_name=u.display_name,
        email=u.email,
        dept_id=u.dept_id,
        dept_path=dept_path,
        clearance=u.clearance,
        role_names=list(u.role_names or []),
        status=u.status,
    )


@router.get("", response_model=UserPageResponse)
async def list_users(
    page: int = Query(1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(20, ge=1, le=200, description="每页条数，1-200"),
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> UserPageResponse:
    """分页列出用户（含 dept_path）。"""
    rows, total = await user_service.list_users(
        session, user.tenant_id, page=page, page_size=page_size
    )
    return UserPageResponse(
        items=[_row_to_node(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreateRequest,
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> UserResponse:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    try:
        try:
            new_user = await user_service.create_user(
                session, redis, user.tenant_id,
                username=payload.username,
                display_name=payload.display_name,
                email=payload.email,
                password=payload.password,
                dept_id=payload.dept_id,
                clearance=payload.clearance,
                role_names=payload.role_names,
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
        await session.commit()
        await session.refresh(new_user)
        dept_path = None
        if new_user.dept_id is not None:
            from app.models import Department
            dept = await session.get(Department, new_user.dept_id)
            if dept is not None:
                dept_path = dept.path
        return _user_to_response(new_user, dept_path)
    finally:
        await redis.aclose()


@router.patch("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: uuid.UUID,
    payload: UserUpdateRequest,
    current: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> UserResponse:
    """修改用户属性。用 model_dump(exclude_unset=True) 只取客户端实际传的字段。"""
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    try:
        changes = payload.model_dump(exclude_unset=True)
        try:
            updated = await user_service.update_user(
                session, redis, current.tenant_id, user_id, changes
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
        await session.commit()
        await session.refresh(updated)
        dept_path = None
        if updated.dept_id is not None:
            from app.models import Department
            dept = await session.get(Department, updated.dept_id)
            if dept is not None:
                dept_path = dept.path
        return _user_to_response(updated, dept_path)
    finally:
        await redis.aclose()


@router.post("/{user_id}/reset-password", response_model=UserResponse)
async def reset_password(
    user_id: uuid.UUID,
    payload: UserResetPasswordRequest,
    current: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> UserResponse:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    try:
        try:
            updated = await user_service.reset_password(
                session, redis, current.tenant_id, user_id, payload.new_password
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
        await session.commit()
        await session.refresh(updated)
        dept_path = None
        if updated.dept_id is not None:
            from app.models import Department
            dept = await session.get(Department, updated.dept_id)
            if dept is not None:
                dept_path = dept.path
        return _user_to_response(updated, dept_path)
    finally:
        await redis.aclose()


@router.delete("/{user_id}", response_model=UserDeleteResponse)
async def delete_user(
    user_id: uuid.UUID,
    current: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> UserDeleteResponse:
    """删除用户。禁止删除自己（防止管理员误操作把自己删了）。"""
    if user_id == current.user_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能删除自己")

    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    try:
        try:
            await user_service.delete_user(session, redis, current.tenant_id, user_id)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
        await session.commit()
        return UserDeleteResponse(deleted=True)
    finally:
        await redis.aclose()
