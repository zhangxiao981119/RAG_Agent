"""M3 认证 API —— login / refresh / logout。

安全策略：
  1. IP 速率限制：单 IP 每分钟最多 login_rate_per_minute 次尝试，超限 429
  2. 账户失败锁定：连续失败 login_max_failures 次后锁定 login_lock_minutes 分钟，超限 423
  3. 统一错误文案：账号不存在/密码错误/已锁定均不泄露账号有效性
  4. refresh rotation：每次 refresh 后旧 refresh 立即入黑名单（防盗用）
  5. logout：把 access + refresh 的 jti 加入 Redis 黑名单（TTL=剩余有效期）
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.config.settings import get_settings
from app.models import Department, Tenant, User
from app.schemas.auth import (
    LoginRequest,
    LoginResponse,
    LogoutRequest,
    LogoutResponse,
    PublicKeyResponse,
    RefreshRequest,
    RefreshResponse,
    UserInfo,
)
from app.services import audit
from app.services.auth import (
    JWTError,
    create_access_token,
    create_refresh_token,
    verify_password,
    verify_refresh_token,
)
from app.services.crypto import decrypt_password, get_public_key_spki_b64

router = APIRouter(tags=["auth"])

# Redis key 前缀
_RATE_KEY = "auth:rate:{ip}"          # IP 限流计数，TTL 60s
_FAIL_KEY = "auth:fail:{tid}:{user}"  # 账户失败计数
_LOCK_KEY = "auth:lock:{tid}:{user}"  # 账户锁定标记
_BLACKLIST_KEY = "auth:blacklist:{jti}"  # token 黑名单，TTL=token 剩余有效期


def _client_ip(request: Request) -> str:
    """获取客户端真实 IP，优先读 X-Forwarded-For（经 nginx 代理时）。"""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def _revoke_token(redis: Redis, payload: dict[str, Any]) -> None:
    """把 token 的 jti 加入黑名单，TTL 设为其剩余有效期。"""
    jti = payload.get("jti")
    exp = payload.get("exp")
    if not jti or not exp:
        return
    now = int(time.time())
    ttl = max(1, int(exp) - now)  # 至少留 1 秒，避免 0/负 TTL 被拒
    try:
        await redis.setex(_BLACKLIST_KEY.format(jti=jti), ttl, "1")
    except Exception:  # noqa: BLE001
        # 黑名单写失败不影响主流程；最坏情况是 token 仍可用直到自然过期
        pass


@router.get("/auth/public-key", response_model=PublicKeyResponse)
async def get_public_key() -> PublicKeyResponse:
    """返回 RSA 公钥（SPKI DER base64），供前端 Web Crypto 加密密码。"""
    import asyncio
    key_b64 = await asyncio.to_thread(get_public_key_spki_b64)
    return PublicKeyResponse(public_key=key_b64)


@router.post("/auth/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> LoginResponse:
    """用户名+密码登录，签发 access + refresh token。"""
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
        # decrypt_password 内部首次调用会同步 Redis + RSA CPU 密集，
        # 用 asyncio.to_thread 放到线程池避免阻塞事件循环
        try:
            import asyncio
            plain_password = await asyncio.to_thread(
                decrypt_password, payload.password
            )
        except Exception:  # noqa: BLE001 - 密文损坏/格式错误统一当密码错误，不泄露细节
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")

        # ── 4. 校验用户名密码 ────────────────────────────────
        user = await session.scalar(
            select(User).where(User.tenant_id == tenant.id, User.username == payload.username)
        )
        # 账号不存在或非激活态，也走失败计数（防枚举：不区分"不存在"和"密码错"）
        if user is None or user.status != "active" or not verify_password(plain_password, user.password_hash):
            await audit.record(
                tenant.id, user.id if user else None,
                "auth.login.fail", object_type="user", object_id=payload.username,
                detail={"reason": "用户名或密码错误"}, ip=client_ip,
            )
            fail_key = _FAIL_KEY.format(tid=tenant.id, user=payload.username)
            failures = await redis.incr(fail_key)
            if failures == 1:
                await redis.expire(fail_key, settings.login_lock_minutes * 60)
            if failures >= settings.login_max_failures:
                await redis.setex(lock_key, settings.login_lock_minutes * 60, "1")
                await redis.delete(fail_key)
                await audit.record(
                    tenant.id, user.id if user else None,
                    "auth.login.locked", object_type="user", object_id=payload.username,
                    detail={"reason": f"连续失败 {failures} 次，锁定 {settings.login_lock_minutes} 分钟"},
                    ip=client_ip,
                )
                raise HTTPException(
                    status.HTTP_423_LOCKED,
                    f"账户已临时锁定，请 {settings.login_lock_minutes} 分钟后重试",
                )
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")

        # ── 5. 登录成功，清除失败计数 + 签发双 token ─────────
        fail_key = _FAIL_KEY.format(tid=tenant.id, user=payload.username)
        await redis.delete(fail_key, lock_key)
        await audit.record(
            tenant.id, user.id, "auth.login.success",
            object_type="user", object_id=user.username, ip=client_ip,
        )

        # 部门路径
        dept_path = ""
        if user.dept_id is not None:
            dept = await session.get(Department, user.dept_id)
            if dept is not None:
                dept_path = dept.path

        access_token = create_access_token(user.id, tenant.id, user.username)
        refresh_token = create_refresh_token(user.id, tenant.id, user.username)
        return LoginResponse(
            token=access_token,
            refresh_token=refresh_token,
            user=UserInfo(
                id=user.id,
                display_name=user.display_name,
                dept_path=dept_path,
                clearance=user.clearance,
            ),
        )
    finally:
        await redis.aclose()


@router.post("/auth/refresh", response_model=RefreshResponse)
async def refresh(
    payload: RefreshRequest,
    session: AsyncSession = Depends(get_db),
) -> RefreshResponse:
    """用 refresh token 换新 access + 新 refresh（rotation）。

    流程：
      1. 校验 refresh token 签名 + 过期 + typ
      2. 查 User，校验仍存在 + active + tenant 一致
      3. rotation：用 SET NX 原子把旧 refresh jti 入黑名单
         - 若 SET NX 返回 False，说明并发请求已先 rotation 过，拒绝本次请求
         - 原 EXISTS+SETEX 两步非原子，会被并发 refresh 同时通过黑名单检查
      4. 签发新 access + 新 refresh
    """
    try:
        refresh_payload = verify_refresh_token(payload.refresh_token)
    except JWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "refresh token 无效或已过期") from exc

    jti = refresh_payload.get("jti")
    user_id_raw = refresh_payload.get("sub")
    tenant_id_raw = refresh_payload.get("tenant")
    username = refresh_payload.get("username")
    if not jti or not user_id_raw or not tenant_id_raw or not username:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "refresh token 字段缺失")

    import uuid as _uuid
    try:
        user_id = _uuid.UUID(user_id_raw)
        tenant_id = _uuid.UUID(tenant_id_raw)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "refresh token 字段格式错误") from exc

    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    try:
        user = await session.get(User, user_id)
        if user is None or user.tenant_id != tenant_id or user.username != username:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在或已被替换")
        if user.status != "active":
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户已被禁用")

        # rotation：用 SET NX 原子把旧 refresh jti 入黑名单
        # SET key value NX EX ttl：key 不存在才设置并返回 True，已存在返回 nil（False）
        # 这样并发两次 refresh 同一 jti 只会有一个成功，另一个被拒
        exp = refresh_payload.get("exp")
        if not exp:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "refresh token 字段缺失")
        now = int(time.time())
        ttl = max(1, int(exp) - now)
        blacklisted = await redis.set(
            _BLACKLIST_KEY.format(jti=jti), "1", nx=True, ex=ttl
        )
        if not blacklisted:
            # 已被并发 rotation 或 logout 抢先入黑名单
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "refresh token 已失效，请重新登录")

        new_access = create_access_token(user.id, tenant_id, user.username)
        new_refresh = create_refresh_token(user.id, tenant_id, user.username)
        return RefreshResponse(token=new_access, refresh_token=new_refresh)
    finally:
        await redis.aclose()


@router.post("/auth/logout", response_model=LogoutResponse)
async def logout(
    payload: LogoutRequest,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> LogoutResponse:
    """登出：把 access + refresh 的 jti 加入 Redis 黑名单。

    不强制依赖 get_current_user（access token 鉴权），因为 access 已过期时
    用户无法登出会形成死锁：access 401 → 前端跳登录 → 又拿不到有效 access
    去 logout。改为可选 access（从 Header 取，容错校验）+ 可选 refresh
    （从 body 取，容错校验），两者都失效时也返回 revoked=True（用户本就要登出）。
    """
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    try:
        # access token 入黑名单（可能已过期，跳过即可）
        if authorization and authorization.startswith("Bearer "):
            access_token = authorization.removeprefix("Bearer ").strip()
            try:
                from app.services.auth import verify_access_token
                access_payload = verify_access_token(access_token)
                await _revoke_token(redis, access_payload)
            except JWTError:
                pass  # 已过期/无效，无需入黑名单

        # refresh token 一并吊销（可能已过期，跳过即可）
        if payload.refresh_token:
            try:
                refresh_payload = verify_refresh_token(payload.refresh_token)
                await _revoke_token(redis, refresh_payload)
            except JWTError:
                pass  # refresh 已过期/无效，跳过

        return LogoutResponse(revoked=True)
    finally:
        await redis.aclose()
