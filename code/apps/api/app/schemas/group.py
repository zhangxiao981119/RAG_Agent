"""M3 任务 5 用户组管理 API schema —— 列表 + CRUD + 成员管理。

口径（手册 §3.2.4 + §8）：
  · kind=normal：内部用户组（默认）
  · kind=external：外部协作组（如外包、合作伙伴，权限更受限）
  · 组是 ACL 主体之一：group:<group_id>
  · 改组属性 / 成员增删都触发 acl_epoch+1（subjects 含 group:<id>）
"""
from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class GroupNode(BaseModel):
    """用户组列表项（含成员数）。"""

    id: uuid.UUID
    name: str
    kind: str  # normal / external
    member_count: int


class GroupCreateRequest(BaseModel):
    """POST /api/groups 新建组。"""

    name: str = Field(min_length=1, max_length=64)
    kind: str = Field(default="normal", pattern=r"^(normal|external)$")


class GroupUpdateRequest(BaseModel):
    """PATCH /api/groups/{id} 修改组属性。

    用 model_dump(exclude_unset=True) 取客户端实际传的字段。
    """

    name: str | None = Field(default=None, min_length=1, max_length=64)
    kind: str | None = Field(default=None, pattern=r"^(normal|external)$")


class GroupResponse(BaseModel):
    """单组响应。"""

    id: uuid.UUID
    name: str
    kind: str


class GroupDeleteResponse(BaseModel):
    """删除响应。"""

    deleted: bool = True


class GroupMemberList(BaseModel):
    """组成员列表（含用户基础信息）。"""

    group_id: uuid.UUID
    members: list["GroupMemberItem"]


class GroupMemberItem(BaseModel):
    """组成员项。"""

    user_id: uuid.UUID
    username: str
    display_name: str


class GroupMemberAddRequest(BaseModel):
    """POST /api/groups/{id}/members 批量加成员。"""

    user_ids: list[uuid.UUID] = Field(min_length=1)


class GroupMemberOpResponse(BaseModel):
    """成员操作响应。"""

    added: int = 0
    removed: int = 0
