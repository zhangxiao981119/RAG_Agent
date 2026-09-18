"""敏感词过滤 —— 输入/输出双向命中检测，命中即拒答。

与 PII 脱敏职责不同：
  · PII 脱敏（mask.py）：对隐私数据打码，保留语义
  · 敏感词过滤：政策性禁用词，直接拒答（走 refused 事件）

归一化处理（防止绕过）：
  · NFKC：全角半角、组合字符统一
  · 去零宽字符 U+200B/U+200C/U+200D/U+FEFF
  · 去控制字符（除 \n）
  · 全小写匹配

★ Redis 挂了不能放行（规则 7：消除触发场景），直接走 SQL 查。
"""
from __future__ import annotations

import logging
import re
import unicodedata
import uuid

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.database import SessionLocal
from app.models import SensitiveWord

logger = logging.getLogger(__name__)

# Redis 缓存 key：JSON 列表，TTL 300s
_CACHE_KEY = "sensitive_words:{tenant_id}"
_CACHE_TTL = 300

# 零宽字符 + BOM
_ZERO_WIDTH_RE = re.compile(r"[\u200B\u200C\u200D\uFEFF\u2060]")
# 控制字符（保留 \n\t）
_CONTROL_RE = re.compile(r"[\x00-\x08\x0B-\x1F\x7F]")


def _normalize(text: str) -> str:
    """归一化文本：NFKC + 去零宽 + 去控制 + 小写。

    用于匹配前的输入处理，防止 Unicode 同形字、零宽字符等绕过。
    """
    if not text:
        return ""
    # 全角半角统一（NFKC 把 ０１２ＡＢＣ 变成 012ABC）
    normalized = unicodedata.normalize("NFKC", text)
    # 去零宽字符
    normalized = _ZERO_WIDTH_RE.sub("", normalized)
    # 去控制字符
    normalized = _CONTROL_RE.sub("", normalized)
    return normalized.lower()


async def _load_words(tenant_id: uuid.UUID) -> set[str]:
    """加载租户的全部敏感词。Redis 缓存优先，未命中走 SQL。"""
    redis: Redis | None = None
    try:
        redis = Redis.from_url(get_settings().redis_url)
        cached = await redis.get(_CACHE_KEY.format(tenant_id=tenant_id))
        if cached:
            # 缓存里存的是 JSON 数组，set 化
            import json
            return set(json.loads(cached))
    except Exception:
        logger.warning("Redis 敏感词缓存读失败，走 SQL", exc_info=True)
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
                select(SensitiveWord.word).where(SensitiveWord.tenant_id == tenant_id)
            )
        ).scalars().all()
    words = {w.lower() for w in rows}

    # 写回缓存
    if words:
        redis = None
        try:
            redis = Redis.from_url(get_settings().redis_url)
            import json
            await redis.set(
                _CACHE_KEY.format(tenant_id=tenant_id),
                json.dumps(list(words)),
                ex=_CACHE_TTL,
            )
        except Exception:
            logger.warning("Redis 敏感词缓存写失败（不影响查询）", exc_info=True)
        finally:
            if redis is not None:
                try:
                    await redis.aclose()
                except Exception:
                    pass
    return words


async def _invalidate_cache(tenant_id: uuid.UUID) -> None:
    """写操作后清缓存，下次查询走 SQL 重建。"""
    redis = None
    try:
        redis = Redis.from_url(get_settings().redis_url)
        await redis.delete(_CACHE_KEY.format(tenant_id=tenant_id))
    except Exception:
        logger.warning("Redis 缓存清理失败（不影响写操作）", exc_info=True)
    finally:
        if redis is not None:
            try:
                await redis.aclose()
            except Exception:
                pass


def _check_against(text: str, words: set[str]) -> str | None:
    """对归一化后的 text 做子串匹配，返回命中的词或 None。

    用 str in 而非正则：词表可能上千条，预编译正则的内存与启动开销不值。
    长文本可考虑 Aho-Corasick，当前实现优先简单。
    """
    if not words or not text:
        return None
    for word in words:
        if word in text:
            return word
    return None


async def check_input(question: str, tenant_id: uuid.UUID) -> tuple[bool, str | None]:
    """输入侧检查：用户问题命中敏感词 → 返回 (True, 命中词)。"""
    words = await _load_words(tenant_id)
    normalized = _normalize(question)
    hit = _check_against(normalized, words)
    return (hit is not None, hit)


async def check_output(text: str, tenant_id: uuid.UUID) -> tuple[bool, str | None]:
    """输出侧检查：LLM 回答命中敏感词 → 返回 (True, 命中词)。"""
    words = await _load_words(tenant_id)
    normalized = _normalize(text)
    hit = _check_against(normalized, words)
    return (hit is not None, hit)
