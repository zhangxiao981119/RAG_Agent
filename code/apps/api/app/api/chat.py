"""Chat SSE 接口 —— 手册 §5.2 POST /api/chat/ask。

SSE 事件顺序：
  meta → stage(retrieving) → [refused | citations → delta... → done]

MUST 遵守（手册 §5.2 约束）：
  1. kb_ids 与用户 authorized_kb_ids 求交集（M2 信任前端，M4 补）
  2. citations MUST 在 delta 之前
  3. 拒答 MUST 发 refused 事件，MUST NOT 用 delta 发拒答文案
  4. delta 是增量文本，前端累加
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user, get_db
from app.api.rate_limit import check_chat_rate_limit
from app.config import decisions
from app.database import SessionLocal
from app.models import Conversation, KnowledgeBase, Message, User
from app.schemas.chat import ChatAskRequest
from app.services import audit
from app.services import feature_flag as flag_service
from app.services import quota as quota_service
from app.services import sensitive as sensitive_service
from app.services.generate import get_stream_generation_service
from app.services.memory import build_memory_prompt, compress_history, extract_facts
from app.services.retrieve import get_retrieval_service

logger = logging.getLogger(__name__)
router = APIRouter(tags=["chat"])


def _sse(event: str, data: dict) -> str:
    """构造 SSE 事件块。data 用 JSON 序列化。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@router.post("/chat/ask")
async def chat_ask(
    payload: ChatAskRequest,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    _: None = Depends(check_chat_rate_limit),
) -> StreamingResponse:
    # admin 豁免 G2 库级授权校验；普通用户求交集（手册 §3.2.7 G2）
    if user.clearance >= 40:
        effective_kb_ids = list(payload.kb_ids)
    else:
        effective_kb_ids = [
            kb for kb in payload.kb_ids if kb in user.authorized_kb_ids
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
                })
                yield _sse("refused", {
                    "reason": "SENSITIVE_INPUT",
                    "message": "问题包含敏感词，已被拦截",
                })
                await audit.record(
                    user.tenant_id, user.user_id, "chat.sensitive.block",
                    object_type="conversation", object_id=None,
                    detail={"word": word, "side": "input"},
                )
            return StreamingResponse(
                _sensitive_refused_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

    # 复用或创建 conversation
    conversation_id = payload.conversation_id
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

    # 写 user message
    user_msg = Message(
        tenant_id=user.tenant_id,
        conversation_id=conversation_id,
        role="user",
        content=payload.question,
    )
    session.add(user_msg)
    await session.commit()
    await session.refresh(conv)
    message_id = uuid.uuid4()

    async def event_stream() -> AsyncIterator[str]:
        import time as _time
        _t0 = _time.monotonic()

        # meta 事件
        yield _sse("meta", {
            "conversation_id": str(conversation_id),
            "message_id": str(message_id),
            "stage": "retrieving",
        })

        # 检索
        _t_retrieve = _time.monotonic()
        async with SessionLocal() as retrieve_session:
            retrieval = get_retrieval_service()
            try:
                result = await retrieval.retrieve(
                    retrieve_session,
                    payload.question,
                    user.tenant_id,
                    # 必须传 effective_kb_ids（含 admin G2 豁免 + 前端所选库交集），
                    # 传 user.authorized_kb_ids 会导致 admin 豁免失效、且检索范围
                    # 扩大到用户全部已授权库而非本次选择的库
                    effective_kb_ids,
                    user.clearance,
                    user.subjects,
                )
            except Exception as exc:
                logger.exception("检索失败")
                yield _sse("refused", {
                    "reason": "VECTOR_STORE_DOWN",
                    "message": "知识库暂时不可用，请稍后重试",
                })
                return

        yield _sse("stage", {"stage": "retrieving", "ms": result.stage_ms.get("retrieving", 0)})

        _retrieve_ms = int((_time.monotonic() - _t_retrieve) * 1000)
        logger.info("chat.retrieve", extra={
            "user_id": str(user.user_id),
            "retrieve_ms": _retrieve_ms,
            "chunks_found": len(result.chunks),
            "refused": result.refused,
        })

        if result.refused:
            yield _sse("refused", {
                "reason": result.refuse_reason,
                "message": "知识库中未找到相关内容",
            })
            # 持久化拒答 assistant message
            await _persist_assistant(
                conversation_id, user, message_id,
                text="", refused=True, citations=[],
                meta={"stage_ms": result.stage_ms, "refused": True},
            )
            # 审计：拒答也记录（refused=True 无引用）
            await audit.record(
                user.tenant_id, user.user_id, "chat.ask",
                object_type="conversation", object_id=str(conversation_id),
                detail={
                    "question": payload.question,
                    "kb_ids": [str(kb) for kb in effective_kb_ids],
                    "doc_ids": [],
                    "refused": True,
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
            await audit.record(
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
        # 1. 读最近 N 条历史消息作为上下文
        history: list[dict] = []
        async with SessionLocal() as hist_session:
            hist_rows = (await hist_session.execute(
                select(Message)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.role.in_(["user", "assistant"]),
                )
                .order_by(Message.created_at.desc())
                .limit(max(decisions.MAX_HISTORY_TURNS + 1, decisions.MEMORY_COMPRESS_THRESHOLD + 1))
            )).scalars().all()
            # 按时间正序，跳过最新那条（就是当前正在处理的 user message）
            for m in reversed(hist_rows[:-1]):
                # 跳过被拒答的 assistant 消息（content 为空）
                if m.role == "assistant" and not m.content:
                    continue
                history.append({"role": m.role, "content": m.content})

        # 2. 历史过长时压缩（把较早的合并成摘要）
        # >= 而非 > ：达到阈值即触发，否则 limit(N+1)+[:-1] 最多 N 条永不压缩
        if len(history) >= decisions.MEMORY_COMPRESS_THRESHOLD:
            history = await compress_history(history, decisions.MAX_HISTORY_TURNS)

        # 3. 读用户画像
        user_memory: dict = {}
        async with SessionLocal() as mem_session:
            u = await mem_session.get(User, user.user_id)
            if u and u.memory:
                user_memory = u.memory or {}

        # ── M6 续篇：配额检查 + 四级降级（feature flag 守护）─────
        # feature flag 关闭 → 跳过配额检查，用原 history/memory
        # feature flag 开启 → 调 check_and_degrade 返回裁剪方案
        # 软阈值 90% 触发降级，硬超限直接 refused
        memory_prompt = build_memory_prompt(user_memory)
        degraded_from = "none"
        effective_chunks = result.chunks
        quota_enabled = await flag_service.is_enabled(
            user.tenant_id, decisions.QUOTA_FEATURE_KEY, user.dept_path, user.user_id,
        )
        if quota_enabled:
            plan = await quota_service.check_and_degrade(
                user.tenant_id, user.user_id,
                payload.question, history, memory_prompt, result.chunks,
            )
            if plan.refused:
                yield _sse("refused", {
                    "reason": plan.refuse_reason,
                    "message": "今日配额已用尽，请明日再试",
                })
                await _persist_assistant(
                    conversation_id, user, message_id,
                    text="", refused=True, citations=[],
                    meta={
                        "stage_ms": result.stage_ms,
                        "refused": True,
                        "reason": plan.refuse_reason,
                    },
                )
                await audit.record(
                    user.tenant_id, user.user_id, "chat.quota.exceeded",
                    object_type="conversation", object_id=str(conversation_id),
                    detail={"reason": plan.refuse_reason},
                )
                return
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
                "snippet": c.content[:200],
            }
            for i, c in enumerate(effective_chunks)
        ]
        yield _sse("citations", {"citations": citations_payload})

        # 审计：谁在何时问了什么、引用到哪些文档（手册 F4 / M5 任务 3）
        await audit.record(
            user.tenant_id, user.user_id, "chat.ask",
            object_type="conversation", object_id=str(conversation_id),
            detail={
                "question": payload.question,
                "kb_ids": [str(kb) for kb in effective_kb_ids],
                "doc_ids": sorted({c["doc_id"] for c in citations_payload}),
                "refused": False,
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
        async for stream_evt in generation.stream_generate(
            payload.question, effective_chunks,
            history=history,
            memory_prompt=memory_prompt,
            tenant_id=user.tenant_id,
        ):
            if stream_evt.type == "delta":
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

        if gen_result is not None and gen_result.refused:
            reason = gen_result.refuse_reason
            user_message = {
                "NO_RELEVANT_CONTENT": "知识库中未找到相关内容",
                "UNGROUNDED": "知识库中未找到相关内容",
                "LLM_TIMEOUT": "响应较慢，请重试",
                "LLM_ERROR": "生成服务异常，请稍后重试",
                "SENSITIVE_OUTPUT": "回答内容包含敏感词，已被拦截",
            }.get(reason, "知识库中未找到相关内容")
            yield _sse("refused", {"reason": reason, "message": user_message})
            await _persist_assistant(
                conversation_id, user, message_id,
                text="", refused=True, citations=citations_payload,
                meta={
                    "stage_ms": result.stage_ms,
                    "refused": True,
                    "reason": reason,
                    "grounding_stripped": gen_result.stripped_sentences,
                },
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
            await audit.record(
                user.tenant_id, user.user_id, "chat.ask.metric",
                object_type="conversation", object_id=str(conversation_id),
                detail={
                    "total_ms": _total_ms,
                    "retrieve_ms": _retrieve_ms,
                    "refused": True,
                    "refuse_reason": reason,
                    "chunks_count": len(result.chunks),
                    "grounding_stripped": gen_result.stripped_sentences,
                },
            )
            return

        # done
        yield _sse("done", {
            "finish_reason": "stopped",
            "grounding": {"stripped_sentences": final_stripped},
            "usage": final_usage,
            "suggestions": final_suggestions,
        })

        # M6 可观测：记录端到端指标
        _total_ms = int((_time.monotonic() - _t0) * 1000)
        logger.info("chat.ask.complete", extra={
            "user_id": str(user.user_id),
            "total_ms": _total_ms,
            "retrieve_ms": _retrieve_ms,
            "refused": False,
            "refuse_reason": None,
            "chunks_count": len(result.chunks),
            "grounding_stripped": final_stripped,
            "prompt_tokens": final_usage.get("prompt_tokens", 0),
            "completion_tokens": final_usage.get("completion_tokens", 0),
            "conversation_id": str(conversation_id),
        })
        await audit.record(
            user.tenant_id, user.user_id, "chat.ask.metric",
            object_type="conversation", object_id=str(conversation_id),
            detail={
                "total_ms": _total_ms,
                "retrieve_ms": _retrieve_ms,
                "refused": False,
                "refuse_reason": None,
                "chunks_count": len(result.chunks),
                "grounding_stripped": final_stripped,
                "prompt_tokens": final_usage.get("prompt_tokens", 0),
                "completion_tokens": final_usage.get("completion_tokens", 0),
            },
        )

        # ── M6 续篇：配额用量上报（Redis 计数器累加）─────────
        await quota_service._incr_usage(
            user.tenant_id, user.user_id,
            tokens=final_usage.get("prompt_tokens", 0) + final_usage.get("completion_tokens", 0),
        )

        # 持久化
        await _persist_assistant(
            conversation_id, user, message_id,
            text=final_text, refused=False, citations=citations_payload,
            meta={
                "stage_ms": result.stage_ms,
                "grounding_stripped": final_stripped,
                "usage": final_usage,
                "suggestions": final_suggestions,
                "quota_degraded_from": degraded_from,
            },
        )

        # 后台更新用户画像（不阻塞流式响应）
        asyncio.create_task(_update_user_memory(user.user_id, payload.question, final_text))

    return StreamingResponse(
        event_stream(),
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
) -> None:
    """持久化 assistant message。citations 存快照（手册 §4.2.9）。"""
    async with SessionLocal() as session:
        msg = Message(
            id=message_id,
            tenant_id=user.tenant_id,
            conversation_id=conversation_id,
            role="assistant",
            content=text,
            citations=citations,
            meta={**meta, "refused": refused},
        )
        session.add(msg)
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
            new_memory = await extract_facts(user_msg, assistant_msg, u.memory or {})
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
            .where(Conversation.tenant_id == user.tenant_id, Conversation.user_id == user.user_id)
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
    """加载某会话的全部消息（按时间正序）。"""
    conv = await session.get(Conversation, conversation_id)
    if conv is None or conv.tenant_id != user.tenant_id or conv.user_id != user.user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
    rows = (
        await session.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id)
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
    if conv is None or conv.tenant_id != user.tenant_id or conv.user_id != user.user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
    await session.delete(conv)
    await session.commit()
