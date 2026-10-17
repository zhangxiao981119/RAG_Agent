"""M6 任务 1：知识源同步 API（§8 D-07）。

  POST   /kbs/{kb_id}/sync-sources          创建同步源（admin）
  GET    /kbs/{kb_id}/sync-sources          列出同步源
  PATCH  /sync-sources/{id}                 更新（暂停/恢复/改配置）
  DELETE /sync-sources/{id}                  删除
  POST   /sync-sources/{id}/sync            手动触发同步
"""
from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user, get_db, require_admin
from app.config.settings import get_settings
from app.infra import trace
from app.models import KnowledgeBase, SyncSource
from app.schemas.sync import SyncSourceCreate, SyncSourceOut, SyncSourceUpdate, SyncResult
from app.services import audit
from app.services.crypto import encrypt_credential

router = APIRouter(tags=["sync-sources"])


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
        auth_type=source.auth_type,
        # INV-M-07：凭据（明文/密文）都不出接口，只给"是否已配置"
        has_auth=bool(source.auth_payload),
        entry_urls=list(source.entry_urls or []),
        scope_rules=dict(source.scope_rules or {}),
    )


@router.post(
    "/kbs/{kb_id}/sync-sources",
    response_model=SyncSourceOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_sync_source(
    kb_id: uuid.UUID,
    body: SyncSourceCreate,
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> SyncSourceOut:
    # 校验 KB 存在且属于当前租户
    kb = await session.get(KnowledgeBase, kb_id)
    if kb is None or kb.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="NOT_FOUND")

    # INV-M-07：凭据只在创建时进一次、加密落库；audit 只记 auth_type 不记内容
    auth_payload_ciphertext: str | None = None
    if body.source_type == "web" and body.auth:
        auth_payload_ciphertext = encrypt_credential(json.dumps(body.auth, ensure_ascii=False))

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
        auth_type=body.auth_type if body.source_type == "web" else "none",
        auth_payload=auth_payload_ciphertext,
        entry_urls=list(body.entry_urls) if body.source_type == "web" else [],
        scope_rules=dict(body.scope_rules) if body.source_type == "web" else {},
    )
    session.add(source)
    await session.commit()
    await session.refresh(source)

    await audit.record(
        user.tenant_id, user.user_id, "sync_source.create",
        object_type="sync_source", object_id=str(source.id),
        # 凭据明文/密文 MUST NOT 进日志（INV-M-07），只记类型
        detail={"kb_id": str(kb_id), "name": body.name, "source_type": body.source_type,
                "auth_type": source.auth_type, "has_auth": bool(auth_payload_ciphertext)},
    )
    return _to_out(source)


@router.get("/kbs/{kb_id}/sync-sources", response_model=list[SyncSourceOut])
async def list_sync_sources(
    kb_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> list[SyncSourceOut]:
    # 校验 KB 存在 + 同租户 + G2 库级授权（admin 豁免）
    # 避免同租户任意用户枚举他人 KB 的 git URL/path 等敏感配置
    kb = await session.get(KnowledgeBase, kb_id)
    if kb is None or kb.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="NOT_FOUND")
    if user.clearance < 40 and kb_id not in user.authorized_kb_ids:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="FORBIDDEN")
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
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> SyncSourceOut:
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

    # ── web 源字段（M7 WU-M-11）──
    if body.auth_type is not None:
        source.auth_type = body.auth_type
    if body.entry_urls is not None:
        source.entry_urls = list(body.entry_urls)
    if body.scope_rules is not None:
        source.scope_rules = dict(body.scope_rules)
    # 凭据更新语义（schemas.sync.SyncSourceUpdate 注释）：None=不动；clear_auth=显式清除
    if body.clear_auth:
        source.auth_payload = None
        if body.auth_type is None:
            source.auth_type = "none"
    elif body.auth is not None:
        # INV-M-07：新凭据加密落库（全量替换，非合并）
        source.auth_payload = encrypt_credential(json.dumps(body.auth, ensure_ascii=False))
        if source.auth_type == "none":
            source.auth_type = "basic"

    await session.commit()
    await session.refresh(source)

    await audit.record(
        user.tenant_id, user.user_id, "sync_source.update",
        object_type="sync_source", object_id=str(source_id),
        # 凭据内容 MUST NOT 进日志（INV-M-07）
        detail={"fields": [k for k in body.model_dump(exclude_none=True) if k != "auth"],
                "auth_changed": body.auth is not None or body.clear_auth},
    )
    return _to_out(source)


@router.delete("/sync-sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sync_source(
    source_id: uuid.UUID,
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> None:
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
    request: Request,
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> SyncResult:
    """手动触发同步 —— 入队 arq 异步执行，立即返回。

    同步过程可能涉及 git clone（大仓库 300s），HTTP 同步等待会触发网关超时。
    改为 arq 入队后立即返回，前端可轮询 sync_source.last_sync_count 查看进度。
    连接池复用 lifespan 初始化的全局 arq pool。
    """
    source = await session.get(SyncSource, source_id)
    if source is None or source.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="NOT_FOUND")

    settings = get_settings()
    # 透传 trace_id，让 worker 日志与本次触发请求同链
    await request.app.state.arq_pool.enqueue_job(
        "run_sync_source", str(source_id), trace.get_trace_id(),
        _queue_name=settings.arq_queue_name,
    )

    await audit.record(
        user.tenant_id, user.user_id, "sync_source.trigger",
        object_type="sync_source", object_id=str(source_id),
        detail={},
    )

    return SyncResult(
        source_id=source_id,
        new_count=0,
        message="同步已触发，请稍后查看结果",
    )
