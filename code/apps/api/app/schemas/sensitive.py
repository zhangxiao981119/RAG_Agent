"""敏感词管理 schema。M6 续篇 — 命中即拒答，DB 表 + admin 管理。"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class SensitiveWordOut(BaseModel):
    """GET /api/sensitive-words 响应项。"""

    id: uuid.UUID
    word: str
    category: str | None = None
    created_by: uuid.UUID | None = None
    created_at: datetime


class SensitiveWordCreate(BaseModel):
    """POST 请求：单个新增。"""

    word: str = Field(min_length=1, max_length=200)
    category: str | None = Field(default=None, max_length=50)


class SensitiveWordBatchCreate(BaseModel):
    """POST /api/sensitive-words/batch 请求：批量新增。

    words 允许传多个，按行或逗号分隔；服务端做去重 + 小写归一化。
    """

    words: list[str] = Field(min_length=1, max_length=500)
    category: str | None = Field(default=None, max_length=50)


class SensitiveWordBatchResponse(BaseModel):
    """批量新增响应。"""

    added: int
    duplicates_skipped: int
