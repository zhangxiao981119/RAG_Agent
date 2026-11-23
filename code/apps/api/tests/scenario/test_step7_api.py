"""Step 7 S7-5：租户治理面 HTTP 接口（真库 + ASGI 直连）。

覆盖面（每条对应任务书的一项）：
  1. 非管理员 → 403（GET / PUT 各一条）；
  2. 开关关闭 → 写接口 400 `GOVERNANCE_DISABLED`、详情 `governance_enabled=false`；
  3. 库白名单：非法元素 / 合法值 / 审计 before-after / GET 回读；
  4. 钉定：draft 拒 / published 接受 / 解析按钉定、关闭开关按 published；
  5. 分级：hot 拒绝 / 缺 db_binding 拒 / 有 db_binding 通过；
  6. effective 预览：租户 cap 收紧与放开后的来源（tenant / scenario）；
  7. 幂等：同值重复 PUT 仍 200，审计照写（before == after）；
  8. 失败请求不写成功审计。

★ 库读一律用独立连接（`committed_rows` / `committed_scalar`），不读响应体自证。
★ 所有断言写**字面量**码值，不写枚举成员：枚举值改错时用例要能变红。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import replace

import pytest

from app.api.deps import get_current_user
from app.config.settings import get_settings
from app.main import app
from app.models import IntegrationApplication
from app.services.scenario import resolve_scenario
from app.services.scenario import resolver as resolver_mod
from tests.scenario.pg_helpers import (
    _exec_committed,
    committed_application,
    committed_rows,
    committed_scalar,
    committed_scenario,
)

pytestmark = pytest.mark.asyncio

DB_SCOPE_URL = "/api/admin/tenant/db-scope"
SCENARIO_GOVERNANCE_URL = "/api/admin/scenario-governance"


# ══════════════════════════════════════════════════════════════════════
# 构造 / 回读工具
# ══════════════════════════════════════════════════════════════════════


async def _mk_scenario(api, key: str) -> uuid.UUID:
    response = await api.client.post(
        "/api/scenarios", json={"scenario_key": key, "name": "治理测试场景"}
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


async def _mk_version(
    api, scenario_id: uuid.UUID, *, policy: dict | None = None, note: str = "初稿"
) -> dict:
    response = await api.client.post(
        f"/api/scenarios/{scenario_id}/versions",
        json={
            "policy": policy or {},
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


async def _validate_and_publish(api, scenario_id: uuid.UUID, version: dict) -> None:
    await _action(api, scenario_id, version["version"], "validate", version["checksum"])
    await _action(api, scenario_id, version["version"], "publish", version["checksum"])


async def _put_governance(api, scenario_id: uuid.UUID, body: dict):
    return await api.client.put(
        f"{SCENARIO_GOVERNANCE_URL}/{scenario_id}", json=body
    )


def _set_governance(monkeypatch, enabled: bool) -> None:
    monkeypatch.setattr(
        get_settings(), "tenant_governance_enabled", enabled
    )


async def _audit_rows(api, action: str, object_id: str | None = None) -> list[dict]:
    sql = (
        "SELECT action, detail, object_id FROM audit_logs "
        "WHERE tenant_id = $1 AND action = $2"
    )
    args: list[object] = [api.tenant_id, action]
    if object_id is not None:
        sql += " AND object_id = $3"
        args.append(object_id)
    sql += " ORDER BY id"
    rows = await committed_rows(sql, *args)
    for row in rows:
        # asyncpg 不解码 jsonb：显式 loads，才能断言 before/after 的结构
        row["detail"] = json.loads(row["detail"])
    return rows


# ══════════════════════════════════════════════════════════════════════
# 零、挂载守卫：治理面路由必须真的挂在 app 上
# ══════════════════════════════════════════════════════════════════════


async def test_governance_router_is_mounted():
    """漏一行 `include_router` 时，所有行为用例都会 404 —— 先守这一层。

    集合精确相等：漏挂会红，多挂一条也会红（治理面必须只暴露这三个端点）。
    """
    paths = app.openapi()["paths"]
    expected = {
        "/api/admin/tenant/db-scope": {"get", "put"},
        "/api/admin/scenario-governance": {"get"},
        "/api/admin/scenario-governance/{scenario_id}": {"get", "put"},
    }
    actual = {
        path: set(methods)
        for path, methods in paths.items()
        if path.startswith("/api/admin/tenant/")
        or path.startswith("/api/admin/scenario-governance")
    }
    assert actual == expected


# ══════════════════════════════════════════════════════════════════════
# 一、鉴权：非管理员 403
# ══════════════════════════════════════════════════════════════════════


def _override_non_admin(api) -> None:
    async def _non_admin():
        return replace(api.user, clearance=20)

    app.dependency_overrides[get_current_user] = _non_admin


async def test_non_admin_get_is_forbidden(api):
    _override_non_admin(api)

    response = await api.client.get(DB_SCOPE_URL)

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "FORBIDDEN"


async def test_non_admin_put_is_forbidden(api):
    _override_non_admin(api)
    scenario_id = await committed_scenario(api.tenant_id)

    response = await _put_governance(api, scenario_id, {"recall_top_k_max": 8})

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "FORBIDDEN"


# ══════════════════════════════════════════════════════════════════════
# 二、开关关闭：写拒绝、读标记 false
# ══════════════════════════════════════════════════════════════════════


async def test_switch_off_rejects_db_scope_write(api, monkeypatch):
    _set_governance(monkeypatch, False)

    response = await api.client.put(
        DB_SCOPE_URL, json={"allowed_db_keys": ["default"]}
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "GOVERNANCE_DISABLED"


async def test_switch_off_rejects_scenario_governance_write(api, monkeypatch):
    _set_governance(monkeypatch, False)
    scenario_id = await committed_scenario(api.tenant_id)

    response = await _put_governance(api, scenario_id, {"pinned_version": None})

    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "GOVERNANCE_DISABLED"


async def test_switch_off_detail_reports_disabled(api, monkeypatch):
    _set_governance(monkeypatch, False)
    scenario_id = await committed_scenario(api.tenant_id)

    response = await api.client.get(f"{SCENARIO_GOVERNANCE_URL}/{scenario_id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["governance_enabled"] is False
    # 无版本的环境下不编造有效值
    assert body["version"] is None
    assert body["version_source"] == "unresolvable"
    assert body["effective"] is None


# ══════════════════════════════════════════════════════════════════════
# 三、库白名单
# ══════════════════════════════════════════════════════════════════════


async def test_db_scope_rejects_invalid_key(api, monkeypatch):
    _set_governance(monkeypatch, True)

    response = await api.client.put(
        DB_SCOPE_URL, json={"allowed_db_keys": ["default", "not_a_db_key"]}
    )

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "DB_SCOPE_INVALID"
    assert detail["field"] == "allowed_db_keys"
    assert detail["context"]["value"] == "not_a_db_key"


async def test_db_scope_write_normalizes_and_audits(api, monkeypatch):
    _set_governance(monkeypatch, True)

    response = await api.client.put(
        DB_SCOPE_URL, json={"allowed_db_keys": ["kb_finance", "default", "kb_finance"]}
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "allowed_db_keys": ["default", "kb_finance"],
        "governance_enabled": True,
    }

    rows = await _audit_rows(api, "tenant.db_scope.update")
    assert len(rows) == 1
    detail = rows[0]["detail"]
    assert detail["before"] is None
    assert detail["after"] == ["default", "kb_finance"]
    assert rows[0]["object_id"] == str(api.tenant_id)

    # 读回用 GET（管理端读最新值），再用独立连接确认库里存的就是它
    readback = await api.client.get(DB_SCOPE_URL)
    assert readback.status_code == 200, readback.text
    assert readback.json()["allowed_db_keys"] == ["default", "kb_finance"]

    stored = await committed_rows(
        "SELECT allowed_db_keys FROM tenants WHERE id = $1", api.tenant_id
    )
    assert stored[0]["allowed_db_keys"] == ["default", "kb_finance"]


# ══════════════════════════════════════════════════════════════════════
# 四、钉定版本
# ══════════════════════════════════════════════════════════════════════


async def test_pin_requires_published_and_drives_resolution(api, monkeypatch):
    _set_governance(monkeypatch, True)
    scenario_id = await _mk_scenario(api, "gov_pin")
    v1 = await _mk_version(api, scenario_id)

    # draft 版本不可钉
    rejected = await _put_governance(api, scenario_id, {"pinned_version": 1})
    assert rejected.status_code == 400, rejected.text
    assert rejected.json()["detail"]["code"] == "PIN_VERSION_NOT_PUBLISHED"

    await _validate_and_publish(api, scenario_id, v1)

    pinned = await _put_governance(api, scenario_id, {"pinned_version": 1})
    assert pinned.status_code == 200, pinned.text
    assert pinned.json()["pinned_version"] == 1

    # 发 v2 之后 v1 被归档，钉定仍应解析到 v1（archived 必须被接受）
    v2 = await _mk_version(api, scenario_id, note="第二版")
    await _validate_and_publish(api, scenario_id, v2)

    application_id = await committed_application(
        api.tenant_id,
        created_by=api.user_id,
        scenario_id=scenario_id,
        kb_ids=api.kb_ids,
    )

    resolver_mod.clear_cache()
    async with api.session_factory() as session:
        application = await session.get(IntegrationApplication, application_id)
        pinned_snapshot = await resolve_scenario(
            session,
            tenant_id=api.tenant_id,
            application=application,
            requested_kb_ids=list(api.kb_ids),
        )
    assert pinned_snapshot.version == 1
    assert pinned_snapshot.version_source == "pinned"

    _set_governance(monkeypatch, False)
    resolver_mod.clear_cache()
    async with api.session_factory() as session:
        application = await session.get(IntegrationApplication, application_id)
        published_snapshot = await resolve_scenario(
            session,
            tenant_id=api.tenant_id,
            application=application,
            requested_kb_ids=list(api.kb_ids),
        )
    assert published_snapshot.version == 2
    assert published_snapshot.version_source == "published"


# ══════════════════════════════════════════════════════════════════════
# 五、分级 tier
# ══════════════════════════════════════════════════════════════════════


async def test_tier_hot_rejected_and_warm_requires_binding(api, monkeypatch):
    _set_governance(monkeypatch, True)
    scenario_id = await committed_scenario(api.tenant_id)

    hot = await _put_governance(api, scenario_id, {"tier": "hot"})
    assert hot.status_code == 400, hot.text
    assert hot.json()["detail"]["code"] == "TIER_HOT_UNSUPPORTED"

    warm_without_binding = await _put_governance(api, scenario_id, {"tier": "warm"})
    assert warm_without_binding.status_code == 400, warm_without_binding.text
    detail = warm_without_binding.json()["detail"]
    assert detail["code"] == "TIER_BINDING_MISMATCH"
    assert detail["context"]["db_binding"] is None

    # ★ 任务书给的 `UPDATE scenarios SET db_binding=...` 会违反 0031 的
    #   `ck_scenarios_tier_binding`（cold ⇔ db_binding IS NULL），
    #   因此必须同一条语句把 tier 一起改成 warm。
    await _exec_committed(
        "UPDATE scenarios SET db_binding = 'kb_finance', tier = 'warm' WHERE id = $1",
        scenario_id,
    )

    accepted = await _put_governance(api, scenario_id, {"tier": "warm"})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["tier"] == "warm"


# ══════════════════════════════════════════════════════════════════════
# 六、effective 预览
# ══════════════════════════════════════════════════════════════════════


async def test_effective_preview_reflects_tenant_cap(api, monkeypatch):
    _set_governance(monkeypatch, True)
    scenario_id = await _mk_scenario(api, "gov_eff")
    version = await _mk_version(api, scenario_id, policy={"recall_top_k": 10})
    await _validate_and_publish(api, scenario_id, version)

    capped = await _put_governance(api, scenario_id, {"recall_top_k_max": 8})
    assert capped.status_code == 200, capped.text
    assert capped.json()["effective"]["recall_top_k"] == {
        "scenario": 10,
        "cap": 8,
        "effective": 8,
        "source": "tenant",
    }

    readback = await api.client.get(f"{SCENARIO_GOVERNANCE_URL}/{scenario_id}")
    assert readback.status_code == 200, readback.text
    body = readback.json()
    assert body["version"] == 1
    assert body["version_source"] == "published"
    assert body["effective"]["recall_top_k"] == {
        "scenario": 10,
        "cap": 8,
        "effective": 8,
        "source": "tenant",
    }

    loosened = await _put_governance(api, scenario_id, {"recall_top_k_max": 20})
    assert loosened.status_code == 200, loosened.text
    assert loosened.json()["effective"]["recall_top_k"] == {
        "scenario": 10,
        "cap": 20,
        "effective": 10,
        "source": "scenario",
    }


# ══════════════════════════════════════════════════════════════════════
# 七、幂等：同值重复 PUT
# ══════════════════════════════════════════════════════════════════════


async def test_repeated_put_is_idempotent_and_audited(api, monkeypatch):
    _set_governance(monkeypatch, True)
    scenario_id = await committed_scenario(api.tenant_id)
    # db_binding 非空 ⇒ tier 必须是 warm/hot（0031 的 CHECK）
    await _exec_committed(
        "UPDATE scenarios SET db_binding = 'kb_finance', tier = 'hot' WHERE id = $1",
        scenario_id,
    )

    first = await _put_governance(api, scenario_id, {"tier": "warm"})
    second = await _put_governance(api, scenario_id, {"tier": "warm"})

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json()["tier"] == "warm"

    updates = await _audit_rows(
        api, "scenario.governance.update", object_id=str(scenario_id)
    )
    assert len(updates) == 2
    # 第二次是同值重放：before 必须等于 after
    assert updates[1]["detail"]["before"] == updates[1]["detail"]["after"]
    assert updates[1]["detail"]["before"]["tier"] == "warm"

    # ★ 与任务书的偏差（已登记）：tier 审计按「仅改变时写一条」实现，
    #   因此第二次同值 PUT 不会产生第二条 tier.change；这里断言真实发生过的那一条。
    tier_changes = await _audit_rows(
        api, "scenario.tier.change", object_id=str(scenario_id)
    )
    assert len(tier_changes) == 1
    assert tier_changes[0]["detail"] == {"before": "hot", "after": "warm"}


# ══════════════════════════════════════════════════════════════════════
# 八、失败请求不写成功审计
# ══════════════════════════════════════════════════════════════════════


async def test_failed_pin_does_not_write_audit(api, monkeypatch):
    _set_governance(monkeypatch, True)
    scenario_id = await _mk_scenario(api, "gov_audit")
    await _mk_version(api, scenario_id)  # v1 仍是 draft

    before = await committed_scalar(
        "SELECT count(*) FROM audit_logs "
        "WHERE tenant_id = $1 AND action = 'scenario.governance.update' "
        "AND object_id = $2",
        api.tenant_id,
        str(scenario_id),
    )
    assert before == 0

    rejected = await _put_governance(api, scenario_id, {"pinned_version": 1})
    assert rejected.status_code == 400, rejected.text
    assert rejected.json()["detail"]["code"] == "PIN_VERSION_NOT_PUBLISHED"

    after = await committed_scalar(
        "SELECT count(*) FROM audit_logs "
        "WHERE tenant_id = $1 AND action = 'scenario.governance.update' "
        "AND object_id = $2",
        api.tenant_id,
        str(scenario_id),
    )
    assert after == 0
