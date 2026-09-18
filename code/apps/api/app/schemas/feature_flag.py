"""特性开关 schema。M6 续篇 — 按部门灰度，关闭即回滚。"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class FeatureFlagOut(BaseModel):
    """GET 响应项。"""

    id: uuid.UUID
    tenant_id: uuid.UUID
    feature_key: str
    dept_path_pattern: str
    enabled: bool
    rollout_percent: int
    created_at: datetime
    updated_at: datetime


class FeatureFlagCreate(BaseModel):
    """POST 请求：新增一条灰度规则。"""

    feature_key: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    dept_path_pattern: str = Field(min_length=1, max_length=500)
    enabled: bool = False
    rollout_percent: int = Field(default=100, ge=0, le=100)


class FeatureFlagUpdate(BaseModel):
    """PATCH 请求：用 model_dump(exclude_unset=True) 只取实际传入字段。"""

    enabled: bool | None = None
    rollout_percent: int | None = Field(default=None, ge=0, le=100)
    dept_path_pattern: str | None = Field(default=None, min_length=1, max_length=500)
