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
from app.config import decisions
from app.database import SessionLocal
from app.models import Conversation, KnowledgeBase, Message, User
from app.schemas.chat import ChatAskRequest
from app.services import audit
from app.services.generate import get_generation_service
from app.services.memory import build_memory_prompt, compress_history, extract_facts
from app.services.retrieve import get_retrieval_service

logger = logging.getLogger(__name__)
router = APIRouter(tags=["chat"])


def _sse(event: str, data: dict) -> str:
    """构造 SSE 事件块。data 用 JSON 序列化。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


async def _simulate_stream(text: str) -> AsyncIterator[str]:
    """模拟流式推送：3 字符/片，间隔 100ms（约 30 字符/秒，用户偏好可读速度）。"""
    chars = list(text)
    step = 3
    for start in range(0, len(chars), step):
        chunk_text = "".join(chars[start : start + step])
        yield _sse("delta", {"text": chunk_text})
        await asyncio.sleep(0.1)


@router.post("/chat/ask")
async def chat_ask(
    payload: ChatAskRequest,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    # 校验 kb_ids 与 user.authorized_kb_ids 求交集（手册 §3.2.7 G2：前端可能传无权限的库）
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
        # meta 事件
        yield _sse("meta", {
            "conversation_id": str(conversation_id),
            "message_id": str(message_id),
            "stage": "retrieving",
        })

        # 检索
        async with SessionLocal() as retrieve_session:
            retrieval = get_retrieval_service()
            try:
                result = await retrieval.retrieve(
                    retrieve_session,
                    payload.question,
                    user.tenant_id,
                    user.authorized_kb_ids,
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
            return

        # citations（MUST 在 delta 之前）
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
            for i, c in enumerate(result.chunks)
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
        if len(history) > decisions.MEMORY_COMPRESS_THRESHOLD:
            history = await compress_history(history, decisions.MAX_HISTORY_TURNS)

        # 3. 读用户画像
        user_memory: dict = {}
        async with SessionLocal() as mem_session:
            u = await mem_session.get(User, user.user_id)
            if u and u.memory:
                user_memory = u.memory or {}

        # 4. 生成（注入画像 + 压缩后的历史）
        generation = get_generation_service()
        gen_result = await generation.generate(
            payload.question, result.chunks,
            history=history,
            memory_prompt=build_memory_prompt(user_memory),
        )

        if gen_result.refused:
            reason = gen_result.refuse_reason
            user_message = {
                "NO_RELEVANT_CONTENT": "知识库中未找到相关内容",
                "UNGROUNDED": "知识库中未找到相关内容",
                "LLM_TIMEOUT": "响应较慢，请重试",
                "LLM_ERROR": "生成服务异常，请稍后重试",
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
            return

        # 模拟流式推送最终文本
        async for delta_event in _simulate_stream(gen_result.text):
            yield delta_event

        # done
        yield _sse("done", {
            "finish_reason": "stopped",
            "grounding": {"stripped_sentences": gen_result.stripped_sentences},
            "usage": gen_result.usage,
        })

        # 持久化
        await _persist_assistant(
            conversation_id, user, message_id,
            text=gen_result.text, refused=False, citations=citations_payload,
            meta={
                "stage_ms": result.stage_ms,
                "grounding_stripped": gen_result.stripped_sentences,
                "usage": gen_result.usage,
            },
        )

        # 后台更新用户画像（不阻塞流式响应）
        asyncio.create_task(_update_user_memory(user.user_id, payload.question, gen_result.text))

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
