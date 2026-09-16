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
    last_error: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
