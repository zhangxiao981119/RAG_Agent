"""共享 Redis 客户端 —— 连接池复用（进程级单例）。

## 为什么必须共享

原写法在每个调用点都是：

```python
redis = Redis.from_url(settings.redis_url)   # 新建连接池
try:
    ...
finally:
    await redis.aclose()                     # 用完全部关掉
```

低并发看不出来。**高并发下会出三类问题**：

1. **耗尽连接数** —— 每个并发请求各持一个池，Redis `maxclients`（默认 10000）
   被迅速吃满；每个连接还占 fd 与内存。
2. **建连/断连开销随 QPS 线性增长** —— 建连要 TCP 握手 + 认证，
   这部分开销不产生任何业务价值。
3. **保护机制自己先挂** —— 限流（`rate_limit.py`）就是用 Redis 实现的。
   它先挂掉，等于在高并发时**先失去保护**。

本项目 `main.py` 里 arq 已经是池化写法（注释「避免每次入队 create_pool + close」），
Redis 侧却没有 —— 本模块补齐这个不对称。

## 用法契约

```python
from app.infra.redis_client import get_redis

redis = await get_redis()      # 进程内共享；可放心反复调用
await redis.incr(key)          # 正常使用
```

> ★ **调用方 MUST NOT 调用 `aclose()`。**
> 连接池是进程级共享资源，任何调用点关掉它，都会让后续所有请求重新建连 ——
> 等于把池化退化回「每次新建」，只是故障延后到下一个请求。
> 释放统一由 `close_redis()` 在应用关闭时执行（见 `main.py` 的 lifespan）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from redis.asyncio import Redis

from app.config.settings import get_settings

logger = logging.getLogger(__name__)

_client: _SharedRedis | None = None
_lock = asyncio.Lock()


class _SharedRedis:
    """共享客户端的包装 —— 转发一切，唯独 `aclose()` / `close()` 是 no-op。

    ## 存在的理由

    调用方原本普遍是这种形态：

    ```python
    redis = Redis.from_url(settings.redis_url)
    try:
        ...
    finally:
        await redis.aclose()      # 请求级关闭
    ```

    要池化就必须去掉这个 `aclose()`。但**直接删 finally 块会改动 30+ 处控制流**
    （有的 try 没有 except，删掉 finally 会留下裸 try —— 直接语法错），
    重写缩进的风险远大于收益。

    包一层则只需替换 `Redis.from_url(...)` 一处，`aclose()` 变成无害操作：

    ```python
    redis = await get_redis()     # 拿到 _SharedRedis
    await redis.incr(key)         # 正常转发
    await redis.aclose()          # no-op，池不受影响
    ```

    ★ 这**不是**为了兼容而妥协：请求级调用点本来就不该关闭进程级共享资源。
      把 `aclose()` 定义成 no-op，恰好把「不该做的事」变成「做了也没关系」。
    """

    __slots__ = ("_client",)

    def __init__(self, client: Redis) -> None:
        self._client = client

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    async def aclose(self, *args: Any, **kwargs: Any) -> None:
        """no-op —— 连接池为进程级共享，不在此处关闭。"""
        return None

    async def close(self, *args: Any, **kwargs: Any) -> None:
        """`aclose` 的别名，同样是 no-op。"""
        return None

    async def _close_pool(self) -> None:
        """**仅供 `close_redis()` 调用** —— 真正关闭底层连接池。"""
        await self._client.aclose()


async def get_redis() -> Redis:
    """返回共享 Redis 客户端的包装（内部持有进程级连接池）。

    首次调用建连，之后复用。并发的首次调用由锁保护 ——
    不加锁会在冷启动瞬间建出多个池（双检锁的标准写法）。

    返回值类型标注为 `Redis`，运行时是 `_SharedRedis`（行为一致，仅 `aclose` 为 no-op）。
    """
    global _client
    if _client is None:
        async with _lock:
            if _client is None:
                settings = get_settings()
                _client = _SharedRedis(
                    Redis.from_url(
                        settings.redis_url,
                        max_connections=settings.redis_max_connections,
                        socket_connect_timeout=settings.redis_socket_timeout_s,
                        socket_timeout=settings.redis_socket_timeout_s,
                        health_check_interval=30,
                    )
                )
                logger.info(
                    "redis.client.init",
                    extra={"max_connections": settings.redis_max_connections},
                )
    return _client  # type: ignore[return-value]


async def close_redis() -> None:
    """应用关闭时释放连接池。

    **这是全项目唯一允许 `aclose()` 的地方。**
    """
    global _client
    if _client is None:
        return
    try:
        await _client._close_pool()
        logger.info("redis.client.closed")
    except Exception:
        logger.warning("redis.client.close_failed", exc_info=True)
    finally:
        _client = None


def reset_for_tests() -> None:
    """仅供单测使用：丢弃当前客户端引用（不关闭，避免影响其他测试）。"""
    global _client
    _client = None
