"""Step 7 S7-5：租户治理面 HTTP 接口（真库 + ASGI 直连）。

覆盖面（每条对应任务书的一项）：
  1. 非管理员 → 403（GET / PUT 各一条）；
  2. 开关关闭 → 写接口 400 `GOVERNANCE_DISABLED`、详情 `governance_enabled=false`；
  3. 库白名单：非法元素 / 合法值 / 审计 before-after / GET 回读；
  4. 钉定：draft 拒 / published 接受 / 解析按钉定、关闭开关按 published；
  5. 分级：专用搬库登记端点（cold→warm / warm→cold / hot 拒绝 / 一致性 / 格式 /
     未注册 / 租户白名单 / 404）；
  6. effective 预览：租户 cap 收紧与放开后的来源（tenant / scenario）；
  7. 幂等：分级同值重复 PUT 仍 200，审计照写（before == after）；
  8. 通用 PUT 契约收窄：传 `tier` → 422，策略字段仍 200；
  9. 失败请求不写成功审计，DB 行保持原值。

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
    committed_tenant,
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


async def _put_tier(api, scenario_id: uuid.UUID, body: dict):
    """搬库登记端点（tier 与 db_binding 必须同时提交）。"""
    return await api.client.put(
        f"{SCENARIO_GOVERNANCE_URL}/{scenario_id}/tier", json=body
    )


def _register_db_keys(monkeypatch, *keys: str) -> None:
    """把「当前进程已注册库位」替换成本用例需要的集合（测试边界替身）。"""
    from app.services.tenant import governance as governance_mod

    monkeypatch.setattr(governance_mod, "registered_db_keys", lambda: tuple(keys))


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
        "/api/admin/scenario-governance/{scenario_id}/tier": {"put"},
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
# 五、分级 / 搬库登记（PUT .../{id}/tier）
# ══════════════════════════════════════════════════════════════════════


async def _tier_row(scenario_id: uuid.UUID) -> dict:
    """独立连接回读场景的分级两列（不读响应体自证）。"""
    rows = await committed_rows(
        "SELECT tier, db_binding FROM scenarios WHERE id = $1", scenario_id
    )
    assert rows, "场景行不存在"
    return rows[0]


def _capture_invalidate(monkeypatch) -> list[tuple]:
    """猴补丁解析缓存失效，记录 (tenant_id, scenario_id) 调用。"""
    calls: list[tuple] = []

    def _record(tenant_id, scenario_id=None):  # noqa: ANN001
        calls.append((tenant_id, scenario_id))

    monkeypatch.setattr(resolver_mod, "invalidate", _record)
    return calls


async def test_tier_cold_to_warm_updates_row_and_invalidates_cache(api, monkeypatch):
    """cold → warm：响应 / DB 行 / 解析缓存失效三者同时成立。"""
    _set_governance(monkeypatch, True)
    _register_db_keys(monkeypatch, "default", "kb_finance")
    scenario_id = await committed_scenario(api.tenant_id)
    invalidated = _capture_invalidate(monkeypatch)

    assert await _tier_row(scenario_id) == {"tier": "cold", "db_binding": None}

    response = await _put_tier(
        api, scenario_id, {"tier": "warm", "db_binding": "kb_finance"}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tier"] == "warm"
    assert body["db_binding"] == "kb_finance"
    assert await _tier_row(scenario_id) == {
        "tier": "warm",
        "db_binding": "kb_finance",
    }
    # 提交后才失效；参数必须是本场景
    assert invalidated == [(api.tenant_id, scenario_id)]

    # 写后响应必须与 GET 逐字段一致
    readback = await api.client.get(f"{SCENARIO_GOVERNANCE_URL}/{scenario_id}")
    assert readback.status_code == 200, readback.text
    assert readback.json() == body


async def test_tier_warm_to_cold_clears_binding(api, monkeypatch):
    """warm → cold：db_binding 必须置 NULL。"""
    _set_governance(monkeypatch, True)
    scenario_id = await committed_scenario(api.tenant_id)
    await _exec_committed(
        "UPDATE scenarios SET tier = 'warm', db_binding = 'kb_finance' WHERE id = $1",
        scenario_id,
    )

    response = await _put_tier(api, scenario_id, {"tier": "cold", "db_binding": None})

    assert response.status_code == 200, response.text
    assert response.json()["tier"] == "cold"
    assert response.json()["db_binding"] is None
    assert await _tier_row(scenario_id) == {"tier": "cold", "db_binding": None}


@pytest.mark.parametrize(
    "db_binding", ["kb_finance", None], ids=["with-binding", "without-binding"]
)
async def test_tier_hot_is_rejected(api, monkeypatch, db_binding):
    """hot 在一致性检查之前就被拒：binding 有无都必须返回 TIER_HOT_UNSUPPORTED。"""
    _set_governance(monkeypatch, True)
    _register_db_keys(monkeypatch, "default", "kb_finance")
    scenario_id = await committed_scenario(api.tenant_id)

    response = await _put_tier(
        api, scenario_id, {"tier": "hot", "db_binding": db_binding}
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "TIER_HOT_UNSUPPORTED"
    assert await _tier_row(scenario_id) == {"tier": "cold", "db_binding": None}


@pytest.mark.parametrize(
    ("tier", "db_binding"),
    [("cold", "kb_finance"), ("warm", None)],
    ids=["cold-with-binding", "warm-without-binding"],
)
async def test_tier_binding_mismatch_rejected(api, monkeypatch, tier, db_binding):
    _set_governance(monkeypatch, True)
    scenario_id = await committed_scenario(api.tenant_id)

    response = await _put_tier(
        api, scenario_id, {"tier": tier, "db_binding": db_binding}
    )

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "TIER_BINDING_MISMATCH"
    assert detail["context"] == {"tier": tier, "db_binding": db_binding}
    assert await _tier_row(scenario_id) == {"tier": "cold", "db_binding": None}


@pytest.mark.parametrize(
    "db_binding", ["default", "kb_a", "KB_FINANCE", "finance"],
    ids=["reserved-default", "too-short", "uppercase", "no-prefix"],
)
async def test_tier_rejects_invalid_db_key(api, monkeypatch, db_binding):
    """库位键格式非法（含保留虚拟键 `default`）→ DB_SCOPE_INVALID，不碰库。"""
    _set_governance(monkeypatch, True)
    _register_db_keys(monkeypatch, "default", db_binding)
    scenario_id = await committed_scenario(api.tenant_id)

    response = await _put_tier(
        api, scenario_id, {"tier": "warm", "db_binding": db_binding}
    )

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "DB_SCOPE_INVALID"
    assert detail["field"] == "db_binding"
    assert await _tier_row(scenario_id) == {"tier": "cold", "db_binding": None}


async def test_tier_rejects_unregistered_db_key(api, monkeypatch):
    """格式合法但本进程没注册 → DB_BINDING_UNREGISTERED，context 带已注册集合。"""
    _set_governance(monkeypatch, True)
    _register_db_keys(monkeypatch, "default")
    scenario_id = await committed_scenario(api.tenant_id)

    response = await _put_tier(
        api, scenario_id, {"tier": "warm", "db_binding": "kb_finance"}
    )

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "DB_BINDING_UNREGISTERED"
    assert detail["field"] == "db_binding"
    assert detail["context"] == {
        "db_binding": "kb_finance",
        "registered": ["default"],
    }
    assert await _tier_row(scenario_id) == {"tier": "cold", "db_binding": None}


async def test_tier_rejects_db_key_outside_tenant_whitelist(api, monkeypatch):
    """已注册但不在租户白名单 → DB_BINDING_NOT_ALLOWED，context 带白名单。"""
    _set_governance(monkeypatch, True)
    _register_db_keys(monkeypatch, "default", "kb_finance", "kb_hr")
    scenario_id = await committed_scenario(api.tenant_id)

    scope = await api.client.put(DB_SCOPE_URL, json={"allowed_db_keys": ["kb_hr"]})
    assert scope.status_code == 200, scope.text

    response = await _put_tier(
        api, scenario_id, {"tier": "warm", "db_binding": "kb_finance"}
    )

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "DB_BINDING_NOT_ALLOWED"
    assert detail["field"] == "db_binding"
    assert detail["context"] == {
        "allowed": ["kb_hr"],
        "denied": "kb_finance",
    }
    assert await _tier_row(scenario_id) == {"tier": "cold", "db_binding": None}


async def test_tier_accepted_when_whitelist_contains_key(api, monkeypatch):
    """正向对照：键在白名单内就通过（防「一律拒绝」的假实现）。"""
    _set_governance(monkeypatch, True)
    _register_db_keys(monkeypatch, "default", "kb_finance")
    scenario_id = await committed_scenario(api.tenant_id)
    await api.client.put(DB_SCOPE_URL, json={"allowed_db_keys": ["kb_finance"]})

    response = await _put_tier(
        api, scenario_id, {"tier": "warm", "db_binding": "kb_finance"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["db_binding"] == "kb_finance"


@pytest.mark.parametrize("other_tenant", [False, True], ids=["missing", "cross-tenant"])
async def test_tier_scenario_not_found(api, monkeypatch, other_tenant):
    """不存在与跨租户共用 404 `SCENARIO_NOT_FOUND`（状态码差异就是存在性探针）。"""
    _set_governance(monkeypatch, True)
    _register_db_keys(monkeypatch, "default", "kb_finance")
    if other_tenant:
        # 真实的另一个租户（FK 要求 tenant 行存在），用 A 租户身份访问
        scenario_id = await committed_scenario(await committed_tenant())
    else:
        scenario_id = uuid.uuid4()

    response = await _put_tier(
        api, scenario_id, {"tier": "warm", "db_binding": "kb_finance"}
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "SCENARIO_NOT_FOUND"


async def test_tier_rejected_when_governance_disabled(api, monkeypatch):
    _set_governance(monkeypatch, False)
    _register_db_keys(monkeypatch, "default", "kb_finance")
    scenario_id = await committed_scenario(api.tenant_id)
    invalidated = _capture_invalidate(monkeypatch)

    response = await _put_tier(
        api, scenario_id, {"tier": "warm", "db_binding": "kb_finance"}
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "GOVERNANCE_DISABLED"
    assert await _tier_row(scenario_id) == {"tier": "cold", "db_binding": None}
    assert invalidated == []


async def test_tier_repeated_put_is_idempotent_and_audited(api, monkeypatch):
    """同值重复 PUT：两次都 200，且**每条**都留下 scenario.tier.change。"""
    _set_governance(monkeypatch, True)
    _register_db_keys(monkeypatch, "default", "kb_finance")
    scenario_id = await committed_scenario(api.tenant_id)

    first = await _put_tier(
        api, scenario_id, {"tier": "warm", "db_binding": "kb_finance"}
    )
    second = await _put_tier(
        api, scenario_id, {"tier": "warm", "db_binding": "kb_finance"}
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text

    rows = await _audit_rows(api, "scenario.tier.change", object_id=str(scenario_id))
    assert len(rows) == 2
    assert rows[0]["detail"] == {
        "before": {"tier": "cold", "db_binding": None},
        "after": {"tier": "warm", "db_binding": "kb_finance"},
    }
    # 同值重放：before == after（但仍留痕）
    assert rows[1]["detail"]["before"] == rows[1]["detail"]["after"] == {
        "tier": "warm",
        "db_binding": "kb_finance",
    }
    # 本接口不写 governance.update
    assert await _audit_rows(
        api, "scenario.governance.update", object_id=str(scenario_id)
    ) == []


async def test_tier_omitting_db_binding_is_422(api, monkeypatch):
    """`db_binding` 是必填键（可显式 null）：省略（而非 null）→ 422，不碰库。"""
    _set_governance(monkeypatch, True)
    _register_db_keys(monkeypatch, "default", "kb_finance")
    scenario_id = await committed_scenario(api.tenant_id)

    response = await _put_tier(api, scenario_id, {"tier": "warm"})

    assert response.status_code == 422, response.text
    assert "db_binding" in response.text
    assert await _tier_row(scenario_id) == {"tier": "cold", "db_binding": None}
    assert await _audit_rows(
        api, "scenario.tier.change", object_id=str(scenario_id)
    ) == []


async def test_tier_warm_to_warm_changes_binding_only(api, monkeypatch):
    """仅绑定变化（tier 不变）：同一端点覆盖，审计 before/after 只差 db_binding。"""
    _set_governance(monkeypatch, True)
    _register_db_keys(monkeypatch, "default", "kb_finance", "kb_hr")
    scenario_id = await committed_scenario(api.tenant_id)
    await _exec_committed(
        "UPDATE scenarios SET tier = 'warm', db_binding = 'kb_finance' WHERE id = $1",
        scenario_id,
    )

    response = await _put_tier(
        api, scenario_id, {"tier": "warm", "db_binding": "kb_hr"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["tier"] == "warm"
    assert response.json()["db_binding"] == "kb_hr"
    assert await _tier_row(scenario_id) == {"tier": "warm", "db_binding": "kb_hr"}

    rows = await _audit_rows(api, "scenario.tier.change", object_id=str(scenario_id))
    assert len(rows) == 1
    assert rows[0]["detail"] == {
        "before": {"tier": "warm", "db_binding": "kb_finance"},
        "after": {"tier": "warm", "db_binding": "kb_hr"},
    }


async def test_tier_failed_paths_do_not_invalidate_cache(api, monkeypatch):
    """失败路径在提交前返回：不失效 resolver 缓存（缓存与库不会错位）。"""
    _set_governance(monkeypatch, True)
    _register_db_keys(monkeypatch, "default", "kb_finance")
    scenario_id = await committed_scenario(api.tenant_id)
    invalidated = _capture_invalidate(monkeypatch)

    for body in (
        {"tier": "hot", "db_binding": "kb_finance"},
        {"tier": "warm", "db_binding": "default"},
        {"tier": "warm", "db_binding": "kb_hr"},
    ):
        response = await _put_tier(api, scenario_id, body)
        assert response.status_code == 400, (body, response.text)

    assert invalidated == []


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
# 七、通用 PUT 契约收窄：tier 已移出，策略字段照旧
# ══════════════════════════════════════════════════════════════════════


async def test_governance_update_rejects_tier_field(api, monkeypatch):
    """`tier` 已从通用 PUT 契约移除（extra=forbid）→ 422，且不碰库。"""
    _set_governance(monkeypatch, True)
    scenario_id = await committed_scenario(api.tenant_id)

    response = await _put_governance(api, scenario_id, {"tier": "warm"})

    assert response.status_code == 422, response.text
    assert "tier" in response.text
    assert await _tier_row(scenario_id) == {"tier": "cold", "db_binding": None}


async def test_governance_update_still_accepts_policy_fields(api, monkeypatch):
    """策略字段仍走通用 PUT（收窄只针对 tier / db_binding）。"""
    _set_governance(monkeypatch, True)
    scenario_id = await committed_scenario(api.tenant_id)

    response = await _put_governance(api, scenario_id, {"recall_top_k_max": 8})

    assert response.status_code == 200, response.text
    assert response.json()["caps"]["recall_top_k_max"] == 8
    assert await _tier_row(scenario_id) == {"tier": "cold", "db_binding": None}


async def test_tier_failed_paths_leave_row_and_audit_untouched(api, monkeypatch):
    """所有拒绝路径都在提交前返回：DB 行保持原值，且一条 tier.change 都不写。"""
    _set_governance(monkeypatch, True)
    _register_db_keys(monkeypatch, "default", "kb_finance")
    scenario_id = await committed_scenario(api.tenant_id)

    failures = [
        {"tier": "hot", "db_binding": "kb_finance"},
        {"tier": "cold", "db_binding": "kb_finance"},
        {"tier": "warm", "db_binding": None},
        {"tier": "warm", "db_binding": "default"},
        {"tier": "warm", "db_binding": "kb_hr"},
    ]
    for body in failures:
        response = await _put_tier(api, scenario_id, body)
        assert response.status_code == 400, (body, response.text)

    assert await _tier_row(scenario_id) == {"tier": "cold", "db_binding": None}
    assert await _audit_rows(
        api, "scenario.tier.change", object_id=str(scenario_id)
    ) == []


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
