"""提示词版本管理 schema。"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class PromptKeyOut(BaseModel):
    """某个提示词 key 的当前状态（列表页）。"""

    key: str
    label: str
    locked: str                     # 锁定区文本（只读展示，不可编辑）
    default_editable: str           # 内置默认可编辑区（供对照）
    active_version: int | None      # 当前生效版本号；None 表示用内置默认
    active_version_id: uuid.UUID | None
    is_customized: bool             # 是否已切到某个 DB 版本
    version_count: int


class PromptVersionOut(BaseModel):
    """版本列表项（不含 content，列表页保持轻量）。"""

    id: uuid.UUID
    prompt_key: str
    version: int
    status: str
    note: str | None
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class PromptVersionDetail(PromptVersionOut):
    """版本详情：额外带可编辑区 content 与拼装后的完整文本。"""

    content: str
    built: str


class PromptVersionCreate(BaseModel):
    """新建草稿版本。"""

    content: str = Field(min_length=1, max_length=20000)
    note: str | None = Field(default=None, max_length=500)


class PromptDiffOut(BaseModel):
    """某版本与内置默认的 unified diff。"""

    key: str
    version: int | None             # None 表示与内置默认自身比
    diff: str