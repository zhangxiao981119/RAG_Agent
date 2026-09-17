"""M3 JWT 认证工具 —— 签发/校验 JWT + bcrypt 密码哈希。

仅负责无副作用的纯函数，业务流程在 api/auth.py 与 api/deps.py 中编排。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
from jose import JWTError, jwt

from app.config.settings import get_settings

ALGORITHM = "HS256"


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


def create_access_token(
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    username: str,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """签发 JWT，payload 包含 sub(user_id)/tenant/username + 过期时间。"""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=settings.jwt_expire_minutes)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "tenant": str(tenant_id),
        "username": username,
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def verify_access_token(token: str) -> dict[str, Any]:
    """校验 JWT 签名 + 过期时间，返回 payload；失败抛 JWTError。"""
    settings = get_settings()
    return jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])


__all__ = ["hash_password", "verify_password", "create_access_token", "verify_access_token", "JWTError"]
