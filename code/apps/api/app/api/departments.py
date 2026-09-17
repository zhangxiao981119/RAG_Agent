"""M3 任务 7+8 部门管理 API —— 树形查询 + CRUD + 移动。

手册 §6 L1294-L1302 要求：
  · 树形展示（GET /api/departments 返回完整树）
  · 新建子部门（POST /api/departments）
  · 重命名 / 改 visible_to_parent / 改 sort_order（PATCH /api/departments/{id}）
  · 移动（POST /api/departments/{id}/move）
  · 删除（DELETE /api/departments/{id}，约束空部门）

所有写操作后由 services/dept 自动触发 tenant_acl_epoch+1 + 清旧缓存。
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user, get_db  # noqa: F401  (get_current_user 用于 Depends)
from app.config.settings import get_settings
from app.models import Department, Tenant
from app.schemas.department import (
    DepartmentCreateRequest,
    DepartmentDeleteResponse,
    DepartmentMoveRequest,
    DepartmentNode,
    DepartmentResponse,
    DepartmentUpdateRequest,
)
from app.services import dept as dept_service

router = APIRouter(prefix="/departments", tags=["departments"])


def _to_response(d: Department) -> DepartmentResponse:
    return DepartmentResponse(
        id=d.id,
        name=d.name,
        path=d.path,
        depth=d.depth,
        sort_order=d.sort_order,
        visible_to_parent=d.visible_to_parent,
    )


def _to_node_dto(node) -> DepartmentNode:  # node: dept_service.DeptNode
    return DepartmentNode(
        id=node.id,
        name=node.name,
        path=node.path,
        depth=node.depth,
        sort_order=node.sort_order,
        visible_to_parent=node.visible_to_parent,
        user_count=node.user_count,
        children=[_to_node_dto(c) for c in node.children],
    )


@router.get("", response_model=list[DepartmentNode])
async def list_departments(
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> list[DepartmentNode]:
    """返回完整部门树（含每节点 user_count）。"""
    tree = await dept_service.build_tree(session, user.tenant_id)
    return [_to_node_dto(n) for n in tree]


@router.post("", response_model=DepartmentResponse, status_code=status.HTTP_201_CREATED)
async def create_department(
    payload: DepartmentCreateRequest,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> DepartmentResponse:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    try:
        try:
            dept = await dept_service.create_dept(
                session, redis, user.tenant_id,
                name=payload.name,
                parent_id=payload.parent_id,
                sort_order=payload.sort_order,
                visible_to_parent=payload.visible_to_parent,
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
        await session.commit()
        await session.refresh(dept)
        return _to_response(dept)
    finally:
        await redis.aclose()


@router.patch("/{dept_id}", response_model=DepartmentResponse)
async def update_department(
    dept_id: uuid.UUID,
    payload: DepartmentUpdateRequest,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> DepartmentResponse:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    try:
        # 重命名走 rename_dept（触发子树 path 级联重写）
        if payload.name is not None:
            try:
                dept = await dept_service.rename_dept(
                    session, redis, user.tenant_id, dept_id, payload.name
                )
            except ValueError as exc:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
        else:
            # 没改名，只用 update_dept_attrs 改 sort_order / visible_to_parent
            try:
                dept = await dept_service.update_dept_attrs(
                    session, redis, user.tenant_id, dept_id,
                    sort_order=payload.sort_order,
                    visible_to_parent=payload.visible_to_parent,
                )
            except ValueError as exc:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
        await session.commit()
        await session.refresh(dept)
        return _to_response(dept)
    finally:
        await redis.aclose()


@router.post("/{dept_id}/move", response_model=DepartmentResponse)
async def move_department(
    dept_id: uuid.UUID,
    payload: DepartmentMoveRequest,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> DepartmentResponse:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    try:
        try:
            dept = await dept_service.move_dept(
                session, redis, user.tenant_id, dept_id, payload.new_parent_id
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
        await session.commit()
        await session.refresh(dept)
        return _to_response(dept)
    finally:
        await redis.aclose()


@router.delete("/{dept_id}", response_model=DepartmentDeleteResponse)
async def delete_department(
    dept_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> DepartmentDeleteResponse:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    try:
        try:
            await dept_service.delete_dept(session, redis, user.tenant_id, dept_id)
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
        await session.commit()
        return DepartmentDeleteResponse(deleted=True)
    finally:
        await redis.aclose()
