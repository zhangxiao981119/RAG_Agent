"""迁移 0031 的往返、列契约、约束牙齿 —— NO-DB 层 + Real-DB 层。

分层理由同 `test_0029_migration.py` / `test_0030_migration.py`：

  · NO-DB（`sql=True` 离线渲染）：证明 `upgrade()` / `downgrade()` 真的被执行，
    渲染出 PG 方言 DDL；抓「语句没生成」「先 SET NOT NULL 后回填」这类顺序错误。
  · Real-DB（`SCENARIO_TEST_DB_URL`）：证明 PG 接受这段 DDL，
    证明 CHECK 真的会拒绝非法 `tier` / 含 NULL 的白名单，证明回填与降级往返。

★ 本迁移**产生约束**，因此约束名必须在文件里可被文本断言；
  不把本文件登记进 `test_0028_migration.py::_PG_CONSTRAINT_SOURCES`：
  那张表只覆盖 0028 自己的 upgrade SQL，0031 的约束不在其中。

★ 真库用例是同步函数（`asyncio.run` 开连接），原因同 0030：
  `pg_helpers.migrate` 经 `alembic/env.py` 用 `asyncio.run` 执行迁移，
  在已运行的事件循环里调用会抛「asyncio.run() cannot be called from a running event loop」。
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import pathlib

import asyncpg
import pytest

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from tests.scenario.pg_helpers import SCENARIO_TEST_DB_URL, alembic_config, dsn, migrate

_API_ROOT = pathlib.Path(__file__).resolve().parents[2]
_ALEMBIC_DIR = _API_ROOT / "alembic"
_MIGRATION_FILE = _ALEMBIC_DIR / "versions" / "0031_step7_tenant_governance.py"

_NEW_COLUMNS = {
    "tenants": ("allowed_db_keys",),
    "scenarios": (
        "pinned_version",
        "recall_top_k_max",
        "threshold_vector_min",
        "threshold_rerank_min",
        "rerank_allowed",
        "tier",
    ),
}


# ══════════════════════════════════════════════════════════════════════
# NO-DB 层：离线渲染 upgrade / downgrade
# ══════════════════════════════════════════════════════════════════════


def _render(direction: str, revision_range: str) -> str:
    """离线渲染迁移 SQL（不建连；URL 只用于解析方言）。"""
    config = Config()
    config.set_main_option("script_location", str(_ALEMBIC_DIR))
    config.set_main_option("sqlalchemy.url", "postgresql+asyncpg://u:p@localhost:5432/db")
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        if direction == "up":
            command.upgrade(config, revision_range, sql=True)
        else:
            command.downgrade(config, revision_range, sql=True)
    return buffer.getvalue()


@pytest.fixture(scope="module")
def upgrade_sql() -> str:
    return _render("up", "0030:0031")


@pytest.fixture(scope="module")
def downgrade_sql() -> str:
    return _render("down", "0031:0030")


def test_upgrade_adds_all_seven_columns(upgrade_sql):
    for column in _NEW_COLUMNS["tenants"]:
        assert f"ALTER TABLE tenants ADD COLUMN {column}" in upgrade_sql
    for column in _NEW_COLUMNS["scenarios"]:
        assert f"ALTER TABLE scenarios ADD COLUMN {column}" in upgrade_sql


def test_tier_backfill_runs_before_not_null(upgrade_sql):
    """顺序不能反：先回填，再 SET NOT NULL，否则空库以外的部署会直接迁移失败。"""
    backfill = upgrade_sql.find("SET tier = CASE")
    not_null = upgrade_sql.find("ALTER COLUMN tier SET NOT NULL")
    default = upgrade_sql.find("ALTER COLUMN tier SET DEFAULT 'cold'")
    assert backfill >= 0 and not_null >= 0 and default >= 0
    assert backfill < not_null < default


def test_tier_backfill_is_guarded_for_scene_databases(upgrade_sql):
    """回填必须包在「表非空才写」的守卫里。

    ★ 场景库（M-B 手术之后）对 `scenarios` 装了 FOR EACH STATEMENT 的写拦截触发器：
      哪怕命中 0 行的 UPDATE 也会被拒。场景库的 `scenarios` 恒为空，
      守卫能让迁移在控制库（要回填）与场景库（不许写）上同时成立。
    """
    guard = upgrade_sql.find("IF EXISTS (SELECT 1 FROM scenarios)")
    update = upgrade_sql.find("UPDATE scenarios")
    assert guard >= 0, "回填缺少 `IF EXISTS (SELECT 1 FROM scenarios)` 守卫"
    assert update >= 0
    assert guard < update, "守卫必须在 UPDATE 之前"
    # 不允许出现守卫之外的裸 UPDATE（那会在场景库上直接失败）
    assert upgrade_sql.count("UPDATE scenarios") == 1


def test_tier_binding_check_is_a_real_predicate(upgrade_sql):
    assert "ck_scenarios_tier_binding" in upgrade_sql
    assert "(tier = 'cold' AND db_binding IS NULL)" in upgrade_sql
    assert "(tier IN ('warm','hot') AND db_binding IS NOT NULL)" in upgrade_sql


def test_nullable_governance_columns_have_no_default(upgrade_sql):
    """治理列必须可 NULL —— 这就是回滚开关：置 NULL 即回到 Step 6 口径。"""
    for column in (
        "allowed_db_keys",
        "pinned_version",
        "recall_top_k_max",
        "threshold_vector_min",
        "threshold_rerank_min",
        "rerank_allowed",
    ):
        assert f"ADD COLUMN {column}" in upgrade_sql
        assert f"ADD COLUMN {column} " not in upgrade_sql or "DEFAULT" not in upgrade_sql.split(
            f"ADD COLUMN {column}", 1
        )[1].split(";", 1)[0]


def test_tenant_array_constraints_reject_bad_shapes(upgrade_sql):
    assert "ck_tenants_allowed_db_keys_dims" in upgrade_sql
    assert "array_ndims(allowed_db_keys) = 1" in upgrade_sql
    assert "ck_tenants_allowed_db_keys_no_null" in upgrade_sql
    assert "array_position(allowed_db_keys, NULL) IS NULL" in upgrade_sql


def test_upgrade_creates_tier_index(upgrade_sql):
    assert "CREATE INDEX idx_scenarios_tenant_tier ON scenarios (tenant_id, tier)" in upgrade_sql


def test_downgrade_drops_in_safe_order(downgrade_sql):
    """先删索引，再删约束，最后删列；被依赖者先走。"""
    index = downgrade_sql.find("DROP INDEX IF EXISTS idx_scenarios_tenant_tier")
    tier_binding = downgrade_sql.find("DROP CONSTRAINT IF EXISTS ck_scenarios_tier_binding")
    tier_column = downgrade_sql.find("DROP COLUMN IF EXISTS tier")
    assert index >= 0 and tier_binding >= 0 and tier_column >= 0
    assert index < tier_binding < tier_column
    for column in (
        "allowed_db_keys",
        "pinned_version",
        "recall_top_k_max",
        "threshold_vector_min",
        "threshold_rerank_min",
        "rerank_allowed",
    ):
        assert f"DROP COLUMN IF EXISTS {column}" in downgrade_sql


def test_downgrade_touches_no_business_rows(downgrade_sql):
    for keyword in ("DELETE FROM", "TRUNCATE", "DROP TABLE"):
        assert keyword not in downgrade_sql


def test_migration_is_linear_single_segment(upgrade_sql):
    assert "-- Running upgrade 0030 -> 0031" in upgrade_sql
    assert upgrade_sql.count("-- Running upgrade") == 1


def test_alembic_heads_is_0031_only():
    script = ScriptDirectory.from_config(alembic_config())
    assert script.get_heads() == ["0031"], f"迁移链出现分叉或多个头：{script.get_heads()}"


def test_migration_file_documents_no_fk_decision():
    source = _MIGRATION_FILE.read_text(encoding="utf-8")
    assert "循环外键" in source
    assert "不建 `scenarios(pinned_version) → scenario_versions` 外键" in source


# ══════════════════════════════════════════════════════════════════════
# Real-DB 层：列存在 / 回填 / 约束牙齿 / 降级往返
# ══════════════════════════════════════════════════════════════════════


def _run(coro):
    return asyncio.run(coro)


async def _columns() -> dict[str, set[str]]:
    connection = await asyncpg.connect(dsn(SCENARIO_TEST_DB_URL))
    try:
        rows = await connection.fetch(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name = ANY($1::text[])",
            list(_NEW_COLUMNS),
        )
    finally:
        await connection.close()
    result: dict[str, set[str]] = {table: set() for table in _NEW_COLUMNS}
    for row in rows:
        result[row["table_name"]].add(row["column_name"])
    return result


def test_real_db_columns_exist(schema):
    migrate("head")
    columns = _run(_columns())
    for table, expected in _NEW_COLUMNS.items():
        missing = set(expected) - columns[table]
        assert not missing, f"{table} 缺少列：{missing}"


def test_real_db_tier_backfill_matches_binding(schema):
    migrate("head")
    count = _run(_mismatched_tier_count())
    assert count == 0, f"回填后仍有 {count} 行 tier 与 db_binding 不一致"


async def _mismatched_tier_count() -> int:
    connection = await asyncpg.connect(dsn(SCENARIO_TEST_DB_URL))
    try:
        value = await connection.fetchval(
            "SELECT count(*) FROM scenarios WHERE tier <> "
            "CASE WHEN db_binding IS NULL THEN 'cold' ELSE 'warm' END"
        )
    finally:
        await connection.close()
    return int(value)


def test_real_db_cold_with_binding_is_rejected(schema):
    migrate("head")
    violation = _run(_expect_check_violation(
        "UPDATE scenarios SET db_binding = 'kb_test01', tier = 'cold' "
        "WHERE id = (SELECT id FROM scenarios ORDER BY id LIMIT 1)"
    ))
    assert violation is not None, "cold + db_binding 未被 ck_scenarios_tier_binding 拒绝"


def test_real_db_warm_and_hot_with_binding_are_allowed(schema):
    migrate("head")
    for tier in ("warm", "hot"):
        result = _run(_try_update(
            "UPDATE scenarios SET db_binding = 'kb_test01', tier = $1 "
            "WHERE id = (SELECT id FROM scenarios ORDER BY id LIMIT 1)",
            tier,
        ))
        assert result is None, f"tier={tier} + db_binding 被错误拒绝：{result}"


def test_real_db_allowed_db_keys_with_null_element_is_rejected(schema):
    migrate("head")
    violation = _run(_expect_check_violation(
        "UPDATE tenants SET allowed_db_keys = ARRAY['default', NULL] "
        "WHERE id = (SELECT id FROM tenants ORDER BY id LIMIT 1)"
    ))
    assert violation is not None, "白名单里的 NULL 元素未被 ck_tenants_allowed_db_keys_no_null 拒绝"


async def _expect_check_violation(sql: str) -> str | None:
    """在事务里执行一条应失败的语句，回滚后返回错误类名；成功则返回 None。"""
    connection = await asyncpg.connect(dsn(SCENARIO_TEST_DB_URL))
    transaction = connection.transaction()
    await transaction.start()
    try:
        try:
            await connection.execute(sql)
        except asyncpg.exceptions.CheckViolationError as exc:
            return type(exc).__name__
        return None
    finally:
        await transaction.rollback()
        await connection.close()


async def _try_update(sql: str, *args) -> str | None:
    connection = await asyncpg.connect(dsn(SCENARIO_TEST_DB_URL))
    transaction = connection.transaction()
    await transaction.start()
    try:
        try:
            await connection.execute(sql, *args)
            return None
        except Exception as exc:  # noqa: BLE001 - 测试要把失败原因原样带回断言
            return f"{type(exc).__name__}: {exc}"
    finally:
        await transaction.rollback()
        await connection.close()


def test_real_db_downgrade_and_upgrade_roundtrip(schema):
    """降级 0030 后新列消失；再升级 head 复原。"""
    migrate("head")
    try:
        migrate("0030", downgrade=True)
        columns = _run(_columns())
        for table, expected in _NEW_COLUMNS.items():
            assert not (set(expected) & columns[table]), f"{table} 降级后仍残留：{expected}"
    finally:
        migrate("head")
    columns = _run(_columns())
    for table, expected in _NEW_COLUMNS.items():
        assert set(expected) <= columns[table], f"{table} 重新升级后缺少列：{expected}"
