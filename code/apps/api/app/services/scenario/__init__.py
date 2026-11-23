"""场景化配置的解析层（Step 2）。

对外只暴露四样东西，其余都是实现细节：

    resolve_scenario   唯一 impure 入口（一次 DB 读），失败 raise ScenarioError
    ScenarioSnapshot   一次请求解析出的不可变口径快照
    ScenarioError      场景解析失败（fail-closed 的唯一载体）
    COMPONENT          日志 / metrics 的组件名

★ Step 2 的边界：本包**不接线 policy**、不调用检索、不改任何检索行为。
  `policy_spec` 这一步只做「规格表 + 校验 + 落库」，读它属 Step 3。
"""
from __future__ import annotations

from app.services.scenario.errors import COMPONENT, ScenarioCode, ScenarioError
from app.services.scenario.resolver import resolve_scenario
from app.services.scenario.snapshot import (
    ScenarioPolicy,
    ScenarioRow,
    ScenarioSnapshot,
    build_snapshot,
    compute_checksum,
    current_dna,
)

__all__ = [
    "COMPONENT",
    "ScenarioCode",
    "ScenarioError",
    "ScenarioPolicy",
    "ScenarioRow",
    "ScenarioSnapshot",
    "build_snapshot",
    "compute_checksum",
    "current_dna",
    "resolve_scenario",
]
