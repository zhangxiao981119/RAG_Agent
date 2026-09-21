"""知识库 + 文档 API（手册 §5.1 M1/M2 接口）。

M2 范围：
  GET    /kbs                      知识库列表
  POST   /kbs                      新建知识库
  GET    /kbs/{kb_id}              知识库详情
  GET    /kbs/{kb_id}/documents    文档列表
  POST   /kbs/{kb_id}/documents    上传文档（multipart）
"""
from __future__ import annotations

import uuid

from arq.connections import RedisSettings, create_pool
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user, get_db
from app.config.settings import get_settings
from app.models import Document, KnowledgeBase, KnowledgeBaseMember, ParseJob
from app.schemas.documents import DocumentOut
from app.schemas.kbs import (
    KnowledgeBaseCreate,
    KnowledgeBaseDetail,
    KnowledgeBaseOut,
    KbMemberItem,
    KbMemberListResponse,
    KbMemberSetRequest,
    KbMemberSetResponse,
)
from app.services import audit
from app.services import kb_member as kb_member_service
from app.services.acl import compute_doc_acl_tags
from app.services.acl.subjects import invalidate_tenant_acl
from app.services.storage import get_storage

router = APIRouter(tags=["knowledge-bases"])


@router.get("/kbs", response_model=list[KnowledgeBaseOut])
async def list_kbs(
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> list[KnowledgeBaseOut]:
    # admin（clearance >= 40）可看租户下全部知识库；普通用户走 G2 库级授权过滤
    if user.clearance >= 40:
        stmt = select(KnowledgeBase).where(
            KnowledgeBase.tenant_id == user.tenant_id,
        )
    else:
        auth_ids = user.authorized_kb_ids
        if not auth_ids:
            return []
        stmt = select(KnowledgeBase).where(
            KnowledgeBase.tenant_id == user.tenant_id,
            KnowledgeBase.id.in_(auth_ids),
        )
    rows = (await session.execute(stmt)).scalars().all()
    result: list[KnowledgeBaseOut] = []
    for kb in rows:
        count_stmt = (
            select(func.count(Document.id))
            .where(Document.kb_id == kb.id, Document.deleted_at.is_(None))
        )
        doc_count = (await session.execute(count_stmt)).scalar_one()
        result.append(
            KnowledgeBaseOut(
                id=kb.id,
                name=kb.name,
                description=kb.description,
                is_public=kb.is_public,
                doc_count=doc_count,
            )
        )
    return result


@router.post("/kbs", response_model=KnowledgeBaseOut, status_code=status.HTTP_201_CREATED)
async def create_kb(
    payload: KnowledgeBaseCreate,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> KnowledgeBaseOut:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    try:
        # 公开库互斥（手册 §3.2.4 + entities 唯一索引）
        kb = KnowledgeBase(
            tenant_id=user.tenant_id,
            name=payload.name,
            description=payload.description,
            visibility="public" if payload.is_public else "restricted",
            is_public=payload.is_public,
            owner_id=user.user_id,
        )
        session.add(kb)
        await session.flush()
        # 创建者自动成为库成员：消除"受限库建完创建者自己看不到"的场景
        session.add(
            KnowledgeBaseMember(
                tenant_id=user.tenant_id,
                kb_id=kb.id,
                subject_type="user",
                subject_id=str(user.user_id),
            )
        )
        # 权限变更点：INCR tenant.acl_epoch（§3.2.6），与成员写入同事务提交
        await invalidate_tenant_acl(session, redis, user.tenant_id)
        try:
            await session.commit()
        except Exception as exc:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"CONFLICT: {exc}",
            ) from exc
        # 审计：建库动作（含库可见性）
        await audit.record(
            user.tenant_id, user.user_id, "kb.create",
            object_type="kb", object_id=str(kb.id),
            detail={"name": kb.name, "is_public": kb.is_public},
        )
        return KnowledgeBaseOut(
            id=kb.id,
            name=kb.name,
            description=kb.description,
            is_public=kb.is_public,
            doc_count=0,
        )
    finally:
        await redis.aclose()


@router.delete("/kbs/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_kb(
    kb_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> None:
    """删除知识库（级联删文档/chunks/members/sync_sources）。

    权限：仅 admin（clearance >= 40）或库 owner 可删。
    """
    kb = await session.get(KnowledgeBase, kb_id)
    if kb is None or kb.tenant_id != user.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="NOT_FOUND")
    if user.clearance < 40 and kb.owner_id != user.user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="FORBIDDEN")

    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    try:
        await invalidate_tenant_acl(session, redis, user.tenant_id)
        await session.delete(kb)
        await session.commit()
    finally:
        await redis.aclose()

    await audit.record(
        user.tenant_id, user.user_id, "kb.delete",
        object_type="kb", object_id=str(kb_id),
        detail={"name": kb.name},
    )


@router.get("/kbs/{kb_id}", response_model=KnowledgeBaseDetail)
async def get_kb(
    kb_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> KnowledgeBaseDetail:
    # admin 豁免 G2 库级授权校验
    if user.clearance < 40 and kb_id not in user.authorized_kb_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="FORBIDDEN")
    kb = await session.get(KnowledgeBase, kb_id)
    if kb is None or kb.tenant_id != user.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="NOT_FOUND")
    count_stmt = (
        select(func.count(Document.id))
        .where(Document.kb_id == kb.id, Document.deleted_at.is_(None))
    )
    doc_count = (await session.execute(count_stmt)).scalar_one()
    return KnowledgeBaseDetail(
        id=kb.id,
        name=kb.name,
        description=kb.description,
        is_public=kb.is_public,
        doc_count=doc_count,
        created_at=kb.created_at,
    )


@router.get("/kbs/{kb_id}/members", response_model=KbMemberListResponse)
async def list_kb_members(
    kb_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> KbMemberListResponse:
    """列出知识库成员（四种主体，含展示用 label）。"""
    kb = await session.get(KnowledgeBase, kb_id)
    if kb is None or kb.tenant_id != user.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="NOT_FOUND")
    rows = await kb_member_service.list_members(session, user.tenant_id, kb_id)
    return KbMemberListResponse(
        kb_id=kb_id,
        members=[
            KbMemberItem(subject_type=r.subject_type, subject_id=r.subject_id, label=r.label)
            for r in rows
        ],
    )


@router.put("/kbs/{kb_id}/members", response_model=KbMemberSetResponse)
async def set_kb_members(
    kb_id: uuid.UUID,
    payload: KbMemberSetRequest,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> KbMemberSetResponse:
    """全量设置知识库成员（四种主体，手册 §5.1）。

    成员实际变化时由 services/kb_member 触发 tenant_acl_epoch+1。
    权限：admin（clearance >= 40）或库 owner 可改，避免普通用户给自己加成员。
    """
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    try:
        kb = await session.get(KnowledgeBase, kb_id)
        if kb is None or kb.tenant_id != user.tenant_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="NOT_FOUND")
        # 仅 admin 或库 owner 可改成员
        if user.clearance < 40 and kb.owner_id != user.user_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="FORBIDDEN")
        try:
            rows, changed = await kb_member_service.set_members(
                session, redis, user.tenant_id, kb_id,
                [(m.subject_type, m.subject_id) for m in payload.members],
                operator_user_id=user.user_id,
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
        await session.commit()
        return KbMemberSetResponse(
            kb_id=kb_id,
            changed=changed,
            members=[
                KbMemberItem(subject_type=r.subject_type, subject_id=r.subject_id, label=r.label)
                for r in rows
            ],
        )
    finally:
        await redis.aclose()


@router.get("/kbs/{kb_id}/documents", response_model=list[DocumentOut])
async def list_documents(
    kb_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> list[DocumentOut]:
    # admin 豁免 G2 库级授权校验
    if user.clearance < 40 and kb_id not in user.authorized_kb_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="FORBIDDEN")
    stmt = (
        select(Document)
        .where(
            Document.kb_id == kb_id,
            Document.tenant_id == user.tenant_id,
            Document.deleted_at.is_(None),
            Document.is_latest.is_(True),
        )
        .order_by(Document.created_at.desc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [
        DocumentOut(
            id=d.id,
            kb_id=d.kb_id,
            filename=d.filename,
            ext=d.ext,
            size_bytes=d.size_bytes,
            status=d.status,
            version=d.version,
            level_rank=d.level_rank,
            uploaded_at=d.created_at,
        )
        for d in rows
    ]


@router.post(
    "/kbs/{kb_id}/documents",
    response_model=DocumentOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    kb_id: uuid.UUID,
    file: UploadFile = File(...),
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> DocumentOut:
    settings = get_settings()

    # 校验 KB 存在
    kb = await session.get(KnowledgeBase, kb_id)
    if kb is None or kb.tenant_id != user.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="NOT_FOUND")
    # G2：库级授权（kb_id ∈ authorized_kb_ids），admin 豁免
    # 与 list_documents 一致，避免普通用户向任意库上传文档
    if user.clearance < 40 and kb_id not in user.authorized_kb_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="FORBIDDEN")

    # 读文件内容（限制大小，避免 OOM）
    data = await file.read()
    size_bytes = len(data)
    if size_bytes > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"FILE_TOO_LARGE: 上限 {settings.max_upload_mb}MB",
        )

    # 扩展名
    filename = file.filename or "unnamed"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in set(settings.allowed_ext):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"UNSUPPORTED_FILE_TYPE: 支持的类型 {', '.join(settings.allowed_ext)}",
        )

    # 落对象存储
    doc_group_id = uuid.uuid4()
    storage_key = f"{user.tenant_id}/{kb_id}/{doc_group_id}/v1/{filename}"
    storage = await get_storage()
    await storage.put_object(storage_key, data, file.content_type or "application/octet-stream")

    # M4 任务 2：owner_dept_path = 上传者部门路径（仅自身路径，不展开祖先）。
    # CurrentUser.dept_path 来自主体解析（departments 表原值），admin 无部门时为空串
    owner_dept_path: str | None = user.dept_path or None

    # acl_tags 由 compute_doc_acl_tags 唯一生成（§4.2.8 H1：dept 仅自身路径）
    acl_tags = sorted(compute_doc_acl_tags(dept_path=owner_dept_path))

    # 建 document
    document = Document(
        tenant_id=user.tenant_id,
        kb_id=kb_id,
        doc_group_id=doc_group_id,
        version=1,
        is_latest=True,
        filename=filename,
        ext=ext,
        size_bytes=size_bytes,
        storage_key=storage_key,
        status="pending",
        level=1,
        level_rank=20,
        owner_dept_path=owner_dept_path,
        acl_tags=acl_tags,
        deny_subjects=[],
        uploaded_by=user.user_id,
    )
    session.add(document)
    await session.flush()

    # 建 parse_job
    parse_job = ParseJob(
        tenant_id=user.tenant_id,
        document_id=document.id,
        status="queued",
        max_attempts=settings.arq_max_attempts,
    )
    session.add(parse_job)
    await session.commit()
    await session.refresh(document)
    await session.refresh(parse_job)

    # 审计：上传文档（含目标库/文件名/大小/密级）
    await audit.record(
        user.tenant_id, user.user_id, "doc.upload",
        object_type="document", object_id=str(document.id),
        detail={"kb_id": str(kb_id), "filename": filename, "size_bytes": size_bytes, "level_rank": 20},
    )

    # 入队 arq 任务（必须指定与 WorkerSettings.queue_name 一致）
    redis = await create_pool(
        RedisSettings.from_dsn(settings.redis_url)
    )
    await redis.enqueue_job(
        "run_parse_job", str(parse_job.id), _queue_name=settings.arq_queue_name
    )
    await redis.close()

    return DocumentOut(
        id=document.id,
        kb_id=document.kb_id,
        filename=document.filename,
        ext=document.ext,
        size_bytes=document.size_bytes,
        status=document.status,
        version=document.version,
        level_rank=document.level_rank,
        uploaded_at=document.created_at,
    )
