from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class KnowledgeBaseOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None = None
    is_public: bool
    doc_count: int = 0


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = None
    is_public: bool = False


class KnowledgeBaseDetail(KnowledgeBaseOut):
    created_at: datetime


# ── M4 任务 1：知识库成员管理（四种主体）─────────────────────


class KbMemberItem(BaseModel):
    """成员项（含展示用 label）。

    subject_id 存储口径（§4.2.6）：user/group → UUID 字符串；
    dept → 规整后的部门路径（如 /总部/技术中心/）；role → 角色名。
    """

    subject_type: str  # user / group / dept / role
    subject_id: str
    label: str  # 用户显示名 / 组名 / 部门路径 / 角色名


class KbMemberListResponse(BaseModel):
    """GET /api/kbs/{kb_id}/members 响应。"""

    kb_id: uuid.UUID
    members: list[KbMemberItem]


class KbMemberSetItem(BaseModel):
    """PUT 请求里的成员项。"""

    subject_type: str = Field(pattern=r"^(user|group|dept|role)$")
    subject_id: str = Field(min_length=1, max_length=256)


class KbMemberSetRequest(BaseModel):
    """PUT /api/kbs/{kb_id}/members 全量设置成员（手册 §5.1：设置成员）。

    members 允许为空数组 = 清空该库所有成员（restricted 库将无人可访问）。
    """

    members: list[KbMemberSetItem]


class KbMemberSetResponse(BaseModel):
    """PUT 响应：设置后的成员列表 + 是否发生实际变更。"""

    kb_id: uuid.UUID
    changed: bool
    members: list[KbMemberItem]
