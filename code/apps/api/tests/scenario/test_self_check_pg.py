"""启动自检的**扫描口径** —— Real-DB 层。

只证明一件事：自检扫到的东西与请求期解析到的东西是同一批。

  · 治理开启：`v1 published → 钉定 v1 → 发布 v2`（v1 变 archived）的场景**必须被扫到**，
    且按 v1（archived）折叠；如果扫描 SQL 写死 `v.state='published'`，这条场景会漏扫。
  · 治理关闭：同一场景不进入扫描（只扫 published，v2 被扫到但 v1 的钉定口径不生效）。
  · `tier='hot'` 的场景（CHECK 允许 hot + db_binding）必须被 S-10 计数并 WARN。

★ 不用「总数恰好等于 N」断言：测试库里还有种子租户/其它用例留下的行。每个用例造
  **独立租户**，断言按 tenant_id / scenario_key 过滤 —— 可区分、可定位，不靠碰巧。

★ 关闭 `SCENARIO_TEST_DB_URL` 时 `schema` 夹具自动 skip（同 `test_step7_pin_pg.py`）。
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from app.config.settings import get_settings
from app.services.scenario import self_check
from app.services.scenario.dna_declare import build_dna_declared
from app.services.scenario.snapshot import compute_checksum

pytestmark = pytest.mark.asyncio

_LOGGER_NAME = "app.services.scenario.self_check"


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


async def _insert_version(
    session,
    *,
    tenant_id: uuid.UUID,
    scenario_id: uuid.UUID,
    version: int,
    state: str,
    policy: dict,
    archived: bool = False,
) -> None:
    """插入一条版本行（约束要求的列全部显式给，不依赖 server_default）。"""
    dna = build_dna_declared()
    checksum = compute_checksum(
        policy=policy, kb_ids=[], binding_mode="unrestricted", dna_declared=dna
    )
    now = datetime.now(timezone.utc)
    await session.execute(
        text(
            "INSERT INTO scenario_versions "
            "(tenant_id, scenario_id, version, state, policy, kb_ids, binding_mode, "
            " dna_declared, checksum, validated_at, published_at, archived_at) "
            "VALUES (:t, :s, :v, :state, CAST(:policy AS jsonb), '[]'::jsonb, "
            "        'unrestricted', CAST(:dna AS jsonb), :checksum, :now, :now, :archived_at)"
        ),
        {
            "t": tenant_id,
            "s": scenario_id,
            "v": version,
            "state": state,
            "policy": json.dumps(policy),
            "dna": json.dumps(dna),
            "checksum": checksum,
            "now": now,
            "archived_at": now if archived else None,
        },
    )


async def _seed_pinned_archived(
    session, *, recall_top_k: int = 3
) -> tuple[uuid.UUID, uuid.UUID, str]:
    """造「v1 published → 钉定 v1 → 发布 v2」的终态：v1 archived / v2 published。

    返回 (tenant_id, scenario_id, scenario_key)。`recall_top_k` 写进 v1 的 policy，
    让 S-2b 命中 —— 于是「这一行真的被扫到」能落到一条带本租户 id 的 WARN 上，
    而不是只能断言一个无法区分的总数。
    """
    tenant_id = uuid.uuid4()
    scenario_id = uuid.uuid4()
    scenario_key = f"selfchk_{str(scenario_id)[:8]}"

    await session.execute(
        text("INSERT INTO tenants (id, name, code) VALUES (:id, '自检扫描口径租户', :code)"),
        {"id": tenant_id, "code": f"selfchk-{tenant_id}"},
    )
    # tier=cold 要求 db_binding IS NULL（ck_scenarios_tier_binding）
    await session.execute(
        text(
            "INSERT INTO scenarios "
            "(id, tenant_id, scenario_key, name, is_builtin, db_binding, tier, pinned_version) "
            "VALUES (:id, :t, :key, '自检扫描口径场景', false, NULL, 'cold', 1)"
        ),
        {"id": scenario_id, "t": tenant_id, "key": scenario_key},
    )
    # v1 当时发布过、后被 v2 顶成 archived；钉定仍指向它
    await _insert_version(
        session,
        tenant_id=tenant_id,
        scenario_id=scenario_id,
        version=1,
        state="archived",
        policy={"recall_top_k": recall_top_k},
        archived=True,
    )
    await _insert_version(
        session,
        tenant_id=tenant_id,
        scenario_id=scenario_id,
        version=2,
        state="published",
        policy={},
    )
    await session.commit()
    return tenant_id, scenario_id, scenario_key


def _set_switches(monkeypatch, *, governance: bool) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "tenant_governance_enabled", governance)
    monkeypatch.setattr(settings, "rerank_enabled", False)


def _records_of_tenant(caplog, tenant_id: uuid.UUID) -> list[logging.LogRecord]:
    """本租户的 WARN 记录（测试库里有别的行，必须按 tenant_id 过滤）。"""
    return [
        record
        for record in caplog.records
        if record.name == _LOGGER_NAME
        and getattr(record, "tenant_id", None) == str(tenant_id)
    ]


# ══════════════════════════════════════════════════════════════════════
# 一、治理开启：钉定到归档版本的场景必须被扫到，并按 archived 折叠
# ══════════════════════════════════════════════════════════════════════


async def test_governed_scan_includes_pinned_archived_scenario(
    db_session, monkeypatch, caplog
):
    tenant_id, _, scenario_key = await _seed_pinned_archived(db_session)
    try:
        _set_switches(monkeypatch, governance=True)
        caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)

        folded = await self_check._load_all_for_scan(db_session)
        ours = [row for row in folded if row.tenant_id == tenant_id]
        assert len(ours) == 1, f"钉定归档版本未被扫到：{[r.scenario_key for r in folded]}"
        assert ours[0].scenario_key == scenario_key
        assert ours[0].version == 1, "钉定场景必须折叠到 pinned_version，而不是最新的 v2"
        assert ours[0].version_state == "archived"
        assert ours[0].pinned_version == 1

        counts = await self_check.check_scenario_data(db_session, max_step=3)
        assert counts["scanned_versions"] >= 1

        # 唯一标识断言：v1 的 policy 里 recall_top_k=3 → S-2b 命中本租户，
        # 且本租户只有这一条 WARN（既证明扫到了，也证明没有 fold 成 unparsable）
        ours_records = _records_of_tenant(caplog, tenant_id)
        assert len(ours_records) == 1, [
            (r.getMessage(), getattr(r, "scenario_key", None)) for r in ours_records
        ]
        s2b = [r for r in ours_records if getattr(r, "recall_top_k", None) == 3]
        assert s2b, (
            "钉定到归档版本的行没有被判据扫到（WARN 里找不到本租户的 recall_top_k=3）"
        )
        assert s2b[0].scenario_key == scenario_key
    finally:
        await _cleanup(db_session, tenant_id)


# ══════════════════════════════════════════════════════════════════════
# 二、S-10：tier='hot' 声明了但不生效
# ══════════════════════════════════════════════════════════════════════


async def test_scan_counts_hot_tier_declared(db_session, monkeypatch, caplog):
    """CHECK 允许 hot + db_binding；自检必须把它计成 `tier_hot_unsupported`。"""
    tenant_id = uuid.uuid4()
    scenario_id = uuid.uuid4()
    scenario_key = f"hot_{str(scenario_id)[:8]}"
    try:
        await db_session.execute(
            text("INSERT INTO tenants (id, name, code) VALUES (:id, 'hot 档租户', :code)"),
            {"id": tenant_id, "code": f"hot-{tenant_id}"},
        )
        await db_session.execute(
            text(
                "INSERT INTO scenarios "
                "(id, tenant_id, scenario_key, name, is_builtin, db_binding, tier) "
                "VALUES (:id, :t, :key, 'hot 档场景', false, 'kb_hot01', 'hot')"
            ),
            {"id": scenario_id, "t": tenant_id, "key": scenario_key},
        )
        await _insert_version(
            db_session,
            tenant_id=tenant_id,
            scenario_id=scenario_id,
            version=1,
            state="published",
            policy={},
        )
        await db_session.commit()

        _set_switches(monkeypatch, governance=False)
        caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)

        counts = await self_check.check_scenario_data(db_session, max_step=3)

        assert counts["tier_hot_unsupported"] >= 1
        hot = [
            r
            for r in _records_of_tenant(caplog, tenant_id)
            if getattr(r, "tier", None) == "hot"
        ]
        assert hot, "hot 档必须留下 WARN"
        assert hot[0].scenario_key == scenario_key
        assert "不会生效" in hot[0].getMessage()
    finally:
        await _cleanup(db_session, tenant_id)


# ══════════════════════════════════════════════════════════════════════
# 三、治理关闭：同一钉定归档场景不进入扫描
# ══════════════════════════════════════════════════════════════════════


async def test_closed_scan_excludes_pinned_archived_scenario(db_session, monkeypatch, caplog):
    """关闭态只扫 published：v1（archived）不进扫描，本租户不留任何判据 WARN。"""
    tenant_id, _, scenario_key = await _seed_pinned_archived(db_session)
    try:
        _set_switches(monkeypatch, governance=False)
        caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)

        folded = await self_check._load_all_for_scan(db_session)
        ours = [row for row in folded if row.tenant_id == tenant_id]
        assert ours, "关闭态应扫描到 v2（published）"
        assert all(row.version == 2 for row in ours), (
            f"关闭态不许扫到 archived 的 v1：{[row.version for row in ours]}"
        )
        assert all(row.version_state == "published" for row in ours)

        counts = await self_check.check_scenario_data(db_session, max_step=3)
        assert counts["scanned_versions"] >= 1
        # v1 的 recall_top_k=3 只在 archived 行上；它没被扫到 → 本租户不应有 S-2b WARN
        assert not _records_of_tenant(caplog, tenant_id), (
            f"关闭态扫到了不属于 published 口径的行：{scenario_key}"
        )
    finally:
        await _cleanup(db_session, tenant_id)
