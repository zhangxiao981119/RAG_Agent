"""依赖注入 —— M3 JWT 认证 + 主体解析。

从 Authorization: Bearer <token> 解析 JWT，校验后调 `load_principal_from_db`
完成主体解析（含 Redis 缓存，§3.2.6），返回 CurrentUser。
其他 API 层只依赖 CurrentUser 抽象，不感知具体来源。
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.database import SessionLocal
from app.models import User
from app.services.acl import load_principal_from_db
from app.services.auth import JWTError, verify_access_token

logger = logging.getLogger(__name__)

# Redis 黑名单 key 前缀；value=1，TTL=token 剩余有效期
_BLACKLIST_KEY_FMT = "auth:blacklist:{jti}"


@dataclass(frozen=True)
class CurrentUser:
    """当前登录用户上下文（API 层只依赖此抽象）。

    `subjects` 与 `authorized_kb_ids` 由 `load_principal_from_db` 解析得到
    （含 §3.2.3 双向展开 + §3.2.7 公开库自动成员），随 `tenant_acl_epoch` 失效。
    """

    user_id: uuid.UUID
    tenant_id: uuid.UUID
    username: str
    display_name: str
    clearance: int
    dept_path: str
    subjects: list[str]
    authorized_kb_ids: list[uuid.UUID]
    acl_epoch: int


async def get_db() -> AsyncSession:
    async with SessionLocal() as session:
        yield session


async def _is_token_blacklisted(redis: Redis, jti: str | None) -> bool:
    """检查 jti 是否在 Redis 黑名单中。"""
    if not jti:
        return False
    try:
        return bool(await redis.exists(_BLACKLIST_KEY_FMT.format(jti=jti)))
    except Exception:
        logger.warning("黑名单查询失败，按未拉黑处理（fail-open）", exc_info=True)
        return False


async def get_current_user(
    session: AsyncSession = Depends(get_db),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> CurrentUser:
    """从 Authorization: Bearer <token> 解析 JWT 并返回当前用户。

    流程：JWT 校验 → 黑名单检查（Redis）→ 用户状态校验 → 主体解析（含缓存）。
    主体解析走 `load_principal_from_db`：Redis 命中则秒级返回；未命中则全量加载并写回；
    Redis 异常降级直连 DB（§3.2.6）。
    """
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
    jti = payload.get("jti")
    if not user_id_raw or not tenant_id_raw or not username:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "认证信息字段缺失")

    try:
        user_id = uuid.UUID(user_id_raw)
        tenant_id = uuid.UUID(tenant_id_raw)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "认证信息字段格式错误") from exc

    # ── 黑名单检查（Redis）─────────────────────────────────
    settings = get_settings()
    redis: Redis | None = None
    try:
        redis = Redis.from_url(settings.redis_url)
        if await _is_token_blacklisted(redis, jti):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "认证信息已失效（已登出）")

        user = await session.get(User, user_id)
        if user is None or user.tenant_id != tenant_id or user.username != username:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在或已被替换")
        if user.status != "active":
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户已被禁用")

        # ── 主体解析（含 Redis 缓存，§3.2.6）──────────────────────
        principal, acl_epoch, dept_path = await load_principal_from_db(
            session, redis, user_id, tenant_id
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("主体解析失败")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "权限解析服务暂时不可用，请稍后重试",
        ) from exc
    finally:
        if redis is not None:
            try:
                await redis.aclose()
            except Exception:
                logger.warning("redis aclose 失败", exc_info=True)

    return CurrentUser(
        user_id=user.id,
        tenant_id=tenant_id,
        username=user.username,
        display_name=user.display_name,
        clearance=principal.clearance,
        dept_path=dept_path,
        subjects=sorted(principal.subjects),
        authorized_kb_ids=sorted(
            uuid.UUID(kid) for kid in principal.authorized_kb_ids
        ),
        acl_epoch=acl_epoch,
    )


async def require_admin(
    user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """管理员依赖：clearance < 40 拒绝（403）。

    统一 admin 鉴权口径，消除旧代码中 "role:admin" in subjects 与
    clearance < 40 两套判定并存的歧义（M1）。
    """
    if user.clearance < 40:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="FORBIDDEN")
    return user
