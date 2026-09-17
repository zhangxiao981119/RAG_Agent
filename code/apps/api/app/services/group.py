"""M3 任务 5 用户组管理服务 —— list / create / update / delete + 成员管理。

口径：
  · kind=normal/external（手册 §3.2.4 + §8 D-12）
  · 改组属性 / 成员增删都触发 acl_epoch+1
  · 删除组时连带 UserGroup 关联（ondelete=CASCADE 已配）
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy import select, func, delete, insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Group, User, UserGroup
from app.services.acl import invalidate_tenant_acl


@dataclass
class GroupRow:
    """list_groups 返回行（含 member_count）。"""

    id: uuid.UUID
    name: str
    kind: str
    member_count: int


@dataclass
class GroupMember:
    """组成员项。"""

    user_id: uuid.UUID
    username: str
    display_name: str


async def _bump_acl_epoch(session: AsyncSession, redis: Redis, tenant_id: uuid.UUID) -> int:
    """触发 tenant_acl_epoch+1 + 清旧缓存。"""
    return await invalidate_tenant_acl(session, redis, tenant_id)


async def list_groups(session: AsyncSession, tenant_id: uuid.UUID) -> list[GroupRow]:
    """列出所有组（含 member_count）。"""
    rows = (await session.execute(
        select(
            Group.id,
            Group.name,
            Group.kind,
            func.count(UserGroup.user_id).label("member_count"),
        )
        .outerjoin(UserGroup, UserGroup.group_id == Group.id)
        .where(Group.tenant_id == tenant_id)
        .group_by(Group.id, Group.name, Group.kind)
        .order_by(Group.kind, Group.name)
    )).all()
    return [
        GroupRow(id=r[0], name=r[1], kind=r[2], member_count=r[3] or 0)
        for r in rows
    ]


async def create_group(
    session: AsyncSession,
    redis: Redis,
    tenant_id: uuid.UUID,
    *,
    name: str,
    kind: str,
) -> Group:
    """新建组。name 冲突抛 ValueError。"""
    existing = (await session.execute(
        select(Group).where(Group.tenant_id == tenant_id, Group.name == name)
    )).scalar_one_or_none()
    if existing is not None:
        raise ValueError(f"组名已存在：{name}")

    group = Group(tenant_id=tenant_id, name=name, kind=kind)
    session.add(group)
    await session.flush()
    # 新组本身没有成员，但为保持缓存一致性仍 +1（影响微小）
    await _bump_acl_epoch(session, redis, tenant_id)
    return group


async def update_group(
    session: AsyncSession,
    redis: Redis,
    tenant_id: uuid.UUID,
    group_id: uuid.UUID,
    changes: dict,
) -> Group:
    """修改组属性。"""
    group = await session.get(Group, group_id)
    if group is None or group.tenant_id != tenant_id:
        raise ValueError("组不存在")

    need_bump = False
    if "name" in changes and changes["name"] is not None:
        # 校验 name 唯一
        if changes["name"] != group.name:
            existing = (await session.execute(
                select(Group).where(
                    Group.tenant_id == tenant_id,
                    Group.name == changes["name"],
                    Group.id != group_id,
                )
            )).scalar_one_or_none()
            if existing is not None:
                raise ValueError(f"组名已存在：{changes['name']}")
            group.name = changes["name"]
            need_bump = True
    if "kind" in changes and changes["kind"] is not None:
        if group.kind != changes["kind"]:
            group.kind = changes["kind"]
            need_bump = True

    await session.flush()
    if need_bump:
        await _bump_acl_epoch(session, redis, tenant_id)
    return group


async def delete_group(
    session: AsyncSession,
    redis: Redis,
    tenant_id: uuid.UUID,
    group_id: uuid.UUID,
) -> None:
    """删除组（连带 UserGroup 关联，CASCADE）。"""
    group = await session.get(Group, group_id)
    if group is None or group.tenant_id != tenant_id:
        raise ValueError("组不存在")
    await session.delete(group)
    await session.flush()
    await _bump_acl_epoch(session, redis, tenant_id)


async def list_members(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    group_id: uuid.UUID,
) -> list[GroupMember]:
    """列出组成员。"""
    # 先校验组存在
    group = await session.get(Group, group_id)
    if group is None or group.tenant_id != tenant_id:
        raise ValueError("组不存在")

    rows = (await session.execute(
        select(User.id, User.username, User.display_name)
        .join(UserGroup, UserGroup.user_id == User.id)
        .where(UserGroup.tenant_id == tenant_id, UserGroup.group_id == group_id)
        .order_by(User.username)
    )).all()
    return [
        GroupMember(user_id=r[0], username=r[1], display_name=r[2])
        for r in rows
    ]


async def add_members(
    session: AsyncSession,
    redis: Redis,
    tenant_id: uuid.UUID,
    group_id: uuid.UUID,
    user_ids: list[uuid.UUID],
) -> int:
    """批量加成员。返回新增数（已存在的跳过）。"""
    group = await session.get(Group, group_id)
    if group is None or group.tenant_id != tenant_id:
        raise ValueError("组不存在")

    # 校验所有 user_id 都存在且属于本 tenant
    existing_users = set((await session.execute(
        select(User.id).where(User.tenant_id == tenant_id, User.id.in_(user_ids))
    )).scalars().all())
    invalid = set(user_ids) - existing_users
    if invalid:
        raise ValueError(f"以下用户不存在：{[str(u)[:8] for u in invalid]}")

    # 查已存在的成员关系
    already = set((await session.execute(
        select(UserGroup.user_id).where(
            UserGroup.tenant_id == tenant_id,
            UserGroup.group_id == group_id,
            UserGroup.user_id.in_(user_ids),
        )
    )).scalars().all())

    to_add = [uid for uid in user_ids if uid not in already]
    for uid in to_add:
        session.add(UserGroup(tenant_id=tenant_id, user_id=uid, group_id=group_id))
    await session.flush()
    if to_add:
        await _bump_acl_epoch(session, redis, tenant_id)
    return len(to_add)


async def remove_member(
    session: AsyncSession,
    redis: Redis,
    tenant_id: uuid.UUID,
    group_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    """移除单个成员。"""
    group = await session.get(Group, group_id)
    if group is None or group.tenant_id != tenant_id:
        raise ValueError("组不存在")

    result = (await session.execute(
        delete(UserGroup).where(
            UserGroup.tenant_id == tenant_id,
            UserGroup.group_id == group_id,
            UserGroup.user_id == user_id,
        )
    ))
    if result.rowcount == 0:
        raise ValueError("成员不存在")
    await session.flush()
    await _bump_acl_epoch(session, redis, tenant_id)


__all__ = [
    "GroupRow",
    "GroupMember",
    "list_groups",
    "create_group",
    "update_group",
    "delete_group",
    "list_members",
    "add_members",
    "remove_member",
]
