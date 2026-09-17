"""M3 认证 API —— POST /api/auth/login 签发 JWT。

安全策略：
  1. IP 速率限制：单 IP 每分钟最多 login_rate_per_minute 次尝试，超限 429
  2. 账户失败锁定：连续失败 login_max_failures 次后锁定 login_lock_minutes 分钟，超限 423
  3. 统一错误文案：账号不存在/密码错误/已锁定均不泄露账号有效性
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.config.settings import get_settings
from app.models import Department, Tenant, User
from app.schemas.auth import LoginRequest, LoginResponse, PublicKeyResponse, UserInfo
from app.services.auth import create_access_token, verify_password
from app.services.crypto import decrypt_password, get_public_key_spki_b64

router = APIRouter(tags=["auth"])

# Redis key 前缀
_RATE_KEY = "auth:rate:{ip}"          # IP 限流计数，TTL 60s
_FAIL_KEY = "auth:fail:{tid}:{user}"  # 账户失败计数
_LOCK_KEY = "auth:lock:{tid}:{user}"  # 账户锁定标记


def _client_ip(request: Request) -> str:
    """获取客户端真实 IP，优先读 X-Forwarded-For（经 nginx 代理时）。"""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@router.get("/auth/public-key", response_model=PublicKeyResponse)
async def get_public_key() -> PublicKeyResponse:
    """返回 RSA 公钥（SPKI DER base64），供前端加密密码。"""
    return PublicKeyResponse(public_key=get_public_key_spki_b64())


@router.post("/auth/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> LoginResponse:
    """用户名+密码登录，签发 JWT。"""
    settings = get_settings()
    tenant = await session.scalar(select(Tenant).where(Tenant.code == settings.tenant_code))
    if tenant is None:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "租户未初始化，请先运行 scripts/seed.py")

    client_ip = _client_ip(request)
    redis = Redis.from_url(settings.redis_url)
    try:
        # ── 1. IP 速率限制 ────────────────────────────────────
        rate_key = _RATE_KEY.format(ip=client_ip)
        rate_count = await redis.incr(rate_key)
        if rate_count == 1:
            await redis.expire(rate_key, 60)  # 每分钟窗口
        if rate_count > settings.login_rate_per_minute:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "登录尝试过于频繁，请稍后再试",
            )

        # ── 2. 账户锁定检查 ──────────────────────────────────
        lock_key = _LOCK_KEY.format(tid=tenant.id, user=payload.username)
        if await redis.exists(lock_key):
            ttl = await redis.ttl(lock_key)
            raise HTTPException(
                status.HTTP_423_LOCKED,
                f"账户已临时锁定，请 {max(1, ttl // 60)} 分钟后重试",
            )

        # ── 3. 解密密码（前端 RSA-OAEP 加密的密文）────────────
        try:
            plain_password = decrypt_password(payload.password)
        except Exception:  # noqa: BLE001 - 密文损坏/格式错误统一当密码错误，不泄露细节
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")

        # ── 4. 校验用户名密码 ────────────────────────────────
        user = await session.scalar(
            select(User).where(User.tenant_id == tenant.id, User.username == payload.username)
        )
        # 账号不存在或非激活态，也走失败计数（防枚举：不区分"不存在"和"密码错"）
        if user is None or user.status != "active" or not verify_password(plain_password, user.password_hash):
            fail_key = _FAIL_KEY.format(tid=tenant.id, user=payload.username)
            failures = await redis.incr(fail_key)
            if failures == 1:
                await redis.expire(fail_key, settings.login_lock_minutes * 60)
            if failures >= settings.login_max_failures:
                await redis.setex(lock_key, settings.login_lock_minutes * 60, "1")
                await redis.delete(fail_key)
                raise HTTPException(
                    status.HTTP_423_LOCKED,
                    f"账户已临时锁定，请 {settings.login_lock_minutes} 分钟后重试",
                )
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")

        # ── 4. 登录成功，清除失败计数 ─────────────────────────
        fail_key = _FAIL_KEY.format(tid=tenant.id, user=payload.username)
        await redis.delete(fail_key, lock_key)

        # 部门路径
        dept_path = ""
        if user.dept_id is not None:
            dept = await session.get(Department, user.dept_id)
            if dept is not None:
                dept_path = dept.path

        token = create_access_token(user.id, tenant.id, user.username)
        return LoginResponse(
            token=token,
            user=UserInfo(
                id=user.id,
                display_name=user.display_name,
                dept_path=dept_path,
                clearance=user.clearance,
            ),
        )
    finally:
        await redis.aclose()
