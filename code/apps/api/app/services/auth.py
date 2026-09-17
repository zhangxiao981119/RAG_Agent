"""M3 JWT 认证工具 —— 签发/校验 access + refresh token + bcrypt 密码哈希。

仅负责无副作用的纯函数，业务流程在 api/auth.py 与 api/deps.py 中编排。

Token 体系（手册 §5.1）：
  · access  token：短期，typ="access"，用于业务 API 鉴权
  · refresh token：长期，typ="refresh"，仅用于 /api/auth/refresh 换新 access
  · 登出：把两个 token 的 jti 加入 Redis 黑名单（TTL = 剩余有效期）
  · refresh rotation：每次 refresh 后旧 refresh 立即失效（jti 入黑名单）
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
from jose import JWTError, jwt

from app.config.settings import get_settings

ALGORITHM = "HS256"

# token 类型标识
TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_REFRESH = "refresh"


def hash_password(plain: str) -> str:
    """bcrypt 哈希明文密码，返回可存库的字符串。

    轮数从 settings.bcrypt_rounds 读取（默认 12），显式控制哈希强度。
    """
    settings = get_settings()
    salt = bcrypt.gensalt(rounds=settings.bcrypt_rounds)
    return bcrypt.hashpw(plain.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """校验明文密码与 bcrypt 哈希是否匹配。"""
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def _create_token(
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    username: str,
    typ: str,
    expire_delta: timedelta,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """签发 JWT 内部函数，typ 字段区分 access/refresh。"""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    expire = now + expire_delta
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "tenant": str(tenant_id),
        "username": username,
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
        "jti": uuid.uuid4().hex,            # 每个 token 唯一 id，用于黑名单
        "typ": typ,
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def create_access_token(
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    username: str,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """签发 access token，默认 jwt_expire_minutes（24h）。"""
    settings = get_settings()
    return _create_token(
        user_id, tenant_id, username,
        typ=TOKEN_TYPE_ACCESS,
        expire_delta=timedelta(minutes=settings.jwt_expire_minutes),
        extra_claims=extra_claims,
    )


def create_refresh_token(
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    username: str,
) -> str:
    """签发 refresh token，默认 jwt_refresh_expire_days（7d）。"""
    settings = get_settings()
    return _create_token(
        user_id, tenant_id, username,
        typ=TOKEN_TYPE_REFRESH,
        expire_delta=timedelta(days=settings.jwt_refresh_expire_days),
    )


def verify_access_token(token: str) -> dict[str, Any]:
    """校验 access token 签名 + 过期 + typ，返回 payload；失败抛 JWTError。

    ★ 不检查黑名单 —— 黑名单在 deps.get_current_user 中查 Redis。
    """
    settings = get_settings()
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
    if payload.get("typ") != TOKEN_TYPE_ACCESS:
        raise JWTError(f"token typ 不是 access：{payload.get('typ')}")
    return payload


def verify_refresh_token(token: str) -> dict[str, Any]:
    """校验 refresh token 签名 + 过期 + typ，返回 payload；失败抛 JWTError。

    ★ 不检查黑名单 —— 黑名单在 api/auth.refresh 端点中查 Redis。
    """
    settings = get_settings()
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
    if payload.get("typ") != TOKEN_TYPE_REFRESH:
        raise JWTError(f"token typ 不是 refresh：{payload.get('typ')}")
    return payload


__all__ = [
    "TOKEN_TYPE_ACCESS",
    "TOKEN_TYPE_REFRESH",
    "hash_password",
    "verify_password",
    "create_access_token",
    "create_refresh_token",
    "verify_access_token",
    "verify_refresh_token",
    "JWTError",
]
