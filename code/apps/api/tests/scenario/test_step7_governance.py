"""Step 7 S7-2：租户治理读取（钉定版本 / 策略 cap / 库白名单）+ 快照接线。

两层结构（同仓库既有做法）：

  · NO-DB 层：开关选择哪条 SQL、行折叠是否进入快照、指纹基线、
    `get_allowed_db_keys` 的三态语义与关闭态零查询。
  · Real-DB 层（`SCENARIO_TEST_DB_URL` 缺失则 skip）：`LOOKUP_GOVERNED_SQL`
    在真库上确实按 `pinned_version` 选中 archived 旧版，而关闭态仍取 published。

★ 关闭态「与 Step 6 逐字段一致」是这一阶段的硬约束（tasks.md S7-2 完成判据），
  所以既断言 SQL 文本（不含钉定列），也断言快照字段与指纹完全相等 —— 只靠其中
  一条都可能漏掉「读了新列但没影响行为」之外的另一半问题。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text

import app.services.scenario.resolver as resolver_mod
from app.config.settings import get_settings
from app.services.scenario import resolve_scenario
from app.services.scenario.dna_declare import build_dna_declared
from app.services.scenario.snapshot import compute_checksum
from app.services.tenant.governance import (
    EMPTY_CAPS,
    TenantNotFoundError,
    TenantPolicyCaps,
    get_allowed_db_keys,
    invalidate_allowed_db_keys,
    validate_db_key,
)
from tests.scenario.conftest import (
    KB_A,
    SCENARIO_ID,
    TENANT_ID,
    FakeSession,
    build_row,
    make_application,
)

pytestmark = pytest.mark.asyncio


# ══════════════════════════════════════════════════════════════════════
# 行工厂：治理 SQL 会多返回 6 个键，FakeSession 由用例脚本直接给整行
# ══════════════════════════════════════════════════════════════════════


def _governed_row(**overrides):
    """非内置、显式绑定 KB_A 的场景行（治理 SQL 的完整列形态）。"""
    row = build_row(
        tenant_id=TENANT_ID,
        scenario_id=SCENARIO_ID,
        is_builtin=False,
        version=2,
        kb_ids=(str(KB_A),),
        binding_mode="explicit",
    )
    row.update(
        {
            "pinned_version": None,
            "recall_top_k_max": None,
            "threshold_vector_min": None,
            "threshold_rerank_min": None,
            "rerank_allowed": None,
            "version_state": "published",
        }
    )
    row.update(overrides)
    return row


def _app():
    return make_application(scenario_id=SCENARIO_ID)


def _set_switch(monkeypatch, enabled: bool) -> None:
    """运行期切换治理开关（settings 是进程单例，monkeypatch 自动还原）。"""
    monkeypatch.setattr(get_settings(), "tenant_governance_enabled", enabled)


# ══════════════════════════════════════════════════════════════════════
# 一、开关：关闭走原 SQL、开启走治理 SQL
# ══════════════════════════════════════════════════════════════════════


async def test_switch_off_uses_plain_sql_and_returns_published(monkeypatch):
    """关闭态：即使行里带着 pinned_version，也不读、不生效。"""
    _set_switch(monkeypatch, False)
    session = FakeSession([_governed_row(version=2, pinned_version=1)])

    snapshot = await resolve_scenario(
        session, tenant_id=TENANT_ID, application=_app(), requested_kb_ids=[KB_A]
    )

    assert snapshot.version == 2
    assert snapshot.version_source == "published"
    assert snapshot.caps == EMPTY_CAPS
    sql = session.statements[0].text
    assert "pinned_version" not in sql, "关闭态不得读钉定列"
    assert "v.state = 'published'" in sql


async def test_switch_on_selects_governed_sql_with_pin_branch(monkeypatch):
    """开启态：SQL 必须同时表达「未钉定取 published」与「钉定取 published/archived」。"""
    _set_switch(monkeypatch, True)
    session = FakeSession(
        [_governed_row(version=1, pinned_version=1, version_state="archived")]
    )

    snapshot = await resolve_scenario(
        session, tenant_id=TENANT_ID, application=_app(), requested_kb_ids=[KB_A]
    )

    assert snapshot.version == 1
    assert snapshot.version_source == "pinned"
    sql = session.statements[0].text
    assert "s.pinned_version IS NULL AND v.state = 'published'" in sql
    assert "v.version = s.pinned_version" in sql
    assert "v.state IN ('published','archived')" in sql
    # 跨租户防线不因开启治理而放宽
    assert "s.tenant_id = :tenant_id" in sql
    assert "v.tenant_id   = :tenant_id" in sql


async def test_switch_on_without_pin_equals_switch_off(monkeypatch):
    """开启但无钉定：快照与关闭态逐字段相等（含指纹）。"""
    _set_switch(monkeypatch, False)
    off = await resolve_scenario(
        FakeSession([_governed_row()]),
        tenant_id=TENANT_ID,
        application=_app(),
        requested_kb_ids=[KB_A],
    )
    resolver_mod.clear_cache()

    _set_switch(monkeypatch, True)
    on = await resolve_scenario(
        FakeSession([_governed_row()]),
        tenant_id=TENANT_ID,
        application=_app(),
        requested_kb_ids=[KB_A],
    )

    for attr in (
        "scenario_id", "scenario_key", "version", "checksum", "is_builtin",
        "binding_mode", "binding_kb_ids", "kb_ids", "db_binding",
        "caps", "version_source", "source",
    ):
        assert getattr(off, attr) == getattr(on, attr), f"{attr} 在开关之间不一致"
    assert off.policy.declared() == on.policy.declared()
    assert off.fingerprint == on.fingerprint


# ══════════════════════════════════════════════════════════════════════
# 二、caps 进入快照与指纹
# ══════════════════════════════════════════════════════════════════════


async def test_caps_are_carried_into_snapshot(monkeypatch):
    _set_switch(monkeypatch, True)
    snapshot = await resolve_scenario(
        FakeSession(
            [
                _governed_row(
                    recall_top_k_max=8,
                    threshold_vector_min=0.55,
                    threshold_rerank_min=0.6,
                    rerank_allowed=False,
                )
            ]
        ),
        tenant_id=TENANT_ID,
        application=_app(),
        requested_kb_ids=[KB_A],
    )

    assert snapshot.caps == TenantPolicyCaps(
        recall_top_k_max=8,
        threshold_vector_min=0.55,
        threshold_rerank_min=0.6,
        rerank_allowed=False,
    )
    assert snapshot.caps.is_empty() is False
    assert snapshot.caps.declared() == {
        "recall_top_k_max": 8,
        "threshold_vector_min": 0.55,
        "threshold_rerank_min": 0.6,
        "rerank_allowed": False,
    }


async def test_caps_change_fingerprint(monkeypatch):
    """caps 是口径的一部分：收紧值不同 ⇒ 指纹不同（审计要能区分）。"""
    _set_switch(monkeypatch, True)
    plain = await resolve_scenario(
        FakeSession([_governed_row()]),
        tenant_id=TENANT_ID,
        application=_app(),
        requested_kb_ids=[KB_A],
    )
    resolver_mod.clear_cache()
    capped = await resolve_scenario(
        FakeSession([_governed_row(recall_top_k_max=8)]),
        tenant_id=TENANT_ID,
        application=_app(),
        requested_kb_ids=[KB_A],
    )

    assert plain.caps == EMPTY_CAPS
    assert len(plain.fingerprint) == 12
    assert plain.fingerprint != capped.fingerprint


async def test_empty_caps_fingerprint_matches_step6_formula(monkeypatch):
    """空 caps 时指纹 = Step 6 的公式（`{}` 是空 policy 的规范形态）。

    ★ 用公式复算而不是钉死一个字面量：`index.fingerprint` 含 env 绑定字段
      （embedding_model/dim 等），字面量会随 `.env` 与部署环境漂移，
      而这里要证明的是「没有追加 caps 段」，与具体环境无关。
    """
    _set_switch(monkeypatch, True)
    snapshot = await resolve_scenario(
        FakeSession([_governed_row()]),
        tenant_id=TENANT_ID,
        application=_app(),
        requested_kb_ids=[KB_A],
    )

    raw = "|".join(
        (snapshot.scenario_key, str(snapshot.version), snapshot.index.fingerprint, "{}")
    )
    assert snapshot.fingerprint == hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


# ══════════════════════════════════════════════════════════════════════
# 三、库白名单读取
# ══════════════════════════════════════════════════════════════════════


async def test_allowed_db_keys_switch_off_issues_zero_query(monkeypatch):
    """关闭态直接返回 None 且**不发出任何 SQL**（零新增查询是硬约束）。"""
    _set_switch(monkeypatch, False)
    session = FakeSession([{"allowed_db_keys": ["kb_finance"]}])

    assert await get_allowed_db_keys(session, TENANT_ID) is None
    assert session.statements == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),  # NULL = 不限制
        ([], frozenset()),  # 空数组 = 全部拒绝
        (["kb_finance", "default"], frozenset({"kb_finance", "default"})),
    ],
)
async def test_allowed_db_keys_three_states(monkeypatch, raw, expected):
    _set_switch(monkeypatch, True)
    session = FakeSession([{"allowed_db_keys": raw}])

    assert await get_allowed_db_keys(session, TENANT_ID) == expected
    assert len(session.statements) == 1


async def test_allowed_db_keys_cached(monkeypatch):
    """同一租户第二次读取不再打 DB（TTL 60s 内）。"""
    _set_switch(monkeypatch, True)
    session = FakeSession([{"allowed_db_keys": ["kb_finance"]}])

    first = await get_allowed_db_keys(session, TENANT_ID)
    second = await get_allowed_db_keys(session, TENANT_ID)

    assert first == second == frozenset({"kb_finance"})
    assert len(session.statements) == 1


async def test_allowed_db_keys_invalidate_forces_reload(monkeypatch):
    _set_switch(monkeypatch, True)
    session = FakeSession([{"allowed_db_keys": ["kb_finance"]}])
    await get_allowed_db_keys(session, TENANT_ID)
    invalidate_allowed_db_keys(TENANT_ID)
    await get_allowed_db_keys(session, TENANT_ID)
    assert len(session.statements) == 2


async def test_allowed_db_keys_missing_tenant_fails_closed(monkeypatch):
    """tenant 行缺失是数据损坏：抛错，绝不回落成「不限制」。"""
    _set_switch(monkeypatch, True)
    session = FakeSession([])

    with pytest.raises(TenantNotFoundError):
        await get_allowed_db_keys(session, TENANT_ID)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("default", True),
        ("kb_finance", True),
        ("kb_ab", True),
        ("kb_a", False),  # 少一位
        ("kb_Finance", False),  # 大写
        ("kb_finance-x", False),  # 连字符
        ("finance", False),  # 缺前缀
        ("", False),
    ],
)
async def test_validate_db_key(value, expected):
    assert validate_db_key(value) is expected


# ══════════════════════════════════════════════════════════════════════
# 四、真库：钉定确实改变「解析到哪个版本」
# ══════════════════════════════════════════════════════════════════════


@pytest_asyncio.fixture()
async def db_session(schema):
    """一条绑定真库的 AsyncSession（NullPool：用例只关心语义，不关心连接复用）。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from tests.scenario.pg_helpers import SCENARIO_TEST_DB_URL

    engine = create_async_engine(SCENARIO_TEST_DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def test_governed_sql_picks_pinned_archived_version_in_real_db(
    db_session, monkeypatch
):
    """真库语义：v1 archived + v2 published + pin=1 ⇒ 开启态取 v1，关闭态取 v2。

    ★ 为什么必须真库：FakeSession 不会执行 SQL，钉定分支的正确性只能由 PG 证明。
    """
    tenant_id = uuid.uuid4()
    scenario_id = uuid.uuid4()
    kb = [str(KB_A)]
    dna = build_dna_declared()
    checksum = compute_checksum(
        policy={}, kb_ids=kb, binding_mode="explicit", dna_declared=dna
    )

    async def _insert_version(version: int, state: str) -> None:
        # archived_at 由 Python 给值（不在 SQL 里用参数做 CASE：未定型的绑定参数
        # 与文本字面量比较时 PG 会报 could not determine data type）。
        archived_at = datetime.now(timezone.utc) if state == "archived" else None
        await db_session.execute(
            text(
                "INSERT INTO scenario_versions "
                "(tenant_id, scenario_id, version, state, policy, kb_ids, "
                " binding_mode, dna_declared, checksum, published_at, archived_at) "
                "VALUES (:tenant, :scenario, :version, :state, '{}'::jsonb, "
                "        CAST(:kb AS jsonb), 'explicit', CAST(:dna AS jsonb), :checksum, "
                "        now(), :archived_at)"
            ),
            {
                "tenant": tenant_id,
                "scenario": scenario_id,
                "version": version,
                "state": state,
                "kb": json.dumps(kb),
                "dna": json.dumps(dna),
                "checksum": checksum,
                "archived_at": archived_at,
            },
        )

    try:
        await db_session.execute(
            text(
                "INSERT INTO tenants (id, name, code) VALUES (:id, '治理测试租户', :code)"
            ),
            {"id": tenant_id, "code": f"gov-{tenant_id}"},
        )
        await db_session.execute(
            text(
                "INSERT INTO scenarios (id, tenant_id, scenario_key, name, is_builtin) "
                "VALUES (:id, :tenant, :key, '治理测试场景', false)"
            ),
            {"id": scenario_id, "tenant": tenant_id, "key": f"gov_{str(scenario_id)[:8]}"},
        )
        await _insert_version(1, "archived")
        await _insert_version(2, "published")
        await db_session.execute(
            text("UPDATE scenarios SET pinned_version = 1 WHERE id = :id"),
            {"id": scenario_id},
        )
        await db_session.commit()

        _set_switch(monkeypatch, False)
        published = await resolver_mod._load_published(
            db_session, tenant_id=tenant_id, scenario_id=scenario_id
        )
        assert published is not None
        assert published.version == 2
        assert published.version_state == "published"

        _set_switch(monkeypatch, True)
        pinned = await resolver_mod._load_published(
            db_session, tenant_id=tenant_id, scenario_id=scenario_id
        )
        assert pinned is not None
        assert pinned.version == 1
        assert pinned.version_state == "archived"
        assert pinned.pinned_version == 1
    finally:
        await db_session.rollback()
        await db_session.execute(
            text("DELETE FROM scenario_versions WHERE tenant_id = :tenant"),
            {"tenant": tenant_id},
        )
        await db_session.execute(
            text("DELETE FROM scenarios WHERE tenant_id = :tenant"),
            {"tenant": tenant_id},
        )
        await db_session.execute(
            text("DELETE FROM tenants WHERE id = :tenant"), {"tenant": tenant_id}
        )
        await db_session.commit()
