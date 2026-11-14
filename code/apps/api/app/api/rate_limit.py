"""M6 安全加固 — 限流依赖（G4 验收项）。

用 Redis INCR + EXPIRE 实现固定窗口计数器：
  key = rate_limit:chat:{tenant_id}:{user_id}:{minute_epoch}
  每分钟窗口内 INCR，首次设 TTL=60s 自动过期
  超过 chat_rate_limit_per_minute 返回 429
"""
from __future__ import annotations

import time

from fastapi import Depends, HTTPException, status

from app.api.deps import CurrentUser, get_current_user
from app.config.settings import get_settings
from app.infra.redis_client import get_redis
from app.models import IntegrationApplication


async def check_chat_rate_limit(user: CurrentUser = Depends(get_current_user)) -> None:
    """对 chat/ask 接口按用户限流。超限返回 429。"""
    settings = get_settings()
    limit = settings.chat_rate_limit_per_minute
    # 当前分钟窗口的时间戳（每 60 秒一个新窗口）
    window = int(time.time() // 60)
    key = f"rate_limit:chat:{user.tenant_id}:{user.user_id}:{window}"

    redis = await get_redis()
    try:
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, 60)
        if count > limit:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                f"请求过于频繁，每分钟限 {limit} 次，请稍后再试",
                headers={"Retry-After": "60"},
            )
    except HTTPException:
        raise
    except Exception:
        # Redis 不可用时 fail-open：限流不阻断正常使用
        pass
    finally:
        try:
            await redis.aclose()
        except Exception:
            pass


async def check_integration_rate_limit(
    user: CurrentUser,
    application: IntegrationApplication,
) -> None:
    """同时按应用全局限额和应用内真实用户限额限流。"""
    window = int(time.time() // 60)
    prefix = f"rate_limit:integration:{user.tenant_id}:{application.id}"
    app_key = f"{prefix}:app:{window}"
    user_key = f"{prefix}:user:{user.user_id}:{window}"
    user_limit = get_settings().chat_rate_limit_per_minute

    redis = await get_redis()
    try:
        pipe = redis.pipeline()
        pipe.incr(app_key)
        pipe.incr(user_key)
        app_count, user_count = await pipe.execute()
        if app_count == 1:
            await redis.expire(app_key, 60)
        if user_count == 1:
            await redis.expire(user_key, 60)
        if app_count > application.rate_limit_per_minute:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                f"应用请求过于频繁，每分钟限 {application.rate_limit_per_minute} 次",
                headers={"Retry-After": "60"},
            )
        if user_count > user_limit:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                f"用户请求过于频繁，每分钟限 {user_limit} 次",
                headers={"Retry-After": "60"},
            )
    except HTTPException:
        raise
    except Exception as exc:
        # 集成调用不静默绕过限流，避免限流存储故障期间形成无约束调用窗口。
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "限流服务暂时不可用，请稍后重试",
        ) from exc
    finally:
        try:
            await redis.aclose()
        except Exception:
            pass
