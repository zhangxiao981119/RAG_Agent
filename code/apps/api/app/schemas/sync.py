"""M6 任务 1：知识源同步 — pydantic schema（§8 D-07）。"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class SyncSourceCreate(BaseModel):
    """创建同步源。"""

    name: str = Field(min_length=1, max_length=100)
    source_type: str = Field(default="local", pattern="^(local|git)$")
    # local: /data/sync 下的子目录名；git: 仓库 URL
    path: str = Field(min_length=1, max_length=500)
    branch: str | None = None  # 仅 git 类型
    file_patterns: str = Field(default="*.md,*.pdf,*.txt,*.docx")
    level_rank: int = Field(default=20, ge=1, le=40)


class SyncSourceUpdate(BaseModel):
    """更新同步源（部分字段）。"""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    file_patterns: str | None = None
    level_rank: int | None = Field(default=None, ge=1, le=40)
    status: str | None = Field(default=None, pattern="^(active|paused)$")


class SyncSourceOut(BaseModel):
    """同步源详情。"""

    id: uuid.UUID
    kb_id: uuid.UUID
    name: str
    source_type: str
    path: str
    branch: str | None = None
    file_patterns: str
    level_rank: int
    status: str
    last_synced_at: datetime | None = None
    last_sync_count: int = 0
    last_error: str | None = None
    synced_file_count: int = 0  # 已同步的文件数
    created_at: datetime
    updated_at: datetime


class SyncResult(BaseModel):
    """手动触发同步的返回。"""

    source_id: uuid.UUID
    new_count: int
    message: str
