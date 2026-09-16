from __future__ import annotations

import asyncio

import httpx
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config.settings import Settings
from app.schemas.health import DependencyStatus, HealthResponse


async def _check_database(engine: AsyncEngine) -> DependencyStatus:
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return DependencyStatus(status="ok")
    except Exception as exc:  # noqa: BLE001 - 健康检查必须捕获所有异常以报告状态
        return DependencyStatus(status="error", detail=type(exc).__name__)


async def _check_vector(engine: AsyncEngine) -> DependencyStatus:
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')")
            )
        if result.scalar_one():
            return DependencyStatus(status="ok")
        return DependencyStatus(status="error", detail="vector extension is not installed")
    except Exception as exc:  # noqa: BLE001
        return DependencyStatus(status="error", detail=type(exc).__name__)


async def _check_redis(redis_url: str) -> DependencyStatus:
    client = Redis.from_url(redis_url)
    try:
        if await client.ping():
            return DependencyStatus(status="ok")
        return DependencyStatus(status="error", detail="PING returned false")
    except Exception as exc:  # noqa: BLE001
        return DependencyStatus(status="error", detail=type(exc).__name__)
    finally:
        await client.aclose()


async def _check_model(base_url: str | None, api_key: str = "") -> DependencyStatus:
    if not base_url:
        return DependencyStatus(status="not_configured", detail="未配置模型端点")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            response = await client.get(f"{base_url.rstrip('/')}/models", headers=headers)
            response.raise_for_status()
        return DependencyStatus(status="ok")
    except Exception as exc:  # noqa: BLE001
        return DependencyStatus(status="error", detail=type(exc).__name__)


async def collect_health(engine: AsyncEngine, settings: Settings) -> HealthResponse:
    names = ("db", "redis", "vector", "llm", "embedding", "rerank")
    checks = await asyncio.gather(
        _check_database(engine),
        _check_redis(settings.redis_url),
        _check_vector(engine),
        _check_model(settings.llm_base_url, settings.llm_api_key),
        _check_model(settings.embedding_base_url),
        _check_model(settings.rerank_base_url),
    )
    dependencies = dict(zip(names, checks, strict=True))
    required = (dependencies["db"], dependencies["redis"], dependencies["vector"])
    configured_models = [dependencies[name] for name in ("llm", "embedding", "rerank") if dependencies[name].status != "not_configured"]
    healthy = all(item.status == "ok" for item in (*required, *configured_models))
    return HealthResponse(status="ok" if healthy else "error", dependencies=dependencies)
