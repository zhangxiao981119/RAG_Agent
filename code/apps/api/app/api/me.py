"""用户自身相关端点。

- `GET /api/me`            主体解析结果（手册 §5.1 M3 任务 2）
- `GET /api/me/memory`     查看「我被记住了什么」（§4.20.5）
- `DELETE /api/me/memory/items/{memory_id}`  删除单条记忆（§4.20.5）
- `DELETE /api/me/memory`  清空全部长期记忆（§4.20.5）

`/me` 返回当前登录用户的主体解析结果：
  · subjects（含 §3.2.3 双向展开：祖先 ∪ 自己 ∪ 子孙，受 visible_to_parent 约束）
  · clearance（密级数值）
  · dept_path（用户主部门路径）
  · authorized_kb_ids（G2 用：用户可访问的知识库，含公开库自动成员 §3.2.7）
  · acl_epoch（当前权限缓存版本号，调试用）

主体解析在 get_current_user 内调 load_principal_from_db 完成（带 Redis 缓存，
§3.2.6），本端点只是把 CurrentUser 格式化为响应体。

★ 记忆管理端点**不接受 user_id 参数** —— 一律作用于当前登录用户。
  从接口形态上排除「查看/删除他人画像」的可能（D-21 权限副本防护的一部分）。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user, get_db
from app.config import decisions
from app.models import User
from app.schemas.auth import MeResponse
from app.services import audit
from app.services import memory as memory_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["me"])


@router.get("/me", response_model=MeResponse)
async def get_me(user: CurrentUser = Depends(get_current_user)) -> MeResponse:
    """返回当前登录用户的主体解析结果。"""
    return MeResponse(
        user_id=user.user_id,
        username=user.username,
        display_name=user.display_name,
        dept_path=user.dept_path,
        clearance=user.clearance,
        subjects=user.subjects,
        authorized_kb_ids=user.authorized_kb_ids,
        acl_epoch=user.acl_epoch,
    )


@router.get("/me/memory")
async def get_my_memory(
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """查看「我被记住了什么」（§4.20.5 合规要求）。

    用户有权知悉自己被记录了哪些偏好；这也是排查「错误记忆」的唯一入口。
    """
    row = await session.get(User, user.user_id)
    memory = row.memory if row is not None else None
    return {
        "items": memory_service.list_memory_items(memory),
        "recallable_kinds": sorted(decisions.MEMORY_LONG_TERM_KINDS),
    }


@router.delete("/me/memory/items/{memory_id}")
async def delete_my_memory_item(
    memory_id: str,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """按 memory_id 删除单条记忆（**物理删除**）。

    用途：撤销一次错误的提炼，防止错误记忆在后续对话里自我强化。
    """
    row = await session.get(User, user.user_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")

    new_memory, deleted = memory_service.delete_memory_item(row.memory, memory_id)
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "记忆项不存在")

    row.memory = new_memory
    await session.commit()
    await audit.record(
        user.tenant_id, user.user_id, "memory.delete",
        object_type="memory_item", object_id=memory_id,
    )
    logger.info("memory.item.deleted user_id=%s memory_id=%s", user.user_id, memory_id)
    return {"deleted": True, "memory_id": memory_id}


@router.delete("/me/memory")
async def clear_my_memory(
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """清空本人全部长期记忆（合规兜底：账号注销 / 用户申诉）。"""
    row = await session.get(User, user.user_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")

    removed = len(memory_service.list_memory_items(row.memory))
    row.memory = memory_service.clear_memory()
    await session.commit()
    await audit.record(
        user.tenant_id, user.user_id, "memory.clear",
        object_type="user_memory", object_id=str(user.user_id),
        detail={"removed": removed},
    )
    logger.info("memory.cleared user_id=%s removed=%d", user.user_id, removed)
    return {"cleared": True, "removed": removed}
