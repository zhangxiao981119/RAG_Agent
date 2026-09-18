"""配额管理 schema。M6 续篇 — 单/租户双层 token 限额。"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class TenantQuotaOut(BaseModel):
    """GET /api/quota 响应：租户级配额。"""

    tenant_id: uuid.UUID
    daily_token_limit: int
    daily_message_limit: int
    monthly_token_limit: int
    updated_at: datetime


class TenantQuotaUpdate(BaseModel):
    """PUT /api/quota 请求：修改租户配额。"""

    daily_token_limit: int = Field(ge=1)
    daily_message_limit: int = Field(ge=1)
    monthly_token_limit: int = Field(ge=1)


class QuotaUsage(BaseModel):
    """GET /api/quota/usage/{user_id} 响应：今日用量。"""

    user_id: uuid.UUID
    # 今日已用 token（用户维度 + 租户维度）
    user_tokens_today: int
    user_messages_today: int
    tenant_tokens_today: int
    tenant_tokens_this_month: int
    # 配额上限（用户层走 settings 默认；租户层从 tenant_quotas 取）
    user_daily_token_limit: int
    user_daily_message_limit: int
    tenant_daily_token_limit: int
    tenant_monthly_token_limit: int
