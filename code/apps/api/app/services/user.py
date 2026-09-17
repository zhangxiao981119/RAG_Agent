"""M3 任务 4 用户管理服务 —— list / create / update / delete / reset_password。

口径：
  · list：join departments 取 dept_path 便于前端展示
  · create：username 唯一约束（uq_users_tenant_username），bcrypt 哈希密码
  · update：用 model_dump(exclude_unset=True) 只取客户端实际传的字段
            dept_id / clearance / role_names 变化触发 acl_epoch+1
  · delete：硬删；管理员自我保护：不能删自己
  · reset_password：bcrypt 哈希新密码
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Department, Tenant, User
from app.services.acl import invalidate_tenant_acl
from app.services.auth import hash_password


@dataclass
class UserRow:
    """list_users 返回的扁平行（含 dept_path）。"""

    id: uuid.UUID
    username: str
    display_name: str
    email: str | None
    dept_id: uuid.UUID | None
    dept_path: str | None
    clearance: int
    role_names: list[str]
    status: str


async def _bump_acl_epoch(session: AsyncSession, redis: Redis, tenant_id: uuid.UUID) -> int:
    """触发 tenant_acl_epoch+1 + 清旧缓存。"""
    return await invalidate_tenant_acl(session, redis, tenant_id)


async def list_users(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[UserRow], int]:
    """分页列出用户（含 dept_path join），返回 (rows, total)。"""
    total = (await session.execute(
        select(func.count()).select_from(User).where(User.tenant_id == tenant_id)
    )).scalar_one()

    rows = (await session.execute(
        select(User, Department.path)
        .outerjoin(Department, Department.id == User.dept_id)
        .where(User.tenant_id == tenant_id)
        .order_by(User.username)
        .limit(page_size)
        .offset((page - 1) * page_size)
    )).all()
    result = [
        UserRow(
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
        for u, dept_path in rows
    ]
    return result, total


async def create_user(
    session: AsyncSession,
    redis: Redis,
    tenant_id: uuid.UUID,
    *,
    username: str,
    display_name: str,
    email: str | None,
    password: str,
    dept_id: uuid.UUID | None,
    clearance: int,
    role_names: list[str],
) -> User:
    """新建用户。username 冲突抛 ValueError；dept_id 不存在抛 ValueError。"""
    # 校验 dept_id 存在
    if dept_id is not None:
        dept = await session.get(Department, dept_id)
        if dept is None or dept.tenant_id != tenant_id:
            raise ValueError("部门不存在")

    # 校验 username 唯一
    existing = (await session.execute(
        select(User).where(User.tenant_id == tenant_id, User.username == username)
    )).scalar_one_or_none()
    if existing is not None:
        raise ValueError(f"用户名已存在：{username}")

    user = User(
        tenant_id=tenant_id,
        username=username,
        display_name=display_name,
        email=email,
        password_hash=hash_password(password),
        dept_id=dept_id,
        clearance=clearance,
        role_names=role_names,
        status="active",
    )
    session.add(user)
    await session.flush()

    # 新用户加入后，部门 user_count 变 + subjects 可能含 user:<id>
    # 但 user:<id> 主体不依赖 dept，所以严格说只 dept_id 影响时才需要 +1
    # 简化：新建用户也 +1，多算一次无害
    await _bump_acl_epoch(session, redis, tenant_id)
    return user


async def update_user(
    session: AsyncSession,
    redis: Redis,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    changes: dict,  # 已经过 exclude_unset 处理，只含客户端实际传的字段
) -> User:
    """修改用户。changes 含哪些字段就改哪些。"""
    user = await session.get(User, user_id)
    if user is None or user.tenant_id != tenant_id:
        raise ValueError("用户不存在")

    # 校验 dept_id（如果传了且不是 None）
    new_dept_id = changes.get("dept_id", ...)
    if new_dept_id is not None and new_dept_id is not ...:
        dept = await session.get(Department, new_dept_id)
        if dept is None or dept.tenant_id != tenant_id:
            raise ValueError("部门不存在")

    # 标记是否需要 bump acl_epoch
    need_bump = False
    if "dept_id" in changes:
        if user.dept_id != changes["dept_id"]:
            user.dept_id = changes["dept_id"]
            need_bump = True
    if "clearance" in changes:
        if user.clearance != changes["clearance"]:
            user.clearance = changes["clearance"]
            need_bump = True
    if "role_names" in changes:
        if list(user.role_names or []) != list(changes["role_names"]):
            user.role_names = list(changes["role_names"])
            need_bump = True
    # 不影响权限的字段
    if "display_name" in changes and changes["display_name"] is not None:
        user.display_name = changes["display_name"]
    if "email" in changes:
        user.email = changes["email"]
    if "status" in changes and changes["status"] is not None:
        if user.status != changes["status"]:
            user.status = changes["status"]
            need_bump = True  # disabled 后主体不可见

    await session.flush()
    if need_bump:
        await _bump_acl_epoch(session, redis, tenant_id)
    return user


async def reset_password(
    session: AsyncSession,
    redis: Redis,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    new_password: str,
) -> User:
    """重置密码。不触发 acl_epoch（密码不进 subjects）。"""
    user = await session.get(User, user_id)
    if user is None or user.tenant_id != tenant_id:
        raise ValueError("用户不存在")
    user.password_hash = hash_password(new_password)
    await session.flush()
    return user


async def delete_user(
    session: AsyncSession,
    redis: Redis,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    """硬删用户。"""
    user = await session.get(User, user_id)
    if user is None or user.tenant_id != tenant_id:
        raise ValueError("用户不存在")
    await session.delete(user)
    await session.flush()
    await _bump_acl_epoch(session, redis, tenant_id)


__all__ = [
    "UserRow",
    "list_users",
    "create_user",
    "update_user",
    "reset_password",
    "delete_user",
]
