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
    # ── 上传溯源（P-e）：来源类型 + 上传人展示名 ──
    # source_kind: upload=手动上传 / sync_local=目录同步 / sync_git=Git 同步 /
    # sync_web=Web 源抓取；同步/Web 来源无个人上传者，uploader_name 为 None
    source_kind: str | None = None
    uploader_name: str | None = None
    # ── 前端操作标志（P-c/P-d）：判定在后端，前端只做展示渲染 ──
    # can_delete = admin 或 库 owner 或 上传者本人；can_upload = admin 或 库 owner
    can_delete: bool = False
    can_upload: bool = False
    # ── M7 知识治理（WU-M-18）：四维分数 NULL = 未审查，weight ∈ [-50, +50] ──
    governance_status: str = "pending"
    freshness_score: int | None = None
    authority_score: int | None = None
    consistency_score: int | None = None
    completeness_score: int | None = None
    governance_report: dict = {}
    effective_at: datetime | None = None
    expire_at: datetime | None = None


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


# ── 批量上传（M7 WU-M-21 / INV-M-18）────────────────────────────────


class BatchUploadFailure(BaseModel):
    """逐文件失败结果：文件名 + 原因（便于定位是哪个文件、为何失败）。"""

    filename: str
    detail: str


class BatchUploadResponse(BaseModel):
    succeeded: list[DocumentOut] = []
    failed: list[BatchUploadFailure] = []


# ── 知识治理端点（M7 REST 清单：四维结论 + 手动重跑）────────────────


class GovernanceDetail(BaseModel):
    """单文档四维审查结论（GET /documents/{id}/governance）。"""

    document_id: uuid.UUID
    governance_status: str
    freshness_score: int | None
    authority_score: int | None
    consistency_score: int | None
    completeness_score: int | None
    # 含 dims.{freshness,authority,consistency,completeness}.basis 依据 +
    # governance_weight / reviewed_at / content_chars
    governance_report: dict = {}
    effective_at: datetime | None
    expire_at: datetime | None


class GovernanceRerunResponse(BaseModel):
    """手动重跑审查受理结果（POST /documents/{id}/governance/rerun）。"""

    document_id: uuid.UUID
    job_id: uuid.UUID
