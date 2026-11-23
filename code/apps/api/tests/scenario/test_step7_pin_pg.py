"""Step 7 S7-3：钉定版本 → 库位映射 / 白名单 / resolver 三者同口径 —— Real-DB 层。

只用一条真实链路证明一件事：**版本与库位来自同一个版本号**。

  · v1（archived，kb={A}）+ v2（published，kb={A,B}）+ pin=1：
      治理开启 → A→kb_finance、B→default（v1 没声明 B），resolver 对 B 报 R4；
      治理关闭 → 按 v2，A 与 B 都落在 kb_finance。
  · 白名单越界（含 default）→ `DB_SCOPE_NOT_ALLOWED`；关闭态忽略白名单。
  · 钉定版本被删（无该版本行）→ `SCENARIO_UNRESOLVABLE`，不回落 published。

★ 关闭 `SCENARIO_TEST_DB_URL` 时整文件 skip（`schema` / `db_session` 夹具自带守卫）。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from app.config.settings import get_settings
from app.services.db_scope import (
    DB_SCOPE_NOT_ALLOWED,
    DbScopeError,
    map_kb_db_keys,
    resolve_kb_db_key,
)
from app.services.scenario import resolve_scenario
from app.services.scenario.dna_declare import build_dna_declared
from app.services.scenario.errors import ScenarioCode, ScenarioError
from app.services.scenario.snapshot import compute_checksum
from tests.scenario.conftest import KB_A, KB_B, make_application

pytestmark = pytest.mark.asyncio

DEFAULT_DB_BINDING = "kb_finance"
"""测试场景绑定的库位键（符合 `ck_scenarios_db_binding_format`，且不需要真实注册连接）。"""


async def _cleanup(session, tenant_id: uuid.UUID) -> None:
    """按 FK 方向逆序清理（scenarios / scenario_versions 的 tenant FK 无 CASCADE）。"""
    await session.rollback()
    await session.execute(
        text("DELETE FROM scenario_versions WHERE tenant_id = :t"), {"t": tenant_id}
    )
    await session.execute(
        text("DELETE FROM scenarios WHERE tenant_id = :t"), {"t": tenant_id}
    )
    await session.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})
    await session.commit()


async def _seed(
    session,
    *,
    pinned: int | None,
    allowed: list[str] | None,
    v1_kb: list[uuid.UUID],
    v2_kb: list[uuid.UUID],
) -> tuple[uuid.UUID, uuid.UUID, dict[int, str]]:
    """造一个「v1 archived / v2 published / 指定钉定 / 指定白名单」的租户。

    返回 (tenant_id, scenario_id, {version: checksum})。
    """
    tenant_id = uuid.uuid4()
    scenario_id = uuid.uuid4()
    dna = build_dna_declared()
    checksums: dict[int, str] = {}

    async def _insert_version(version: int, state: str, kb: list[uuid.UUID]) -> None:
        kb_text = [str(item) for item in kb]
        checksum = compute_checksum(
            policy={}, kb_ids=kb_text, binding_mode="explicit", dna_declared=dna
        )
        checksums[version] = checksum
        await session.execute(
            text(
                "INSERT INTO scenario_versions "
                "(tenant_id, scenario_id, version, state, policy, kb_ids, binding_mode, "
                " dna_declared, checksum, published_at, archived_at) "
                "VALUES (:t, :s, :v, :state, '{}'::jsonb, CAST(:kb AS jsonb), 'explicit', "
                "        CAST(:dna AS jsonb), :checksum, now(), :archived_at)"
            ),
            {
                "t": tenant_id,
                "s": scenario_id,
                "v": version,
                "state": state,
                "kb": json.dumps(kb_text),
                "dna": json.dumps(dna),
                "checksum": checksum,
                "archived_at": (
                    datetime.now(timezone.utc) if state == "archived" else None
                ),
            },
        )

    await session.execute(
        text("INSERT INTO tenants (id, name, code) VALUES (:id, '钉定测试租户', :code)"),
        {"id": tenant_id, "code": f"pin-{tenant_id}"},
    )
    # db_binding 非空 ⇒ tier 必须是 warm/hot（0031 的 ck_scenarios_tier_binding）
    await session.execute(
        text(
            "INSERT INTO scenarios "
            "(id, tenant_id, scenario_key, name, is_builtin, db_binding, tier, pinned_version) "
            "VALUES (:id, :t, :key, '钉定测试场景', false, :binding, 'warm', :pinned)"
        ),
        {
            "id": scenario_id,
            "t": tenant_id,
            "key": f"pin_{str(scenario_id)[:8]}",
            "binding": DEFAULT_DB_BINDING,
            "pinned": pinned,
        },
    )
    await _insert_version(1, "archived", v1_kb)
    await _insert_version(2, "published", v2_kb)

    if allowed is None:
        await session.execute(
            text("UPDATE tenants SET allowed_db_keys = NULL WHERE id = :t"),
            {"t": tenant_id},
        )
    else:
        await session.execute(
            text(
                "UPDATE tenants SET allowed_db_keys = CAST(:keys AS text[]) WHERE id = :t"
            ),
            {"t": tenant_id, "keys": allowed},
        )
    await session.commit()
    return tenant_id, scenario_id, checksums


def _set_switches(monkeypatch, *, governance: bool) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "scene_db_split_enabled", True)
    monkeypatch.setattr(settings, "tenant_governance_enabled", governance)


# ══════════════════════════════════════════════════════════════════════
# 一、钉定版本同时决定「映射到哪个库」与「解析到哪个版本」
# ══════════════════════════════════════════════════════════════════════


async def test_pinned_version_drives_mapping_and_resolver(db_session, monkeypatch):
    tenant_id, scenario_id, checksums = await _seed(
        db_session,
        pinned=1,
        allowed=None,
        v1_kb=[KB_A],
        v2_kb=[KB_A, KB_B],
    )
    try:
        # 关闭治理：按 v2 的 kb 集合，B 也在绑定库位
        _set_switches(monkeypatch, governance=False)
        assert await map_kb_db_keys(db_session, tenant_id, [KB_B]) == {
            KB_B: DEFAULT_DB_BINDING
        }

        # 开启治理 + 钉定 v1：B 不在旧版声明里 → 落 default
        _set_switches(monkeypatch, governance=True)
        assert await map_kb_db_keys(db_session, tenant_id, [KB_A, KB_B]) == {
            KB_A: DEFAULT_DB_BINDING,
            KB_B: "default",
        }
        assert await resolve_kb_db_key(db_session, tenant_id, [KB_A]) == DEFAULT_DB_BINDING

        # resolver 与映射同口径：B 被 R4 拒绝（不是"查了一半返回空"）
        with pytest.raises(ScenarioError) as mismatch:
            await resolve_scenario(
                db_session,
                tenant_id=tenant_id,
                application=make_application(scenario_id=scenario_id),
                requested_kb_ids=[KB_B],
            )
        assert mismatch.value.code is ScenarioCode.MISMATCH

        snapshot = await resolve_scenario(
            db_session,
            tenant_id=tenant_id,
            application=make_application(scenario_id=scenario_id),
            requested_kb_ids=[KB_A],
        )
        assert snapshot.version == 1
        assert snapshot.version_source == "pinned"
        assert snapshot.checksum == checksums[1], "版本与校验和必须来自同一个版本行"
    finally:
        await _cleanup(db_session, tenant_id)


# ══════════════════════════════════════════════════════════════════════
# 二、白名单拒绝（含 default）；关闭态忽略白名单
# ══════════════════════════════════════════════════════════════════════


async def test_whitelist_blocks_pinned_db_key(db_session, monkeypatch):
    tenant_id, _, _ = await _seed(
        db_session,
        pinned=1,
        allowed=["default"],
        v1_kb=[KB_A],
        v2_kb=[KB_A, KB_B],
    )
    try:
        _set_switches(monkeypatch, governance=True)
        with pytest.raises(DbScopeError) as denied:
            await resolve_kb_db_key(db_session, tenant_id, [KB_A])
        assert denied.value.code == DB_SCOPE_NOT_ALLOWED
        assert denied.value.detail["denied"] == [DEFAULT_DB_BINDING]

        # 关闭治理：白名单整体不生效（与 Step 6 等价），A 仍解析到绑定库位
        _set_switches(monkeypatch, governance=False)
        assert await resolve_kb_db_key(db_session, tenant_id, [KB_A]) == DEFAULT_DB_BINDING
    finally:
        await _cleanup(db_session, tenant_id)


async def test_empty_whitelist_denies_default(db_session, monkeypatch):
    """`{}` 全禁：连 default 也不能用（v1 未声明的 B 会落 default）。"""
    tenant_id, _, _ = await _seed(
        db_session,
        pinned=1,
        allowed=[],
        v1_kb=[KB_A],
        v2_kb=[KB_A, KB_B],
    )
    try:
        _set_switches(monkeypatch, governance=True)
        with pytest.raises(DbScopeError) as denied:
            await resolve_kb_db_key(db_session, tenant_id, [KB_B])
        assert denied.value.code == DB_SCOPE_NOT_ALLOWED
        assert denied.value.detail["denied"] == ["default"]
    finally:
        await _cleanup(db_session, tenant_id)


# ══════════════════════════════════════════════════════════════════════
# 三、钉定版本不存在 → 不可解析（fail-closed）
# ══════════════════════════════════════════════════════════════════════


async def test_missing_pinned_version_is_unresolvable(db_session, monkeypatch):
    tenant_id, scenario_id, _ = await _seed(
        db_session,
        pinned=99,
        allowed=None,
        v1_kb=[KB_A],
        v2_kb=[KB_A],
    )
    try:
        _set_switches(monkeypatch, governance=True)
        with pytest.raises(ScenarioError) as unresolvable:
            await resolve_scenario(
                db_session,
                tenant_id=tenant_id,
                application=make_application(scenario_id=scenario_id),
                requested_kb_ids=[KB_A],
            )
        assert unresolvable.value.code is ScenarioCode.UNRESOLVABLE
    finally:
        await _cleanup(db_session, tenant_id)
