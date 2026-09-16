"""文档详情 + 删除 API（手册 §5.1 M2）。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user, get_db
from app.models import Chunk, Document
from app.schemas.documents import ChunkPreview, DocumentDetail, DocumentOut
from app.services.storage import get_storage

router = APIRouter(tags=["documents"])


@router.get("/documents/{doc_id}", response_model=DocumentDetail)
async def get_document(
    doc_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> DocumentDetail:
    doc = await session.get(Document, doc_id)
    if doc is None or doc.tenant_id != user.tenant_id or doc.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="NOT_FOUND")

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


@router.delete("/documents/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    doc_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> None:
    doc = await session.get(Document, doc_id)
    if doc is None or doc.tenant_id != user.tenant_id or doc.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="NOT_FOUND")

    # 软删 document
    doc.deleted_at = datetime.now(timezone.utc)
    doc.is_latest = False
    # 软删 chunks：is_latest=false（不物理删，保留历史回答引用可读）
    await session.execute(
        update(Chunk).where(Chunk.document_id == doc_id).values(is_latest=False)
    )
    await session.commit()

    # 清理对象存储（best-effort）
    try:
        storage = await get_storage()
        await storage.delete_object(doc.storage_key)
    except Exception:
        pass
