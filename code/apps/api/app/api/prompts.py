"""提示词版本管理 Admin API。

  GET    /api/admin/prompts                                   列出全部 key 的当前状态
  GET    /api/admin/prompts/{key}/versions                    版本列表（分页）
  GET    /api/admin/prompts/{key}/versions/{version_id}       版本详情
  POST   /api/admin/prompts/{key}/versions                    新建草稿版本
  POST   /api/admin/prompts/{key}/versions/{version_id}/activate   激活（生效）
  POST   /api/admin/prompts/{key}/versions/{version_id}/archive    归档
  GET    /api/admin/prompts/{key}/versions/{version_id}/diff       与内置默认 diff

设计：内容落库，约束段锁定在代码（app/config/prompts.py）。
激活前 MUST 通过 validate_prompt 校验，防止把约束改没（fail-closed）。
写操作后清 Redis 缓存，立即生效（缓存 TTL 60s 内）。
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_db, require_admin
from app.config.prompts import (
    PROMPT_SPECS,
    PromptInvalidError,
    build_prompt,
    validate_prompt,
)
from app.models import PromptVersion
from app.schemas.prompt import (
    PromptDiffOut,
    PromptKeyOut,
    PromptVersionCreate,
    PromptVersionDetail,
    PromptVersionOut,
)
from app.services import audit
from app.services import prompt_registry as registry

router = APIRouter(prefix="/admin", tags=["admin"])


def _ensure_key(key: str) -> None:
    """校验 key 是已注册的提示词；未知 key 返回 404。"""
    if key not in PROMPT_SPECS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="UNKNOWN_PROMPT_KEY")


def _to_out(row: PromptVersion) -> PromptVersionOut:
    return PromptVersionOut(
        id=row.id,
        prompt_key=row.prompt_key,
        version=row.version,
        status=row.status,
        note=row.note,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_detail(row: PromptVersion) -> PromptVersionDetail:
    return PromptVersionDetail(
        **_to_out(row).model_dump(),
        content=row.content,
        built=build_prompt(row.prompt_key, row.content),
    )


async def _get_version(
    session: AsyncSession, tenant_id: uuid.UUID, key: str, version_id: uuid.UUID
) -> PromptVersion:
    """按 id 取版本，并校验归属（租户 + key 都一致），否则 404。"""
    row = await session.get(PromptVersion, version_id)
    if row is None or row.tenant_id != tenant_id or row.prompt_key != key:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="NOT_FOUND")
    return row


@router.get("/prompts", response_model=list[PromptKeyOut])
async def list_prompts(
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> list[PromptKeyOut]:
    """列出全部提示词 key 的当前状态（含生效版本号）。"""
    rows = (
        await session.execute(
            select(PromptVersion).where(PromptVersion.tenant_id == user.tenant_id)
        )
    ).scalars().all()
    grouped: dict[str, list[PromptVersion]] = {}
    for r in rows:
        grouped.setdefault(r.prompt_key, []).append(r)

    out: list[PromptKeyOut] = []
    for key, spec in PROMPT_SPECS.items():
        versions = grouped.get(key, [])
        active = next((v for v in versions if v.status == "active"), None)
        out.append(
            PromptKeyOut(
                key=key,
                label=spec.label,
                locked=spec.locked,
                default_editable=spec.default_editable,
                active_version=active.version if active else None,
                active_version_id=active.id if active else None,
                is_customized=active is not None,
                version_count=len(versions),
            )
        )
    return out


@router.get("/prompts/{key}/versions", response_model=list[PromptVersionOut])
async def list_versions(
    key: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> list[PromptVersionOut]:
    """分页列出某 key 的版本（按版本号倒序）。"""
    _ensure_key(key)
    stmt = (
        select(PromptVersion)
        .where(
            PromptVersion.tenant_id == user.tenant_id,
            PromptVersion.prompt_key == key,
        )
        .order_by(PromptVersion.version.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [_to_out(r) for r in rows]


@router.get("/prompts/{key}/versions/{version_id}", response_model=PromptVersionDetail)
async def get_version(
    key: str,
    version_id: uuid.UUID,
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> PromptVersionDetail:
    """版本详情（含可编辑区与拼装后的完整文本）。"""
    _ensure_key(key)
    row = await _get_version(session, user.tenant_id, key, version_id)
    return _to_detail(row)


@router.post(
    "/prompts/{key}/versions",
    response_model=PromptVersionDetail,
    status_code=status.HTTP_201_CREATED,
)
async def create_version(
    key: str,
    payload: PromptVersionCreate,
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> PromptVersionDetail:
    """新建草稿版本（version = 该 key 现有最大版本号 + 1）。"""
    _ensure_key(key)
    try:
        validate_prompt(key, payload.content)
    except PromptInvalidError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    max_version = (
        await session.execute(
            select(func.max(PromptVersion.version)).where(
                PromptVersion.tenant_id == user.tenant_id,
                PromptVersion.prompt_key == key,
            )
        )
    ).scalar()
    row = PromptVersion(
        tenant_id=user.tenant_id,
        prompt_key=key,
        version=(max_version or 0) + 1,
        content=payload.content,
        status="draft",
        note=payload.note,
        created_by=user.user_id,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    await audit.record(
        user.tenant_id,
        user.user_id,
        "prompt.create",
        object_type="prompt",
        object_id=str(row.id),
        detail={"key": key, "version": row.version},
    )
    return _to_detail(row)


@router.post(
    "/prompts/{key}/versions/{version_id}/activate", response_model=PromptVersionDetail
)
async def activate_version(
    key: str,
    version_id: uuid.UUID,
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> PromptVersionDetail:
    """激活某版本：归档当前 active，再置本条为 active（每 key 至多一条 active）。"""
    _ensure_key(key)
    row = await _get_version(session, user.tenant_id, key, version_id)
    try:
        validate_prompt(key, row.content)
    except PromptInvalidError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    # 先归档旧 active（部分唯一索引 uq_prompt_versions_active 要求同 key 只有一条 active）
    await session.execute(
        update(PromptVersion)
        .where(
            PromptVersion.tenant_id == user.tenant_id,
            PromptVersion.prompt_key == key,
            PromptVersion.status == "active",
        )
        .values(status="archived", updated_at=func.now())
    )
    row.status = "active"
    row.updated_at = func.now()
    await session.commit()
    await session.refresh(row)
    await registry._invalidate(user.tenant_id, key)
    await audit.record(
        user.tenant_id,
        user.user_id,
        "prompt.activate",
        object_type="prompt",
        object_id=str(row.id),
        detail={"key": key, "version": row.version},
    )
    return _to_detail(row)


@router.post(
    "/prompts/{key}/versions/{version_id}/archive", response_model=PromptVersionOut
)
async def archive_version(
    key: str,
    version_id: uuid.UUID,
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> PromptVersionOut:
    """归档某版本。若归档的是当前 active，则该 key 回落到内置默认。"""
    _ensure_key(key)
    row = await _get_version(session, user.tenant_id, key, version_id)
    if row.status == "archived":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "版本已归档")
    was_active = row.status == "active"
    row.status = "archived"
    row.updated_at = func.now()
    await session.commit()
    await session.refresh(row)
    if was_active:
        await registry._invalidate(user.tenant_id, key)
    await audit.record(
        user.tenant_id,
        user.user_id,
        "prompt.archive",
        object_type="prompt",
        object_id=str(row.id),
        detail={"key": key, "version": row.version},
    )
    return _to_out(row)


@router.get(
    "/prompts/{key}/versions/{version_id}/diff", response_model=PromptDiffOut
)
async def diff_version(
    key: str,
    version_id: uuid.UUID,
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> PromptDiffOut:
    """该版本与内置默认的 unified diff。"""
    _ensure_key(key)
    row = await _get_version(session, user.tenant_id, key, version_id)
    return PromptDiffOut(
        key=key,
        version=row.version,
        diff=registry.diff_with_default(key, row.content),
    )