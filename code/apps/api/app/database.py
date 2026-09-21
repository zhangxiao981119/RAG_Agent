from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.config.settings import get_settings

settings = get_settings()
# asyncpg 默认 pool_size=5 且无回收，生产环境长连接会被 PG idle_timeout 切断
# 这里显式配置：pool_size=10 + max_overflow=10 扛并发，pool_recycle=1800s 定时回收
engine: AsyncEngine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=10,
    pool_recycle=1800,  # 30 分钟主动回收，避免 PG idle_timeout 切断
    pool_timeout=30,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
