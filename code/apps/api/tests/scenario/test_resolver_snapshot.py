"""F.2（NO-DB 层）：口径快照的值语义 —— Z-2、DNA 只从平台读、frozen、指纹、缓存不缓存快照。

其中 `test_resolve_default_returns_platform_dna` 是 **Step 2 的验收硬指标**：
resolver 对默认（内置）场景返回的 `IndexProfile` 必须与 `current_index_profile()`
逐字段相等，且 `fingerprint` 相等。
"""
from __future__ import annotations

import dataclasses
import uuid

import pytest

import app.services.scenario.resolver as resolver_mod
from app.config.graph_profile import current_graph_profile
from app.config.index_profile import current_index_profile
from app.config.settings import get_settings
from app.config.wiki_profile import current_wiki_profile
from app.services.scenario import resolve_scenario
from app.services.scenario.snapshot import (
    ScenarioRow,
    ScenarioSnapshot,
    current_dna,
)
from tests.scenario.conftest import (
    KB_A,
    KB_B,
    TENANT_ID,
    FakeSession,
    build_row,
    make_application,
)

pytestmark = pytest.mark.asyncio


# ── Z-2：默认场景 == 平台口径（Step 2 硬指标）──────────────────────────


async def test_resolve_default_returns_platform_dna():
    """未绑定场景的应用：三个 profile 与 `current_*_profile()` 逐字段相等（Z-2）。"""
    snapshot = await resolve_scenario(
        FakeSession([build_row(is_builtin=True)]),
        tenant_id=TENANT_ID,
        application=make_application(scenario_id=None),
        requested_kb_ids=[KB_A],
    )
    index = current_index_profile()
    graph = current_graph_profile()
    wiki = current_wiki_profile()

    # ── 逐字段（不是整体 ==，失败时要看得出是哪个字段）──
    assert snapshot.index.embedding_model == index.embedding_model
    assert snapshot.index.embedding_dim == index.embedding_dim
    assert snapshot.index.embedding_text_version == index.embedding_text_version
    assert snapshot.index.chunker_version == index.chunker_version
    assert snapshot.graph.extractor_model == graph.extractor_model
    assert snapshot.graph.prompt_version == graph.prompt_version
    assert snapshot.wiki.generator_model == wiki.generator_model
    assert snapshot.wiki.prompt_version == wiki.prompt_version

    # ── 硬指标的第二半：指纹相等（过滤口径完全一致，旧 chunk 不会被误排除）──
    assert snapshot.index == index
    assert snapshot.index.fingerprint == index.fingerprint
    assert snapshot.graph.fingerprint == graph.fingerprint
    assert snapshot.wiki.fingerprint == wiki.fingerprint


async def test_current_dna_equals_three_profiles():
    """组合器与三个原函数逐字段相等（防未来重构让「零改动」悄悄漂移）。"""
    index, graph, wiki = current_dna()
    assert index == current_index_profile()
    assert graph == current_graph_profile()
    assert wiki == current_wiki_profile()


# ── DNA 只从平台读，绝不从 dna_declared 读（D-A3 落地证明）──────────────


async def test_snapshot_dna_is_platform_not_row():
    """把行的 dna_declared 改成 `etext-evil`，快照里仍是 `etext-v1`。

    这一条是 `dna_declared` 只是声明镜像的**行为证据**（不是文档口号）：
    一次 `UPDATE scenario_versions SET dna_declared=...` 无法换掉口径。
    """
    evil = dict(build_row()["dna_declared"])
    evil["embedding_text_version"] = "etext-evil"
    evil["chunker_version"] = "c-evil"
    evil["graph_prompt_version"] = "gprompt-evil"
    evil["wiki_prompt_version"] = "wprompt-evil"

    snapshot = await resolve_scenario(
        FakeSession([build_row(is_builtin=True, dna_declared=evil)]),
        tenant_id=TENANT_ID,
        application=make_application(scenario_id=None),
        requested_kb_ids=[KB_A],
    )
    assert snapshot.index.embedding_text_version == "etext-v1"
    assert snapshot.index.chunker_version == "c400-800-60"
    assert snapshot.graph.prompt_version == "gprompt-v1"
    assert snapshot.wiki.prompt_version == "wprompt-v1"
    for evil_value in ("etext-evil", "c-evil", "gprompt-evil", "wprompt-evil"):
        assert evil_value not in repr(snapshot)


async def test_empty_model_resolved_not_persisted(monkeypatch):
    """`GRAPH_EXTRACTOR_MODEL=""` 时快照里是 `settings.llm_model` 的实际值，不是空串。

    镜像 `graph_profile.py:61-65` 的纪律：空串落库会让「换主模型」在图上悄悄生效，
    且再也回溯不到当时到底用了哪个模型。
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "graph_extractor_model", "")
    monkeypatch.setattr(settings, "wiki_generator_model", "")
    monkeypatch.setattr(settings, "llm_model", "unit-test-model")

    snapshot = await resolve_scenario(
        FakeSession([build_row(is_builtin=True)]),
        tenant_id=TENANT_ID,
        application=make_application(scenario_id=None),
        requested_kb_ids=[KB_A],
    )
    assert snapshot.graph.extractor_model == "unit-test-model"
    assert snapshot.graph.extractor_model != ""
    assert snapshot.wiki.generator_model == "unit-test-model"


# ── 不可变 / 指纹 ─────────────────────────────────────────────────────


async def test_snapshot_frozen():
    snapshot = await resolve_scenario(
        FakeSession([build_row(is_builtin=True)]),
        tenant_id=TENANT_ID,
        application=make_application(scenario_id=None),
        requested_kb_ids=[KB_A],
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.kb_ids = ()  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.scenario_key = "other"  # type: ignore[misc]


async def test_fingerprint_stable():
    """同输入两次指纹相同；改 version 则不同（审计要能回指到具体版本行）。"""
    app = make_application(scenario_id=None)
    first = await resolve_scenario(
        FakeSession([build_row(is_builtin=True, version=1)]),
        tenant_id=TENANT_ID, application=app, requested_kb_ids=[KB_A],
    )
    resolver_mod.clear_cache()
    second = await resolve_scenario(
        FakeSession([build_row(is_builtin=True, version=1)]),
        tenant_id=TENANT_ID, application=app, requested_kb_ids=[KB_A],
    )
    resolver_mod.clear_cache()
    bumped = await resolve_scenario(
        FakeSession([build_row(is_builtin=True, version=2)]),
        tenant_id=TENANT_ID, application=app, requested_kb_ids=[KB_A],
    )
    assert first.fingerprint == second.fingerprint
    assert first.fingerprint != bumped.fingerprint
    assert len(first.fingerprint) == 12


async def test_fingerprint_covers_declared_policy():
    """指纹覆盖**已声明**的策略：同一场景改了阈值就是不同口径。"""
    app = make_application(scenario_id=None)
    plain = await resolve_scenario(
        FakeSession([build_row(is_builtin=True, policy={})]),
        tenant_id=TENANT_ID, application=app, requested_kb_ids=[KB_A],
    )
    resolver_mod.clear_cache()
    tuned = await resolve_scenario(
        FakeSession([build_row(is_builtin=True, policy={"relevance_threshold_vector": 0.62})]),
        tenant_id=TENANT_ID, application=app, requested_kb_ids=[KB_A],
    )
    assert plain.fingerprint != tuned.fingerprint
    assert tuned.policy.relevance_threshold_vector == 0.62


# ── 缓存：只缓存行，不缓存快照（防跨请求越权）──────────────────────────


async def test_snapshot_not_cached_across_requests():
    """两次请求 `K_r` 不同 ⇒ 两次 `kb_ids` 各等于自己请求的集合。

    若缓存了快照，第二次会把第一次的集合发给第二个用户 —— 最严重的一类越权。
    """
    app = make_application(scenario_id=None)
    session = FakeSession([build_row(is_builtin=True)])

    first = await resolve_scenario(
        session, tenant_id=TENANT_ID, application=app, requested_kb_ids=[KB_A]
    )
    second = await resolve_scenario(
        session, tenant_id=TENANT_ID, application=app, requested_kb_ids=[KB_B]
    )
    assert first.kb_ids == (KB_A,)
    assert second.kb_ids == (KB_B,)
    assert first is not second

    # 结构证据：缓存里存的是**行**（无请求态字段），不是快照
    cached = list(resolver_mod._CACHE.values())
    assert cached, "首次读之后应有一行缓存"
    for _stored_at, value in cached:
        assert isinstance(value, ScenarioRow)
        assert not isinstance(value, ScenarioSnapshot)


async def test_cache_avoids_second_query():
    """同一 (tenant, scenario) 第二次解析不再打 DB（行缓存生效）。"""
    app = make_application(scenario_id=None)
    session = FakeSession([build_row(is_builtin=True)])
    await resolve_scenario(session, tenant_id=TENANT_ID, application=app, requested_kb_ids=[KB_A])
    assert len(session.statements) == 1
    await resolve_scenario(session, tenant_id=TENANT_ID, application=app, requested_kb_ids=[KB_B])
    assert len(session.statements) == 1, "缓存未生效：第二次又打了一次 DB"


async def test_invalidate_forces_reload():
    app = make_application(scenario_id=None)
    session = FakeSession([build_row(is_builtin=True)])
    await resolve_scenario(session, tenant_id=TENANT_ID, application=app, requested_kb_ids=[KB_A])
    resolver_mod.invalidate(TENANT_ID, None)
    await resolve_scenario(session, tenant_id=TENANT_ID, application=app, requested_kb_ids=[KB_A])
    assert len(session.statements) == 2


async def test_miss_is_not_negatively_cached():
    """未命中的行**不进缓存**：补种内置行后应立即生效，无需等 TTL。"""
    tenant = uuid.uuid4()
    app = make_application(tenant_id=tenant, scenario_id=None)
    empty = FakeSession([])
    await resolve_scenario(empty, tenant_id=tenant, application=app, requested_kb_ids=[KB_A])
    assert (tenant, None) not in resolver_mod._CACHE

    seeded = FakeSession([build_row(tenant_id=tenant, is_builtin=True)])
    snapshot = await resolve_scenario(
        seeded, tenant_id=tenant, application=app, requested_kb_ids=[KB_A]
    )
    assert snapshot.source == "builtin_row"
