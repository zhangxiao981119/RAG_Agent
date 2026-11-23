"""T4 的发布期第二道闸（`ensure_publishable`）。

保持 `self_check.py` **一行不改**（已验收文件）：本模块只做两件事的**组合**：
  1. 复核 `binding_mode`（`unrestricted` 且非内置 → 拒）—— 防「绕过 API 直写 SQL
     之后，再用 API 发布」这个组合路径；
  2. 调用 `check_policy_publishable()`（`self_check.py:634-660`，Step 3 已交付但
     **无调用方** —— 本模块就是它的第一个调用方）。

★ 两道闸性质不同（入口不存在 vs 状态复核），不是重复：
  写侧根本不接受 `binding_mode` 字段（`VersionContent` 无此字段 + `extra="forbid"`），
  所以正常路径永远到不了这里；这里拦的是绕过写接口的数据。
"""
from __future__ import annotations

from collections.abc import Mapping

from app.services.scenario.self_check import check_policy_publishable
from app.services.scenario.policy_spec import PolicyValidationError


def ensure_publishable(
    *,
    policy: Mapping[str, object],
    binding_mode: str,
    is_builtin: bool,
    platform_rerank_enabled: bool,
) -> dict[str, object]:
    """发布前判定。合规返回归一化 policy；不合规抛 `PolicyValidationError`。

    ★ 失败类型用 `PolicyValidationError`（带 `field`）而不是 `WriteError`：
      这样「哪些字段不合法」这件事仍由 `policy_spec` / `self_check` 的既有判据表达，
      本模块不复制任何判据；路由层再把 `field` 映射成 `SCENARIO_NOT_PUBLISHABLE`。
    """
    if binding_mode == "unrestricted" and not is_builtin:
        raise PolicyValidationError(
            "binding_mode=unrestricted 只允许内置场景（该模式下场景不限制知识集合）",
            field="binding_mode",
        )
    return check_policy_publishable(
        policy, platform_rerank_enabled=platform_rerank_enabled
    )
