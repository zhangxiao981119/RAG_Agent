"""场景解析的错误码与异常 —— 日志 / metrics 与响应体严格分离。

**为什么要分离**：集成侧是**外部系统**。`X-Application-Id` 已经足够让运维定位调用方，
它不需要也不应该知道租户内部的 `scenario_id`、真实的 `kb_ids` 边界或「场景是停用还是
草稿」。因此：

  · 响应体只有本模块 `_MESSAGES` 里的**固定文案**；
  · `diagnostic`（含 scenario_id / tenant_id / reason）只进结构化日志与 metrics。

★ 三类失败**不细分**的理由逐条可反驳：
  · `SCENARIO_NOT_ALLOWED` 不区分「停用」与「草稿」—— 细分即信息泄露；
  · `SCENARIO_MISMATCH` 不回显场景真实 kb_ids —— 否则可被逐位枚举出场景边界；
  · `SCENARIO_UNRESOLVABLE` 不带 SQL / 表名 / scenario_id —— 那是内部实现细节。
"""
from __future__ import annotations

from enum import Enum
from typing import Final

COMPONENT: Final[str] = "scenario"
"""日志与 metrics 的组件名（`resolve_scenario` 侧统一挂在它下面，便于按组件过滤）。"""


class ScenarioCode(str, Enum):
    """场景解析的失败码。值即响应体里出现的机器可读标识。"""

    NOT_ALLOWED = "SCENARIO_NOT_ALLOWED"
    MISMATCH = "SCENARIO_MISMATCH"
    UNRESOLVABLE = "SCENARIO_UNRESOLVABLE"
    NOT_FOUND = "SCENARIO_NOT_FOUND"


# 码 → (HTTP 状态, 固定文案)。文案 MUST NOT 含场景 key / status / 真实 kb_ids。
_MESSAGES: Final[dict[ScenarioCode, tuple[int, str]]] = {
    # R3：场景 status != active（停用或草稿，故意不区分）
    ScenarioCode.NOT_ALLOWED: (400, "场景不可用"),
    # R4：请求的知识库不在场景声明范围内（不回显场景集合）
    ScenarioCode.MISMATCH: (400, "请求的知识库不在场景范围内"),
    # R1：引用的场景解析不出已发布版本（数据一致性破坏，不静默退回默认）
    ScenarioCode.UNRESOLVABLE: (500, "场景配置不可解析"),
    # Web 入口：请求体里的 scenario_id 查不到（客户端给了不存在的场景）。
    # ★ 与 UNRESOLVABLE 不可合并：Web 入口的场景 ID 来自请求体，查不到是**客户端错误**
    #   （404）；集成入口的「应用绑定场景解析不出」是**数据损坏**（500）。
    #   跨租户与不存在共用同一个 404，避免状态码差异变成存在性探针。
    ScenarioCode.NOT_FOUND: (404, "场景不存在"),
}


class ScenarioError(Exception):
    """场景解析失败。**fail-closed** 语义的唯一载体 —— 绝不作为「返回默认快照」的信号。

    ★ 调用方 MUST 把它转成 HTTP 错误（`http_status`），MUST NOT 捕获后继续执行：
      「退回默认场景」正是框架稿 §12 点名的失败形态（日志无红、指标无异常、
      只有答准率缓慢下滑）。
    """

    code: ScenarioCode
    http_status: int
    message: str
    # 只用于日志与 metrics，**不进响应体**
    diagnostic: dict[str, str]

    def __init__(
        self, code: ScenarioCode, *, diagnostic: dict[str, str] | None = None
    ) -> None:
        self.code = code
        self.http_status, self.message = _MESSAGES[code]
        self.diagnostic = dict(diagnostic or {})
        # str(exc) 里只带码与固定文案：异常被 repr 进日志时不会泄露 internal id
        super().__init__(f"{COMPONENT}: {code.value}: {self.message}")

    def __repr__(self) -> str:  # pragma: no cover - 便于排障时直接打印
        return f"ScenarioError({self.code.value}, diagnostic={self.diagnostic!r})"
