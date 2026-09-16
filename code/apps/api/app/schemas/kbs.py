from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class KnowledgeBaseOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None = None
    is_public: bool
    doc_count: int = 0


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = None
    is_public: bool = False


class KnowledgeBaseDetail(KnowledgeBaseOut):
    created_at: datetime
