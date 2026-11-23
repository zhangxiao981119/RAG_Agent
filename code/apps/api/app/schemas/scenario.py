"""场景写接口的请求/响应模型（pydantic v2，`extra="forbid"`）。

★ 两个「契约里故意没有的字段」是设计的一部分（不留入口 > 加校验）：
  · `binding_mode`：服务端恒按 `kb_ids` 非空推导 `'explicit'`。客户端若能设
    `unrestricted`，就要在写侧做校验；不给入口则连校验都不需要
    （`ck_..._binding_consistent` 恒成立）。发布期仍有一道复核（`step4_check`），
    拦的是「绕过 API 直写 SQL 之后再用 API 发布」。
  · `dna_declared`：DNA 是 L0 平台事实，由场景指定即等于让场景改平台口径；
    服务端用 `build_dna_declared()` 生成。

★ `policy` 刻意**不建嵌套模型**：键闭集与范围必须由 `policy_spec.validate_policy`
  判（唯一真源）。在这里再写一遍类型就是第二个真源，两者必然分叉。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ScenarioCreate(BaseModel):
    """W1 请求体。"""

    model_config = ConfigDict(extra="forbid")

    scenario_key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=500)


class VersionContent(BaseModel):
    """W2 / W3 共用的内容体（W3 是**整体替换**，不是键级合并）。

    ★ 为什么 `PUT` 整体替换而不是 `PATCH`：稀疏覆盖集里「删除一个键」= 回到跟随平台，
      而 JSONB 里 `null` 与「键不存在」在写入后无法区分（写 `{"recall_top_k": null}`
      会撞 `ck_..._policy_recall_top_k` 的 `::int` 转换）。键级合并表达不了删除，
      只剩「写 null 当哨兵」这条一定会出事的歪路。整体替换让「内容 = 请求体」成为
      单射，`checksum` 才有确定语义。
    """

    model_config = ConfigDict(extra="forbid")

    policy: dict[str, object] = Field(default_factory=dict)
    kb_ids: list[uuid.UUID] = Field(min_length=1)
    change_note: str = Field(min_length=1, max_length=500)


class PublishedBrief(BaseModel):
    version: int
    checksum: str
    binding_mode: Literal["explicit", "unrestricted"]
    kb_ids: list[uuid.UUID]


class ScenarioOut(BaseModel):
    id: uuid.UUID
    scenario_key: str
    name: str
    status: str
    is_builtin: bool
    db_binding: str | None = None
    description: str = ""
    published: PublishedBrief | None = None
    draft_version: int | None = None
    created_at: datetime
    updated_at: datetime


class VersionSummary(BaseModel):
    version: int
    state: Literal["draft", "validated", "published", "archived"]
    checksum: str
    change_note: str
    created_at: datetime
    validated_at: datetime | None = None
    published_at: datetime | None = None
    archived_at: datetime | None = None


class ScenarioDetail(ScenarioOut):
    versions: list[VersionSummary] = Field(default_factory=list)


class VersionOut(BaseModel):
    version: int
    state: Literal["draft", "validated", "published", "archived"]
    checksum: str
    policy: dict[str, object]
    kb_ids: list[uuid.UUID]
    binding_mode: Literal["explicit", "unrestricted"]
    dna_declared: dict[str, object]
    change_note: str
    created_at: datetime
    validated_at: datetime | None = None
    published_at: datetime | None = None
    archived_at: datetime | None = None


class VersionActionOut(BaseModel):
    """W4–W6 的统一响应。`changed=false` = 幂等重放。

    ★ `dna_snapshot`（Step 4B-1）**只有 W5 会填**，其余动作恒 `None`。它是**只读
      旁路**：不进 `checksum`、不写 `scenario_versions`、不触发缓存失效
      （I-4B-5，守：`T-4B-5`）。加默认值 = 对既有三个动作零影响。
    """

    version: int
    state: str
    checksum: str
    changed: bool
    archived_previous_version: int | None = None
    dna_snapshot: dict[str, object] | None = None


class ScenarioBindingUpdate(BaseModel):
    """W7 请求体。`null` = 解绑（回内置）。"""

    model_config = ConfigDict(extra="forbid")

    scenario_id: uuid.UUID | None = None
