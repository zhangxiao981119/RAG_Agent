"""文档详情 + 原文回链 + 删除 API（手册 §5.1 M2 + M7 WU-M-19）。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user, get_db
from app.models import Chunk, Document
from app.schemas.documents import (
    ChunkPreview,
    DocumentDetail,
    ParsePreviewBlock,
    ParsePreviewResponse,
    ParsePreviewSource,
)
# 预览提取服务（所有上传格式统一转纯文本）
from app.services import audit
from app.services.parse.image_parser import IMAGE_EXTS
from app.services.preview import PreviewError, extract_preview_text
from app.services.storage import get_storage

router = APIRouter(tags=["documents"])


async def _get_visible_document(
    doc_id: uuid.UUID,
    user: CurrentUser,
    session: AsyncSession,
) -> Document:
    """按租户 + G2 库级授权 + G1 密级三重校验取文档，不通过则抛 403/404。"""
    doc = await session.get(Document, doc_id)
    if doc is None or doc.tenant_id != user.tenant_id or doc.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="NOT_FOUND")
    # G2：库级授权（kb_id ∈ authorized_kb_ids），admin 豁免
    if user.clearance < 40 and doc.kb_id not in user.authorized_kb_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="FORBIDDEN")
    # G1：密级（文档 level_rank 不得超过用户 clearance）
    if doc.level_rank > user.clearance:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="FORBIDDEN")
    return doc


@router.get("/documents/{doc_id}", response_model=DocumentDetail)
async def get_document(
    doc_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> DocumentDetail:
    doc = await _get_visible_document(doc_id, user, session)

    # chunk 预览（前 20 条）
    stmt = (
        select(Chunk)
        .where(Chunk.document_id == doc_id)
        .order_by(Chunk.chunk_index)
        .limit(20)
    )
    chunks = (await session.execute(stmt)).scalars().all()
    return DocumentDetail(
        id=doc.id,
        kb_id=doc.kb_id,
        filename=doc.filename,
        ext=doc.ext,
        size_bytes=doc.size_bytes,
        status=doc.status,
        version=doc.version,
        level_rank=doc.level_rank,
        uploaded_at=doc.created_at,
        chunks=[
            ChunkPreview(
                id=c.id,
                chunk_index=c.chunk_index,
                content=c.content,
                heading_path=c.heading_path,
                page_no=c.page_no,
                token_count=c.token_count,
            )
            for c in chunks
        ],
    )


def _content_disposition(filename: str, disposition: str = "inline") -> str:
    """RFC 6266：非 ASCII 文件名必须用 filename*=UTF-8''urlencoded 形式，
    直接把中文塞 filename= 会导致 httpx/uvicorn latin-1 编码炸掉。"""
    ascii_fallback = filename.encode("ascii", "replace").decode("ascii")
    utf8_encoded = quote(filename)
    return f"{disposition}; filename=\"{ascii_fallback}\"; filename*=UTF-8''{utf8_encoded}"


@router.get("/documents/{doc_id}/raw")
async def get_document_raw(
    doc_id: uuid.UUID,
    page: int | None = Query(default=None, ge=1),
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> Response:
    """文档原文回链（M7 WU-M-19 / INV-M-16）。

    按文档形态分流（显式信号 = doc.ext，INV-M-01 同源口径）：
      · 图片扩展 → 流式转发对象原始字节，Content-Type 用对象存储记录值
        （前端 <img>/引用回链直接消费相对路径）
      · PDF 带 ?page=N → pymupdf 渲染该页为 PNG（扫描件逐页回链，INV-M-05）
      · 其余（含不带 page 的 PDF）→ 维持提取纯文本（文档列表预览的现行行为）
    ★ 响应只含对象字节 / 渲染结果，不出现 S3_ENDPOINT / 签名串 ——
      MinIO 直连地址与 presigned URL 一律不出网（INV-M-16 红线）。
    """
    doc = await _get_visible_document(doc_id, user, session)

    # ── ① 图片：流式转发原始字节 ──────────────────────────────
    if doc.ext.lower() in IMAGE_EXTS:
        storage = await get_storage()
        byte_stream, content_type = await storage.open_object(doc.storage_key)
        return StreamingResponse(
            byte_stream,
            media_type=content_type,
            headers={"Content-Disposition": _content_disposition(doc.filename)},
        )

    # ── ② PDF 指定页：渲染该页 PNG（扫描件/文本层 PDF 同样支持逐页回链）──
    if doc.ext.lower() == "pdf" and page is not None:
        storage = await get_storage()
        data = await storage.get_object(doc.storage_key)
        try:
            import pymupdf

            with pymupdf.open(stream=data, filetype="pdf") as pdf:
                if page > pdf.page_count:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="PAGE_OUT_OF_RANGE",
                    )
                png = pdf[page - 1].get_pixmap(dpi=150).tobytes("png")
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail=f"PDF 打开失败: {exc}",
            ) from exc
        return Response(
            content=png,
            media_type="image/png",
            headers={
                "Content-Disposition": _content_disposition(doc.filename, "inline")
            },
        )

    # ── ③ 文本系（及不带 page 的 PDF）：提取纯文本（列表预览现行行为）──
    storage = await get_storage()
    data = await storage.get_object(doc.storage_key)
    try:
        text = extract_preview_text(doc.ext, data)
    except PreviewError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=str(exc),
        )
    return Response(
        content=text,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": _content_disposition(doc.filename)},
    )


@router.get("/documents/{doc_id}/parse-preview")
async def get_document_parse_preview(
    doc_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ParsePreviewResponse:
    """解析预览：原图/原文件 + 文本化结果同屏（M7 WU-M-20 / INV-M-17）。

    ★ blocks 从 chunks 表读 —— 人工核对的对象是**真实入库被检索的**
      文本化结果，不是解析器的"此刻重放"（重放对图片/扫描件会再烧一次
      视觉调用，且与库内结果可能因口径漂移不一致）。
    ★ raw_url 一律相对路径（INV-M-16），多页扫描件逐页给 page_raw_urls。
    """
    doc = await _get_visible_document(doc_id, user, session)

    rows = (
        (await session.execute(
            select(Chunk)
            .where(
                Chunk.document_id == doc.id,
                Chunk.tenant_id == doc.tenant_id,
            )
            .order_by(Chunk.chunk_index)
        ))
        .scalars()
        .all()
    )

    blocks = [
        ParsePreviewBlock(
            index=idx,
            page_no=chunk.page_no,
            # chunks 表未落 is_table（解析链路内存信号）；按扩展名推断仅作
            # 预览样式提示 —— xlsx/xls 整文档皆为表格块，图片/扫描件的视觉
            # 转录与 pdf/docx 混合文档的内嵌表格不标（注释即边界承诺）
            is_table=doc.ext.lower() in ("xls", "xlsx"),
            text=chunk.content,
        )
        for idx, chunk in enumerate(rows)
    ]

    raw_url = f"/api/documents/{doc.id}/raw"
    if doc.ext.lower() == "pdf":
        # 扫描件每页至少一块（页级转录失败会整份 ParseError），文本层 PDF
        # 同理有页级 page_no —— max(page_no) 即有效页数
        page_count = max((c.page_no or 1) for c in rows) if rows else 1
        page_raw_urls = [f"{raw_url}?page={n}" for n in range(1, page_count + 1)]
    else:
        page_count = 1
        page_raw_urls = [raw_url]

    return ParsePreviewResponse(
        document_id=doc.id,
        modality="image" if doc.ext.lower() in IMAGE_EXTS else "text",
        source=ParsePreviewSource(
            raw_url=raw_url,
            page_count=page_count,
            page_raw_urls=page_raw_urls,
        ),
        blocks=blocks,
    )


@router.delete("/documents/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    doc_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> None:
    # G1/G2/属主三重校验，与 GET /documents/{id} 一致（避免 IDOR 越权删他人文档）
    doc = await _get_visible_document(doc_id, user, session)

    # 软删 document
    doc.deleted_at = datetime.now(timezone.utc)
    doc.is_latest = False
    # 软删 chunks：is_latest=false（不物理删，保留历史回答引用可读）
    await session.execute(
        update(Chunk).where(Chunk.document_id == doc_id).values(is_latest=False)
    )
    await session.commit()

    # 审计：删除文档（记录文件名便于追溯）
    await audit.record(
        user.tenant_id, user.user_id, "doc.delete",
        object_type="document", object_id=str(doc_id),
        detail={"filename": doc.filename, "kb_id": str(doc.kb_id)},
    )

    # 清理对象存储（best-effort）
    try:
        storage = await get_storage()
        await storage.delete_object(doc.storage_key)
    except Exception:
        pass
