"""提示词注册表服务 —— 解析生效版本 + Redis 缓存（写后失效）。

职责：
  1. resolve_prompt      取某 key 在某租户下生效的完整文本（锁定区 + 可编辑区）
  2. _invalidate         版本写操作后清缓存，下次解析走 SQL 重建
  3. diff_with_default   把某版本与内置默认做 unified diff（供 admin 预览改动）

缓存：Redis key `prompt:{tenant_id}:{key}`，TTL 60s（与 feature_flag 同构）。
★ Redis 读写失败不阻断主流程：读失败走 SQL，写失败只告警 ——
  提示词解析是问答主链路，不能因为缓存抖动就报错。
"""
from __future__ import annotations

import difflib
import logging
import uuid

from redis.asyncio import Redis
from sqlalchemy import select

from app.config.prompts import PROMPT_SPECS, build_prompt, get_spec, validate_prompt
from app.database import SessionLocal
from app.infra.redis_client import get_redis
from app.models import PromptVersion

logger = logging.getLogger(__name__)

_CACHE_KEY = "prompt:{tenant_id}:{key}"
_CACHE_TTL = 60  # 秒


async def _load_active_content(tenant_id: uuid.UUID, key: str) -> str | None:
    """读某租户某 key 的 active 版本可编辑区文本；无 active 版本返回 None。"""
    async with SessionLocal() as session:
        return (
            await session.execute(
                select(PromptVersion.content).where(
                    PromptVersion.tenant_id == tenant_id,
                    PromptVersion.prompt_key == key,
                    PromptVersion.status == "active",
                )
            )
        ).scalar_one_or_none()


async def resolve_prompt(key: str, tenant_id: uuid.UUID | None = None) -> str:
    """返回 key 生效的完整文本（锁定区 + 可编辑区）。

    tenant_id 为 None（无租户上下文）时直接返回内置默认。
    ★ 返回前 MUST 校验：DB 内容损坏则抛 PromptInvalidError，
      而不是静默用一个把约束改没了的文案（fail-closed）。
    """
    if tenant_id is None:
        return build_prompt(key, None)

    cache_key = _CACHE_KEY.format(tenant_id=tenant_id, key=key)
    redis: Redis | None = None
    try:
        redis = await get_redis()
        cached = await redis.get(cache_key)
        if cached:
            return cached.decode() if isinstance(cached, bytes) else cached
    except Exception:
        logger.warning("Redis 提示词缓存读失败，走 SQL", exc_info=True)
    finally:
        if redis is not None:
            try:
                await redis.aclose()
            except Exception:
                pass

    content = await _load_active_content(tenant_id, key)
    validate_prompt(key, content)  # fail-closed：DB 内容必须合规
    built = build_prompt(key, content)

    redis = None
    try:
        redis = await get_redis()
        await redis.set(cache_key, built, ex=_CACHE_TTL)
    except Exception:
        logger.warning("Redis 提示词缓存写失败（不影响解析）", exc_info=True)
    finally:
        if redis is not None:
            try:
                await redis.aclose()
            except Exception:
                pass
    return built


async def _invalidate(tenant_id: uuid.UUID, key: str) -> None:
    """版本写操作后清缓存，下次解析走 SQL 重建。"""
    redis: Redis | None = None
    try:
        redis = await get_redis()
        await redis.delete(_CACHE_KEY.format(tenant_id=tenant_id, key=key))
    except Exception:
        logger.warning("Redis 提示词缓存清理失败（不影响写操作）", exc_info=True)
    finally:
        if redis is not None:
            try:
                await redis.aclose()
            except Exception:
                pass


async def resolve_versions(tenant_id: uuid.UUID | None = None) -> dict[str, int]:
    """返回各 key 当前生效的版本号映射，供口径快照 / 审计留痕。

    无 active 版本的 key 记 0（表示用内置默认）。tenant_id 为 None 时全部记 0。
    """
    if tenant_id is None:
        return {key: 0 for key in PROMPT_SPECS}
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(PromptVersion.prompt_key, PromptVersion.version).where(
                    PromptVersion.tenant_id == tenant_id,
                    PromptVersion.status == "active",
                )
            )
        ).all()
    active = dict(rows)
    return {key: active.get(key, 0) for key in PROMPT_SPECS}


def diff_with_default(key: str, content: str | None) -> str:
    """把某版本可编辑区与内置默认做 unified diff（供 admin 预览改动）。

    content 为 None 表示与内置默认比（结果为空 diff）。
    """
    default_built = build_prompt(key, None)
    target_built = build_prompt(key, content)
    diff = difflib.unified_diff(
        default_built.splitlines(),
        target_built.splitlines(),
        fromfile="内置默认",
        tofile=get_spec(key).label,
        lineterm="",
    )
    return "\n".join(diff)