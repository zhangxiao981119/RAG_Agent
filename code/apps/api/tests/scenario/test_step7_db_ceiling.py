"""Step 7 S7-3：库可见性上限（`tenants.allowed_db_keys`）语义 —— NO-DB 层。

覆盖四件事：

  1. 关闭态：不查 `tenants`、不拒绝（与 Step 6 等价）；
  2. 三态：`NULL` 不限 / `{}` 全禁 / 数组白名单（含 `default` 也受约束）；
  3. 拒绝点是 `resolve_kb_db_key`（解析出库位之后），错误码 `DB_SCOPE_NOT_ALLOWED`；
  4. 映射 SQL 在治理开启时是 pin-aware（版本与库位同一口径）。

真库侧的"钉定改变映射"见 `test_step7_pin_pg.py`。
"""
from __future__ import annotations

import pytest

import app.services.db_scope as db_scope
from app.config.settings import get_settings
from app.services.db_scope import (
    DB_SCOPE_MULTI_KEY,
    DB_SCOPE_NOT_ALLOWED,
    DbScopeError,
    ensure_db_keys_allowed,
    map_kb_db_keys,
    resolve_kb_db_key,
)
from tests.scenario.conftest import KB_A, KB_B, TENANT_ID, FakeSession

pytestmark = pytest.mark.asyncio


def _set_switch(monkeypatch, *, governance: bool, split: bool = True) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "tenant_governance_enabled", governance)
    monkeypatch.setattr(settings, "scene_db_split_enabled", split)


async def _empty_map(session, tenant_id, kb_ids):  # noqa: ANN001
    """把映射固定为「全部未命中」（等价于所有 kb 都在默认库）。"""
    return {kb_id: db_scope.DEFAULT_DB_KEY for kb_id in kb_ids}


# ══════════════════════════════════════════════════════════════════════
# 一、ensure_db_keys_allowed：开关与三态
# ══════════════════════════════════════════════════════════════════════


async def test_switch_off_allows_without_any_query(monkeypatch):
    _set_switch(monkeypatch, governance=False)
    session = FakeSession([{"allowed_db_keys": []}])

    await ensure_db_keys_allowed(session, TENANT_ID, ["kb_finance", "default"])

    assert session.statements == [], "关闭态不得为白名单多查一次 tenants"


async def test_null_allowlist_allows_everything(monkeypatch):
    _set_switch(monkeypatch, governance=True)
    session = FakeSession([{"allowed_db_keys": None}])

    await ensure_db_keys_allowed(session, TENANT_ID, ["kb_finance", "default"])

    assert len(session.statements) == 1


async def test_empty_allowlist_denies_everything_or_400(monkeypatch):
    """`{}` 是"全部拒绝"，连 default 也拒绝（不是"未配置"）。"""
    _set_switch(monkeypatch, governance=True)
    session = FakeSession([{"allowed_db_keys": []}])

    with pytest.raises(DbScopeError) as excinfo:
        await ensure_db_keys_allowed(session, TENANT_ID, ["default"])

    assert excinfo.value.code == DB_SCOPE_NOT_ALLOWED
    assert excinfo.value.detail == {"tenant_id": TENANT_ID, "denied": ["default"]}


async def test_allowlist_denies_only_outside_keys_sorted(monkeypatch):
    _set_switch(monkeypatch, governance=True)
    session = FakeSession([{"allowed_db_keys": ["kb_finance"]}])

    with pytest.raises(DbScopeError) as excinfo:
        await ensure_db_keys_allowed(
            session, TENANT_ID, ["kb_other", "kb_finance", "kb_alpha"]
        )

    # 只报被拒的键，且稳定排序（日志可断言、不随集合顺序抖动）
    assert excinfo.value.detail == {
        "tenant_id": TENANT_ID,
        "denied": ["kb_alpha", "kb_other"],
    }


async def test_allowlist_pass_when_all_keys_allowed(monkeypatch):
    _set_switch(monkeypatch, governance=True)
    session = FakeSession([{"allowed_db_keys": ["kb_finance", "default"]}])

    await ensure_db_keys_allowed(session, TENANT_ID, ["kb_finance"])


# ══════════════════════════════════════════════════════════════════════
# 二、resolve_kb_db_key 的拒绝点与错误码
# ══════════════════════════════════════════════════════════════════════


async def test_resolve_checks_default_when_no_kb_mapped(monkeypatch):
    """没有任何 kb 命中绑定库位时，解析结果是 default —— 它同样要过白名单。"""
    _set_switch(monkeypatch, governance=True)
    monkeypatch.setattr(db_scope, "map_kb_db_keys", _empty_map)
    session = FakeSession([{"allowed_db_keys": []}])

    with pytest.raises(DbScopeError) as excinfo:
        await resolve_kb_db_key(session, TENANT_ID, [KB_A])

    assert excinfo.value.code == DB_SCOPE_NOT_ALLOWED
    assert excinfo.value.detail["denied"] == ["default"]


async def test_resolve_prefers_whitelist_error_over_multi(monkeypatch):
    """同时越界又跨库时，白名单拒绝优先（更根本，且不给调用方探边界的机会）。"""
    _set_switch(monkeypatch, governance=True)

    async def _two_keys(session, tenant_id, kb_ids):  # noqa: ANN001
        return {KB_A: "kb_alpha", KB_B: "kb_beta"}

    monkeypatch.setattr(db_scope, "map_kb_db_keys", _two_keys)
    session = FakeSession([{"allowed_db_keys": ["default"]}])

    with pytest.raises(DbScopeError) as excinfo:
        await resolve_kb_db_key(session, TENANT_ID, [KB_A, KB_B])

    assert excinfo.value.code == DB_SCOPE_NOT_ALLOWED
    assert excinfo.value.detail["denied"] == ["kb_alpha", "kb_beta"]


async def test_resolve_still_reports_multi_when_allowed(monkeypatch):
    _set_switch(monkeypatch, governance=True)

    async def _two_keys(session, tenant_id, kb_ids):  # noqa: ANN001
        return {KB_A: "kb_alpha", KB_B: "kb_beta"}

    monkeypatch.setattr(db_scope, "map_kb_db_keys", _two_keys)
    session = FakeSession([{"allowed_db_keys": ["kb_alpha", "kb_beta"]}])

    with pytest.raises(DbScopeError) as excinfo:
        await resolve_kb_db_key(session, TENANT_ID, [KB_A, KB_B])

    assert excinfo.value.code == DB_SCOPE_MULTI_KEY


async def test_resolve_switch_off_returns_default_with_zero_query(monkeypatch):
    _set_switch(monkeypatch, governance=False, split=False)
    session = FakeSession([{"allowed_db_keys": []}])

    assert await resolve_kb_db_key(session, TENANT_ID, [KB_A]) == "default"
    assert session.statements == []


# ══════════════════════════════════════════════════════════════════════
# 三、映射 SQL 的 pin-aware 选择
# ══════════════════════════════════════════════════════════════════════


async def test_map_sql_governed_is_pin_aware():
    plain = db_scope._MAP_SQL.text
    governed = db_scope._MAP_SQL_GOVERNED.text

    assert "pinned_version" not in plain, "关闭态 SQL 不得读钉定列"
    assert "s.pinned_version IS NULL AND v.state = 'published'" in governed
    assert "v.version = s.pinned_version" in governed
    assert "v.state IN ('published','archived')" in governed
    # 跨租户防线两种口径都必须在
    assert "v.tenant_id   = s.tenant_id" in governed
    assert "s.tenant_id = :tenant_id" in governed
    assert "s.db_binding IS NOT NULL" in governed


async def test_map_uses_governed_sql_when_switch_on(monkeypatch):
    _set_switch(monkeypatch, governance=True)
    session = FakeSession([])

    await map_kb_db_keys(session, TENANT_ID, [KB_A])

    assert len(session.statements) == 1
    assert "pinned_version" in session.statements[0].text


async def test_map_uses_plain_sql_when_switch_off(monkeypatch):
    _set_switch(monkeypatch, governance=False)
    session = FakeSession([])

    await map_kb_db_keys(session, TENANT_ID, [KB_A])

    assert len(session.statements) == 1
    assert "pinned_version" not in session.statements[0].text
