"""S6-2：场景库 M-B 手术、白名单守卫与控制面禁写触发器。

两层结构（与 `tests/scenario/conftest.py` 的既有约定一致）：

  (a) **NO-DB 层（必跑）**：
      ① 搬迁集恰好 10 张且都存在于 SQLAlchemy metadata；
      ② 控制面集与搬迁集互斥，且 kb_members 在排除集内；
      ③ 跨库外键 SQL 走 pg_constraint 且动态取约束名、schema 无关；
      ④ 卫兵 SQL 走 information_schema 且对两个排除集都做了判断；
      ⑤ 触发器 DDL 覆盖 5 张表且函数体含 RAISE；
      ⑥ DSN 渲染不泄露账号口令。
  (b) **Real-DB 层（`SCENARIO_TEST_DB_URL` 缺失即 skip）**：
      临时库 `alembic upgrade head` → 手术前跨库外键 > 0 → 手术后三项 PASS →
      再手术一次验证幂等 → 5 张表的 INSERT/UPDATE/DELETE 全部抛错、SELECT 正常 →
      finally 里终止连接并 DROP DATABASE。

★ 真库用例必须真跑，权限不足时 `pytest.skip` 并写明真实原因，不得伪造通过。
"""
from __future__ import annotations

import asyncio
import uuid
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
from alembic import command

from app.infra.scene_db_schema import (
    CONTROL_PLANE_TABLES,
    CROSS_DB_FK_SQL,
    GUARD_SQL,
    MIGRATION_TABLES,
    TRIGGER_SQL,
    WRITE_BLOCKED_TABLES,
    apply_surgery,
    trigger_ddl,
    verify,
)
from scripts.scene_db_surgery import _asyncpg_dsn, _describe_dsn
from tests.scenario.pg_helpers import (
    SCENARIO_TEST_DB_URL,
    alembic_config,
    dsn,
    requires_db,
)


# ══════════════════════════════════════════════════════════════════════
# A. NO-DB 层
# ══════════════════════════════════════════════════════════════════════


def test_migration_tables_are_exactly_ten_and_exist_in_metadata():
    """搬迁集必须恰好是设计 §4.1 的 10 张，且每张都真实存在于 metadata。"""
    from app.models import entities

    assert MIGRATION_TABLES == frozenset(
        {
            "documents",
            "chunks",
            "kb_terms",
            "graph_entities",
            "graph_entity_mentions",
            "graph_relations",
            "wiki_pages",
            "parse_jobs",
            "parse_segments",
            "governance_jobs",
        }
    )
    assert len(MIGRATION_TABLES) == 10

    missing = MIGRATION_TABLES - set(entities.Base.metadata.tables)
    assert not missing, f"搬迁集里有 metadata 中不存在的表：{sorted(missing)}"


def test_control_plane_is_disjoint_and_covers_kb_members():
    """控制面集与搬迁集互斥；kb_members（有 kb_id 但属控制面）必须在排除集内。"""
    from app.models import entities

    assert CONTROL_PLANE_TABLES.isdisjoint(MIGRATION_TABLES)
    assert "kb_members" in CONTROL_PLANE_TABLES
    # 通配前缀必须已展开成真实表名，而不是保留 `integration_*` 这种字面量
    assert {
        "integration_applications",
        "integration_conversations",
        "user_groups",
        "user_roles",
        "eval_cases",
        "eval_runs",
        "sync_sources",
        "conversations",
        "messages",
    } <= CONTROL_PLANE_TABLES

    unknown = CONTROL_PLANE_TABLES - set(entities.Base.metadata.tables)
    assert not unknown, f"控制面集里有 metadata 中不存在的表：{sorted(unknown)}"


def test_cross_db_fk_sql_is_dynamic_and_schema_scoped():
    """跨库外键 SQL：查 pg_constraint、用 current_schema()、约束名不硬编码。"""
    sql = CROSS_DB_FK_SQL.lower()

    assert "pg_constraint" in sql
    assert "current_schema()" in sql
    assert "$1" in sql and "$2" in sql
    # 硬编码约束名会让新迁移的约束被漏删
    assert "_fkey" not in sql


def test_guard_sql_uses_information_schema_and_both_exclusion_sets():
    """卫兵 SQL：走 information_schema，且搬迁集与控制面集两个排除判断都在。"""
    sql = GUARD_SQL.lower()

    assert "information_schema" in sql
    assert "current_schema()" in sql
    assert "kb_id" in sql and "document_id" in sql and "chunk_id" in sql
    # P3 实测：只查三个 id 列会漏掉「只有 tenant_id 的新表」
    assert "tenant_id" in sql
    assert sql.count("<> all(") == 2


def test_trigger_sql_scopes_function_to_current_schema():
    """校验 SQL 必须把函数限定在本 schema，否则别的 schema 的同名函数会造成假 PASS。"""
    sql = TRIGGER_SQL.lower()

    assert "pg_namespace" in sql
    assert "pronamespace" in sql
    assert sql.count("current_schema()") == 2
    # 被禁用（D/R）的触发器实际不生效，必须视为缺失
    assert "tgenabled" in sql


def test_trigger_ddl_covers_five_tables_and_raises():
    """触发器 DDL：5 张表各一条 STATEMENT 级触发器，函数体含 RAISE 与固定措辞。"""
    ddl = trigger_ddl()

    assert WRITE_BLOCKED_TABLES == (
        "tenants",
        "knowledge_bases",
        "users",
        "scenarios",
        "audit_logs",
    )
    assert ddl.count("CREATE TRIGGER") == 5
    assert ddl.count("DROP TRIGGER IF EXISTS") == 5
    assert ddl.count("FOR EACH STATEMENT") == 5
    assert "RAISE EXCEPTION" in ddl
    assert "恒为空且禁止写入" in ddl
    for table in WRITE_BLOCKED_TABLES:
        assert f' ON "{table}"' in ddl


def test_dsn_description_masks_credentials():
    """脚本只输出 host:port/dbname，账号与口令不得出现。"""
    raw = "postgresql+asyncpg://alice:s3cret@db.internal:6543/kagent_finance"
    described = _describe_dsn(raw)

    assert described == "db.internal:6543/kagent_finance"
    assert "alice" not in described
    assert "s3cret" not in described
    assert _asyncpg_dsn(raw).startswith("postgresql://")


# ══════════════════════════════════════════════════════════════════════
# B. Real-DB 层
# ══════════════════════════════════════════════════════════════════════


def _with_database(url: str, database: str) -> str:
    """替换 DSN 的库名（保留账号、口令、主机与端口）。"""
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{database}"))


async def _run_roundtrip() -> None:
    base_url = SCENARIO_TEST_DB_URL or ""
    temp_name = f"scene_surgery_{uuid.uuid4().hex[:12]}"
    admin_dsn = _with_database(dsn(base_url), "postgres")
    temp_url = _with_database(base_url, temp_name)
    temp_dsn = _with_database(dsn(base_url), temp_name)

    admin = await asyncpg.connect(admin_dsn)
    created = False
    try:
        try:
            await admin.execute(f'CREATE DATABASE "{temp_name}" TEMPLATE template0')
        except asyncpg.exceptions.InsufficientPrivilegeError as exc:
            pytest.skip(f"当前账号无 CREATE DATABASE 权限（{exc}）")
        created = True

        # 全链迁移：alembic 内部自己 asyncio.run，必须放到线程里跑
        await asyncio.to_thread(command.upgrade, alembic_config(temp_url), "head")

        before = await verify(temp_dsn)
        assert before["cross_db_fks"], (
            "手术前没有查到跨库外键：迁移结构变了，或 CONTROL_PLANE_TABLES 漏了被引用表"
        )
        assert before["ok"] is False

        # 目标库身份校验：控制面非空时必须拒绝，且不执行任何 DDL（此时触发器未装，能插入）
        probe_id = uuid.uuid4()
        probe = await asyncpg.connect(temp_dsn)
        try:
            await probe.execute(
                "INSERT INTO tenants (id, name, code) VALUES ($1, $2, $3)",
                probe_id,
                "非空探测",
                f"probe-{probe_id.hex[:8]}",
            )
        finally:
            await probe.close()
        with pytest.raises(RuntimeError, match="这不是空的场景库"):
            await apply_surgery(temp_dsn)
        probe = await asyncpg.connect(temp_dsn)
        try:
            await probe.execute("DELETE FROM tenants WHERE id = $1", probe_id)
        finally:
            await probe.close()

        first = await apply_surgery(temp_dsn)
        assert first["dropped"], "第一次手术应删掉跨库外键"
        assert first["triggers"] == [
            f"trg_scene_db_block_{table}" for table in WRITE_BLOCKED_TABLES
        ]

        after = await verify(temp_dsn)
        assert after["ok"] is True, after
        assert after["cross_db_fks"] == []
        assert after["guard_tables"] == []
        assert after["missing_triggers"] == []

        # 触发器被禁用后 verify 必须 FAIL；重跑手术（重建触发器）后恢复 PASS
        connection = await asyncpg.connect(temp_dsn)
        try:
            await connection.execute('ALTER TABLE "tenants" DISABLE TRIGGER ALL')
        finally:
            await connection.close()
        disabled = await verify(temp_dsn)
        assert disabled["ok"] is False
        assert "tenants" in disabled["missing_triggers"]
        assert (await apply_surgery(temp_dsn))["dropped"] == []
        assert (await verify(temp_dsn))["ok"] is True

        # 幂等：第二次手术 dropped 为空，且校验仍然全 PASS
        second = await apply_surgery(temp_dsn)
        assert second["dropped"] == []
        assert (await verify(temp_dsn))["ok"] is True

        # 触发器生效：INSERT / UPDATE / DELETE 全部抛错；普通 SELECT 不受影响
        connection = await asyncpg.connect(temp_dsn)
        try:
            assert await connection.fetchval("SELECT count(*) FROM tenants") == 0
            with pytest.raises(asyncpg.exceptions.RaiseError):
                await connection.execute(
                    "INSERT INTO tenants (id, name, code) VALUES ($1, $2, $3)",
                    uuid.uuid4(),
                    "禁写探测",
                    f"probe-{uuid.uuid4().hex[:8]}",
                )
            with pytest.raises(asyncpg.exceptions.RaiseError):
                await connection.execute("UPDATE tenants SET name = name")
            with pytest.raises(asyncpg.exceptions.RaiseError):
                await connection.execute("DELETE FROM tenants")
        finally:
            await connection.close()
    finally:
        if created:
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid()",
                temp_name,
            )
            await admin.execute(f'DROP DATABASE IF EXISTS "{temp_name}"')
        await admin.close()


@requires_db
def test_real_db_surgery_roundtrip():
    """临时库上的完整往返：迁移 → 手术 → 校验 → 幂等 → 禁写 → 清理。"""
    asyncio.run(_run_roundtrip())
