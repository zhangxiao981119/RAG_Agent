from __future__ import annotations

import uuid
from typing import Union

from pydantic import BaseModel, model_validator


class ImageRef(BaseModel):
    """聊天带图提问的图片引用（M7 WU-M-06），`storage_key` / `data_url` 二选一。

    · data_url：内联 data URL（`data:image/png;base64,...`），前端粘贴 / 上传时携带
    · storage_key：引用既有对象存储中的图片对象，按 key 的扩展名补 MIME

    ★ 二者互斥且 MUST 恰有其一（总规范 §0.5：MUST NOT 发明默认值）——
      缺失或同时给出都无法确定图片来源，直接 422 拒绝。
    """

    storage_key: str | None = None
    data_url: str | None = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> ImageRef:
        if bool(self.storage_key) == bool(self.data_url):
            raise ValueError("ImageRef MUST 恰含 storage_key / data_url 之一")
        return self


class ChatAskRequest(BaseModel):
    kb_ids: list[uuid.UUID]
    question: str
    conversation_id: uuid.UUID | None = None
    # 带图提问（M7 WU-M-07）：默认空 = 纯文本形态
    # 数量 / 单图大小校验在端点层做（规范要求超限返 400，pydantic 校验失败是 422）
    images: list[ImageRef] = []


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
