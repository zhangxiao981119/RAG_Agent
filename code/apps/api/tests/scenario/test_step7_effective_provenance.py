"""Step 7 S7-4：L0 平台 → L2 场景 → L1 租户 三段折叠 + provenance。

不变量：

  · 空 caps ⇒ 结果（含 `sources`）与 Step 6 逐字段相等；
  · `sources` 只记录**最终取值来源**，且只有租户 cap 真的改变值时才记 `tenant`；
  · `clamped` 仍只表示「平台收窄」（否则告警文案会说谎）；
  · `policy_caps` MUST 透传到 chain 的**每一轮**与**单趟**出口。
"""
from __future__ import annotations

import uuid

import pytest

import app.services.retrieve as retrieve_mod
from app.config import decisions
from app.services.retrieve import RetrievalService
from app.services.retrieve.base import RetrievalResult
from app.services.scenario import policy_spec
from app.services.scenario.effective import effective_retrieval
from app.services.scenario.snapshot import ScenarioPolicy
from app.services.tenant.governance import EMPTY_CAPS, TenantPolicyCaps

PROVENANCE_KEYS = (
    "recall_top_k",
    "rerank_enabled",
    "threshold_vector",
    "threshold_rerank",
)

pytestmark = pytest.mark.asyncio


def _sources(eff) -> dict[str, str]:  # noqa: ANN001
    return dict(eff.sources)


# ══════════════════════════════════════════════════════════════════════
# 一、空 caps = Step 6
# ══════════════════════════════════════════════════════════════════════


async def test_empty_caps_is_identical_to_step6():
    policy = ScenarioPolicy(recall_top_k=12, rerank_enabled=False)
    base = effective_retrieval(policy, platform_rerank_enabled=True)
    with_empty = effective_retrieval(
        policy, platform_rerank_enabled=True, caps=EMPTY_CAPS
    )

    assert with_empty == base, "空 caps 必须逐字段（含 sources）等于 Step 6"
    assert base.clamped == ()
    assert [key for key, _ in base.sources] == list(PROVENANCE_KEYS)
    assert _sources(base) == {
        "recall_top_k": "scenario",
        "rerank_enabled": "scenario",
        "threshold_vector": "platform",
        "threshold_rerank": "platform",
    }


async def test_all_follow_platform_marks_platform():
    eff = effective_retrieval(None, platform_rerank_enabled=True, caps=None)

    assert _sources(eff) == {key: "platform" for key in PROVENANCE_KEYS}
    assert eff.clamped == ()


# ══════════════════════════════════════════════════════════════════════
# 二、recall_top_k：平台收窄 → 租户收紧
# ══════════════════════════════════════════════════════════════════════


async def test_platform_clamp_wins_over_looser_tenant_cap():
    """场景 300 / 平台上限 200 / cap 250 ⇒ 200，来源 platform（cap 未改变值）。"""
    platform_max = policy_spec.RECALL_TOP_K_MAX
    eff = effective_retrieval(
        ScenarioPolicy(recall_top_k=platform_max + 100),
        platform_rerank_enabled=True,
        caps=TenantPolicyCaps(recall_top_k_max=platform_max + 50),
    )

    assert eff.recall_top_k == platform_max
    assert eff.clamped == ("recall_top_k",)
    assert _sources(eff)["recall_top_k"] == "platform"


async def test_tenant_cap_tightens_after_platform():
    platform_max = policy_spec.RECALL_TOP_K_MAX
    eff = effective_retrieval(
        ScenarioPolicy(recall_top_k=platform_max + 100),
        platform_rerank_enabled=True,
        caps=TenantPolicyCaps(recall_top_k_max=platform_max - 1),
    )

    assert eff.recall_top_k == platform_max - 1
    # clamped 只描述平台收窄；租户收紧走 sources，不混进告警口径
    assert eff.clamped == ("recall_top_k",)
    assert _sources(eff)["recall_top_k"] == "tenant"


async def test_tenant_cap_below_scenario():
    eff = effective_retrieval(
        ScenarioPolicy(recall_top_k=10),
        platform_rerank_enabled=True,
        caps=TenantPolicyCaps(recall_top_k_max=8),
    )

    assert eff.recall_top_k == 8
    assert _sources(eff)["recall_top_k"] == "tenant"


async def test_tenant_cap_above_scenario_keeps_scenario():
    eff = effective_retrieval(
        ScenarioPolicy(recall_top_k=10),
        platform_rerank_enabled=True,
        caps=TenantPolicyCaps(recall_top_k_max=20),
    )

    assert eff.recall_top_k == 10
    assert _sources(eff)["recall_top_k"] == "scenario"


async def test_tenant_cap_below_platform_default():
    eff = effective_retrieval(
        None,
        platform_rerank_enabled=True,
        caps=TenantPolicyCaps(recall_top_k_max=8),
    )

    assert eff.recall_top_k == 8
    assert _sources(eff)["recall_top_k"] == "tenant"


# ══════════════════════════════════════════════════════════════════════
# 三、rerank：单向 AND → 租户只能关
# ══════════════════════════════════════════════════════════════════════


async def test_tenant_cap_can_disable_rerank():
    eff = effective_retrieval(
        ScenarioPolicy(rerank_enabled=True),
        platform_rerank_enabled=True,
        caps=TenantPolicyCaps(rerank_allowed=False),
    )

    assert eff.rerank_enabled is False
    assert _sources(eff)["rerank_enabled"] == "tenant"


async def test_tenant_cap_true_keeps_scenario_value():
    eff = effective_retrieval(
        ScenarioPolicy(rerank_enabled=True),
        platform_rerank_enabled=True,
        caps=TenantPolicyCaps(rerank_allowed=True),
    )

    assert eff.rerank_enabled is True
    assert _sources(eff)["rerank_enabled"] == "scenario"


async def test_platform_off_cannot_be_opened_by_scenario_or_cap():
    """平台关时，场景开 + cap true 也仍是 False，来源 platform。"""
    eff = effective_retrieval(
        ScenarioPolicy(rerank_enabled=True),
        platform_rerank_enabled=False,
        caps=TenantPolicyCaps(rerank_allowed=True),
    )

    assert eff.rerank_enabled is False
    assert _sources(eff)["rerank_enabled"] == "platform"


# ══════════════════════════════════════════════════════════════════════
# 四、阈值：租户只能抬高
# ══════════════════════════════════════════════════════════════════════


async def test_threshold_vector_min_raises_platform_default():
    platform_value = decisions.RELEVANCE_THRESHOLD_VECTOR
    eff = effective_retrieval(
        None,
        platform_rerank_enabled=True,
        caps=TenantPolicyCaps(threshold_vector_min=platform_value + 0.2),
    )

    assert eff.threshold_vector == pytest.approx(platform_value + 0.2)
    assert _sources(eff)["threshold_vector"] == "tenant"


async def test_threshold_vector_min_below_scenario_keeps_scenario():
    eff = effective_retrieval(
        ScenarioPolicy(relevance_threshold_vector=0.6),
        platform_rerank_enabled=True,
        caps=TenantPolicyCaps(threshold_vector_min=0.5),
    )

    assert eff.threshold_vector == 0.6
    assert _sources(eff)["threshold_vector"] == "scenario"


async def test_threshold_rerank_min_raises_and_marks_tenant():
    platform_value = decisions.RELEVANCE_THRESHOLD_RERANK
    eff = effective_retrieval(
        None,
        platform_rerank_enabled=True,
        caps=TenantPolicyCaps(threshold_rerank_min=platform_value + 0.1),
    )

    assert eff.threshold_rerank == pytest.approx(platform_value + 0.1)
    assert _sources(eff)["threshold_rerank"] == "tenant"


async def test_mixed_sources_report_all_three_stages():
    eff = effective_retrieval(
        ScenarioPolicy(recall_top_k=10, rerank_enabled=False),
        platform_rerank_enabled=True,
        caps=TenantPolicyCaps(
            recall_top_k_max=8,
            threshold_vector_min=decisions.RELEVANCE_THRESHOLD_VECTOR + 0.2,
        ),
    )

    assert _sources(eff) == {
        "recall_top_k": "tenant",
        "rerank_enabled": "scenario",
        "threshold_vector": "tenant",
        "threshold_rerank": "platform",
    }


# ══════════════════════════════════════════════════════════════════════
# 五、caps 透传：单趟与多轮都不能漏
# ══════════════════════════════════════════════════════════════════════


async def test_chain_single_path_passes_caps(monkeypatch):
    service = RetrievalService()
    seen: dict[str, object] = {}

    async def _fake_retrieve(*args, **kwargs):  # noqa: ANN001
        seen.update(kwargs)
        return RetrievalResult()

    monkeypatch.setattr(service, "retrieve", _fake_retrieve)
    caps = TenantPolicyCaps(recall_top_k_max=8)
    policy = ScenarioPolicy(recall_top_k=10)

    await service.retrieve_chain(
        None,
        "问题",
        uuid.uuid4(),
        [],
        1,
        [],
        rewrite_score=None,  # 拿不到复杂度信号 ⇒ 单趟
        policy=policy,
        policy_caps=caps,
    )

    assert seen["policy"] is policy
    assert seen["policy_caps"] is caps


async def test_chain_multi_round_passes_caps_to_every_round(monkeypatch):
    service = RetrievalService()
    seen: list[object] = []

    async def _fake_retrieve(*args, **kwargs):  # noqa: ANN001
        seen.append(kwargs.get("policy_caps"))
        return RetrievalResult()

    async def _fake_decompose(query, tenant_id):  # noqa: ANN001
        return ["子问题一", "子问题二"]

    monkeypatch.setattr(service, "retrieve", _fake_retrieve)
    monkeypatch.setattr(retrieve_mod, "decompose_question", _fake_decompose)
    monkeypatch.setattr(decisions, "should_use_chain", lambda score: True)

    caps = TenantPolicyCaps(recall_top_k_max=8)
    await service.retrieve_chain(
        None,
        "复杂问题",
        uuid.uuid4(),
        [],
        1,
        [],
        rewrite_score=0,
        policy=None,
        policy_caps=caps,
    )

    assert seen == [caps, caps]
