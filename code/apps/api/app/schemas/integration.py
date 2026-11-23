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
    # 场景绑定（Step 4）：null = 跟随租户内置场景（= 平台默认）。
    # 没有这个字段，W7 的结果在响应里不可观测（绑定成功与没绑定看起来一样）。
    scenario_id: uuid.UUID | None = None


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
    # 本次请求的口径快照（**纯增量，只增不改**，旧客户端忽略未知键即可）。
    # Step 2 只有 4 个键，见 app/api/integrations.py::_scenario_meta。
    meta: dict | None = None


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
    # 同 IntegrationAskResponse.meta
    meta: dict | None = None
