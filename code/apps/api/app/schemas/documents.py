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


# ── 解析预览（M7 WU-M-20 / INV-M-17，结构对齐规范 §3.5）──────────────


class ParsePreviewSource(BaseModel):
    """原图/原文件来源：raw_url 一律相对路径（INV-M-16 红线）。"""

    raw_url: str
    page_count: int
    # 多页扫描件逐页链接；单页对象（图片/文本系）只有 raw_url 一项
    page_raw_urls: list[str]


class ParsePreviewBlock(BaseModel):
    index: int
    page_no: int | None
    is_table: bool
    text: str


class ParsePreviewResponse(BaseModel):
    document_id: uuid.UUID
    # 与模态路由同源口径：image = 图片扩展入库，text = 其余
    modality: str
    source: ParsePreviewSource
    blocks: list[ParsePreviewBlock] = []
