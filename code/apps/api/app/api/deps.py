"""依赖注入 —— M2 用固定 admin 用户（无权限版本，手册 §6 M2 任务 6）。

M3 接 JWT 后替换这里。其他 API 层只依赖 CurrentUser 抽象，不感知具体来源。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.database import SessionLocal
from app.models import Tenant, User


@dataclass(frozen=True)
class CurrentUser:
    """M2 固定 admin 用户。M3 替换为 JWT 解析结果。"""

    user_id: uuid.UUID
    tenant_id: uuid.UUID
    username: str
    display_name: str
    clearance: int
    dept_path: str
    subjects: list[str]
    authorized_kb_ids: list[uuid.UUID]


async def get_db() -> AsyncSession:
    async with SessionLocal() as session:
        yield session


async def get_current_user(
    session: AsyncSession = Depends(get_db),
) -> CurrentUser:
    """M2 占位：固定返回 admin 用户。M3 改为从 JWT 解析。"""
    settings = get_settings()
    tenant = await session.scalar(
        select(Tenant).where(Tenant.code == settings.tenant_code)
    )
    if tenant is None:
        raise RuntimeError("租户未初始化，请先运行 scripts/seed.py")
    user = await session.scalar(
        select(User).where(User.tenant_id == tenant.id, User.username == "admin")
    )
    if user is None:
        raise RuntimeError("admin 用户未初始化，请先运行 scripts/seed.py")

    return CurrentUser(
        user_id=user.id,
        tenant_id=tenant.id,
        username=user.username,
        display_name=user.display_name,
        clearance=user.clearance,
        dept_path="",  # M2 不查 dept，M3 补
        subjects=["public"],  # M2 无权限版本
        authorized_kb_ids=[],  # M2 不限，chat 直接信任前端 kb_ids
    )
