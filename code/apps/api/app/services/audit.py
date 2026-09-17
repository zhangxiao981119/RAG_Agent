"""审计服务 —— 手册 M5 任务 3：记录 chat.ask / doc.* / acl.* / auth.login.*。

写入原则：
  · 审计失败 MUST NOT 影响主业务流程（记 error 日志，不抛异常）
  · 独立会话写入（调用方可能处于回滚中的事务，复用会话会连带回滚丢失日志）
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionLocal
from app.models import AuditLog, User

logger = logging.getLogger(__name__)


async def record(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID | None,
    action: str,
    object_type: str | None = None,
    object_id: str | None = None,
    detail: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    """异步写一条审计日志。审计失败只记日志，不影响主流程。"""
    try:
        async with SessionLocal() as session:
            session.add(
                AuditLog(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    action=action,
                    object_type=object_type,
                    object_id=object_id,
                    detail=detail or {},
                    ip=ip,
                )
            )
            await session.commit()
    except Exception:
        logger.exception("审计日志写入失败 action=%s", action)


async def record_with_session(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID | None,
    action: str,
    object_type: str | None = None,
    object_id: str | None = None,
    detail: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    """复用调用方会话写审计（同一事务，主流程回滚则日志一并回滚）。

    用于事务性操作（如 acl.member.set 与成员变更同事务，保证日志与变更一致）。
    """
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            user_id=user_id,
            action=action,
            object_type=object_type,
            object_id=object_id,
            detail=detail or {},
            ip=ip,
        )
    )


async def list_logs(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    page: int,
    page_size: int,
    action: str | None = None,
    user_id: uuid.UUID | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """服务端分页查询审计日志，返回 (items, total)。item 附 user 展示名。"""
    conditions = [AuditLog.tenant_id == tenant_id]
    if action:
        # 前缀匹配：传 doc.* 可筛全部文档类动作
        conditions.append(AuditLog.action.like(f"{action}%"))
    if user_id:
        conditions.append(AuditLog.user_id == user_id)

    total = (
        await session.execute(select(func.count()).select_from(AuditLog).where(*conditions))
    ).scalar_one()

    rows = (
        (
            await session.execute(
                select(AuditLog, User.display_name, User.username)
                .outerjoin(User, User.id == AuditLog.user_id)
                .where(*conditions)
                .order_by(AuditLog.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .all()
    )
    items = [
        {
            "id": log.id,
            "user_id": str(log.user_id) if log.user_id else None,
            "user_label": display or username or ("已删除用户" if log.user_id else "匿名/系统"),
            "action": log.action,
            "object_type": log.object_type,
            "object_id": log.object_id,
            "detail": log.detail,
            "ip": log.ip,
            "created_at": log.created_at.isoformat(),
        }
        for log, display, username in rows
    ]
    return items, total
