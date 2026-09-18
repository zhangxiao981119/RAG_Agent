"""M6 任务 1：知识源同步 API（§8 D-07）。

  POST   /kbs/{kb_id}/sync-sources          创建同步源（admin）
  GET    /kbs/{kb_id}/sync-sources          列出同步源
  PATCH  /sync-sources/{id}                 更新（暂停/恢复/改配置）
  DELETE /sync-sources/{id}                  删除
  POST   /sync-sources/{id}/sync            手动触发同步
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user, get_db
from app.models import KnowledgeBase, SyncSource
from app.schemas.sync import SyncSourceCreate, SyncSourceOut, SyncSourceUpdate, SyncResult
from app.services import audit
from app.services.sync_service import sync_source

router = APIRouter(tags=["sync-sources"])


def _require_admin(user: CurrentUser) -> None:
    if "role:admin" not in user.subjects:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="FORBIDDEN: 需要管理员权限")


def _to_out(source: SyncSource) -> SyncSourceOut:
    return SyncSourceOut(
        id=source.id,
        kb_id=source.kb_id,
        name=source.name,
        source_type=source.source_type,
        path=source.path,
        branch=source.branch,
        file_patterns=source.file_patterns,
        level_rank=source.level_rank,
        status=source.status,
        last_synced_at=source.last_synced_at,
        last_sync_count=source.last_sync_count,
        last_error=source.last_error,
        synced_file_count=len(source.synced_files or {}),
        created_at=source.created_at,
        updated_at=source.updated_at,
    )


@router.post(
    "/kbs/{kb_id}/sync-sources",
    response_model=SyncSourceOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_sync_source(
    kb_id: uuid.UUID,
    body: SyncSourceCreate,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> SyncSourceOut:
    _require_admin(user)

    # 校验 KB 存在且属于当前租户
    kb = await session.get(KnowledgeBase, kb_id)
    if kb is None or kb.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="NOT_FOUND")

    source = SyncSource(
        tenant_id=user.tenant_id,
        kb_id=kb_id,
        name=body.name,
        source_type=body.source_type,
        path=body.path,
        branch=body.branch if body.source_type == "git" else None,
        file_patterns=body.file_patterns,
        level_rank=body.level_rank,
        status="active",
        synced_files={},
    )
    session.add(source)
    await session.commit()
    await session.refresh(source)

    await audit.record(
        user.tenant_id, user.user_id, "sync_source.create",
        object_type="sync_source", object_id=str(source.id),
        detail={"kb_id": str(kb_id), "name": body.name, "source_type": body.source_type},
    )
    return _to_out(source)


@router.get("/kbs/{kb_id}/sync-sources", response_model=list[SyncSourceOut])
async def list_sync_sources(
    kb_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> list[SyncSourceOut]:
    # 知识库成员即可查看同步源
    result = await session.execute(
        select(SyncSource)
        .where(SyncSource.kb_id == kb_id, SyncSource.tenant_id == user.tenant_id)
        .order_by(SyncSource.created_at)
    )
    sources = list(result.scalars().all())
    return [_to_out(s) for s in sources]


@router.patch("/sync-sources/{source_id}", response_model=SyncSourceOut)
async def update_sync_source(
    source_id: uuid.UUID,
    body: SyncSourceUpdate,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> SyncSourceOut:
    _require_admin(user)

    source = await session.get(SyncSource, source_id)
    if source is None or source.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="NOT_FOUND")

    if body.name is not None:
        source.name = body.name
    if body.file_patterns is not None:
        source.file_patterns = body.file_patterns
    if body.level_rank is not None:
        source.level_rank = body.level_rank
    if body.status is not None:
        source.status = body.status

    await session.commit()
    await session.refresh(source)

    await audit.record(
        user.tenant_id, user.user_id, "sync_source.update",
        object_type="sync_source", object_id=str(source_id),
        detail=body.model_dump(exclude_none=True),
    )
    return _to_out(source)


@router.delete("/sync-sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sync_source(
    source_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> None:
    _require_admin(user)

    source = await session.get(SyncSource, source_id)
    if source is None or source.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="NOT_FOUND")

    await session.delete(source)
    await session.commit()

    await audit.record(
        user.tenant_id, user.user_id, "sync_source.delete",
        object_type="sync_source", object_id=str(source_id),
        detail={},
    )


@router.post("/sync-sources/{source_id}/sync", response_model=SyncResult)
async def trigger_sync(
    source_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> SyncResult:
    """手动触发同步（不等 cron 定时，立即执行一次）。"""
    _require_admin(user)

    source = await session.get(SyncSource, source_id)
    if source is None or source.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="NOT_FOUND")

    # session 需要脱离 API 生命周期（sync_source 内部用自己的 session）
    # 先 detach
    session.expunge(source)

    new_count = await sync_source(source)
    return SyncResult(
        source_id=source_id,
        new_count=new_count,
        message=f"同步完成，新增/更新 {new_count} 个文件" if new_count > 0 else "无变更",
    )
