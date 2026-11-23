"""chunk 口径分布的两个**响应**模型（Step 4B-1）。

★ 为什么响应模型不加 `extra="forbid"`：`extra` 只对**请求**体有意义
  （"客户端多传字段"要报错）。响应体由服务端构造，加 `forbid` 只会让
  "在返回值里多塞一个调试字段"从无害变成 500。

★ 为什么 `newest_*_at` 是 `datetime | None` 而不是"部署级用另一个模型"：
  部署级视角**不允许**返回任何时间戳与明细（跨租户越权）。用一个模型 +
  路由层的 `response_model_exclude_none=True` 表达"这一档没有这些键"，
  比维护两个几乎相同的模型更不容易漂 —— 而"有没有泄露"这件事由
  `T-4B-7` 用正则扫响应体原文来守（不靠模型定义）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class ProfileOut(BaseModel):
    """当前部署的口径三元组回显。

    ★ 没有它，数字无法被解释（"108 条过期"相对哪个口径？）。
      回显的是**当前部署**的值，不是用户的输入 —— 端点**不接受**口径参数
      （允许传口径等于允许"用别的口径问这个部署"）。
    """

    embedding_model: str
    embedding_text_version: str
    chunker_version: str


class VerdictOut(BaseModel):
    """告警式结果。`level` **没有** `error`：`hard_stale > 0` 在换模型窗口内合法。"""

    level: Literal["ok", "info", "warn"]
    reason: str
    detail: str = ""


class NeedsReindexOut(BaseModel):
    """`GET /api/admin/needs-reindex` 的响应。

    ★ `reconciles` **必须**是响应字段，不能只在日志里：否则"对账失败"这件事
      只有翻日志的人知道。
    ★ 四桶互斥 + `universe` 同时给：这是"能自己验算"的最小信息集。
    """

    profile: ProfileOut
    scope: Literal["tenant", "deployment"]
    universe: int
    ok: int
    hard_stale: int
    chunker_only: int
    embedding_missing: int
    in_dead_documents: int
    generation_conflicts: int
    newest_ok_at: datetime | None = None
    newest_stale_at: datetime | None = None
    reconciles: bool
    verdict: VerdictOut
    note: str


class DeclarationOut(BaseModel):
    """版本声明与平台当前 DNA 的差异（只给键名 + 两项可比的值）。"""

    stale_keys: list[str]
    declared: dict[str, object | None]
    current: dict[str, object]


class KbReadabilityOut(BaseModel):
    """该场景绑定 kb 集合的口径可检程度（**不含授权维度**）。"""

    kb_count: int
    chunks_total: int
    chunks_vector_readable: int
    chunks_hard_stale: int
    documents_indexed: int


class PreflightOut(BaseModel):
    """`GET /api/scenarios/{id}/versions/{version}/preflight` 的响应。

    ★ 这个端点**不冻结**任何结论：它回答"如果**现在**点发布会发生什么"，
      结论随时可变（W5 会重算一遍）。它不是 W4 的一部分，也不改 `validated` 语义。
    """

    version: int
    state: str
    checksum: str
    policy_publishable: bool
    declaration: DeclarationOut
    kb_readability: KbReadabilityOut
    publish_blockers: list[str]
    publish_warnings: list[str]
