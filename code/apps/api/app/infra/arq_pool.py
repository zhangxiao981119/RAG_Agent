"""共享 arq 连接池 —— 进程级单例。

## 为什么需要它

`main.py` 启动时建了全局池并挂在 `app.state.arq_pool`，请求路径
（`kbs.py` / `sync.py`）都复用它。但 **`sync_service.py` 是后台任务模块**，
拿不到 `app.state`，于是退化成每次入队都 `create_pool()` + `close()`：

```
批量同步 500 个文件  →  建 500 次连接池 + 关 500 次
```

这与 Redis 侧「每次请求 `from_url` + `aclose`」是**同一类问题**，
只是发生在 arq 池上 —— 见 `app/infra/redis_client.py` 的同类说明。

本模块提供进程内单例，让**请求路径与后台任务共用同一个池**。

## 用法契约

```python
from app.infra.arq_pool import get_arq_pool

pool = await get_arq_pool()
await pool.enqueue_job("run_parse_job", str(job_id))
```

> ★ **调用方 MUST NOT 调用 `close()`。**
> 池是进程级共享资源，任何调用点关掉它，后续所有入队都会重新建连。
> 释放统一由 `close_arq_pool()` 在进程退出时执行（见 `main.py` 的 lifespan）。
"""
from __future__ import annotations

import asyncio
import logging

from arq.connections import ArqRedis, RedisSettings, create_pool

from app.config.settings import get_settings

logger = logging.getLogger(__name__)

_pool: ArqRedis | None = None
_lock = asyncio.Lock()


async def get_arq_pool() -> ArqRedis:
    """返回进程内共享的 arq 连接池。

    首次调用建池，之后复用。并发的首次调用由锁保护 ——
    不加锁会在冷启动瞬间建出多个池（恰恰在最需要复用的时刻）。
    """
    global _pool
    if _pool is None:
        async with _lock:
            if _pool is None:
                settings = get_settings()
                _pool = await create_pool(
                    RedisSettings.from_dsn(settings.redis_url)
                )
                logger.info("arq.pool.init", extra={"pool_id": id(_pool)})
    return _pool


async def close_arq_pool() -> None:
    """进程退出时释放连接池。**这是唯一允许 `aclose()` 的地方。**"""
    global _pool
    if _pool is None:
        return
    try:
        await _pool.aclose()
        logger.info("arq.pool.closed")
    except Exception:
        logger.warning("arq.pool.close_failed", exc_info=True)
    finally:
        _pool = None


def reset_for_tests() -> None:
    """仅供单测使用：丢弃当前池引用（不关闭）。"""
    global _pool
    _pool = None
