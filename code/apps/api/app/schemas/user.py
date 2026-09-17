"""M3 任务 4 用户管理 API schema —— 列表 + CRUD + 重置密码。

口径（手册 §3.2.3 + §5.2）：
  · clearance（密级）：integer，高密级可看低密级文档，常用 10/20/30/40
  · status：active / disabled，禁用后 JWT 立即拒签（get_current_user 已校验）
  · dept_id：用户归属部门，改 dept_id 触发 acl_epoch+1（subjects 含 dept:<path> 变了）
  · role_names：role:<name> 主体列表

dept_id 区分"未传"和"传 null"：用 `model_dump(exclude_unset=True)` 取客户端实际传的字段，
未传则不改；传 null 则清空 dept_id（用户脱离部门）。
"""
from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class UserNode(BaseModel):
    """用户列表项（前端表格用）。"""

    id: uuid.UUID
    username: str
    display_name: str
    email: str | None
    dept_id: uuid.UUID | None
    dept_path: str | None  # join 出来便于展示
    clearance: int
    role_names: list[str]
    status: str


class UserPageResponse(BaseModel):
    """用户分页响应（AntD Table 服务端分页用）。"""

    items: list[UserNode]
    total: int
    page: int
    page_size: int


class UserCreateRequest(BaseModel):
    """POST /api/users 新建用户。

    password 为明文（HTTPS/TLS 已加密传输层），后端用 bcrypt 哈希后存库。
    """

    username: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    display_name: str = Field(min_length=1, max_length=64)
    email: str | None = Field(default=None, max_length=128)
    password: str = Field(min_length=8, max_length=128)
    dept_id: uuid.UUID | None = None
    clearance: int = Field(default=20, ge=0, le=100)
    role_names: list[str] = Field(default_factory=list)


class UserUpdateRequest(BaseModel):
    """PATCH /api/users/{id} 修改用户属性（不重置密码）。

    所有字段默认 None；API 层用 `model_dump(exclude_unset=True)` 取客户端实际传的字段。
    dept_id 传 null 表示清空（用户脱离部门）。
    """

    display_name: str | None = Field(default=None, min_length=1, max_length=64)
    email: str | None = Field(default=None, max_length=128)
    dept_id: uuid.UUID | None = None  # 客户端可传 uuid / null / 不传
    clearance: int | None = Field(default=None, ge=0, le=100)
    role_names: list[str] | None = None
    status: str | None = Field(default=None, pattern=r"^(active|disabled)$")


class UserResetPasswordRequest(BaseModel):
    """POST /api/users/{id}/reset-password 重置密码。"""

    new_password: str = Field(min_length=8, max_length=128)


class UserResponse(BaseModel):
    """单用户响应（新建/修改后返回）。"""

    id: uuid.UUID
    username: str
    display_name: str
    email: str | None
    dept_id: uuid.UUID | None
    dept_path: str | None
    clearance: int
    role_names: list[str]
    status: str


class UserDeleteResponse(BaseModel):
    """删除响应。"""

    deleted: bool = True
