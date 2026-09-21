"""M3 任务 5 用户组管理 API —— list / create / update / delete + 成员管理。

手册 §6 L1296 要求：
  · 列表（GET /api/groups）
  · 新建（POST /api/groups）
  · 改名 / 改 kind（PATCH /api/groups/{id}）
  · 删除（DELETE /api/groups/{id}）
  · 成员管理：GET / POST / DELETE

写操作后由 services/group 自动触发 tenant_acl_epoch+1。
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user, get_db, require_admin
from app.config.settings import get_settings
from app.models import Group
from app.schemas.group import (
    GroupCreateRequest,
    GroupDeleteResponse,
    GroupMemberAddRequest,
    GroupMemberList,
    GroupMemberOpResponse,
    GroupNode,
    GroupResponse,
    GroupUpdateRequest,
)
from app.services import group as group_service

router = APIRouter(prefix="/groups", tags=["groups"])


def _row_to_node(r: group_service.GroupRow) -> GroupNode:
    return GroupNode(id=r.id, name=r.name, kind=r.kind, member_count=r.member_count)


def _group_to_response(g: Group) -> GroupResponse:
    return GroupResponse(id=g.id, name=g.name, kind=g.kind)


@router.get("", response_model=list[GroupNode])
async def list_groups(
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> list[GroupNode]:
    """列出所有组（含 member_count）。"""
    rows = await group_service.list_groups(session, user.tenant_id)
    return [_row_to_node(r) for r in rows]


@router.post("", response_model=GroupResponse, status_code=status.HTTP_201_CREATED)
async def create_group(
    payload: GroupCreateRequest,
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> GroupResponse:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    try:
        try:
            group = await group_service.create_group(
                session, redis, user.tenant_id,
                name=payload.name,
                kind=payload.kind,
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
        await session.commit()
        await session.refresh(group)
        return _group_to_response(group)
    finally:
        await redis.aclose()


@router.patch("/{group_id}", response_model=GroupResponse)
async def update_group(
    group_id: uuid.UUID,
    payload: GroupUpdateRequest,
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> GroupResponse:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    try:
        changes = payload.model_dump(exclude_unset=True)
        try:
            group = await group_service.update_group(
                session, redis, user.tenant_id, group_id, changes
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
        await session.commit()
        await session.refresh(group)
        return _group_to_response(group)
    finally:
        await redis.aclose()


@router.delete("/{group_id}", response_model=GroupDeleteResponse)
async def delete_group(
    group_id: uuid.UUID,
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> GroupDeleteResponse:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    try:
        try:
            await group_service.delete_group(session, redis, user.tenant_id, group_id)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
        await session.commit()
        return GroupDeleteResponse(deleted=True)
    finally:
        await redis.aclose()


# ── 成员管理 ────────────────────────────────────────────────

@router.get("/{group_id}/members", response_model=GroupMemberList)
async def list_members(
    group_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> GroupMemberList:
    """列出组成员。"""
    try:
        members = await group_service.list_members(session, user.tenant_id, group_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return GroupMemberList(
        group_id=group_id,
        members=[
            {"user_id": m.user_id, "username": m.username, "display_name": m.display_name}
            for m in members
        ],
    )


@router.post("/{group_id}/members", response_model=GroupMemberOpResponse)
async def add_members(
    group_id: uuid.UUID,
    payload: GroupMemberAddRequest,
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> GroupMemberOpResponse:
    """批量加成员。"""
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    try:
        try:
            added = await group_service.add_members(
                session, redis, user.tenant_id, group_id, payload.user_ids
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
        await session.commit()
        return GroupMemberOpResponse(added=added)
    finally:
        await redis.aclose()


@router.delete("/{group_id}/members/{user_id}", response_model=GroupMemberOpResponse)
async def remove_member(
    group_id: uuid.UUID,
    user_id: uuid.UUID,
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> GroupMemberOpResponse:
    """移除单个成员。"""
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    try:
        try:
            await group_service.remove_member(session, redis, user.tenant_id, group_id, user_id)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
        await session.commit()
        return GroupMemberOpResponse(removed=1)
    finally:
        await redis.aclose()
