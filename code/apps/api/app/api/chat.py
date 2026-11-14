"""Chat SSE 接口 —— 手册 §5.2 POST /api/chat/ask。

SSE 事件顺序：
  meta → stage(retrieving) → [refused | citations → delta... → done]

MUST 遵守（手册 §5.2 约束）：
  1. kb_ids 与用户 authorized_kb_ids 求交集（M2 信任前端，M4 补）
  2. citations MUST 在 delta 之前
  3. 拒答 MUST 发 refused 事件，MUST NOT 用 delta 发拒答文案
  4. delta 是增量文本，前端累加
  5. 载荷只许**追加**不许改名/删除：`done` 与「检索之后」的 `refused` 额外带
     `path_stats` / `graph_degraded_reason`（§14.6 可测化），既有键与事件顺序不变
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user, get_db
from app.api.rate_limit import check_chat_rate_limit
from app.config import decisions
from app.config.settings import get_settings
from app.database import SessionLocal
from app.infra import trace
from app.models import (
    Conversation,
    IntegrationApplication,
    IntegrationConversation,
    KnowledgeBase,
    Message,
    User,
)
from app.schemas.chat import ChatAskRequest, ImageRef
from app.services import audit
from app.services import feature_flag as flag_service
from app.services import quota as quota_service
from app.services import sensitive as sensitive_service
from app.services.caliber import snapshot_for_message
from app.services.generate import build_system_prompt, get_stream_generation_service
from app.services.guard import injection as injection_guard
from app.services.keyword_text import hit_snippet
from app.services.mask import mask_pii
from app.services.memory import (
    build_memory_prompt,
    ensure_context_within_limit,
    estimate_context_tokens,
    extract_facts,
)
from app.services.multimodal import (
    VisionError,
    VisionNoContentError,
    extract_text_from_image,
)
from app.services.query_rewrite import rewrite_query
from app.services.retrieve import get_retrieval_service
from app.services.retrieve.base import RetrievalResult, assemble_context_chunks
from app.services.router.modality import Modality, detect

logger = logging.getLogger(__name__)
router = APIRouter(tags=["chat"])

# 单轮提问允许携带的图片上限（M7 规范 §3.2：超限直接 400，MUST NOT 静默截断）
MAX_IMAGES_PER_ASK = 3


def _validate_images(images: list[ImageRef]) -> None:
    """带图提问的入参校验（数量 + 单图大小），超限抛 400。

    ★ 放在端点层而非 pydantic：规范 §3.2 要求超限返 400，pydantic 校验失败是 422。
    ★ storage_key 不重复卡大小：bucket 对象必经既有上传链路落库，
      kbs.py 上传时已按同一 max_upload_mb 上限拦截过。
    """
    if len(images) > MAX_IMAGES_PER_ASK:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"TOO_MANY_IMAGES: 单轮最多 {MAX_IMAGES_PER_ASK} 张",
        )
    limit_bytes = get_settings().max_upload_mb * 1024 * 1024
    for ref in images:
        if not ref.data_url:
            continue
        # data URL 形如 data:<mime>;base64,<payload>，按 base64 解码后字节数估算
        payload_part = ref.data_url.partition(",")[2]
        estimated = len(payload_part) * 3 // 4
        if estimated > limit_bytes:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"IMAGE_TOO_LARGE: 单图上限 {get_settings().max_upload_mb}MB",
            )


def _sse(event: str, data: dict) -> str:
    """构造 SSE 事件块。data 用 JSON 序列化。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


async def _with_heartbeat(
    events: AsyncIterator[str], interval_seconds: float = 15.0,
) -> AsyncIterator[str]:
    """在等待检索或生成时发送 SSE 注释心跳，并正确传播断连取消。

    心跳使用 ``: ping`` 注释行，不是业务事件，现有前端解析器会自然忽略。
    每次只推进底层迭代器一次；等待超时不会取消正在执行的检索或生成。
    """
    iterator = events.__aiter__()
    while True:
        next_event = asyncio.create_task(anext(iterator))
        try:
            while True:
                done, _ = await asyncio.wait(
                    {next_event}, timeout=interval_seconds
                )
                if done:
                    break
                yield ": ping\n\n"
            # wait 不取消底层任务；超时与事件完成同时发生时也必须先取回原事件。
            event = next_event.result()
        except StopAsyncIteration:
            return
        except BaseException:
            # 客户端断连时，取消底层事件流，让其中的 CancelledError 分支落库。
            if not next_event.done():
                next_event.cancel()
            try:
                await next_event
            except (StopAsyncIteration, asyncio.CancelledError):
                pass
            raise
        yield event


def _retrieval_observation(result: RetrievalResult) -> dict:
    """检索侧观测字段 —— SSE 追加字段的**唯一构造点**（§14.6 可测化）。

    追加 `path_stats`（三路 + 图路的 covered/unique 计数）与 `graph_degraded_reason`
    （图路降级原因，"" = 正常），让「这一轮图到底补了多少、有没有降级」成为
    SSE 上的**可查询事实**而不是只躺在审计表里 —— 评测脚本（scripts/run_eval.py）
    拿不到审计表，只能从流里读。

    ★ 为什么追加在 `done` / `refused`，不追加在 `meta`：
      `meta` 在检索**之前**下发（`stage="retrieving"` 就是那一刻），那时
      `path_stats` 还不存在。要把它塞进 meta 只能「先检索、再发 meta」，
      首字节延迟会从「立刻」变成「等完整个检索」—— 为一个统计字段牺牲 TTFB 不划算。

    ★ 纯追加：不改既有键名、不删键、不动事件顺序
      （meta → stage → citations → delta → done/refused 是契约，前端依赖它）。
      前端未声明的键会被忽略，多这两个字段不改变渲染。

    ★ 为什么连拒答轮也带，而不是只给成功轮：能拒答说明检索已经跑完，
      图路观测同样有效。只统计成功轮会让 `graph_degraded_reason` 漏掉一部分，
      而「降级发生在哪些轮」恰恰是排障最需要的那部分。
    """
    return {
        "path_stats": dict(result.path_stats),
        "graph_degraded_reason": result.graph_degraded_reason,
    }


def _replay_assistant_response(
    conversation_id: uuid.UUID,
    assistant: Message,
) -> StreamingResponse:
    """把已落库的终态 assistant 消息重新编码为 SSE，不执行任何 RAG 环节。"""
    stored_meta = assistant.meta

    async def replay_stream() -> AsyncIterator[str]:
        yield _sse("meta", {
            "conversation_id": str(conversation_id),
            "message_id": str(assistant.id),
            "stage": "replay",
            "trace_id": trace.get_trace_id(),
            "modality": stored_meta.get("modality", "text"),
        })
        observation = stored_meta.get("retrieval_observation", {})
        if stored_meta.get("refused"):
            reason = stored_meta.get("reason", "NO_RELEVANT_CONTENT")
            message = {
                "NEED_CLARIFICATION": "问题不够明确，请补充具体对象或背景后重试",
                "IMAGE_NO_CONTENT": "图片中未识别到可读内容",
                "VISION_UNAVAILABLE": "图片理解服务暂时不可用，请稍后重试",
                "LLM_TIMEOUT": "响应较慢，请重试",
                "LLM_ERROR": "生成服务异常，请稍后重试",
                "SENSITIVE_OUTPUT": "回答内容包含敏感词，已被拦截",
                "QUOTA_EXCEEDED_USER_DAILY_TOKEN": "今日配额已用尽，请明日再试",
                "QUOTA_EXCEEDED_USER_DAILY_MSG": "今日配额已用尽，请明日再试",
                "QUOTA_EXCEEDED_TENANT_DAILY_TOKEN": "今日配额已用尽，请明日再试",
                "QUOTA_EXCEEDED_TENANT_MONTHLY_TOKEN": "今日配额已用尽，请明日再试",
                "QUOTA_EXCEEDED_APPLICATION_DAILY_TOKEN": "今日配额已用尽，请明日再试",
                "QUOTA_EXCEEDED_APPLICATION_DAILY_MSG": "今日配额已用尽，请明日再试",
                "VECTOR_STORE_DOWN": "知识库暂时不可用，请稍后重试",
                "INTERNAL_ERROR": "服务暂时异常，请稍后重试",
            }.get(reason, "知识库中未找到相关内容")
            yield _sse("refused", {
                "reason": reason,
                "message": message,
                **observation,
            })
            return
        if assistant.citations:
            yield _sse("citations", {"citations": assistant.citations})
        if assistant.content:
            yield _sse("delta", {"text": assistant.content})
        yield _sse("done", {
            "finish_reason": "stopped",
            "grounding": {
                "stripped_sentences": stored_meta.get("grounding_stripped", 0),
            },
            "usage": stored_meta.get("usage", {}),
            "suggestions": stored_meta.get("suggestions", []),
            **observation,
        })

    return StreamingResponse(
        replay_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# 历史答案中的旧引用编号标记，如 [1]、[12]
_CITATION_MARK_RE = re.compile(r"\[\d+\]")


def _strip_citation_marks(content: str) -> str:
    """剥离历史 assistant 消息中的旧 [n] 引用编号。

    本轮 <context> 会重新编号；历史里的旧编号若被模型沿用且恰好落在本轮
    1..N 范围内，L3 引用校验无法识别语义错位（编号合法、指向错误）。
    前端渲染历史走 DB 原文，不受此处 prompt 侧剥离影响。
    """
    return _CITATION_MARK_RE.sub("", content)


def _integration_kb_ids(
    requested_kb_ids: list[uuid.UUID],
    user: CurrentUser,
    application: IntegrationApplication,
) -> list[uuid.UUID]:
    """返回应用白名单、真实用户授权和请求知识库的三重交集。"""
    application_kb_ids = {uuid.UUID(kb_id) for kb_id in application.kb_ids}
    authorized_kb_ids = set(user.authorized_kb_ids)
    return [
        kb_id
        for kb_id in requested_kb_ids
        if kb_id in application_kb_ids and kb_id in authorized_kb_ids
    ]


@router.post("/chat/ask")
async def chat_ask(
    payload: ChatAskRequest,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    _: None = Depends(check_chat_rate_limit),
) -> StreamingResponse:
    return await _chat_ask_impl(payload, user, session)


async def _chat_ask_impl(
    payload: ChatAskRequest,
    user: CurrentUser,
    session: AsyncSession,
    integration: IntegrationApplication | None = None,
) -> StreamingResponse:
    """Web 与集成问答共用主链路，集成模式额外约束会话、权限和配额。"""
    async def audit_record(tenant_id, user_id, action, **kwargs) -> None:
        detail = dict(kwargs.pop("detail", None) or {})
        if integration is not None:
            detail["application_id"] = str(integration.id)
        await audit.record(
            tenant_id, user_id, action, detail=detail, **kwargs
        )

    # ── M7 带图提问：形态判定 + 入参校验（StreamingResponse 之前，可 400）──
    # 图片路径过全仓唯一形态判定点（记 rag.router.decision 指标，INV-M-01）；
    # 纯文本是聊天缺省形态，直接用枚举，不调 detect（无显式信号会抛 UnknownModalityError）
    modality = detect(has_images=True) if payload.images else Modality.TEXT
    _validate_images(payload.images)

    # 集成模式不使用 admin 豁免，严格取应用白名单、用户授权和请求库的三重交集。
    if integration is not None:
        effective_kb_ids = _integration_kb_ids(payload.kb_ids, user, integration)
    elif user.clearance >= 40:
        effective_kb_ids = list(payload.kb_ids)
    else:
        effective_kb_ids = [
            kb_id for kb_id in payload.kb_ids if kb_id in user.authorized_kb_ids
        ]
    if not effective_kb_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="FORBIDDEN")
    kb_rows = (
        await session.execute(
            select(KnowledgeBase).where(
                KnowledgeBase.id.in_(effective_kb_ids),
                KnowledgeBase.tenant_id == user.tenant_id,
            )
        )
    ).scalars().all()
    if len(kb_rows) != len(effective_kb_ids):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="部分知识库不存在")

    # 集成会话只能在原应用、原用户下继续使用，普通 Web 会话也不能跨入口复用。
    if payload.conversation_id is not None:
        integration_conv = await session.get(
            IntegrationConversation, payload.conversation_id
        )
        if integration is not None:
            if (
                integration_conv is None
                or integration_conv.tenant_id != user.tenant_id
                or integration_conv.application_id != integration.id
                or integration_conv.user_id != user.user_id
            ):
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
        elif integration_conv is not None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")

    # 重试先定位并校验原问答。终态立即回放，敏感词、配额、检索与生成均不重复执行。
    retry_assistant: Message | None = None
    user_msg: Message | None = None
    conv: Conversation | None = None
    if payload.retry_message_id is not None:
        if payload.conversation_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="重试必须携带 conversation_id",
            )
        conv = await session.get(Conversation, payload.conversation_id)
        if conv is None or conv.tenant_id != user.tenant_id or conv.user_id != user.user_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
        retry_assistant = await session.get(Message, payload.retry_message_id)
        if (
            retry_assistant is None
            or retry_assistant.tenant_id != user.tenant_id
            or retry_assistant.conversation_id != conv.id
            or retry_assistant.role != "assistant"
        ):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="原回答不存在")
        if retry_assistant.meta.get("generating") is True:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="原回答仍在生成中，请稍后重试",
            )

        # 同一事务创建的 user/assistant 时间戳相同；原始问题因此按 assistant
        # 时间取最近一条 user。之后若已有新消息，则原问答不再是可原位重试的最新问答。
        user_msg = (
            await session.execute(
                select(Message)
                .where(
                    Message.conversation_id == conv.id,
                    Message.role == "user",
                    Message.created_at <= retry_assistant.created_at,
                )
                .order_by(Message.created_at.desc())
                .limit(1)
            )
        ).scalars().first()
        later_message = (
            await session.execute(
                select(Message.id)
                .where(
                    Message.conversation_id == conv.id,
                    Message.created_at > retry_assistant.created_at,
                )
                .limit(1)
            )
        ).scalars().first()
        original_kb_ids = {
            uuid.UUID(kb_id)
            for kb_id in retry_assistant.meta.get("request_kb_ids", [])
        }
        if (
            user_msg is None
            or later_message is not None
            or user_msg.content != payload.question
            or original_kb_ids != set(payload.kb_ids)
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="原问题、知识库范围或最新消息已变化，不能原位重试",
            )
        if retry_assistant.meta.get("interrupted") is not True:
            return _replay_assistant_response(conv.id, retry_assistant)

    # ── M6 续篇：敏感词输入侧检查（feature flag 守护）─────────
    # 命中即拒答（走 refused 事件，不留替换痕迹）
    # 检查放在会话/消息创建之前，避免污染历史
    sensitive_enabled = await flag_service.is_enabled(
        user.tenant_id, decisions.SENSITIVE_FEATURE_KEY, user.dept_path, user.user_id,
    ) if decisions.SENSITIVE_FILTER_ENABLED else False
    if sensitive_enabled:
        hit, word = await sensitive_service.check_input(payload.question, user.tenant_id)
        if hit:
            # 不创建会话/消息，直接返回 refused 流
            async def _sensitive_refused_stream() -> AsyncIterator[str]:
                yield _sse("meta", {
                    "conversation_id": None,
                    "message_id": None,
                    "stage": "blocked",
                    "trace_id": trace.get_trace_id(),
                    "modality": modality.value,
                })
                yield _sse("refused", {
                    "reason": "SENSITIVE_INPUT",
                    "message": "问题包含敏感词，已被拦截",
                })
                await audit_record(
                    user.tenant_id, user.user_id, "chat.sensitive.block",
                    object_type="conversation", object_id=None,
                    detail={"word": word, "side": "input"},
                )
            return StreamingResponse(
                _sensitive_refused_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

    # 复用或创建 conversation；中断重试不再创建任何消息行。
    conversation_id = payload.conversation_id
    request_meta = {
        "generating": True,
        "request_kb_ids": sorted(str(kb_id) for kb_id in payload.kb_ids),
        "modality": modality.value,
    }
    if retry_assistant is not None:
        # 此处 conv / user_msg 已在上方完成归属、原问题和最新消息校验。
        assert conv is not None and user_msg is not None
        conversation_id = conv.id
        message_id = retry_assistant.id
        retry_assistant.content = ""
        retry_assistant.citations = []
        retry_assistant.meta = request_meta
        await session.commit()
        await session.refresh(conv)
    else:
        if conversation_id is not None:
            conv = await session.get(Conversation, conversation_id)
            if conv is None or conv.tenant_id != user.tenant_id or conv.user_id != user.user_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
        else:
            conv = Conversation(
                tenant_id=user.tenant_id,
                user_id=user.user_id,
                title=payload.question[:50],
            )
            session.add(conv)
            await session.flush()
            conversation_id = conv.id
            if integration is not None:
                session.add(
                    IntegrationConversation(
                        conversation_id=conv.id,
                        tenant_id=user.tenant_id,
                        application_id=integration.id,
                        user_id=user.user_id,
                    )
                )

        # 写 user message，并预占位 assistant message（同一事务提交）。
        # meta 下发的 message_id 立即指向真实行，断连后可按该 id 原位重试。
        message_id = uuid.uuid4()
        user_msg = Message(
            tenant_id=user.tenant_id,
            conversation_id=conversation_id,
            role="user",
            content=payload.question,
        )
        assistant_placeholder = Message(
            id=message_id,
            tenant_id=user.tenant_id,
            conversation_id=conversation_id,
            role="assistant",
            content="",
            citations=[],
            meta=request_meta,
        )
        session.add_all([user_msg, assistant_placeholder])
        await session.commit()
        await session.refresh(conv)

    async def event_stream() -> AsyncIterator[str]:
        import time as _time
        _t0 = _time.monotonic()
        # 断连时只保存已经实际下发的正文与引用，不保存模型缓冲区里的不可见内容。
        partial_text = ""
        partial_citations: list[dict] = []
        partial_observation: dict = {}
        try:

            # meta 事件（M7 起增补 modality，事件顺序不变：meta→stage→citations→delta...→done/refused）
            yield _sse("meta", {
                "conversation_id": str(conversation_id),
                "message_id": str(message_id),
                "stage": "retrieving",
                "trace_id": trace.get_trace_id(),
                "modality": modality.value,
            })

            # ── M7 带图提问：视觉抽取 → 与原 question 拼接后进既有链路 ─────
            # ★ 不落库（INV-M-03）：转录文本只存在于本轮内存，不调 upload_document、
            #   不建 Document / ParseJob；user 消息 content 仍是用户原始问题。
            # ★ 异常直抛后映射 refused（INV-M-04）：MUST NOT 退回"只看文本凭先验作答"。
            question_for_pipeline = payload.question
            if modality is Modality.IMAGE:
                try:
                    # ≤3 张并行抽取（gather 保序，首个异常直抛）
                    image_blocks = await asyncio.gather(
                        *(extract_text_from_image(ref) for ref in payload.images)
                    )
                except VisionNoContentError:
                    logger.info("chat.vision: 图中无可读内容，拒答")
                    yield _sse("refused", {
                        "reason": "IMAGE_NO_CONTENT",
                        "message": "图片中未识别到可读内容",
                    })
                    await _persist_assistant(
                        conversation_id, user, message_id,
                        text="", refused=True, citations=[],
                        meta={"refused": True, "reason": "IMAGE_NO_CONTENT",
                              "modality": modality.value},
                    )
                    await audit_record(
                        user.tenant_id, user.user_id, "chat.ask",
                        object_type="conversation", object_id=str(conversation_id),
                        detail={
                            "question": payload.question,
                            "kb_ids": [str(kb) for kb in effective_kb_ids],
                            "doc_ids": [],
                            "refused": True,
                            "refuse_reason": "IMAGE_NO_CONTENT",
                            "image_count": len(payload.images),
                        },
                    )
                    return
                except VisionError as exc:
                    logger.warning("chat.vision: 视觉链路不可用: %s", exc)
                    yield _sse("refused", {
                        "reason": "VISION_UNAVAILABLE",
                        "message": "图片理解服务暂时不可用，请稍后重试",
                    })
                    await _persist_assistant(
                        conversation_id, user, message_id,
                        text="", refused=True, citations=[],
                        meta={"refused": True, "reason": "VISION_UNAVAILABLE",
                              "modality": modality.value},
                    )
                    await audit_record(
                        user.tenant_id, user.user_id, "chat.ask",
                        object_type="conversation", object_id=str(conversation_id),
                        detail={
                            "question": payload.question,
                            "kb_ids": [str(kb) for kb in effective_kb_ids],
                            "doc_ids": [],
                            "refused": True,
                            "refuse_reason": "VISION_UNAVAILABLE",
                            "image_count": len(payload.images),
                        },
                    )
                    return
                transcripts = "\n\n".join(
                    f"[图片{i + 1}转录]\n{block.text}"
                    for i, block in enumerate(image_blocks)
                )
                question_for_pipeline = f"{payload.question}\n\n{transcripts}"
                logger.info("chat.vision: %d 张图转录完成，拼接后进既有链路",
                            len(image_blocks))

            # ── Query 改写（检索前，用历史做指代消解）───────────
            # 提前读历史消息（用于改写指代消解 + 后续生成复用，避免读两次 DB）
            # DB 层只做硬上限（500 行），后续各阶段按 token budget 裁剪
            rewrite_history: list[dict] = []
            async with SessionLocal() as rewrite_session:
                rewrite_rows = (await rewrite_session.execute(
                    select(Message)
                    .where(
                        Message.conversation_id == conversation_id,
                        Message.role.in_(["user", "assistant"]),
                        # 显式排除本轮 user 消息。此前用 [:-1] 切片"跳过最新一条"
                        # 方向写反，实际丢的是最旧一条、本轮问题反而混入历史
                        # （改写与生成各重复一次）。
                        Message.id != user_msg.id,
                    )
                    .order_by(Message.created_at.desc())
                    .limit(decisions.HISTORY_FETCH_MAX_ROWS)
                )).scalars().all()
                # 按时间正序；assistant 占位行（空 content）跳过
                for m in reversed(rewrite_rows):
                    if m.role == "assistant" and not m.content:
                        continue
                    content = (
                        _strip_citation_marks(m.content)
                        if m.role == "assistant"
                        else m.content
                    )
                    rewrite_history.append({"role": m.role, "content": content})

            # 评分 + 改写（一次 LLM 调用完成，score >= 90 直接跳过）
            # 带图时用拼接后的问题（含转录文本），改写模型才能消解对图片内容的指代
            retrieve_query, rewrite_status, rewrite_score = await rewrite_query(
                question_for_pipeline, rewrite_history, user.tenant_id,
            )
            if rewrite_status == "rewritten":
                logger.info("chat.rewrite: score=%d, '%s' → '%s'",
                            rewrite_score, payload.question[:50], retrieve_query[:50])
            else:
                logger.info("chat.rewrite: score=%d, status=%s, 使用原始问题",
                            rewrite_score, rewrite_status)

            # ── 澄清判定（反问用户，不消耗检索）─────────────────────
            # 用独立的澄清阈值（QUERY_CLARIFY_SCORE_THRESHOLD=70），不是改写阈值 90：
            # 70-89 是「有小瑕疵但基本可检索」，照常检索；只有 < 70（指代无法消解
            # / 缺主语）才反问。三条件同时成立才触发，判定逻辑见 decisions.should_clarify。
            # 超时/LLM 失败路径 score 恒回 100（复杂度未知）→ 不触发，维持原行为。
            if decisions.should_clarify(
                rewrite_score, rewrite_status, bool(rewrite_history)
            ):
                logger.info("chat.clarify: score=%d, status=%s, 无历史可消解，反问用户",
                            rewrite_score, rewrite_status)
                yield _sse("refused", {
                    "reason": "NEED_CLARIFICATION",
                    "message": "问题不够明确，请补充具体对象或背景后重试",
                })
                await _persist_assistant(
                    conversation_id, user, message_id,
                    text="", refused=True, citations=[],
                    meta={"refused": True, "reason": "NEED_CLARIFICATION"},
                )
                _total_ms = int((_time.monotonic() - _t0) * 1000)
                await audit_record(
                    user.tenant_id, user.user_id, "chat.ask",
                    object_type="conversation", object_id=str(conversation_id),
                    detail={
                        "question": payload.question,
                        "kb_ids": [str(kb) for kb in effective_kb_ids],
                        "doc_ids": [],
                        "refused": True,
                        "refuse_reason": "NEED_CLARIFICATION",
                        "rewrite_status": rewrite_status,
                        "rewrite_score": rewrite_score,
                    },
                )
                await audit_record(
                    user.tenant_id, user.user_id, "chat.ask.metric",
                    object_type="conversation", object_id=str(conversation_id),
                    detail={
                        "total_ms": _total_ms,
                        "refused": True,
                        "refuse_reason": "NEED_CLARIFICATION",
                        "chunks_count": 0,
                    },
                )
                return

            # 检索（用改写后的 query，原始 question 留给 generation）
            # ★ 走链式检索入口：按 rewrite_score 复杂度信号决定是否拆解为多轮检索，
            #   拆解不可用时内部降级单趟 —— 对下游是同一个 RetrievalResult 契约
            _t_retrieve = _time.monotonic()
            async with SessionLocal() as retrieve_session:
                retrieval = get_retrieval_service()
                try:
                    result = await retrieval.retrieve_chain(
                        retrieve_session,
                        retrieve_query,  # 改写后的 query 只用于检索
                        user.tenant_id,
                        # 必须传 effective_kb_ids（含 admin G2 豁免 + 前端所选库交集），
                        # 传 user.authorized_kb_ids 会导致 admin 豁免失效、且检索范围
                        # 扩大到用户全部已授权库而非本次选择的库
                        effective_kb_ids,
                        user.clearance,
                        user.subjects,
                        rewrite_score=rewrite_score,  # 复杂度信号（复用改写分）
                    )
                except Exception:
                    logger.exception("检索失败")
                    yield _sse("refused", {
                        "reason": "VECTOR_STORE_DOWN",
                        "message": "知识库暂时不可用，请稍后重试",
                    })
                    await _persist_assistant(
                        conversation_id, user, message_id,
                        text="", refused=True, citations=[],
                        meta={"refused": True, "reason": "VECTOR_STORE_DOWN"},
                    )
                    return

            # 检索完成后即使生成阶段断连，也能把本轮观测随部分答案一起落库。
            partial_observation = _retrieval_observation(result)

            # 链式检索：多轮时补一个 stage 事件（前端 SSE switch 无 stage 分支，
            # 未知 stage 会被忽略，新增取值安全）—— 先于 retrieving 告知走了多轮
            if result.chain_mode.startswith("multi"):
                yield _sse("stage", {"stage": "chain", "mode": result.chain_mode})
            yield _sse("stage", {"stage": "retrieving", "ms": result.stage_ms.get("retrieving", 0)})

            _retrieve_ms = int((_time.monotonic() - _t_retrieve) * 1000)
            logger.info("chat.retrieve", extra={
                "user_id": str(user.user_id),
                "retrieve_ms": _retrieve_ms,
                "chunks_found": len(result.chunks),
                "refused": result.refused,
                "chain_mode": result.chain_mode,
            })

            if result.refused:
                yield _sse("refused", {
                    "reason": result.refuse_reason,
                    "message": "知识库中未找到相关内容",
                    # 检索已跑完 → 图路观测有效，与成功轮同口径追加（见
                    # _retrieval_observation 的说明：只统计成功轮会漏掉降级的那部分）
                    **_retrieval_observation(result),
                })
                # 持久化拒答 assistant message
                await _persist_assistant(
                    conversation_id, user, message_id,
                    text="", refused=True, citations=[],
                    meta={
                        "stage_ms": result.stage_ms,
                        "refused": True,
                        "reason": result.refuse_reason,
                        "retrieval_observation": partial_observation,
                    },
                    chain_mode=result.chain_mode,
                )
                # 审计：拒答也记录（refused=True 无引用）
                await audit_record(
                    user.tenant_id, user.user_id, "chat.ask",
                    object_type="conversation", object_id=str(conversation_id),
                    detail={
                        "question": payload.question,
                        "kb_ids": [str(kb) for kb in effective_kb_ids],
                        "doc_ids": [],
                        "refused": True,
                        # 各阶段耗时快照：便于只看审计就能定位慢在哪一环
                        "stage_ms": result.stage_ms,
                    },
                )
                _total_ms = int((_time.monotonic() - _t0) * 1000)
                logger.info("chat.ask.complete", extra={
                    "user_id": str(user.user_id),
                    "total_ms": _total_ms,
                    "retrieve_ms": _retrieve_ms,
                    "refused": True,
                    "refuse_reason": result.refuse_reason,
                    "chunks_count": 0,
                    "conversation_id": str(conversation_id),
                })
                # M6 可观测：指标持久化（供 P95 聚合查询）
                await audit_record(
                    user.tenant_id, user.user_id, "chat.ask.metric",
                    object_type="conversation", object_id=str(conversation_id),
                    detail={
                        "total_ms": _total_ms,
                        "retrieve_ms": _retrieve_ms,
                        "refused": True,
                        "refuse_reason": result.refuse_reason,
                        "chunks_count": 0,
                    },
                )
                return

            # 生成（含 L3 校验，generation 统一处理 LLM 异常）
            # 1. 复用改写阶段提前读的历史（避免重复 DB 查询），做历史压缩
            history: list[dict] = list(rewrite_history)

            # 不再按条数硬压缩（DB 已放开到 500 行），统一交给后面的
            # ensure_context_within_limit 按 token budget 裁剪 + 超限压缩

            # 集成会话只使用当前隔离会话历史，不读取 Web 入口的长期用户画像。
            user_memory: dict = {}
            if integration is None:
                async with SessionLocal() as mem_session:
                    u = await mem_session.get(User, user.user_id)
                    if u and u.memory:
                        user_memory = u.memory or {}

            # ── M6 续篇：配额检查 + 四级降级（feature flag 守护）─────
            # feature flag 关闭 → 跳过配额检查，用原 history/memory
            # feature flag 开启 → 调 check_and_degrade 返回裁剪方案
            # 软阈值 90% 触发降级，硬超限直接 refused
            memory_prompt = build_memory_prompt(user_memory)

            # ── Context 窗口硬约束 ────────────────────────────────
            # system prompt = qa_system（注册表生效版本）+ memory_prompt，
            # 与实际生成阶段拼装口径保持一致
            system_for_estimate = await build_system_prompt(memory_prompt, False, user.tenant_id)
            # 表格决策 A1' + 长文档章节补全 + 图召回：补充片追加在命中片尾部 ——
            # 编号 / citations / 配额裁剪沿用既有机制（L4 按位置裁，超限时补全片
            # 先于命中片被裁，优先级正确）。
            # ★ 拼装顺序的唯一真源在 `assemble_context_chunks`：评测门禁
            #   （eval_service）调用它、这里也调用它 —— 两处各写一份必然漂移，
            #   而漂移的表现是「评测测的不是生产行为」，不会报错。
            current_chunks = assemble_context_chunks(result)

            # 先估算总 token 数，超限则逐轮压缩 history + 裁剪 chunks
            limit_cap = decisions.CONTEXT_WINDOW_LIMIT_TOKENS - decisions.CONTEXT_OUTPUT_RESERVE_TOKENS
            chunks_text_for_est = "\n\n".join(
                f"[{i+1}] {c.content}" for i, c in enumerate(current_chunks)
            )
            est_before = estimate_context_tokens(
                system_for_estimate, memory_prompt, history,
                chunks_text_for_est, question_for_pipeline,
            )
            if est_before > limit_cap:
                history, current_chunks = await ensure_context_within_limit(
                    system_for_estimate, memory_prompt, history,
                    chunks_text_for_est, question_for_pipeline, current_chunks,
                    user.tenant_id,
                )
                logger.warning("chat.context.compressed: est_before=%d → history=%d, chunks=%d (limit=%d)",
                               est_before, len(history), len(current_chunks), limit_cap)

                # ── 压缩次数追踪 ─────────────────────────────
                # 同一个 conversation 内累计压缩次数，超阈值发 SSE 提示
                async with SessionLocal() as comp_session:
                    conv_for_comp = await comp_session.get(Conversation, conversation_id)
                    if conv_for_comp is not None:
                        conv_for_comp.compression_count = (conv_for_comp.compression_count or 0) + 1
                        await comp_session.commit()
                        current_count = conv_for_comp.compression_count
                        logger.info("chat.compression.count: conv=%s, count=%d",
                                    conversation_id, current_count)
                        if current_count >= decisions.COMPRESSION_WARN_THRESHOLD:
                            # 发 SSE 提示事件（在 citations 之前，前端可提前显示）
                            yield _sse("context_warning", {
                                "compression_count": current_count,
                                "threshold": decisions.COMPRESSION_WARN_THRESHOLD,
                                "need_new_conversation": True,
                                "message": "对话上下文已被压缩多次，建议新开对话重置上下文以获得更好的回答效果。",
                            })

            degraded_from = "none"
            effective_chunks = current_chunks  # context 检查后的 chunks 作为初始值
            quota_refuse_reason: str | None = None
            if integration is not None:
                quota_refuse_reason = await quota_service.check_application_quota(
                    integration, question_for_pipeline, history, memory_prompt, effective_chunks,
                )

            # 集成调用始终同时执行应用与真实用户配额，Web 仍沿用原灰度开关。
            quota_enabled = integration is not None or await flag_service.is_enabled(
                user.tenant_id, decisions.QUOTA_FEATURE_KEY, user.dept_path, user.user_id,
            )
            plan = None
            if quota_refuse_reason is None and quota_enabled:
                plan = await quota_service.check_and_degrade(
                    user.tenant_id, user.user_id,
                    question_for_pipeline, history, memory_prompt, effective_chunks,
                )
                if plan.refused:
                    quota_refuse_reason = plan.refuse_reason

            if quota_refuse_reason is not None:
                yield _sse("refused", {
                    "reason": quota_refuse_reason,
                    "message": "今日配额已用尽，请明日再试",
                })
                await _persist_assistant(
                    conversation_id, user, message_id,
                    text="", refused=True, citations=[],
                    meta={
                        "stage_ms": result.stage_ms,
                        "refused": True,
                        "reason": quota_refuse_reason,
                        "retrieval_observation": partial_observation,
                    },
                    chain_mode=result.chain_mode,
                )
                await audit_record(
                    user.tenant_id, user.user_id, "chat.quota.exceeded",
                    object_type="conversation", object_id=str(conversation_id),
                    detail={"reason": quota_refuse_reason},
                )
                return

            if plan is not None:
                # 用降级方案替换原参数
                history = plan.history
                memory_prompt = plan.memory_prompt
                effective_chunks = plan.chunks
                degraded_from = plan.degraded_from
                if degraded_from != "none":
                    logger.info("chat.quota.degraded", extra={
                        "user_id": str(user.user_id),
                        "level": degraded_from,
                        "history_kept": len(history),
                        "chunks_kept": len(effective_chunks),
                    })

            # ── 注入防护（§4.21 输入侧防线）──
            # 命中**不丢弃**片段（制度文本里「请忽略」是正常措辞），只标记降权 + 打指标。
            # 必须在 citations 构造之前执行 —— 降权会改变顺序，
            # 而 citations 的 [n] 编号必须与生成 prompt 中的编号一致。
            injection_hits = injection_guard.scan_chunks(effective_chunks)
            if injection_hits:
                effective_chunks = injection_guard.demote(effective_chunks, injection_hits)

            # citations 基于 effective_chunks 构造：编号必须与生成 prompt 中的 [n] 一致，
            # 否则配额裁剪后 prompt 重编号会与前端引用载荷错位。MUST 在 delta 之前下发。
            citations_payload = [
                {
                    "n": i + 1,
                    "chunk_id": str(c.chunk_id),
                    "doc_id": str(c.document_id),
                    "filename": c.filename,
                    "heading_path": c.heading_path,
                    "page_no": c.page_no,
                    "score": c.display_score,
                    "snippet": hit_snippet(c.content, question_for_pipeline),
                    # M8-P3b 附件：原始文件下载链接（WU-M-19 鉴权代理端点，
                    # 相对路径满足 INV-M-16 —— 禁止直出 MinIO 地址）
                    "raw_url": f"/api/documents/{c.document_id}/raw",
                }
                for i, c in enumerate(effective_chunks)
            ]
            partial_citations = citations_payload
            yield _sse("citations", {"citations": citations_payload})

            # 审计：谁在何时问了什么、引用到哪些文档（手册 F4 / M5 任务 3）
            await audit_record(
                user.tenant_id, user.user_id, "chat.ask",
                object_type="conversation", object_id=str(conversation_id),
                detail={
                    "question": payload.question,
                    "kb_ids": [str(kb) for kb in effective_kb_ids],
                    "doc_ids": sorted({c["doc_id"] for c in citations_payload}),
                    "refused": False,
                    # 带图提问留痕（图片本体与转录文本不落库，只记张数）
                    "image_count": len(payload.images),
                    # 各阶段耗时快照：便于只看审计就能定位慢在哪一环
                    "stage_ms": result.stage_ms,
                    # 口径快照：本条答案由哪个 contract / 模型 / 提示词版本产出（方案二）
                    # chain_mode 为本次检索侧链式推理模式（off / single / multi:N）
                    "caliber": await snapshot_for_message(user.tenant_id, result.chain_mode),
                },
            )

            # 4. 生成（注入画像 + 压缩后的历史 + 可能裁剪后的 chunks）
            #    真流式透传：LLM delta 经行级 grounding / 敏感词 / PII 后立即推给前端
            generation = get_stream_generation_service()
            gen_result = None  # 最终结果（done/refused 时由 stream 填充）
            final_text = ""
            final_stripped = 0
            final_suggestions: list[str] = []
            final_usage: dict = {}
            final_reasoning = ""
            # 首字延迟：从请求起点 _t0（含敏感词/改写/检索）到第一个非空 delta 的间隔
            _ttft_ms: int | None = None
            # 开 CoT 时，前端在收到首个 delta 之前要等"推理段"跑完（推理链不推送），
            # 这里先发一个 stage 事件，让前端有机会显示"思考中"。
            # 前端目前忽略未知 stage 值，所以不加处理也不会坏。
            if decisions.should_enable_cot(rewrite_score):
                yield _sse("stage", {"stage": "reasoning"})

            async for stream_evt in generation.stream_generate(
                question_for_pipeline, effective_chunks,
                history=history,
                memory_prompt=memory_prompt,
                tenant_id=user.tenant_id,
                rewrite_score=rewrite_score,
            ):
                if stream_evt.type == "delta":
                    # 以第一个**非空** delta 为准：空 delta 不代表用户看到内容
                    if stream_evt.text and _ttft_ms is None:
                        _ttft_ms = int((_time.monotonic() - _t0) * 1000)
                    partial_text += stream_evt.text
                    yield _sse("delta", {"text": stream_evt.text})
                elif stream_evt.type == "refused":
                    gen_result = stream_evt
                    break
                elif stream_evt.type == "done":
                    gen_result = stream_evt
                    final_text = stream_evt.full_text
                    final_stripped = stream_evt.stripped_sentences
                    final_suggestions = stream_evt.suggestions
                    final_usage = stream_evt.usage
                    final_reasoning = stream_evt.reasoning

            if gen_result is not None and gen_result.refused:
                reason = gen_result.refuse_reason
                user_message = {
                    "NO_RELEVANT_CONTENT": "知识库中未找到相关内容",
                    "UNGROUNDED": "知识库中未找到相关内容",
                    "LLM_TIMEOUT": "响应较慢，请重试",
                    "LLM_ERROR": "生成服务异常，请稍后重试",
                    "SENSITIVE_OUTPUT": "回答内容包含敏感词，已被拦截",
                }.get(reason, "知识库中未找到相关内容")
                yield _sse("refused", {
                    "reason": reason,
                    "message": user_message,
                    **_retrieval_observation(result),
                })
                await _persist_assistant(
                    conversation_id, user, message_id,
                    text="", refused=True, citations=citations_payload,
                    meta={
                        "stage_ms": result.stage_ms,
                        "refused": True,
                        "reason": reason,
                        "grounding_stripped": gen_result.stripped_sentences,
                        "retrieval_observation": partial_observation,
                    },
                    chain_mode=result.chain_mode,
                )
                _total_ms = int((_time.monotonic() - _t0) * 1000)
                logger.info("chat.ask.complete", extra={
                    "user_id": str(user.user_id),
                    "total_ms": _total_ms,
                    "retrieve_ms": _retrieve_ms,
                    "refused": True,
                    "refuse_reason": reason,
                    "chunks_count": len(result.chunks),
                    "grounding_stripped": gen_result.stripped_sentences,
                    "conversation_id": str(conversation_id),
                })
                await audit_record(
                    user.tenant_id, user.user_id, "chat.ask.metric",
                    object_type="conversation", object_id=str(conversation_id),
                    detail={
                        "total_ms": _total_ms,
                        "retrieve_ms": _retrieve_ms,
                        "refused": True,
                        "refuse_reason": reason,
                        "chunks_count": len(result.chunks),
                        "grounding_stripped": gen_result.stripped_sentences,
                        **result.path_stats,
                    },
                )
                return

            # done — 先做不影响客户端的 Redis/DB 操作（网络延迟不影响流式）
            # 再 yield done，避免 yield 之后客户端已断开导致后续 await 失败
            # ── M6 续篇：配额用量上报（Redis 计数器累加）─────────
            try:
                await quota_service._incr_usage(
                    user.tenant_id, user.user_id,
                    tokens=final_usage.get("prompt_tokens", 0) + final_usage.get("completion_tokens", 0),
                    application_id=integration.id if integration is not None else None,
                )
            except Exception:
                logger.warning("quota._incr_usage 失败，忽略", exc_info=True)

            # 终态必须先落库再发 done：客户端收到 done 即可安全刷新或回放，
            # 不会出现「流已完成但数据库仍是 generating」的窗口。
            await _persist_assistant(
                conversation_id, user, message_id,
                text=final_text, refused=False, citations=citations_payload,
                meta={
                    "stage_ms": result.stage_ms,
                    "grounding_stripped": final_stripped,
                    "usage": final_usage,
                    "suggestions": final_suggestions,
                    "quota_degraded_from": degraded_from,
                    "retrieval_observation": partial_observation,
                    # 思维链只落库、不外发（decisions.COT_REASONING_EXPOSED = False）。
                    # 落库前**必须脱敏**：推理里常直接抄 <context> 内容，
                    # 那里面可能带手机号/身份证（正文走 mask_pii，推理链不经过那条路）。
                    "reasoning": mask_pii(final_reasoning) if final_reasoning else "",
                },
                chain_mode=result.chain_mode,
            )

            yield _sse("done", {
                "finish_reason": "stopped",
                "grounding": {"stripped_sentences": final_stripped},
                "usage": final_usage,
                "suggestions": final_suggestions,
                # 检索侧观测（§14.6）：纯追加，既有四个键一字不动。
                # 落在 done 而不是 meta：meta 发在检索之前，那时还没有 path_stats。
                **_retrieval_observation(result),
            })

            # M6 可观测：记录端到端指标
            _total_ms = int((_time.monotonic() - _t0) * 1000)
            logger.info("chat.ask.complete", extra={
                "user_id": str(user.user_id),
                "total_ms": _total_ms,
                "ttft_ms": _ttft_ms,
                "retrieve_ms": _retrieve_ms,
                "refused": False,
                "refuse_reason": None,
                "chunks_count": len(result.chunks),
                "grounding_stripped": final_stripped,
                "prompt_tokens": final_usage.get("prompt_tokens", 0),
                "completion_tokens": final_usage.get("completion_tokens", 0),
                "conversation_id": str(conversation_id),
            })
            await audit_record(
                user.tenant_id, user.user_id, "chat.ask.metric",
                object_type="conversation", object_id=str(conversation_id),
                detail={
                    "total_ms": _total_ms,
                    "ttft_ms": _ttft_ms,
                    "retrieve_ms": _retrieve_ms,
                    "refused": False,
                    "refuse_reason": None,
                    "chunks_count": len(result.chunks),
                    "grounding_stripped": final_stripped,
                    "prompt_tokens": final_usage.get("prompt_tokens", 0),
                    "completion_tokens": final_usage.get("completion_tokens", 0),
                    **result.path_stats,
                },
            )

            # 集成入口不写长期用户画像，避免不同应用和 Web 入口之间记忆串联。
            if integration is None:
                asyncio.create_task(
                    _update_user_memory(user.user_id, payload.question, final_text)
                )

        except asyncio.CancelledError:
            # 客户端断连不是拒答：保存已流出的部分正文，允许用同一 message_id 重试。
            try:
                await _persist_interrupted(
                    message_id,
                    text=partial_text,
                    citations=partial_citations,
                    meta={
                        "modality": modality.value,
                        "retrieval_observation": partial_observation,
                    },
                )
            except Exception:
                logger.exception("持久化中断 assistant 消息失败")
            raise
        except Exception as exc:
            # 顶层兜底：event_stream 任何未捕获异常都会导致已持久化的 user 消息
            # 没有对应的 assistant 回复（孤立 user 消息）。这里持久化一个失败
            # 的 assistant 消息，保持对话完整性。
            logger.exception("chat.event_stream 未捕获异常")
            yield _sse("refused", {
                "reason": "INTERNAL_ERROR",
                "message": "服务暂时异常，请稍后重试",
            })
            try:
                await _persist_assistant(
                    conversation_id, user, message_id,
                    text="", refused=True, citations=[],
                    meta={
                        "refused": True,
                        "reason": "INTERNAL_ERROR",
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
            except Exception:
                logger.exception("持久化失败 assistant 消息也失败了")


    return StreamingResponse(
        _with_heartbeat(event_stream()),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _persist_assistant(
    conversation_id: uuid.UUID,
    user: CurrentUser,
    message_id: uuid.UUID,
    text: str,
    refused: bool,
    citations: list[dict],
    meta: dict,
    chain_mode: str | None = None,
) -> None:
    """更新流开始前占位的 assistant message（citations 存快照，手册 §4.2.9）。

    占位行已在 chat_ask 中与 user message 同事务提交，此处按 message_id 定位更新；
    meta 去掉 generating 标记并写入 refused 等最终状态。
    chain_mode 为本次检索侧链式推理模式（本请求可变），并入口径快照。
    """
    async with SessionLocal() as session:
        msg = await session.get(Message, message_id)
        if msg is None:
            # 占位行与 user message 同事务提交，正常必存在；缺失说明数据被外部清理
            raise RuntimeError(f"assistant 占位消息不存在: {message_id}")
        msg.content = text
        msg.citations = citations
        # 口径快照（方案二）：记录本条答案由哪个 contract / 模型 / 提示词版本产出。
        # 在此**单点注入** —— 正常 / 拒答各条落库路径自动带上，无需逐处调用点传参。
        caliber = await snapshot_for_message(user.tenant_id, chain_mode)
        # trace_id 在此单点注入：正常 / 拒答各条落库路径自动带上，前端历史里可复制
        msg.meta = {
            **msg.meta,
            **meta,
            "generating": False,
            "refused": refused,
            "caliber": caliber,
            "trace_id": trace.get_trace_id(),
        }
        await session.commit()


async def _persist_interrupted(
    message_id: uuid.UUID,
    text: str,
    citations: list[dict],
    meta: dict,
) -> None:
    """把仍在生成的占位行标记为中断，并保留客户端已经看到的部分答案。

    若原消息已经由其他路径写成终态则不再改动，避免断连清理覆盖 done/refused。
    """
    async with SessionLocal() as session:
        msg = await session.get(Message, message_id)
        if msg is None or msg.meta.get("generating") is not True:
            return
        msg.content = text
        msg.citations = citations
        msg.meta = {
            **msg.meta,
            **meta,
            "generating": False,
            "interrupted": True,
            "refused": False,
            "trace_id": trace.get_trace_id(),
        }
        await session.commit()


async def _update_user_memory(
    user_id: uuid.UUID,
    user_msg: str,
    assistant_msg: str,
) -> None:
    """后台更新用户画像。静默失败不阻塞主流程。"""
    try:
        async with SessionLocal() as session:
            u = await session.get(User, user_id)
            if u is None:
                return
            new_memory = await extract_facts(
                user_msg,
                assistant_msg,
                u.memory or {},
                trace_id=trace.get_trace_id() or "",
                tenant_id=u.tenant_id,
            )
            u.memory = new_memory
            await session.commit()
    except Exception:
        logger.exception("用户画像更新失败")


# ── 会话管理接口（多轮对话历史） ────────────────────────────────


@router.get("/conversations")
async def list_conversations(
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> list[dict]:
    """当前用户的会话列表（最近 50 条，按最后消息时间倒序）。"""
    # 子查询：每个会话最后一条消息的时间
    from sqlalchemy import func, desc

    last_msg_sq = (
        select(
            Message.conversation_id,
            func.max(Message.created_at).label("last_at"),
        )
        .group_by(Message.conversation_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(
                Conversation.id,
                Conversation.title,
                Conversation.created_at,
                func.coalesce(last_msg_sq.c.last_at, Conversation.created_at).label("last_at"),
            )
            .outerjoin(last_msg_sq, last_msg_sq.c.conversation_id == Conversation.id)
            .outerjoin(
                IntegrationConversation,
                IntegrationConversation.conversation_id == Conversation.id,
            )
            .where(
                Conversation.tenant_id == user.tenant_id,
                Conversation.user_id == user.user_id,
                IntegrationConversation.conversation_id.is_(None),
            )
            .order_by(desc("last_at"))
            .limit(50)
        )
    ).all()
    return [
        {
            "id": str(r.id),
            "title": r.title or "新对话",
            "created_at": r.created_at.isoformat(),
            "last_at": r.last_at.isoformat() if r.last_at else None,
        }
        for r in rows
    ]


@router.get("/conversations/{conversation_id}/messages")
async def get_conversation_messages(
    conversation_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> list[dict]:
    """加载某会话的全部消息（按时间正序）。

    过滤 meta.generating=true 的占位行：流式期间客户端断连会残留未落定的空
    assistant 消息，此类消息视为本次回答作废，不在历史中展示。
    """
    conv = await session.get(Conversation, conversation_id)
    integration_conv = await session.get(IntegrationConversation, conversation_id)
    if (
        conv is None
        or conv.tenant_id != user.tenant_id
        or conv.user_id != user.user_id
        or integration_conv is not None
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
    rows = (
        await session.execute(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                # IS NOT true：generating 缺失(NULL)或 false 都保留，仅 true 被过滤
                Message.meta["generating"].as_boolean().isnot(True),
            )
            .order_by(Message.created_at)
        )
    ).scalars().all()
    return [
        {
            "id": str(m.id),
            "role": m.role,
            "content": m.content,
            "citations": m.citations,
            "meta": m.meta,
            "created_at": m.created_at.isoformat(),
        }
        for m in rows
    ]


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> None:
    """删除会话（级联删除消息）。"""
    conv = await session.get(Conversation, conversation_id)
    integration_conv = await session.get(IntegrationConversation, conversation_id)
    if (
        conv is None
        or conv.tenant_id != user.tenant_id
        or conv.user_id != user.user_id
        or integration_conv is not None
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
    await session.delete(conv)
    await session.commit()
