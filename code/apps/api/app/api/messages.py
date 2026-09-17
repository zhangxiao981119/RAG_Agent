"""消息级操作 API —— 采纳回答为微调样本。

采纳语义（POST /api/messages/{message_id}/adopt）：
  · 只能采纳自己会话中的 assistant 回答
  · 拒答 / 空回答不可采纳
  · 幂等：同一条回答重复采纳返回已采纳标记，不重复入库
  · question 取该回答前最近一条 user 消息
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user, get_db
from app.models import Conversation, FinetuneSample, Message

router = APIRouter(tags=["messages"])


class AdoptResponse(BaseModel):
    adopted: bool
    already_adopted: bool
    sample_id: str


@router.post("/messages/{message_id}/adopt", response_model=AdoptResponse)
async def adopt_message(
    message_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> AdoptResponse:
    msg = await session.get(Message, message_id)
    if msg is None or msg.tenant_id != user.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="NOT_FOUND")
    if msg.role != "assistant" or not msg.content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="仅可采纳有效的回答")

    # 会话归属校验：只能采纳自己会话中的回答
    conv = await session.get(Conversation, msg.conversation_id)
    if conv is None or conv.user_id != user.user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="FORBIDDEN")

    # 幂等：已采纳过直接返回
    dup = (
        await session.execute(
            select(FinetuneSample).where(FinetuneSample.assistant_message_id == message_id)
        )
    ).scalar_one_or_none()
    if dup is not None:
        return AdoptResponse(adopted=True, already_adopted=True, sample_id=str(dup.id))

    # 取该回答前最近一条 user 消息作为 question
    conv_msgs = (
        await session.execute(
            select(Message)
            .where(Message.conversation_id == msg.conversation_id)
            .order_by(Message.created_at)
        )
    ).scalars().all()
    question: str | None = None
    user_message_id: uuid.UUID | None = None
    for m in conv_msgs:
        if m.id == msg.id:
            break
        if m.role == "user":
            question = m.content
            user_message_id = m.id
    if question is None or user_message_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="未找到对应的问题消息")

    sample = FinetuneSample(
        tenant_id=user.tenant_id,
        conversation_id=msg.conversation_id,
        user_message_id=user_message_id,
        assistant_message_id=message_id,
        question=question,
        answer=msg.content,
        citations=list(msg.citations or []),
        adopted_by=user.user_id,
    )
    session.add(sample)
    await session.commit()
    return AdoptResponse(adopted=True, already_adopted=False, sample_id=str(sample.id))
