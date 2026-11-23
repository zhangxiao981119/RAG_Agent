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

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user, get_db
from app.config.settings import get_settings
from app.infra import trace
from app.infra.db_router import DEFAULT_DB_KEY
from app.infra.redis_client import get_redis
from app.models import Document, KnowledgeBase, KnowledgeBaseMember, ParseJob, User
from app.schemas.documents import BatchUploadFailure, BatchUploadResponse, DocumentOut
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
from app.services.db_scope import (
    DbScopeError,
    map_kb_db_keys,
    open_data_session,
    resolve_kb_db_key,
)
from app.services.parse.image_parser import IMAGE_EXTS
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

    # ── 两段式：控制面取库列表 → 按库位分桶 → 数据面统计文档数 ──────────
    # 先一次性解析全部 kb 的库位（关闭态零 SQL、全 default），再按库位顺序
    # 逐个打数据面会话。逐库一条 count 会变成"每个 kb 一次连接获取"，
    # 这里按桶聚合成一条 IN 查询；**不做跨库 join**（不同库位的表不能互相引）。
    keys_by_kb = await map_kb_db_keys(session, user.tenant_id, [kb.id for kb in rows])
    buckets: dict[str, list[uuid.UUID]] = {}
    for kb_id, db_key in keys_by_kb.items():
        buckets.setdefault(db_key, []).append(kb_id)

    doc_counts: dict[uuid.UUID, int] = {}
    for db_key in sorted(buckets):
        async with open_data_session(session, db_key) as data_plane:
            count_stmt = (
                select(Document.kb_id, func.count(Document.id))
                .where(
                    Document.kb_id.in_(buckets[db_key]),
                    Document.deleted_at.is_(None),
                )
                .group_by(Document.kb_id)
            )
            for kb_id, count in (await data_plane.execute(count_stmt)).all():
                doc_counts[kb_id] = count

    result: list[KnowledgeBaseOut] = []
    for kb in rows:
        result.append(
            KnowledgeBaseOut(
                id=kb.id,
                name=kb.name,
                description=kb.description,
                is_public=kb.is_public,
                doc_count=doc_counts.get(kb.id, 0),
                # P-d：库管理权标志（admin 或 owner），前端按此渲染上传区
                can_upload=user.clearance >= 40 or kb.owner_id == user.user_id,
            )
        )
    return result


@router.post("/kbs", response_model=KnowledgeBaseOut, status_code=status.HTTP_201_CREATED)
async def create_kb(
    payload: KnowledgeBaseCreate,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> KnowledgeBaseOut:
    redis = await get_redis()
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
            can_upload=True,  # 创建者即 owner（P-d）
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
    ★ 已绑定到非默认库位的 kb 直接 409：跨 database 的 CASCADE 永不触发
      （设计 §4.7），删控制库行会在场景库留下孤儿 documents —— 必须先走
      清理脚本删数据面，再回来删 kb。
    """
    kb = await session.get(KnowledgeBase, kb_id)
    if kb is None or kb.tenant_id != user.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="NOT_FOUND")
    if user.clearance < 40 and kb.owner_id != user.user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="FORBIDDEN")

    # 库位解析失败（同一 kb 命中多个库）时无法判定是否已搬入场景库：
    # 显式 409 并保留机器可读原因，绝不带着未知库位去删控制库行。
    try:
        db_key = await resolve_kb_db_key(session, user.tenant_id, [kb.id])
    except DbScopeError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"KB_SCOPE_CONFLICT: 库位解析冲突（{exc.code}: {exc.detail}），"
                "无法判定该库是否已搬入场景库，拒绝删除"
            ),
        ) from exc
    if db_key != DEFAULT_DB_KEY:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"KB_IN_BOUND_SCENE: 该知识库属于场景库位 {db_key}，"
                "跨库 CASCADE 不生效，请先按清理脚本删除场景库数据后再删 kb"
            ),
        )

    redis = await get_redis()
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
    # 两段式：kb 校验走控制面；文档数在数据面（关闭态复用控制面会话）
    db_key = await resolve_kb_db_key(session, user.tenant_id, [kb.id])
    count_stmt = (
        select(func.count(Document.id))
        .where(Document.kb_id == kb.id, Document.deleted_at.is_(None))
    )
    async with open_data_session(session, db_key) as data_plane:
        doc_count = (await data_plane.execute(count_stmt)).scalar_one()
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
    redis = await get_redis()
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
    # P-c/P-d：库管理权（同 KB 一次判定，控制面）
    kb = await session.get(KnowledgeBase, kb_id)
    can_upload = kb is not None and (
        user.clearance >= 40 or kb.owner_id == user.user_id
    )

    # 两段式：控制面 kb 校验 → 数据面查 documents → 关闭后再回控制面查展示名
    db_key = await resolve_kb_db_key(session, user.tenant_id, [kb_id])
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
    async with open_data_session(session, db_key) as data_plane:
        rows = (await data_plane.execute(stmt)).scalars().all()

    # P-e：上传人姓名批量查询（users 在控制库，数据面会话已关闭）
    uploader_ids = {d.uploaded_by for d in rows if d.uploaded_by is not None}
    uploader_names: dict[uuid.UUID, str] = {}
    if uploader_ids:
        user_rows = (
            await session.execute(
                select(User.id, User.display_name).where(User.id.in_(uploader_ids))
            )
        ).all()
        uploader_names = {u.id: u.display_name for u in user_rows}

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
            # P-e 溯源：来源类型 + 上传人展示名（同步/Web 来源为 None）
            source_kind=d.source_kind,
            uploader_name=(
                uploader_names.get(d.uploaded_by) if d.uploaded_by is not None else None
            ),
            # P-c/P-f 操作标志：判定在后端，前端只做展示渲染
            can_delete=can_upload or d.uploaded_by == user.user_id,
            can_upload=can_upload,
            # M7 WU-M-18：治理面板数据源（四维分数 NULL = 未审查）
            governance_status=d.governance_status,
            freshness_score=d.freshness_score,
            authority_score=d.authority_score,
            consistency_score=d.consistency_score,
            completeness_score=d.completeness_score,
            governance_report=d.governance_report,
            effective_at=d.effective_at,
            expire_at=d.expire_at,
        )
        for d in rows
    ]


async def _upload_single(
    request: Request,
    kb: KnowledgeBase,
    file: UploadFile,
    user: CurrentUser,
    session: AsyncSession,
) -> DocumentOut:
    """单文件上传全流程（WU-M-21 抽取，单文件端点与 batch 端点共用唯一实现）。

    ★ KB 存在性 / G2 授权校验在端点层完成（两端点同一套），本函数只管
      "文件 → 对象存储 → Document → ParseJob → 审计 → 入队" 主链；
    ★ 每个文件独立 commit + 入队：batch 部分成功时不被后续失败拖累，
      失败文件返回逐文件原因（INV-M-18 验收口径）。
    ★ 两段式（S6-3）：KB 校验由端点层在控制面完成；本函数解析库位后，
      在数据面会话里写 documents/parse_jobs，会话关闭后再审计（自开会话）与入队。
    """
    settings = get_settings()

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
    # ★ 图片联动：扩展名放行不等于能解析 —— 图片必须经视觉链路转录。
    #   OCR_ENABLED 是总开关；图片文件与文档内嵌图走视觉 LLM（需多模态模型），
    #   扫描件 PDF 才需要 OCR_BASE_URL 指向本地 OCR 服务。
    #   OCR 关闭时在这里拦下并给出「怎么开」，而不是让 job 落 last_error
    #   （解析层的 fail-closed 仍在，这里是更早、更可读的一道）。
    if ext in IMAGE_EXTS and not settings.ocr_enabled:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"IMAGE_REQUIRES_OCR: {ext} 属图片，需置 OCR_ENABLED=true 并保证 "
                f"LLM_MODEL 支持图片输入（图片经视觉链路转录入库）后才能入库；"
                f"扫描件 PDF 另需注入 OCR_BASE_URL。"
            ),
        )

    # 两段式：KB 校验已在端点层完成（控制面）；这里解析库位，失败即中止，
    # 不产生对象存储副作用。关闭态零 SQL，返回 default。
    db_key = await resolve_kb_db_key(session, user.tenant_id, [kb.id])

    # 落对象存储
    doc_group_id = uuid.uuid4()
    storage_key = f"{user.tenant_id}/{kb.id}/{doc_group_id}/v1/{filename}"
    storage = await get_storage()
    await storage.put_object(storage_key, data, file.content_type or "application/octet-stream")

    # M4 任务 2：owner_dept_path = 上传者部门路径（仅自身路径，不展开祖先）。
    # CurrentUser.dept_path 来自主体解析（departments 表原值），admin 无部门时为空串
    owner_dept_path: str | None = user.dept_path or None

    # acl_tags 由 compute_doc_acl_tags 唯一生成（§4.2.8 H1：dept 仅自身路径）
    acl_tags = sorted(compute_doc_acl_tags(dept_path=owner_dept_path))

    # 数据面写 documents/parse_jobs（关闭态复用控制面会话；绑定库位时先收控制面事务）
    async with open_data_session(session, db_key) as data_plane:
        # 建 document
        document = Document(
            tenant_id=user.tenant_id,
            kb_id=kb.id,
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
        data_plane.add(document)
        await data_plane.flush()

        # 建 parse_job
        parse_job = ParseJob(
            tenant_id=user.tenant_id,
            document_id=document.id,
            status="queued",
            max_attempts=settings.arq_max_attempts,
        )
        data_plane.add(parse_job)
        await data_plane.commit()
        await data_plane.refresh(document)
        await data_plane.refresh(parse_job)

    # 数据面会话已关闭：audit 自开控制面会话
    # 审计：上传文档（含目标库/文件名/大小/密级）
    await audit.record(
        user.tenant_id, user.user_id, "doc.upload",
        object_type="document", object_id=str(document.id),
        detail={"kb_id": str(kb.id), "filename": filename, "size_bytes": size_bytes, "level_rank": 20},
    )

    # 入队 arq 任务（复用 lifespan 初始化的全局连接池）
    # ★ 方案 C：统一走 run_parse_segment 首段（index=0）—— 首段内部判定路径，
    #   普通文档一次跑完，纯扫描件转分段 OCR 链
    # 透传 trace_id，让 worker 日志与本次上传请求同链
    await request.app.state.arq_pool.enqueue_job(
        "run_parse_segment", str(parse_job.id), 0, trace.get_trace_id(),
        _queue_name=settings.arq_queue_name,
    )

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


async def _load_kb_for_upload(
    kb_id: uuid.UUID,
    user: CurrentUser,
    session: AsyncSession,
) -> KnowledgeBase:
    """上传端点共用的 KB 校验（P-d U1）：存在性 + G2 库级授权 + 管理权。

    上传是库级内容管理动作：仅 admin 或 库 owner 可传（与删除口径对称），
    普通授权成员只读 + 问答。
    """
    kb = await session.get(KnowledgeBase, kb_id)
    if kb is None or kb.tenant_id != user.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="NOT_FOUND")
    if user.clearance < 40 and kb_id not in user.authorized_kb_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="FORBIDDEN")
    # P-d：admin / 库 owner 之外禁止上传（G2 通过 ≠ 可传）
    if user.clearance < 40 and kb.owner_id != user.user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="FORBIDDEN")
    return kb


@router.post(
    "/kbs/{kb_id}/documents",
    response_model=DocumentOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    kb_id: uuid.UUID,
    request: Request,
    file: UploadFile = File(...),
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> DocumentOut:
    kb = await _load_kb_for_upload(kb_id, user, session)
    # 库位解析失败（同一 kb 命中多个库）：无法确定写入哪个库，显式 409 并保留原因码。
    # 解析发生在落对象存储之前，因此失败时没有任何副作用。
    try:
        return await _upload_single(request, kb, file, user, session)
    except DbScopeError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"KB_SCOPE_CONFLICT: 库位解析冲突（{exc.code}: {exc.detail}），"
                "无法确定该文件的写入库位，拒绝上传"
            ),
        ) from exc


def _file_ext(filename: str | None) -> str:
    return (filename.rsplit(".", 1)[-1].lower() if filename and "." in filename else "")


@router.post(
    "/kbs/{kb_id}/documents/batch",
    response_model=BatchUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_documents_batch(
    kb_id: uuid.UUID,
    request: Request,
    files: list[UploadFile] = File(...),
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> BatchUploadResponse:
    """批量上传（M7 WU-M-21 / INV-M-18）：只接受同一扩展名的文件集合。

    ★ 混合格式直接 400 —— 混合集合共用一次提交会让解析分派、失败归因、
      部分成功/失败状态机同时复杂化，问题难定位（规范 §4.2 WU-M-21）。
    ★ 逐文件复用 _upload_single（MUST NOT 另写一套上传逻辑），
      返回逐文件成功/失败与原因，便于定位。
    """
    kb = await _load_kb_for_upload(kb_id, user, session)

    # INV-M-18：扩展名集合基数 == 1，混合即 400
    exts = {_file_ext(f.filename) for f in files}
    if len(exts) > 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"MIXED_FILE_TYPES: 批量上传只接受同一格式，检测到 {', '.join(sorted(exts))}",
        )

    succeeded: list[DocumentOut] = []
    failed: list[BatchUploadFailure] = []
    for file in files:
        try:
            out = await _upload_single(request, kb, file, user, session)
        except HTTPException as exc:
            # 单文件失败不影响其他文件（各自已独立 commit / 入队）
            failed.append(
                BatchUploadFailure(
                    filename=file.filename or "unnamed",
                    detail=str(exc.detail),
                )
            )
        except DbScopeError as exc:
            # 库位解析冲突同样是"该文件失败"，不是整批 500：
            # 保留 code，便于按文件定位（此时尚未写对象存储/DB）
            failed.append(
                BatchUploadFailure(
                    filename=file.filename or "unnamed",
                    detail=f"{exc.code}: 库位解析冲突，拒绝写入（{exc.detail}）",
                )
            )
        else:
            succeeded.append(out)

    return BatchUploadResponse(succeeded=succeeded, failed=failed)
