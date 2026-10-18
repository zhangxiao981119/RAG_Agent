from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.config.settings import get_settings

settings = get_settings()
# asyncpg 默认 pool_size=5 且无回收，生产环境长连接会被 PG idle_timeout 切断
# 这里显式配置：池容量 + timer 回收。
#
# ★ 池容量不是"越大越好"，也不是拍脑袋定的 —— 见 settings.db_pool_size 的实测注释：
#   pool 20 时 c=20 并发直接雪崩（47% 失败、QueuePool timeout）。
#   根因是**每个在途问答请求长期占 1 条连接**（get_db 的 yield 覆盖整条 SSE 流），
#   流式过程中还会瞬时再开 1 条，所以 20 并发实际需要 20~40 条。
# ★ pool_timeout 必须短：过载时要快速 503，而不是挂满 30s 再 500。
engine: AsyncEngine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_recycle=1800,  # 30 分钟主动回收，避免 PG idle_timeout 切断
    pool_timeout=settings.db_pool_timeout_s,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
