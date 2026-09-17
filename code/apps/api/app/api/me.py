"""GET /api/me —— 手册 §5.1 M3 任务 2。

返回当前登录用户的主体解析结果：
  · subjects（含 §3.2.3 双向展开：祖先 ∪ 自己 ∪ 子孙，受 visible_to_parent 约束）
  · clearance（密级数值）
  · dept_path（用户主部门路径）
  · authorized_kb_ids（G2 用：用户可访问的知识库，含公开库自动成员 §3.2.7）
  · acl_epoch（当前权限缓存版本号，调试用）

主体解析在 get_current_user 内调 load_principal_from_db 完成（带 Redis 缓存，
§3.2.6），本端点只是把 CurrentUser 格式化为响应体。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import CurrentUser, get_current_user
from app.schemas.auth import MeResponse

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
