"""依赖注入 —— M3 JWT 认证。

从 Authorization: Bearer <token> 解析 JWT，校验后返回 CurrentUser。
其他 API 层只依赖 CurrentUser 抽象，不感知具体来源。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionLocal
from app.models import Department, Tenant, User
from app.services.auth import JWTError, verify_access_token


@dataclass(frozen=True)
class CurrentUser:
    """当前登录用户上下文（API 层只依赖此抽象）。"""

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
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> CurrentUser:
    """从 Authorization: Bearer <token> 解析 JWT 并返回当前用户。"""
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少认证信息")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        payload = verify_access_token(token)
    except JWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "认证信息无效或已过期") from exc

    user_id_raw = payload.get("sub")
    tenant_id_raw = payload.get("tenant")
    username = payload.get("username")
    if not user_id_raw or not tenant_id_raw or not username:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "认证信息字段缺失")

    try:
        user_id = uuid.UUID(user_id_raw)
        tenant_id = uuid.UUID(tenant_id_raw)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "认证信息字段格式错误") from exc

    user = await session.get(User, user_id)
    if user is None or user.tenant_id != tenant_id or user.username != username:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在或已被替换")
    if user.status != "active":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户已被禁用")

    dept_path = ""
    if user.dept_id is not None:
        dept = await session.get(Department, user.dept_id)
        if dept is not None:
            dept_path = dept.path

    return CurrentUser(
        user_id=user.id,
        tenant_id=tenant_id,
        username=user.username,
        display_name=user.display_name,
        clearance=user.clearance,
        dept_path=dept_path,
        subjects=["public"],  # M3 暂无权限版本
        authorized_kb_ids=[],  # M3 不限，chat 直接信任前端 kb_ids
    )
