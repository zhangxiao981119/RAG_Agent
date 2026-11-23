"""Web 入口（`/chat/ask` 的 scenario_id）场景解析的 NO-DB 用例。

与 `test_resolver_truth_table.py` 共用假 Session 与行工厂，只验证新增入口
`resolve_scenario_by_id` 的语义差异：
  · 查不到行 / 跨租户 → 404 SCENARIO_NOT_FOUND（客户端错误，不是数据损坏 500）；
  · 其余（active、explicit 范围、caps、版本来源）与集成入口逐项同源；
  · 缓存只缓存命中行，命中后不再发 SQL。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.scenario import ScenarioCode, ScenarioError, resolve_scenario_by_id
from app.services.scenario import resolver

from tests.scenario.conftest import (
    KB_A,
    KB_B,
    OTHER_TENANT_ID,
    SCENARIO_ID,
    TENANT_ID,
    FakeSession,
    build_row,
)

pytestmark = pytest.mark.asyncio


async def test_missing_row_returns_404() -> None:
    """查不到行 → SCENARIO_NOT_FOUND（404），不是 UNRESOLVABLE（500）。"""
    with pytest.raises(ScenarioError) as exc_info:
        await resolve_scenario_by_id(
            FakeSession([]),
            tenant_id=TENANT_ID,
            scenario_id=SCENARIO_ID,
            requested_kb_ids=[KB_A],
        )
    error = exc_info.value
    assert error.code is ScenarioCode.NOT_FOUND
    assert error.http_status == 404
    assert error.message == "场景不存在"
    assert error.diagnostic["reason"] == "scenario_not_found"
    assert error.diagnostic["scenario_id"] == str(SCENARIO_ID)


async def test_other_tenant_row_returns_same_404() -> None:
    """行属于其他租户 → 与不存在同一个 404，避免状态码差异成为存在性探针。"""
    foreign = build_row(tenant_id=OTHER_TENANT_ID, scenario_id=SCENARIO_ID)
    with pytest.raises(ScenarioError) as exc_info:
        await resolve_scenario_by_id(
            FakeSession([foreign]),
            tenant_id=TENANT_ID,
            scenario_id=SCENARIO_ID,
            requested_kb_ids=[KB_A],
        )
    assert exc_info.value.code is ScenarioCode.NOT_FOUND
    assert exc_info.value.http_status == 404


async def test_active_explicit_scenario_resolves_policy() -> None:
    """正常 active 的 explicit 场景 → 返回快照，policy 与行内声明一致。"""
    row = build_row(
        tenant_id=TENANT_ID,
        scenario_id=SCENARIO_ID,
        is_builtin=False,
        binding_mode="explicit",
        kb_ids=(str(KB_A),),
        policy={"recall_top_k": 8},
    )
    snapshot = await resolve_scenario_by_id(
        FakeSession([row]),
        tenant_id=TENANT_ID,
        scenario_id=SCENARIO_ID,
        requested_kb_ids=[KB_A],
    )
    assert snapshot.policy.recall_top_k == 8
    assert snapshot.kb_ids == (KB_A,)
    assert snapshot.source == "application"


async def test_governance_enabled_caps_applied(monkeypatch) -> None:
    """治理开启：四个收紧上限从行读出，版本来源为 published。"""
    monkeypatch.setattr(
        resolver, "get_settings",
        lambda: SimpleNamespace(tenant_governance_enabled=True),
    )
    row = build_row(
        tenant_id=TENANT_ID,
        scenario_id=SCENARIO_ID,
        is_builtin=False,
        binding_mode="explicit",
        kb_ids=(str(KB_A),),
        policy={"recall_top_k": 8},
    )
    row.update(
        {
            "recall_top_k_max": 5,
            "threshold_vector_min": 0.7,
            "threshold_rerank_min": 0.5,
            "rerank_allowed": False,
            "pinned_version": None,
            "version_state": "published",
        }
    )
    snapshot = await resolve_scenario_by_id(
        FakeSession([row]),
        tenant_id=TENANT_ID,
        scenario_id=SCENARIO_ID,
        requested_kb_ids=[KB_A],
    )
    assert snapshot.caps.recall_top_k_max == 5
    assert snapshot.caps.threshold_vector_min == 0.7
    assert snapshot.caps.threshold_rerank_min == 0.5
    assert snapshot.caps.rerank_allowed is False
    assert snapshot.version_source == "published"


async def test_governance_disabled_caps_empty(monkeypatch) -> None:
    """治理关闭：即便行里带了上限列也必须忽略，caps 为空（与关闭态等价）。"""
    monkeypatch.setattr(
        resolver, "get_settings",
        lambda: SimpleNamespace(tenant_governance_enabled=False),
    )
    row = build_row(
        tenant_id=TENANT_ID,
        scenario_id=SCENARIO_ID,
        is_builtin=False,
        binding_mode="explicit",
        kb_ids=(str(KB_A),),
        policy={"recall_top_k": 8},
    )
    row.update(
        {
            "recall_top_k_max": 5,
            "threshold_vector_min": 0.7,
            "threshold_rerank_min": 0.5,
            "rerank_allowed": False,
            "pinned_version": 3,
            "version_state": "archived",
        }
    )
    snapshot = await resolve_scenario_by_id(
        FakeSession([row]),
        tenant_id=TENANT_ID,
        scenario_id=SCENARIO_ID,
        requested_kb_ids=[KB_A],
    )
    assert snapshot.caps.is_empty() is True
    assert snapshot.version_source == "published"


async def test_governance_pinned_version_reports_pinned(monkeypatch) -> None:
    """治理开启且钉定版本：version_source 为 pinned（与集成入口同源）。"""
    monkeypatch.setattr(
        resolver, "get_settings",
        lambda: SimpleNamespace(tenant_governance_enabled=True),
    )
    row = build_row(
        tenant_id=TENANT_ID,
        scenario_id=SCENARIO_ID,
        is_builtin=False,
        binding_mode="explicit",
        kb_ids=(str(KB_A),),
    )
    row.update({"pinned_version": 3, "version_state": "archived", "version": 3})
    snapshot = await resolve_scenario_by_id(
        FakeSession([row]),
        tenant_id=TENANT_ID,
        scenario_id=SCENARIO_ID,
        requested_kb_ids=[KB_A],
    )
    assert snapshot.version_source == "pinned"
    assert snapshot.version == 3


async def test_disabled_scenario_returns_not_allowed() -> None:
    """场景停用 → NOT_ALLOWED（400），与集成入口同一判决。"""
    row = build_row(
        tenant_id=TENANT_ID,
        scenario_id=SCENARIO_ID,
        status="disabled",
        is_builtin=False,
        binding_mode="explicit",
        kb_ids=(str(KB_A),),
    )
    with pytest.raises(ScenarioError) as exc_info:
        await resolve_scenario_by_id(
            FakeSession([row]),
            tenant_id=TENANT_ID,
            scenario_id=SCENARIO_ID,
            requested_kb_ids=[KB_A],
        )
    assert exc_info.value.code is ScenarioCode.NOT_ALLOWED
    assert exc_info.value.http_status == 400


async def test_requested_kb_outside_explicit_scope_returns_mismatch() -> None:
    """请求的 kb 不在场景声明集合 → MISMATCH（400）。"""
    row = build_row(
        tenant_id=TENANT_ID,
        scenario_id=SCENARIO_ID,
        is_builtin=False,
        binding_mode="explicit",
        kb_ids=(str(KB_A),),
    )
    with pytest.raises(ScenarioError) as exc_info:
        await resolve_scenario_by_id(
            FakeSession([row]),
            tenant_id=TENANT_ID,
            scenario_id=SCENARIO_ID,
            requested_kb_ids=[KB_B],
        )
    assert exc_info.value.code is ScenarioCode.MISMATCH
    assert exc_info.value.http_status == 400


async def test_second_call_hits_row_cache() -> None:
    """连续两次调用只执行一次 SQL：命中后不再查库（同一键空间）。"""
    row = build_row(
        tenant_id=TENANT_ID,
        scenario_id=SCENARIO_ID,
        is_builtin=False,
        binding_mode="explicit",
        kb_ids=(str(KB_A),),
    )
    session = FakeSession([row])
    for _ in range(2):
        await resolve_scenario_by_id(
            session,
            tenant_id=TENANT_ID,
            scenario_id=SCENARIO_ID,
            requested_kb_ids=[KB_A],
        )
    assert len(session.statements) == 1


async def test_missing_row_is_not_negatively_cached() -> None:
    """查不到行不做负缓存：每次调用都重新查库，补种后立刻可见。"""
    session = FakeSession([])
    for _ in range(2):
        with pytest.raises(ScenarioError):
            await resolve_scenario_by_id(
                session,
                tenant_id=TENANT_ID,
                scenario_id=SCENARIO_ID,
                requested_kb_ids=[KB_A],
            )
    assert len(session.statements) == 2


async def test_cache_key_shared_with_resolve_scenario() -> None:
    """与 resolve_scenario 共用 (tenant_id, scenario_id) 键：先集成解析可命中本入口缓存。"""
    from app.services.scenario import resolve_scenario
    from tests.scenario.conftest import make_application

    row = build_row(
        tenant_id=TENANT_ID,
        scenario_id=SCENARIO_ID,
        is_builtin=False,
        binding_mode="explicit",
        kb_ids=(str(KB_A),),
    )
    session = FakeSession([row])
    application = make_application(scenario_id=SCENARIO_ID)

    first = await resolve_scenario(
        session,
        tenant_id=TENANT_ID,
        application=application,
        requested_kb_ids=[KB_A],
    )
    second = await resolve_scenario_by_id(
        session,
        tenant_id=TENANT_ID,
        scenario_id=SCENARIO_ID,
        requested_kb_ids=[KB_A],
    )
    assert len(session.statements) == 1
    assert first.fingerprint == second.fingerprint


async def test_scenario_declaration_does_not_widen_requested_kb_ids() -> None:
    """场景声明集合大于请求集合时，快照 kb_ids 仍是请求集合（场景不扩大检索范围）。"""
    row = build_row(
        tenant_id=TENANT_ID,
        scenario_id=SCENARIO_ID,
        is_builtin=False,
        binding_mode="explicit",
        kb_ids=(str(KB_A), str(KB_B)),
    )
    snapshot = await resolve_scenario_by_id(
        FakeSession([row]),
        tenant_id=TENANT_ID,
        scenario_id=SCENARIO_ID,
        requested_kb_ids=[KB_A],
    )
    assert snapshot.kb_ids == (KB_A,)
    assert snapshot.binding_kb_ids == (KB_A, KB_B)


async def test_cross_tenant_error_is_indistinguishable_from_missing() -> None:
    """不存在与跨租户的异常载荷逐字段相同（状态码/文案/诊断都不可区分）。"""

    # 不存在
    with pytest.raises(ScenarioError) as missing_info:
        await resolve_scenario_by_id(
            FakeSession([]),
            tenant_id=TENANT_ID,
            scenario_id=SCENARIO_ID,
            requested_kb_ids=[KB_A],
        )
    # 跨租户
    foreign = build_row(tenant_id=OTHER_TENANT_ID, scenario_id=SCENARIO_ID)
    with pytest.raises(ScenarioError) as foreign_info:
        await resolve_scenario_by_id(
            FakeSession([foreign]),
            tenant_id=TENANT_ID,
            scenario_id=SCENARIO_ID,
            requested_kb_ids=[KB_A],
        )

    missing, other = missing_info.value, foreign_info.value
    assert missing.code is other.code
    assert missing.http_status == other.http_status
    assert missing.message == other.message
    assert missing.diagnostic == other.diagnostic
