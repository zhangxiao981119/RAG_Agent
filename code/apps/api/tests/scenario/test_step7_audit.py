"""Step 7 S7-7：版本三动作与租户治理的**审计**回归（真库 + ASGI）。

要锁死的两件事：

  · 版本状态机的 validate / publish / archive 三个动作各自落一条成功审计，
    `object_id` 是「场景:版本」，detail 带 version 与 checksum；
  · 治理写接口在**成功**时写 audit（库白名单 / 场景治理 / 分级变化），
    在**被拒**时一条成功审计都不写 —— 审计不能比事实乐观。

★ 审计一律用独立连接回读（`committed_rows`），不读响应体自证：响应体成功
  与库里留下痕迹是两件事，后者才是事后追责的依据。
"""
from __future__ import annotations

import json
import uuid

import pytest

from app.config.settings import get_settings
from tests.scenario.pg_helpers import _exec_committed, committed_rows

pytestmark = pytest.mark.asyncio

DB_SCOPE_URL = "/api/admin/tenant/db-scope"
SCENARIO_GOVERNANCE_URL = "/api/admin/scenario-governance"


# ══════════════════════════════════════════════════════════════════════
# 工具
# ══════════════════════════════════════════════════════════════════════


async def _audit_rows(action: str, object_id: str) -> list[dict]:
    """按动作 + 对象回读审计 detail（asyncpg 的 jsonb 可能是 str，显式 loads）。"""
    rows = await committed_rows(
        "SELECT detail FROM audit_logs WHERE action=$1 AND object_id=$2 ORDER BY created_at",
        action,
        object_id,
    )
    return [
        json.loads(row["detail"]) if isinstance(row["detail"], str) else row["detail"]
        for row in rows
    ]


def _set_governance(monkeypatch, enabled: bool) -> None:
    monkeypatch.setattr(get_settings(), "tenant_governance_enabled", enabled)


async def _mk_scenario(api, key: str) -> uuid.UUID:
    response = await api.client.post(
        "/api/scenarios", json={"scenario_key": key, "name": "审计回归用例"}
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


async def _mk_version(api, scenario_id: uuid.UUID, *, note: str = "初稿") -> dict:
    response = await api.client.post(
        f"/api/scenarios/{scenario_id}/versions",
        json={
            "policy": {},
            "kb_ids": [str(kb) for kb in api.kb_ids],
            "change_note": note,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _action(
    api, scenario_id: uuid.UUID, version: int, action: str, checksum: str
) -> dict:
    response = await api.client.post(
        f"/api/scenarios/{scenario_id}/versions/{version}/{action}",
        headers={"If-Match": f'"{checksum}"'},
    )
    assert response.status_code == 200, response.text
    return response.json()


# ══════════════════════════════════════════════════════════════════════
# 一、版本三动作各自留审计
# ══════════════════════════════════════════════════════════════════════


async def test_version_actions_are_audited(api):
    """validate / publish / archive 各至少一行，object_id=「场景:版本」，detail 带 version+checksum。"""
    scenario_id = await _mk_scenario(api, "audit_version")
    row = await _mk_version(api, scenario_id)
    version = row["version"]
    checksum = row["checksum"]

    for action in ("validate", "publish", "archive"):
        await _action(api, scenario_id, version, action, checksum)

    object_id = f"{scenario_id}:{version}"
    for action in (
        "scenario.version.validate",
        "scenario.version.publish",
        "scenario.version.archive",
    ):
        details = await _audit_rows(action, object_id)
        assert details, f"{action} 没有审计行（object_id={object_id}）"
        for detail in details:
            assert detail["version"] == version
            assert "checksum" in detail, f"{action} 的 detail 丢了 checksum：{detail}"


# ══════════════════════════════════════════════════════════════════════
# 二、治理写动作留审计（before / after 都可回读）
# ══════════════════════════════════════════════════════════════════════


async def test_governance_actions_are_audited(api, monkeypatch):
    """库白名单 / 场景治理 / 分级变化三处成功写都必须留下审计。"""
    _set_governance(monkeypatch, True)

    # ── ① 库白名单：after 必须是排序去重后的列表 ──────────────────
    scope = await api.client.put(
        DB_SCOPE_URL, json={"allowed_db_keys": ["kb_finance", "default"]}
    )
    assert scope.status_code == 200, scope.text

    scope_details = await _audit_rows("tenant.db_scope.update", str(api.tenant_id))
    assert len(scope_details) == 1, scope_details
    scope_detail = scope_details[0]
    assert "before" in scope_detail and "after" in scope_detail
    assert scope_detail["after"] == ["default", "kb_finance"]

    # ── ② 场景治理：钉定要求已发布版本 ───────────────────────────
    scenario_id = await _mk_scenario(api, "audit_governance")
    row = await _mk_version(api, scenario_id)
    await _action(api, scenario_id, row["version"], "validate", row["checksum"])
    await _action(api, scenario_id, row["version"], "publish", row["checksum"])

    pinned = await api.client.put(
        f"{SCENARIO_GOVERNANCE_URL}/{scenario_id}",
        json={"pinned_version": 1, "recall_top_k_max": 8},
    )
    assert pinned.status_code == 200, pinned.text

    governance_details = await _audit_rows(
        "scenario.governance.update", str(scenario_id)
    )
    assert len(governance_details) == 1, governance_details
    governance_detail = governance_details[0]
    assert "before" in governance_detail and "after" in governance_detail
    assert governance_detail["after"]["pinned_version"] == 1
    assert governance_detail["after"]["recall_top_k_max"] == 8

    # ── ③ 分级变化：单独一条 tier.change，before/after 是旧/新 tier ──
    await _exec_committed(
        "UPDATE scenarios SET tier='warm', db_binding='kb_finance' WHERE id=$1",
        scenario_id,
    )
    await _exec_committed(
        "UPDATE scenarios SET tier='hot' WHERE id=$1", scenario_id
    )
    tier = await api.client.put(
        f"{SCENARIO_GOVERNANCE_URL}/{scenario_id}", json={"tier": "warm"}
    )
    assert tier.status_code == 200, tier.text

    tier_details = await _audit_rows("scenario.tier.change", str(scenario_id))
    assert len(tier_details) == 1, tier_details
    assert tier_details[0]["before"] == "hot"
    assert tier_details[0]["after"] == "warm"


# ══════════════════════════════════════════════════════════════════════
# 三、被拒的写不写成功审计
# ══════════════════════════════════════════════════════════════════════


async def test_rejected_write_writes_no_success_audit(api, monkeypatch):
    """钉定 draft 版本被 400 拒绝时，治理与分级审计都必须 0 行。"""
    _set_governance(monkeypatch, True)
    scenario_id = await _mk_scenario(api, "audit_reject")
    await _mk_version(api, scenario_id)  # 保持 draft，不能钉

    rejected = await api.client.put(
        f"{SCENARIO_GOVERNANCE_URL}/{scenario_id}", json={"pinned_version": 1}
    )
    assert rejected.status_code == 400, rejected.text
    assert rejected.json()["detail"]["code"] == "PIN_VERSION_NOT_PUBLISHED"

    assert await _audit_rows("scenario.governance.update", str(scenario_id)) == []
    assert await _audit_rows("scenario.tier.change", str(scenario_id)) == []
