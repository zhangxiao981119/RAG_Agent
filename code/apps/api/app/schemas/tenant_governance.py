"""租户治理面（S7-5）的请求 / 响应模型（pydantic v2，写侧 `extra="forbid"`）。

★ 为什么 `GovernanceUpdate` 的六个字段全部允许 `None`：`None` 在这里是**两种**
  不同语义，靠「键是否出现在请求体里」区分（路由层用 `model_fields_set` 过滤）：
    · 键不出现 → 这个字段不动（稀疏更新，与 `ScenarioPolicy` 同思想）；
    · 键出现且为 `null` → 清除该上限 / 解除钉定。
  若给字段加 `Field(default=...)` 之类的哨兵，两者就会混成同一个值，
  而「没传」与「传了 null」在治理面是**完全相反**的两件事。

★ 响应模型不省 `None` 键（尤其 `caps`）：管理端要区分「上限不存在」与
  「接口没实现这个字段」，省键会让两者在 JSON 里长得一样。
"""
from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict


class DbScopeIn(BaseModel):
    """PUT /admin/tenant/db-scope 请求体。

    `allowed_db_keys` 必填（可为 null）：这是**整体替换**语义，
    缺字段等于「没说要替换成什么」，与「替换成不限制（null）」必须可区分。
    """

    model_config = ConfigDict(extra="forbid")

    allowed_db_keys: list[str] | None


class DbScopeOut(BaseModel):
    allowed_db_keys: list[str] | None
    governance_enabled: bool


class CapsOut(BaseModel):
    """四个租户上限。全 `None` = 不限制。"""

    recall_top_k_max: int | None = None
    threshold_vector_min: float | None = None
    threshold_rerank_min: float | None = None
    rerank_allowed: bool | None = None


class ScenarioGovernanceOut(BaseModel):
    """场景治理列表项（不含版本与有效值）。"""

    id: uuid.UUID
    scenario_key: str
    name: str
    status: str
    db_binding: str | None
    tier: str
    pinned_version: int | None = None
    caps: CapsOut


class GovernanceUpdate(BaseModel):
    """PUT /admin/scenario-governance/{id} 请求体。

    `db_binding` 刻意**不在**这里：它是部署事实，改它必须走搬库流程。
    `tier` 显式传 `null` 由服务层判为非法（不是「不动」）—— 传 null 说明
    调用方以为这个字段可以清空，而分级没有「未分级」这个状态。
    """

    model_config = ConfigDict(extra="forbid")

    pinned_version: int | None = None
    recall_top_k_max: int | None = None
    threshold_vector_min: float | None = None
    threshold_rerank_min: float | None = None
    rerank_allowed: bool | None = None
    tier: Literal["cold", "warm", "hot"] | None = None


class EffectiveKeyOut(BaseModel):
    """单个策略键的「场景声明 / 租户上限 / 生效值 / 来源」。"""

    scenario: object | None = None
    cap: object | None = None
    effective: object | None = None
    source: str


class ScenarioGovernanceDetail(ScenarioGovernanceOut):
    """详情：在列表项之上加「开关状态 / 版本 / 有效值预览」。

    `effective is None` 只在 `version is None`（`version_source='unresolvable'`）时出现：
    解析不到版本时**不编造**有效值。
    """

    governance_enabled: bool
    version: int | None = None
    version_source: str
    effective: dict[str, EffectiveKeyOut] | None = None
