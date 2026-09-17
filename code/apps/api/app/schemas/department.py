"""M3 任务 7+8 部门管理 API schema —— 树形结构 + CRUD/移动请求。

口径：
  · path 全程以 `/` 开头并以 `/` 结尾，例如 `/总部/技术中心/后端组/`
  · depth 从 0 开始（根节点），每深一层 +1
  · visible_to_parent=False 表示对祖先不可见（手册 §8 D-12）
  · 部门改名/移动时，子树所有 path 前缀替换 + depth 重算 + tenant_acl_epoch+1
"""
from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class DepartmentNode(BaseModel):
    """部门树节点（前端构建树形展示用）。

    `children` 为直接子节点；递归到叶子。
    `user_count` 为直接挂在本部门的用户数（不含子孙）。
    """

    id: uuid.UUID
    name: str
    path: str
    depth: int
    sort_order: int
    visible_to_parent: bool
    user_count: int
    children: list["DepartmentNode"] = []


class DepartmentCreateRequest(BaseModel):
    """POST /api/departments 新建部门。

    parent_id=None 表示根节点；name 不能为空且不能含 `/`。
    visible_to_parent=False 表示对祖先不可见（如薪酬组，手册 §8 D-12）。
    """

    name: str = Field(min_length=1, max_length=64, pattern=r"^[^/]+$")
    parent_id: uuid.UUID | None = None
    sort_order: int = 0
    visible_to_parent: bool = True


class DepartmentUpdateRequest(BaseModel):
    """PATCH /api/departments/{id} 修改部门属性。

    所有字段可选；name 修改会触发子树 path 级联重写。
    """

    name: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[^/]+$")
    sort_order: int | None = None
    visible_to_parent: bool | None = None


class DepartmentMoveRequest(BaseModel):
    """POST /api/departments/{id}/move 移动部门到新父下。

    new_parent_id=None 表示移到根；移动后子树 path + depth 级联重写。
    """

    new_parent_id: uuid.UUID | None = None


class DepartmentResponse(BaseModel):
    """单部门响应（新建/修改/移动后返回）。"""

    id: uuid.UUID
    name: str
    path: str
    depth: int
    sort_order: int
    visible_to_parent: bool


class DepartmentDeleteResponse(BaseModel):
    """删除响应。"""

    deleted: bool = True
