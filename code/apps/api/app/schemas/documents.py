from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel


class DocumentOut(BaseModel):
    id: uuid.UUID
    kb_id: uuid.UUID
    filename: str
    ext: str
    size_bytes: int
    status: str
    version: int
    level_rank: int
    uploaded_at: datetime


class ChunkPreview(BaseModel):
    id: uuid.UUID
    chunk_index: int
    content: str
    heading_path: str | None
    page_no: int | None
    token_count: int


class DocumentDetail(DocumentOut):
    chunks: list[ChunkPreview] = []
