"""特性开关 —— 按部门路径灰度 + 百分比放量，关闭即回滚功能。

匹配规则：
  · 查 feature_flags 表 where enabled=True and feature_key=? and tenant_id=?
  · 按 dept_path_pattern 倒序遍历（更具体的前缀优先：'公司/研发中心/%' > '公司/%' > '%'）
  · 用 SQL LIKE 匹配 user_dept_path（部门路径）
  · 命中后用 hash(user_id) % 100 < rollout_percent 判断百分比

缓存：
  · Redis key: feature_flag:{tenant_id}:{feature_key}，存 JSON 列表，TTL 60s
  · 写操作后 _invalidate 清缓存
  · Redis 挂掉直接走 SQL（规则 7：不静默放行）
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.database import SessionLocal
from app.models import FeatureFlag

logger = logging.getLogger(__name__)

_CACHE_KEY = "feature_flag:{tenant_id}:{feature_key}"
_CACHE_TTL = 60  # 秒


def _hash_percent(user_id: uuid.UUID) -> int:
    """把 user_id 哈希到 0-99 的整数，决定该用户是否落在灰度百分比内。

    用 SHA1 取前 8 字节 → 整数 mod 100，分布均匀且稳定（同一 user_id 永远命中同一桶）。
    """
    digest = hashlib.sha1(str(user_id).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % 100


async def _load_rules(tenant_id: uuid.UUID, feature_key: str) -> list[dict]:
    """加载租户某 feature 的全部 enabled=True 规则。

    返回 list[{"dept_path_pattern": str, "rollout_percent": int}]。
    Redis 缓存优先，未命中走 SQL。
    """
    cache_key = _CACHE_KEY.format(tenant_id=tenant_id, feature_key=feature_key)
    redis: Redis | None = None
    try:
        redis = Redis.from_url(get_settings().redis_url)
        cached = await redis.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        logger.warning("Redis feature flag 缓存读失败，走 SQL", exc_info=True)
    finally:
        if redis is not None:
            try:
                await redis.aclose()
            except Exception:
                pass

    # SQL 兜底
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(FeatureFlag.dept_path_pattern, FeatureFlag.rollout_percent)
                .where(
                    FeatureFlag.tenant_id == tenant_id,
                    FeatureFlag.feature_key == feature_key,
                    FeatureFlag.enabled.is_(True),
                )
                .order_by(FeatureFlag.dept_path_pattern.desc())  # 更具体的前缀优先
            )
        ).all()
    rules = [{"dept_path_pattern": r.dept_path_pattern, "rollout_percent": r.rollout_percent} for r in rows]

    # 写回缓存（即使为空也写，避免缓存穿透）
    if rules:
        redis = None
        try:
            redis = Redis.from_url(get_settings().redis_url)
            await redis.set(cache_key, json.dumps(rules), ex=_CACHE_TTL)
        except Exception:
            logger.warning("Redis feature flag 缓存写失败（不影响查询）", exc_info=True)
        finally:
            if redis is not None:
                try:
                    await redis.aclose()
                except Exception:
                    pass
    return rules


async def _invalidate(tenant_id: uuid.UUID, feature_key: str) -> None:
    """写操作后清缓存，下次查询走 SQL 重建。"""
    redis = None
    try:
        redis = Redis.from_url(get_settings().redis_url)
        await redis.delete(_CACHE_KEY.format(tenant_id=tenant_id, feature_key=feature_key))
    except Exception:
        logger.warning("Redis feature flag 缓存清理失败（不影响写操作）", exc_info=True)
    finally:
        if redis is not None:
            try:
                await redis.aclose()
            except Exception:
                pass


def _match_pattern(dept_path: str, pattern: str) -> bool:
    """SQL LIKE 风格匹配：pattern 用 % 作通配符，dept_path 是用户实际部门路径。

    例：dept_path='/公司/研发中心/前端组'，pattern='公司/研发中心/%' → 命中
       dept_path='/公司/行政部门'，pattern='公司/研发中心/%' → 不命中
       pattern='%' → 任意路径都命中
    """
    if not pattern:
        return False
    # 转义 SQL LIKE 的特殊字符 _，然后 % 替换为正则 .*，构建正则
    import re
    # 转义正则元字符（_ 在 SQL LIKE 是单字符通配，但这里我们只支持 %，把 _ 当字面量）
    regex = re.escape(pattern).replace(r"\%", ".*").replace(r"\_", "_")
    return re.fullmatch(regex, dept_path) is not None or re.match(
        # 支持前后 % 都有的常见模式
        "^" + regex + "$",
        dept_path,
    ) is not None


async def is_enabled(
    tenant_id: uuid.UUID,
    feature_key: str,
    user_dept_path: str,
    user_id: uuid.UUID,
) -> bool:
    """判断某 feature 对当前用户是否生效。

    流程：
      1. 加载该 feature 的 enabled=True 规则列表（按 pattern 倒序，更具体优先）
      2. 按 user_dept_path 找到第一条匹配的规则
      3. 该规则再用 rollout_percent 决定用户是否落在灰度桶内
    """
    rules = await _load_rules(tenant_id, feature_key)
    if not rules:
        return False
    for rule in rules:
        if _match_pattern(user_dept_path, rule["dept_path_pattern"]):
            # 命中部门，再判百分比
            if _hash_percent(user_id) < rule["rollout_percent"]:
                return True
            # 命中部门但百分比外，停止匹配（按部门粒度生效一次）
            return False
    return False
