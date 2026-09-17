"""M3 任务 6 角色管理服务 —— list / create / update / delete。

口径：
  · subjects.py 从 user.role_names（PostgreSQL array）直接读
  · 改角色名 → 同步 UPDATE 所有 user.role_names 中的旧名为新名
  · 删除角色 → 从所有 user.role_names 中移除该角色名
  · 任何写操作都触发 acl_epoch+1（subjects 含 role:<name>）

用 PostgreSQL array 操作（contained / replace）做批量同步，比 ORM 遍历快得多。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Role, User
from app.services.acl import invalidate_tenant_acl


@dataclass
class RoleRow:
    """list_roles 返回行（含已分配用户数）。"""

    id: uuid.UUID
    name: str
    user_count: int


async def _bump_acl_epoch(session: AsyncSession, redis: Redis, tenant_id: uuid.UUID) -> int:
    """触发 tenant_acl_epoch+1 + 清旧缓存。"""
    return await invalidate_tenant_acl(session, redis, tenant_id)


async def list_roles(session: AsyncSession, tenant_id: uuid.UUID) -> list[RoleRow]:
    """列出所有角色（含 user_count = role_names 中含此角色的用户数）。"""
    roles = (await session.execute(
        select(Role).where(Role.tenant_id == tenant_id).order_by(Role.name)
    )).scalars().all()

    if not roles:
        return []

    # 一次性查每个角色的 user_count（用 PostgreSQL array contains）
    # 注意 tenant_id 是 UUID，参数需 cast；用 CAST(:tid AS uuid) 避免 SQLAlchemy 误解 :tid::uuid 为参数名
    rows = []
    for r in roles:
        cnt = (await session.execute(
            text("SELECT count(*) FROM users WHERE tenant_id = CAST(:tid AS uuid) AND role_names @> ARRAY[:name]::text[]").bindparams(tid=str(tenant_id), name=r.name)
        )).scalar() or 0
        rows.append(RoleRow(id=r.id, name=r.name, user_count=cnt))
    return rows


async def create_role(
    session: AsyncSession,
    redis: Redis,
    tenant_id: uuid.UUID,
    *,
    name: str,
) -> Role:
    """新建角色。name 冲突抛 ValueError。"""
    existing = (await session.execute(
        select(Role).where(Role.tenant_id == tenant_id, Role.name == name)
    )).scalar_one_or_none()
    if existing is not None:
        raise ValueError(f"角色名已存在：{name}")

    role = Role(tenant_id=tenant_id, name=name)
    session.add(role)
    await session.flush()
    # 新角色本身没分配给任何用户，subjects 不变；但仍 +1 保持缓存一致
    await _bump_acl_epoch(session, redis, tenant_id)
    return role


async def update_role(
    session: AsyncSession,
    redis: Redis,
    tenant_id: uuid.UUID,
    role_id: uuid.UUID,
    new_name: str,
) -> Role:
    """改角色名 → 同步 UPDATE 所有 user.role_names 中的旧名为新名。"""
    role = await session.get(Role, role_id)
    if role is None or role.tenant_id != tenant_id:
        raise ValueError("角色不存在")

    if role.name == new_name:
        return role  # 无变化

    # 校验新名不冲突
    existing = (await session.execute(
        select(Role).where(
            Role.tenant_id == tenant_id,
            Role.name == new_name,
            Role.id != role_id,
        )
    )).scalar_one_or_none()
    if existing is not None:
        raise ValueError(f"角色名已存在：{new_name}")

    old_name = role.name
    role.name = new_name
    await session.flush()

    # 同步：把所有 user.role_names 中的 old_name 替换为 new_name
    # 用 PostgreSQL array_replace 函数；CAST(:tid AS uuid) 避免 SQLAlchemy 误解
    await session.execute(text(
        "UPDATE users SET role_names = array_replace(role_names, :old, :new) "
        "WHERE tenant_id = CAST(:tid AS uuid) AND role_names @> ARRAY[:old]::text[]"
    ), {"tid": str(tenant_id), "old": old_name, "new": new_name})

    await _bump_acl_epoch(session, redis, tenant_id)
    return role


async def delete_role(
    session: AsyncSession,
    redis: Redis,
    tenant_id: uuid.UUID,
    role_id: uuid.UUID,
) -> int:
    """删除角色 → 从所有 user.role_names 中移除该角色名。返回受影响用户数。"""
    role = await session.get(Role, role_id)
    if role is None or role.tenant_id != tenant_id:
        raise ValueError("角色不存在")

    name = role.name

    # 先统计有多少用户含此角色
    affected = (await session.execute(
        text("SELECT count(*) FROM users WHERE tenant_id = CAST(:tid AS uuid) AND role_names @> ARRAY[:name]::text[]").bindparams(tid=str(tenant_id), name=name)
    )).scalar() or 0

    # 从 user.role_names 中移除该角色名（用 array_remove）
    await session.execute(text(
        "UPDATE users SET role_names = array_remove(role_names, :name) "
        "WHERE tenant_id = CAST(:tid AS uuid) AND role_names @> ARRAY[:name]::text[]"
    ), {"tid": str(tenant_id), "name": name})

    await session.delete(role)
    await session.flush()
    await _bump_acl_epoch(session, redis, tenant_id)
    return affected


__all__ = [
    "RoleRow",
    "list_roles",
    "create_role",
    "update_role",
    "delete_role",
]
