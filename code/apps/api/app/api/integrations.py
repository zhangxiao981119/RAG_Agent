"""企业应用集成 API：应用凭证管理、同步问答与权限检索。"""
from __future__ import annotations

import codecs
import hashlib
import hmac
import json
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.chat import _chat_ask_impl, _integration_kb_ids
from app.api.deps import CurrentUser, get_current_user, get_db, require_admin
from app.api.rate_limit import check_integration_rate_limit
from app.models import IntegrationApplication, KnowledgeBase
from app.schemas.chat import ChatAskRequest
from app.schemas.integration import (
    IntegrationApplicationCreate,
    IntegrationApplicationCreated,
    IntegrationApplicationOut,
    IntegrationAskRequest,
    IntegrationAskResponse,
    IntegrationSearchItem,
    IntegrationSearchRequest,
    IntegrationSearchResponse,
)
from app.services import audit
from app.services.retrieve import get_retrieval_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/integrations", tags=["integrations"])


@dataclass(frozen=True)
class IntegrationPrincipal:
    """经双认证确认的应用与真实用户。"""

    user: CurrentUser
    application: IntegrationApplication


def _application_key_hash(application_key: str) -> str:
    return hashlib.sha256(application_key.encode("utf-8")).hexdigest()


def _application_out(application: IntegrationApplication) -> IntegrationApplicationOut:
    return IntegrationApplicationOut(
        id=application.id,
        name=application.name,
        kb_ids=[uuid.UUID(kb_id) for kb_id in application.kb_ids],
        status=application.status,
        rate_limit_per_minute=application.rate_limit_per_minute,
        daily_token_limit=application.daily_token_limit,
        daily_message_limit=application.daily_message_limit,
        created_at=application.created_at,
        disabled_at=application.disabled_at,
    )


async def get_integration_principal(
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    application_id_raw: str | None = Header(default=None, alias="X-Application-Id"),
    application_key: str | None = Header(default=None, alias="X-Application-Key"),
) -> IntegrationPrincipal:
    """验证应用凭证，并将应用绑定到 JWT 解析出的真实用户。"""
    if not application_id_raw or not application_key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少应用凭证")
    try:
        application_id = uuid.UUID(application_id_raw)
    except ValueError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "应用凭证无效") from exc

    application = await session.get(IntegrationApplication, application_id)
    expected_hash = application.key_hash if application is not None else ""
    actual_hash = _application_key_hash(application_key)
    if (
        application is None
        or application.tenant_id != user.tenant_id
        or application.status != "active"
        or not hmac.compare_digest(actual_hash, expected_hash)
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "应用凭证无效或已禁用")
    return IntegrationPrincipal(user=user, application=application)


@router.post(
    "/applications",
    response_model=IntegrationApplicationCreated,
    status_code=status.HTTP_201_CREATED,
)
async def create_application(
    payload: IntegrationApplicationCreate,
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> IntegrationApplicationCreated:
    """签发应用凭证，明文密钥只在本次响应返回。"""
    kb_ids = list(dict.fromkeys(payload.kb_ids))
    kb_rows = (
        await session.execute(
            select(KnowledgeBase.id).where(
                KnowledgeBase.tenant_id == user.tenant_id,
                KnowledgeBase.id.in_(kb_ids),
            )
        )
    ).scalars().all()
    if len(kb_rows) != len(kb_ids):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "部分知识库不存在")

    application_key = secrets.token_urlsafe(32)
    application = IntegrationApplication(
        tenant_id=user.tenant_id,
        name=payload.name,
        key_hash=_application_key_hash(application_key),
        kb_ids=[str(kb_id) for kb_id in kb_ids],
        rate_limit_per_minute=payload.rate_limit_per_minute,
        daily_token_limit=payload.daily_token_limit,
        daily_message_limit=payload.daily_message_limit,
        created_by=user.user_id,
    )
    session.add(application)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "应用名称已存在") from exc
    await session.refresh(application)
    await audit.record(
        user.tenant_id,
        user.user_id,
        "integration.application.create",
        object_type="integration_application",
        object_id=str(application.id),
        detail={
            "application_id": str(application.id),
            "name": application.name,
            "kb_ids": application.kb_ids,
        },
    )
    return IntegrationApplicationCreated(
        **_application_out(application).model_dump(),
        application_key=application_key,
    )


@router.get("/applications", response_model=list[IntegrationApplicationOut])
async def list_applications(
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> list[IntegrationApplicationOut]:
    rows = (
        await session.execute(
            select(IntegrationApplication)
            .where(IntegrationApplication.tenant_id == user.tenant_id)
            .order_by(IntegrationApplication.created_at.desc())
        )
    ).scalars().all()
    return [_application_out(application) for application in rows]


@router.post(
    "/applications/{application_id}/disable",
    response_model=IntegrationApplicationOut,
)
async def disable_application(
    application_id: uuid.UUID,
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> IntegrationApplicationOut:
    application = await session.get(IntegrationApplication, application_id)
    if application is None or application.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "应用不存在")
    if application.status == "active":
        application.status = "disabled"
        application.disabled_at = datetime.now(timezone.utc)
        await session.commit()
        await session.refresh(application)
        await audit.record(
            user.tenant_id,
            user.user_id,
            "integration.application.disable",
            object_type="integration_application",
            object_id=str(application.id),
            detail={"application_id": str(application.id), "name": application.name},
        )
    return _application_out(application)


async def _consume_ask_response(response) -> IntegrationAskResponse:
    """消费共用问答事件流并转换为同步 JSON，不复制 RAG 主链路。"""
    conversation_id: uuid.UUID | None = None
    message_id: uuid.UUID | None = None
    answer_parts: list[str] = []
    citations: list[dict] = []
    usage: dict = {}
    suggestions: list[str] = []
    refused = False
    refuse_reason: str | None = None
    refuse_message: str | None = None
    terminal = False
    buffer = ""
    # 增量解码器：UTF-8 多字节字符可能被切在两个 chunk 边界，
    # 逐块独立 decode 会抛 UnicodeDecodeError，必须跨块保留解码状态。
    utf8_decoder = codecs.getincrementaldecoder("utf-8")()

    async for chunk in response.body_iterator:
        buffer += (
            utf8_decoder.decode(chunk) if isinstance(chunk, bytes) else chunk
        )
        while "\n\n" in buffer:
            block, buffer = buffer.split("\n\n", 1)
            event_name = ""
            data_lines: list[str] = []
            for line in block.splitlines():
                if line.startswith("event:"):
                    event_name = line.removeprefix("event:").strip()
                elif line.startswith("data:"):
                    data_lines.append(line.removeprefix("data:").lstrip())
            if not event_name or not data_lines:
                continue
            data = json.loads("\n".join(data_lines))
            if event_name == "meta":
                if data.get("conversation_id"):
                    conversation_id = uuid.UUID(data["conversation_id"])
                if data.get("message_id"):
                    message_id = uuid.UUID(data["message_id"])
            elif event_name == "citations":
                citations = data.get("citations", [])
            elif event_name == "delta":
                answer_parts.append(data.get("text", ""))
            elif event_name == "done":
                usage = data.get("usage", {})
                suggestions = data.get("suggestions", [])
                terminal = True
            elif event_name == "refused":
                refused = True
                refuse_reason = data.get("reason")
                refuse_message = data.get("message")
                terminal = True

    if not terminal:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "问答事件流未正常结束")
    return IntegrationAskResponse(
        conversation_id=conversation_id,
        message_id=message_id,
        answer="".join(answer_parts),
        citations=citations,
        refused=refused,
        refuse_reason=refuse_reason,
        message=refuse_message,
        usage=usage,
        suggestions=suggestions,
    )


@router.post("/ask", response_model=IntegrationAskResponse)
async def integration_ask(
    payload: IntegrationAskRequest,
    principal: IntegrationPrincipal = Depends(get_integration_principal),
    session: AsyncSession = Depends(get_db),
) -> IntegrationAskResponse:
    await check_integration_rate_limit(principal.user, principal.application)
    response = await _chat_ask_impl(
        ChatAskRequest(
            question=payload.question,
            kb_ids=payload.kb_ids,
            conversation_id=payload.conversation_id,
        ),
        principal.user,
        session,
        integration=principal.application,
    )
    return await _consume_ask_response(response)


@router.post("/search", response_model=IntegrationSearchResponse)
async def integration_search(
    payload: IntegrationSearchRequest,
    principal: IntegrationPrincipal = Depends(get_integration_principal),
    session: AsyncSession = Depends(get_db),
) -> IntegrationSearchResponse:
    user = principal.user
    application = principal.application
    await check_integration_rate_limit(user, application)
    effective_kb_ids = _integration_kb_ids(payload.kb_ids, user, application)
    if not effective_kb_ids:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "FORBIDDEN")

    try:
        result = await get_retrieval_service().retrieve(
            session,
            payload.query,
            user.tenant_id,
            effective_kb_ids,
            user.clearance,
            user.subjects,
        )
    except Exception as exc:
        logger.exception("集成检索失败")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "知识库暂时不可用，请稍后重试"
        ) from exc

    items = [] if result.refused else [
        IntegrationSearchItem(
            chunk_id=chunk.chunk_id,
            document_id=chunk.document_id,
            kb_id=chunk.kb_id,
            filename=chunk.filename,
            content=chunk.content,
            heading_path=chunk.heading_path,
            page_no=chunk.page_no,
            score=chunk.display_score,
        )
        for chunk in result.chunks
    ]
    await audit.record(
        user.tenant_id,
        user.user_id,
        "integration.search",
        object_type="integration_application",
        object_id=str(application.id),
        detail={
            "application_id": str(application.id),
            "query": payload.query,
            "kb_ids": [str(kb_id) for kb_id in effective_kb_ids],
            "doc_ids": sorted({str(item.document_id) for item in items}),
            "result_count": len(items),
        },
    )
    return IntegrationSearchResponse(items=items)
