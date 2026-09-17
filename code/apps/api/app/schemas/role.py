"""M3 任务 6 角色管理 API schema —— list + CRUD。

口径（手册 §3.2.4）：
  · Role 是 ACL 主体之一：role:<name>
  · subjects.py 从 user.role_names（PostgreSQL array）直接读，不通过 UserRole 表
  · 改角色定义 / 删除角色（清理 user.role_names 同名项）触发 acl_epoch+1
  · 用户分配角色走 PATCH /api/users/{id}（任务 4 已实现）
"""
from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class RoleNode(BaseModel):
    """角色列表项（含已分配用户数）。"""

    id: uuid.UUID
    name: str
    user_count: int


class RoleCreateRequest(BaseModel):
    """POST /api/roles 新建角色。"""

    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")


class RoleUpdateRequest(BaseModel):
    """PATCH /api/roles/{id} 修改角色名。

    改名后会同步更新所有 user.role_names 中的旧名为新名。
    """

    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")


class RoleResponse(BaseModel):
    """单角色响应。"""

    id: uuid.UUID
    name: str


class RoleDeleteResponse(BaseModel):
    """删除响应。"""

    deleted: bool = True
    affected_users: int = 0  # 清理了多少用户的 role_names
