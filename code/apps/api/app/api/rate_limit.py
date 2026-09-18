"""M6 安全加固 — 限流依赖（G4 验收项）。

用 Redis INCR + EXPIRE 实现固定窗口计数器：
  key = rate_limit:chat:{tenant_id}:{user_id}:{minute_epoch}
  每分钟窗口内 INCR，首次设 TTL=60s 自动过期
  超过 chat_rate_limit_per_minute 返回 429
"""
from __future__ import annotations

import time

from fastapi import Depends, HTTPException, status
from redis.asyncio import Redis

from app.api.deps import CurrentUser, get_current_user
from app.config.settings import get_settings


async def check_chat_rate_limit(user: CurrentUser = Depends(get_current_user)) -> None:
    """对 chat/ask 接口按用户限流。超限返回 429。"""
    settings = get_settings()
    limit = settings.chat_rate_limit_per_minute
    # 当前分钟窗口的时间戳（每 60 秒一个新窗口）
    window = int(time.time() // 60)
    key = f"rate_limit:chat:{user.tenant_id}:{user.user_id}:{window}"

    redis = Redis.from_url(settings.redis_url)
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
