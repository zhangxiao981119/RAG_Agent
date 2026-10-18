from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel


class JobOut(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    status: str
    attempts: int
    max_attempts: int
    # 方案 C 分段 OCR 进度：非扫描件恒为 0/0；扫描件 pages_total=文档页数、
    # pages_done=已完成 OCR 的页数（长文档解析期间供前端展示进度）
    pages_done: int
    pages_total: int
    last_error: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
