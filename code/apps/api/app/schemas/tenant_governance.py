"""租户治理面（S7-5）的请求 / 响应模型（pydantic v2，写侧 `extra="forbid"`）。

★ 为什么 `GovernanceUpdate` 的策略字段全部允许 `None`：`None` 在这里是**两种**
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
    分级同样不在此接口：改用 `PUT /admin/scenario-governance/{id}/tier`（搬库登记），
    因为 tier 与 db_binding 受 `ck_scenarios_tier_binding` 约束必须同句写入。
    """

    model_config = ConfigDict(extra="forbid")

    pinned_version: int | None = None
    recall_top_k_max: int | None = None
    threshold_vector_min: float | None = None
    threshold_rerank_min: float | None = None
    rerank_allowed: bool | None = None


class ScenarioTierIn(BaseModel):
    """PUT /admin/scenario-governance/{id}/tier 请求体（搬库完成登记）。

    `tier` 与 `db_binding` 都必须出现（显式 null 才表示清空）：
      · cold 必须 null、warm 必须非 null（`ck_scenarios_tier_binding` 同口径）；
      · hot 由服务层拒绝并保留稳定错误码 `TIER_HOT_UNSUPPORTED`。
    此接口只登记「数据已搬完」的事实，**不搬数据**；搬库仍走 Step 6 SOP。
    """

    model_config = ConfigDict(extra="forbid")

    tier: Literal["cold", "warm", "hot"]
    db_binding: str | None


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
