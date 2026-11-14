"""企业应用集成接口的请求与响应模型。"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class IntegrationApplicationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    kb_ids: list[uuid.UUID] = Field(min_length=1)
    rate_limit_per_minute: int = Field(default=60, ge=1, le=10_000)
    daily_token_limit: int = Field(default=200_000, ge=1)
    daily_message_limit: int = Field(default=1_000, ge=1)


class IntegrationApplicationOut(BaseModel):
    id: uuid.UUID
    name: str
    kb_ids: list[uuid.UUID]
    status: str
    rate_limit_per_minute: int
    daily_token_limit: int
    daily_message_limit: int
    created_at: datetime
    disabled_at: datetime | None = None


class IntegrationApplicationCreated(IntegrationApplicationOut):
    # 仅此创建响应包含明文密钥；列表和详情永远不返回该字段。
    application_key: str


class IntegrationAskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1)
    kb_ids: list[uuid.UUID] = Field(min_length=1)
    conversation_id: uuid.UUID | None = None


class IntegrationSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    kb_ids: list[uuid.UUID] = Field(min_length=1)


class IntegrationAskResponse(BaseModel):
    conversation_id: uuid.UUID | None
    message_id: uuid.UUID | None
    answer: str
    citations: list[dict]
    refused: bool
    refuse_reason: str | None = None
    message: str | None = None
    usage: dict
    suggestions: list[str]


class IntegrationSearchItem(BaseModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    kb_id: uuid.UUID
    filename: str
    content: str
    heading_path: str
    page_no: int | None
    score: float


class IntegrationSearchResponse(BaseModel):
    items: list[IntegrationSearchItem]
