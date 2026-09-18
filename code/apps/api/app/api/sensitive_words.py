"""敏感词管理 Admin API（M6 续篇）。

  GET    /api/admin/sensitive-words           分页列表（支持模糊搜）
  POST   /api/admin/sensitive-words           新增单个
  POST   /api/admin/sensitive-words/batch    批量新增（按行/逗号分隔）
  DELETE /api/admin/sensitive-words/{id}     删除单条

写操作后清 Redis 缓存，下次查询走 SQL 重建。
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user, get_db
from app.models import SensitiveWord
from app.schemas.sensitive import (
    SensitiveWordBatchCreate,
    SensitiveWordBatchResponse,
    SensitiveWordCreate,
    SensitiveWordOut,
)
from app.services import sensitive as sensitive_service

router = APIRouter(prefix="/admin", tags=["admin"])


def _require_admin(user: CurrentUser) -> None:
    if user.clearance < 40:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="FORBIDDEN")


@router.get("/sensitive-words", response_model=list[SensitiveWordOut])
async def list_sensitive_words(
    keyword: str | None = Query(None, max_length=100, description="模糊搜词"),
    category: str | None = Query(None, max_length=50),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> list[SensitiveWordOut]:
    """分页列出敏感词（默认 50/页，最多 500）。"""
    _require_admin(user)
    stmt = select(SensitiveWord).where(SensitiveWord.tenant_id == user.tenant_id)
    if keyword:
        stmt = stmt.where(SensitiveWord.word.ilike(f"%{keyword}%"))
    if category:
        stmt = stmt.where(SensitiveWord.category == category)
    stmt = stmt.order_by(SensitiveWord.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    rows = (await session.execute(stmt)).scalars().all()
    return [
        SensitiveWordOut(
            id=r.id,
            word=r.word,
            category=r.category,
            created_by=r.created_by,
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.post("/sensitive-words", response_model=SensitiveWordOut, status_code=status.HTTP_201_CREATED)
async def create_sensitive_word(
    payload: SensitiveWordCreate,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> SensitiveWordOut:
    """新增单个敏感词。重复词 → 409（同租户内 word 唯一）。"""
    _require_admin(user)
    word = payload.word.strip().lower()
    if not word:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "敏感词不可为空")
    # 唯一性检查
    dup = (
        await session.execute(
            select(SensitiveWord).where(
                SensitiveWord.tenant_id == user.tenant_id,
                SensitiveWord.word == word,
            )
        )
    ).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "敏感词已存在")
    row = SensitiveWord(
        tenant_id=user.tenant_id,
        word=word,
        category=payload.category,
        created_by=user.user_id,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    await sensitive_service._invalidate_cache(user.tenant_id)
    return SensitiveWordOut(
        id=row.id,
        word=row.word,
        category=row.category,
        created_by=row.created_by,
        created_at=row.created_at,
    )


@router.post("/sensitive-words/batch", response_model=SensitiveWordBatchResponse)
async def batch_create_sensitive_words(
    payload: SensitiveWordBatchCreate,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> SensitiveWordBatchResponse:
    """批量新增敏感词。words 列表里每个元素做去重 + 小写归一化。

    返回 added（新增条数）+ duplicates_skipped（同租户已存在跳过的条数）。
    """
    _require_admin(user)
    # 归一化：去前后空白 + 小写 + 去重
    candidates: list[str] = []
    seen: set[str] = set()
    for raw in payload.words:
        # 支持传入"逗号或换行分隔的整段文本"，这里做拆分
        for piece in raw.replace("\n", ",").split(","):
            w = piece.strip().lower()
            if not w or w in seen:
                continue
            seen.add(w)
            candidates.append(w)

    if not candidates:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "无可新增的敏感词")

    # 一次查询拿全部已存在，避免 N+1
    existing = set(
        (
            await session.execute(
                select(SensitiveWord.word).where(
                    SensitiveWord.tenant_id == user.tenant_id,
                    SensitiveWord.word.in_(candidates),
                )
            )
        ).scalars().all()
    )

    added = 0
    duplicates_skipped = 0
    for w in candidates:
        if w in existing:
            duplicates_skipped += 1
            continue
        session.add(
            SensitiveWord(
                tenant_id=user.tenant_id,
                word=w,
                category=payload.category,
                created_by=user.user_id,
            )
        )
        existing.add(w)
        added += 1

    await session.commit()
    await sensitive_service._invalidate_cache(user.tenant_id)
    return SensitiveWordBatchResponse(added=added, duplicates_skipped=duplicates_skipped)


@router.delete("/sensitive-words/{word_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sensitive_word(
    word_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> None:
    """删除单条敏感词。"""
    _require_admin(user)
    row = await session.get(SensitiveWord, word_id)
    if row is None or row.tenant_id != user.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="NOT_FOUND")
    await session.delete(row)
    await session.commit()
    await sensitive_service._invalidate_cache(user.tenant_id)
