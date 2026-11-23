"""场景库 schema 手术（M-B）的单一来源：表集、SQL 与异步执行入口。

M-B 的规则（见 `02-design/第六阶段-Step6-前置裁决.md` §4.1/§4.2）：
    1. 场景库跑全链迁移（表结构与控制库一致），控制面表物理存在但恒为空；
    2. 删除「搬迁集 → 控制面集」的跨库外键（跨 database 的 CASCADE 永远不触发，
       保留外键只会制造完整性成立的假象）；
    3. 白名单守卫防未来迁移漏表；
    4. 对 5 张控制面表装 STATEMENT 级禁写触发器，把「控制面元数据不随场景分裂」
       从文档承诺变成数据库强制。

前置条件：场景库必须先 `alembic upgrade head`；**每次升级后都要重跑本模块的
`apply_surgery`（脚本 `scripts/scene_db_surgery.py`），再执行 `verify`** ——
新迁移完全可能把跨库外键加回来。

★ 本模块是 SQL 与表集的唯一来源：脚本与测试都从这里导入，避免三份口径分叉。
★ 所有 SQL 都用 `current_schema()` 限定，测试库在非 public schema 下同样可用。
★ 任何异常原样抛出，不吞异常、不降级。
"""
from __future__ import annotations

from typing import Any

import asyncpg

MIGRATION_TABLES: frozenset[str] = frozenset(
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
"""搬迁到场景库的数据面表集（§4.1，恰好 10 张）。

★ 必须显式列出，不得用「含 kb_id 的表」动态推导：推导与守卫同源会变成同义反复，
  守卫就再也发现不了「新迁移加了一张数据面表但没搬」。
"""

CONTROL_PLANE_TABLES: frozenset[str] = frozenset(
    {
        # 租户与权限
        "tenants",
        "users",
        "departments",
        "groups",
        "user_groups",
        "roles",
        "user_roles",
        "knowledge_bases",
        "kb_members",
        # 场景配置
        "scenarios",
        "scenario_versions",
        # 集成与会话
        "integration_applications",
        "integration_conversations",
        "conversations",
        "messages",
        # 审计 / 同步 / 评估
        "audit_logs",
        "sync_sources",
        "eval_cases",
        "eval_runs",
        "finetune_samples",
        # 平台级配置
        "tenant_quotas",
        "sensitive_words",
        "feature_flags",
        "prompt_versions",
    }
)
"""留在控制库的表集（§4.1「不搬」全表 + 通配前缀展开）。

★ `kb_members` 有 kb_id 列但属控制面：不列入排除集，白名单守卫会把它当成漏表。
★ `integration_*` / `user_*` / `eval_*` 已按 `app/models/entities.py` 展开成真实表名。
★ `finetune_samples` 文档清单未列，但它是 tenant 级控制面数据（无 kb_id），
  必须与其余表一样排除，否则「搬迁集 + 控制面集 = 全部表」这个口径不成立。
"""

WRITE_BLOCKED_TABLES: tuple[str, ...] = (
    "tenants",
    "knowledge_bases",
    "users",
    "scenarios",
    "audit_logs",
)
"""装禁写触发器的 5 张控制面表（§4.2 原文）。

★ 只做设计原文的 5 张，不擅自扩大：其余控制面表的写入拦截属设计外，
  作为残余风险上报，而不是在这里偷偷加表。
"""

BLOCK_FUNCTION_NAME = "fn_scene_db_block_control_plane_write"
"""拒绝函数的名称（触发器共用）。"""

CROSS_DB_FK_SQL = """
SELECT c.conname AS constraint_name,
       src.relname AS source_table,
       dst.relname AS target_table
FROM pg_constraint c
JOIN pg_class src ON src.oid = c.conrelid
JOIN pg_class dst ON dst.oid = c.confrelid
JOIN pg_namespace n ON n.oid = src.relnamespace
WHERE c.contype = 'f'
  AND n.nspname = current_schema()
  AND src.relname = ANY($1::text[])
  AND dst.relname = ANY($2::text[])
ORDER BY src.relname, c.conname
"""
"""跨库外键查询：搬迁集 → 控制面集的外键（约束名由迁移生成，动态查、不硬编码）。

★ 搬迁集内部的外键（如 chunks → documents）不在结果里，手术不会动它们。
"""

GUARD_SQL = """
SELECT c.table_name AS table_name,
       array_agg(c.column_name ORDER BY c.column_name) AS columns
FROM information_schema.columns c
JOIN information_schema.tables t
  ON t.table_schema = c.table_schema AND t.table_name = c.table_name
WHERE c.table_schema = current_schema()
  AND t.table_type = 'BASE TABLE'
  AND c.column_name IN ('kb_id', 'document_id', 'chunk_id', 'tenant_id')
  AND c.table_name <> ALL($1::text[])
  AND c.table_name <> ALL($2::text[])
GROUP BY c.table_name
ORDER BY c.table_name
"""
"""白名单守卫：含 kb_id/document_id/chunk_id/tenant_id 却不在两个已知表集里的表。

两个排除集分别由 $1（搬迁集）与 $2（控制面集）传入；返回非空即 FAIL
（说明新迁移加了数据面表却没登记，未来会漏搬）。

★ P3 实测补强：只查 kb_id/document_id/chunk_id 会漏掉「只有 tenant_id 的新表」，
  故把 tenant_id 一并纳入列集合。metadata 现有表全部落在两个表集内，不会误报。
"""

TRIGGER_SQL = """
SELECT cl.relname AS table_name, t.tgname AS trigger_name
FROM pg_trigger t
JOIN pg_class cl ON cl.oid = t.tgrelid
JOIN pg_namespace n ON n.oid = cl.relnamespace
JOIN pg_proc p ON p.oid = t.tgfoid
JOIN pg_namespace fn ON fn.oid = p.pronamespace
WHERE n.nspname = current_schema()
  AND fn.nspname = current_schema()
  AND NOT t.tgisinternal
  AND t.tgenabled IN ('O', 'A')
  AND p.proname = $1
  AND cl.relname = ANY($2::text[])
ORDER BY cl.relname, t.tgname
"""
"""已安装且启用的禁写触发器（按表名与函数名查，不假设触发器名）。

★ `tgenabled` 只认 'O'（启用）与 'A'（ALWAYS）；'D'/'R'（DISABLE / REPLICA）
  意味着触发器实际不生效，必须视为缺失 —— 否则 `ALTER TABLE ... DISABLE TRIGGER`
  之后 verify 仍会报 PASS（P3 实测发现）。
"""


def trigger_name(table: str) -> str:
    """控制面表对应的触发器名（确定性命名，便于幂等 DROP）。"""
    return f"trg_scene_db_block_{table}"


def trigger_ddl() -> str:
    """返回幂等的触发器 DDL：一个拒绝函数 + 5 张表各一条 STATEMENT 级触发器。

    ★ 用 `FOR EACH STATEMENT`：即使 UPDATE/DELETE 命中 0 行也照样拒绝，
      不会出现「空表上删一行没反应」的漏网。
    ★ `DROP TRIGGER IF EXISTS` 保证重复执行不报错。
    """
    statements: list[str] = [
        f"""
CREATE OR REPLACE FUNCTION "{BLOCK_FUNCTION_NAME}"() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION '控制面表 % 在场景库恒为空且禁止写入（M-B 跨库外键手术）', TG_TABLE_NAME;
END;
$fn$;
"""
    ]
    for table in WRITE_BLOCKED_TABLES:
        statements.append(
            f'DROP TRIGGER IF EXISTS {trigger_name(table)} ON "{table}";\n'
            f"CREATE TRIGGER {trigger_name(table)}\n"
            f'    BEFORE INSERT OR UPDATE OR DELETE ON "{table}"\n'
            f'    FOR EACH STATEMENT EXECUTE FUNCTION "{BLOCK_FUNCTION_NAME}"();\n'
        )
    return "\n".join(statements)


async def _fetch_cross_db_fks(connection: asyncpg.Connection) -> list[dict[str, Any]]:
    rows = await connection.fetch(
        CROSS_DB_FK_SQL,
        sorted(MIGRATION_TABLES),
        sorted(CONTROL_PLANE_TABLES),
    )
    return [dict(row) for row in rows]


async def _fetch_guard_tables(connection: asyncpg.Connection) -> list[dict[str, Any]]:
    rows = await connection.fetch(
        GUARD_SQL,
        sorted(MIGRATION_TABLES),
        sorted(CONTROL_PLANE_TABLES),
    )
    return [dict(row) for row in rows]


async def _fetch_blocking_triggers(
    connection: asyncpg.Connection,
) -> dict[str, list[str]]:
    rows = await connection.fetch(
        TRIGGER_SQL,
        BLOCK_FUNCTION_NAME,
        list(WRITE_BLOCKED_TABLES),
    )
    found: dict[str, list[str]] = {}
    for row in rows:
        found.setdefault(row["table_name"], []).append(row["trigger_name"])
    return found


def _control_plane_count_sql() -> str:
    """控制面表行数统计（UNION ALL，一次往返；表名来自常量，安全标识符）。"""
    selects = [
        f'SELECT \'{table}\' AS table_name, count(*) AS row_count FROM "{table}"'
        for table in sorted(CONTROL_PLANE_TABLES)
    ]
    return "\nUNION ALL\n".join(selects)


async def _fetch_non_empty_control_plane(
    connection: asyncpg.Connection,
) -> list[dict[str, Any]]:
    rows = await connection.fetch(_control_plane_count_sql())
    return [dict(row) for row in rows if row["row_count"] > 0]


async def apply_surgery(dsn: str) -> dict[str, Any]:
    """在单个事务里执行手术：删跨库外键 → 装禁写触发器。

    返回 `{"dropped": [...], "triggers": [...]}`。重复执行幂等：第二次 `dropped`
    为空且不报错。

    ★ 外键按查询结果动态删除，不硬编码约束名（约束名由 SQLAlchemy/alembic 生成）。
    ★ 手术前先做目标库身份校验：控制面表任一非空即拒绝（fail-closed），
      避免 `--dsn` 误指向控制库时删掉控制库外键、装上禁写触发器。
    """
    connection = await asyncpg.connect(dsn)
    try:
        async with connection.transaction():
            non_empty = await _fetch_non_empty_control_plane(connection)
            if non_empty:
                detail = "、".join(
                    f"{row['table_name']}({row['row_count']}行)" for row in non_empty
                )
                raise RuntimeError(
                    f"这不是空的场景库，手术已中止：控制面表非空 -> {detail}。"
                    "请确认 --dsn 指向的是新建的场景库，而不是控制库。"
                )
            fks = await _fetch_cross_db_fks(connection)
            dropped: list[dict[str, str]] = []
            for row in fks:
                await connection.execute(
                    f'ALTER TABLE "{row["source_table"]}" '
                    f'DROP CONSTRAINT IF EXISTS "{row["constraint_name"]}"'
                )
                dropped.append(
                    {
                        "constraint": row["constraint_name"],
                        "table": row["source_table"],
                        "references": row["target_table"],
                    }
                )
            await connection.execute(trigger_ddl())
        return {
            "dropped": dropped,
            "triggers": [trigger_name(table) for table in WRITE_BLOCKED_TABLES],
        }
    finally:
        await connection.close()


async def verify(dsn: str) -> dict[str, Any]:
    """三项检查：跨库外键行数、白名单守卫行数、5 个触发器是否齐全。

    `ok` 为真当且仅当三项全部通过。
    """
    connection = await asyncpg.connect(dsn)
    try:
        cross_db_fks = await _fetch_cross_db_fks(connection)
        guard_tables = await _fetch_guard_tables(connection)
        found = await _fetch_blocking_triggers(connection)
        triggers = {
            table: found.get(table, []) for table in WRITE_BLOCKED_TABLES
        }
        missing_triggers = [
            table for table in WRITE_BLOCKED_TABLES if not triggers[table]
        ]
        return {
            "cross_db_fks": cross_db_fks,
            "guard_tables": guard_tables,
            "triggers": triggers,
            "missing_triggers": missing_triggers,
            "ok": not cross_db_fks and not guard_tables and not missing_triggers,
        }
    finally:
        await connection.close()
