from __future__ import annotations

import uuid
from typing import Literal, Union

from pydantic import BaseModel


class ChatAskRequest(BaseModel):
    kb_ids: list[uuid.UUID]
    question: str
    conversation_id: uuid.UUID | None = None


class Citation(BaseModel):
    n: int
    chunk_id: uuid.UUID
    doc_id: uuid.UUID
    filename: str
    heading_path: str | None = None
    page_no: int | None = None
    score: float


class MetaEvent(BaseModel):
    conversation_id: uuid.UUID
    message_id: uuid.UUID
    stage: str


class StageEvent(BaseModel):
    stage: str
    ms: int


class CitationsEvent(BaseModel):
    citations: list[Citation]


class DeltaEvent(BaseModel):
    text: str


class DoneEvent(BaseModel):
    finish_reason: str
    grounding: dict
    usage: dict


class RefusedEvent(BaseModel):
    reason: str
    message: str


ChatEvent = Union[
    MetaEvent,
    StageEvent,
    CitationsEvent,
    DeltaEvent,
    DoneEvent,
    RefusedEvent,
]
