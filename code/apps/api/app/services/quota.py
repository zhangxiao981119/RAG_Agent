"""配额管理 —— 单用户/租户双层 token 限额 + 手册第 14 步四级降级。

设计要点：
  · 速率（每分钟）由 rate_limit.py 处理，本模块只管总量（每日/每月）
  · Redis 计数器为权威，Redis 挂掉时降级走 SQL 聚合 audit_logs
    （符合规则 7：消除"Redis 挂了无法配额"场景，不静默放行）
  · 软阈值 90% 触发四级降级，按用户画像→历史→压缩→检索片段 顺序裁剪
  · 硬超限直接拒答 QUOTA_EXCEEDED（refused 事件）
  · 绝不丢当前轮的检索片段（手册硬性要求）
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import decisions
from app.config.settings import get_settings
from app.database import SessionLocal
from app.models import AuditLog, TenantQuota
from app.services.memory import compress_history
from app.services.retrieve.base import RetrievedChunk

logger = logging.getLogger(__name__)

# Redis key 模板
_K_USER_TOKENS = "quota:tokens:user:{user_id}:{date}"        # 单用户今日 token
_K_TENANT_TOKENS_DAY = "quota:tokens:tenant:{tenant_id}:{date}"  # 租户今日 token
_K_TENANT_TOKENS_MONTH = "quota:tokens:tenant_month:{tenant_id}:{month}"  # 租户本月 token
_K_USER_MSG = "quota:messages:user:{user_id}:{date}"         # 单用户今日问答次数

# TTL（秒）：36h 跨日窗口足够，月度 35 天
_TTL_DAY = 36 * 3600
_TTL_MONTH = 35 * 24 * 3600


@dataclass
class QuotaLimits:
    """配额上限集合（用户层走 settings 默认，租户层从 tenant_quotas 取）。"""

    user_daily_token: int
    user_daily_msg: int
    tenant_daily_token: int
    tenant_monthly_token: int


@dataclass
class QuotaUsage:
    """当前用量快照。"""

    user_tokens_today: int
    user_messages_today: int
    tenant_tokens_today: int
    tenant_tokens_this_month: int


@dataclass
class DegradationPlan:
    """降级方案：返回裁剪后的 history/memory_prompt/chunks。

    refused=True 表示硬超限，调用方走 refused 事件。
    degraded_from 标记本次降到哪一级（'none' / 'l1_memory' / 'l2_history' /
    'l3_compress' / 'l4_chunks'），便于审计。
    """

    refused: bool = False
    refuse_reason: str = ""
    history: list[dict] = field(default_factory=list)
    memory_prompt: str = ""
    chunks: list[RetrievedChunk] = field(default_factory=list)
    degraded_from: str = "none"


async def _get_redis() -> Redis:
    return Redis.from_url(get_settings().redis_url)


async def get_limits(tenant_id: uuid.UUID) -> QuotaLimits:
    """读取租户配额上限。tenant_quotas 无记录 → 用 settings 默认值。"""
    settings = get_settings()
    async with SessionLocal() as session:
        # SQLAlchemy 主键是 id 不是 tenant_id，用 SELECT 查 tenant_id
        row = (
            await session.execute(
                select(TenantQuota).where(TenantQuota.tenant_id == tenant_id)
            )
        ).scalar_one_or_none()
    if row is not None:
        return QuotaLimits(
            user_daily_token=settings.default_daily_token_limit,
            user_daily_msg=settings.default_daily_message_limit,
            tenant_daily_token=row.daily_token_limit,
            tenant_monthly_token=row.monthly_token_limit,
        )
    return QuotaLimits(
        user_daily_token=settings.default_daily_token_limit,
        user_daily_msg=settings.default_daily_message_limit,
        tenant_daily_token=settings.default_tenant_daily_token_limit,
        tenant_monthly_token=settings.default_tenant_monthly_token_limit,
    )


async def get_usage(tenant_id: uuid.UUID, user_id: uuid.UUID) -> QuotaUsage:
    """读取当前用量。Redis 优先，Redis 不可用 → SQL 聚合 audit_logs。

    audit_logs.action = 'chat.ask.metric' 由 chat.py 在每次回答后写入，
    detail 含 prompt_tokens / completion_tokens。
    """
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y%m%d")
    month_str = now.strftime("%Y%m")
    today_iso = now.strftime("%Y-%m-%d")
    month_start_iso = now.strftime("%Y-%m-01")

    redis = await _get_redis()
    try:
        # 先尝试 Redis
        pipe = redis.pipeline()
        pipe.get(_K_USER_TOKENS.format(user_id=user_id, date=date_str))
        pipe.get(_K_USER_MSG.format(user_id=user_id, date=date_str))
        pipe.get(_K_TENANT_TOKENS_DAY.format(tenant_id=tenant_id, date=date_str))
        pipe.get(_K_TENANT_TOKENS_MONTH.format(tenant_id=tenant_id, month=month_str))
        u_tok, u_msg, t_tok_day, t_tok_month = await pipe.execute()
        return QuotaUsage(
            user_tokens_today=int(u_tok or 0),
            user_messages_today=int(u_msg or 0),
            tenant_tokens_today=int(t_tok_day or 0),
            tenant_tokens_this_month=int(t_tok_month or 0),
        )
    except Exception:
        logger.warning("Redis 用量查询失败，降级走 SQL 聚合", exc_info=True)
    finally:
        try:
            await redis.aclose()
        except Exception:
            pass

    # SQL 聚合（兜底，不静默放行）
    async with SessionLocal() as session:
        # 今日
        today_row = (
            await session.execute(
                select(
                    func.coalesce(
                        func.sum(
                            (AuditLog.detail["prompt_tokens"].as_integer())
                            + (AuditLog.detail["completion_tokens"].as_integer())
                        ),
                        0,
                    ).label("tokens"),
                    func.count().label("cnt"),
                )
                .where(
                    AuditLog.tenant_id == tenant_id,
                    AuditLog.action == "chat.ask.metric",
                    AuditLog.created_at >= today_iso,
                )
            )
        ).one()
        # 用户今日（detail 不带 user_id，走 user_id 列；按 user_id 过滤）
        user_today_row = (
            await session.execute(
                select(
                    func.coalesce(
                        func.sum(
                            (AuditLog.detail["prompt_tokens"].as_integer())
                            + (AuditLog.detail["completion_tokens"].as_integer())
                        ),
                        0,
                    ).label("tokens"),
                    func.count().label("cnt"),
                )
                .where(
                    AuditLog.tenant_id == tenant_id,
                    AuditLog.user_id == user_id,
                    AuditLog.action == "chat.ask.metric",
                    AuditLog.created_at >= today_iso,
                )
            )
        ).one()
        # 租户本月
        month_row = (
            await session.execute(
                select(
                    func.coalesce(
                        func.sum(
                            (AuditLog.detail["prompt_tokens"].as_integer())
                            + (AuditLog.detail["completion_tokens"].as_integer())
                        ),
                        0,
                    ).label("tokens"),
                )
                .where(
                    AuditLog.tenant_id == tenant_id,
                    AuditLog.action == "chat.ask.metric",
                    AuditLog.created_at >= month_start_iso,
                )
            )
        ).one()
    return QuotaUsage(
        user_tokens_today=int(user_today_row.tokens or 0),
        user_messages_today=int(user_today_row.cnt or 0),
        tenant_tokens_today=int(today_row.tokens or 0),
        tenant_tokens_this_month=int(month_row.tokens or 0),
    )


def estimate_tokens(text: str) -> int:
    """估算文本占用 token 数（中文保守按 2 字符/token）。"""
    if not text:
        return 0
    return max(1, len(text) // decisions.QUOTA_TOKEN_ESTIMATE_CHARS_PER_TOKEN)


def _estimate_request_tokens(
    question: str,
    history: list[dict],
    memory_prompt: str,
    chunks: list[RetrievedChunk],
) -> int:
    """估算本次请求总 token 数 = question + history + memory + chunks + 预期输出 1024。"""
    total = estimate_tokens(question)
    total += estimate_tokens(memory_prompt)
    for m in history:
        total += estimate_tokens(m.get("content", ""))
    for c in chunks:
        total += estimate_tokens(c.content)
    # 预期输出 token（按手册配置上限 1024）
    total += 1024
    return total


async def _incr_usage(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    tokens: int,
) -> None:
    """回答完成后累加用量。Redis 不可用 → 不影响主流程（audit_logs 已写入 metric）。"""
    if tokens <= 0:
        return
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y%m%d")
    month_str = now.strftime("%Y%m")
    redis = await _get_redis()
    try:
        pipe = redis.pipeline()
        pipe.incrby(_K_USER_TOKENS.format(user_id=user_id, date=date_str), tokens)
        pipe.incrby(_K_USER_MSG.format(user_id=user_id, date=date_str), 1)
        pipe.incrby(_K_TENANT_TOKENS_DAY.format(tenant_id=tenant_id, date=date_str), tokens)
        pipe.incrby(_K_TENANT_TOKENS_MONTH.format(tenant_id=tenant_id, month=month_str), tokens)
        # 首次写入设 TTL
        for k in (
            _K_USER_TOKENS.format(user_id=user_id, date=date_str),
            _K_USER_MSG.format(user_id=user_id, date=date_str),
            _K_TENANT_TOKENS_DAY.format(tenant_id=tenant_id, date=date_str),
        ):
            pipe.expire(k, _TTL_DAY)
        pipe.expire(
            _K_TENANT_TOKENS_MONTH.format(tenant_id=tenant_id, month=month_str),
            _TTL_MONTH,
        )
        await pipe.execute()
    except Exception:
        logger.warning("Redis 用量累加失败（不影响主流程，audit_logs 已记录）", exc_info=True)
    finally:
        try:
            await redis.aclose()
        except Exception:
            pass


async def check_and_degrade(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    question: str,
    history: list[dict],
    memory_prompt: str,
    chunks: list[RetrievedChunk],
) -> DegradationPlan:
    """配额检查 + 四级降级。

    流程：
      1. 读 limits + usage
      2. 估算本次请求 token 数
      3. 若 usage + estimate > 硬上限 → refused=True（QUOTA_EXCEEDED）
      4. 若 usage / limit >= 90% → 按四级降级裁剪，找到最小可容纳方案
      5. 返回 DegradationPlan

    ★ 绝不丢当前轮的检索片段：L4 只裁到 top-2，不会变 0。
    """
    limits = await get_limits(tenant_id)
    usage = await get_usage(tenant_id, user_id)
    estimate = _estimate_request_tokens(question, history, memory_prompt, chunks)

    # 硬超限判定：用户日 token / 用户日消息 / 租户日 token / 租户月 token 任一+本次估算超上限
    if usage.user_tokens_today + estimate > limits.user_daily_token:
        return DegradationPlan(refused=True, refuse_reason="QUOTA_EXCEEDED_USER_DAILY_TOKEN")
    if usage.user_messages_today + 1 > limits.user_daily_msg:
        return DegradationPlan(refused=True, refuse_reason="QUOTA_EXCEEDED_USER_DAILY_MSG")
    if usage.tenant_tokens_today + estimate > limits.tenant_daily_token:
        return DegradationPlan(refused=True, refuse_reason="QUOTA_EXCEEDED_TENANT_DAILY_TOKEN")
    if usage.tenant_tokens_this_month + estimate > limits.tenant_monthly_token:
        return DegradationPlan(refused=True, refuse_reason="QUOTA_EXCEEDED_TENANT_MONTHLY_TOKEN")

    # 软阈值未触发：不降级
    user_soft = limits.user_daily_token * decisions.QUOTA_SOFT_THRESHOLD
    tenant_soft = limits.tenant_daily_token * decisions.QUOTA_SOFT_THRESHOLD
    if usage.user_tokens_today < user_soft and usage.tenant_tokens_today < tenant_soft:
        return DegradationPlan(
            refused=False,
            history=history,
            memory_prompt=memory_prompt,
            chunks=chunks,
            degraded_from="none",
        )

    # 四级降级：按成本从低到高
    # L1 用户画像置空
    if _estimate_request_tokens(question, history, "", chunks) + usage.user_tokens_today <= limits.user_daily_token:
        return DegradationPlan(
            refused=False,
            history=history,
            memory_prompt="",
            chunks=chunks,
            degraded_from="l1_memory",
        )

    # L2 历史裁剪（保留最近 2 条）
    trimmed_history = history[-2:] if len(history) > 2 else history
    if _estimate_request_tokens(question, trimmed_history, "", chunks) + usage.user_tokens_today <= limits.user_daily_token:
        return DegradationPlan(
            refused=False,
            history=trimmed_history,
            memory_prompt="",
            chunks=chunks,
            degraded_from="l2_history",
        )

    # L3 历史压缩：调 LLM 把更早的历史压缩成 system 摘要
    try:
        compressed = await compress_history(history, keep_recent=2) if len(history) > 2 else history
    except Exception:
        logger.warning("L3 历史压缩失败，跳到 L4", exc_info=True)
        compressed = trimmed_history
    if _estimate_request_tokens(question, compressed, "", chunks) + usage.user_tokens_today <= limits.user_daily_token:
        return DegradationPlan(
            refused=False,
            history=compressed,
            memory_prompt="",
            chunks=chunks,
            degraded_from="l3_compress",
        )

    # L4 检索片段裁剪（保留 top-2，绝不丢当前轮所有片段）
    trimmed_chunks = chunks[:2] if len(chunks) > 2 else chunks
    return DegradationPlan(
        refused=False,
        history=compressed,
        memory_prompt="",
        chunks=trimmed_chunks,
        degraded_from="l4_chunks",
    )
