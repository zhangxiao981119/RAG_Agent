"""存量搬迁（S6-5）：单场景 kb 的数据面表 → 场景库，含校验与回滚回拷。

设计依据：`02-design/第六阶段-Step6-前置裁决.md` §4.3 Phase 3 / §4.5 / §4.10。

三条纪律：
1. **不用 pg_dump 做行过滤**（它没有 `--where`）：逐表 `COPY (SELECT ...) TO STDOUT`
   经临时文件桥接到目标库的 `COPY ... FROM STDIN`，两边列清单来自同一处发现逻辑。
2. **只搬数据面**：搬迁集取 `scene_db_schema.MIGRATION_TABLES`（唯一来源，10 张），
   过滤键逐表显式声明（kb_id / document_id / job_id），不按「含 kb_id 的表」推导。
3. **不静默**：目标库未手术、控制面非空、已存别家 kb、源库在搬迁窗口有写入、
   逐表行数/摘要不等 —— 一律抛 `MigrateError` 并带机器可读 code，绝不「部分成功当成功」。

生成列（`chunks.fts_vec`、`graph_entities.name_vec`）由 `information_schema` 的
`is_generated` 排除：它们不允许显式写入，带上就会 COPY 失败。

`--reverse` 用于 §4.5 回滚：把场景库新增的行增量回拷控制库（COPY 进临时表后
`INSERT ... ON CONFLICT (id) DO NOTHING`），只增不删、可重跑。
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlsplit

import asyncpg

from app.infra.scene_db_schema import MIGRATION_TABLES, control_plane_usage, verify

MIGRATE_TARGET_NOT_PREPARED: Final[str] = "S6_MIGRATE_TARGET_NOT_PREPARED"
MIGRATE_TARGET_HAS_CONTROL_ROWS: Final[str] = "S6_MIGRATE_TARGET_HAS_CONTROL_ROWS"
MIGRATE_TARGET_HAS_FOREIGN_KB: Final[str] = "S6_MIGRATE_TARGET_HAS_FOREIGN_KB"
MIGRATE_SAME_DATABASE: Final[str] = "S6_MIGRATE_SAME_DATABASE"
MIGRATE_SOURCE_EMPTY: Final[str] = "S6_MIGRATE_SOURCE_EMPTY"
MIGRATE_SOURCE_CHANGED: Final[str] = "S6_MIGRATE_SOURCE_CHANGED"
MIGRATE_VERIFY_MISMATCH: Final[str] = "S6_MIGRATE_VERIFY_MISMATCH"
MIGRATE_SCHEMA_DRIFT: Final[str] = "S6_MIGRATE_SCHEMA_DRIFT"
MIGRATE_VERSION_MISMATCH: Final[str] = "S6_MIGRATE_VERSION_MISMATCH"

__all__ = [
    "MIGRATE_TARGET_NOT_PREPARED",
    "MIGRATE_TARGET_HAS_CONTROL_ROWS",
    "MIGRATE_TARGET_HAS_FOREIGN_KB",
    "MIGRATE_SAME_DATABASE",
    "MIGRATE_SOURCE_EMPTY",
    "MIGRATE_SOURCE_CHANGED",
    "MIGRATE_VERIFY_MISMATCH",
    "MIGRATE_SCHEMA_DRIFT",
    "MIGRATE_VERSION_MISMATCH",
    "MIGRATION_ORDER",
    "TableSpec",
    "MigrateError",
    "column_list",
    "server_identity",
    "table_metrics",
    "probe_digest",
    "migrate_forward",
    "migrate_reverse",
]


class MigrateError(Exception):
    """搬迁错误：code 机器可读，detail 供日志与工单直接引用。"""

    def __init__(self, code: str, detail: dict[str, object] | None = None) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class TableSpec:
    """一张搬迁表的过滤规则。

    `predicate` 里只用 `$1`（kb 的 UUID）：COPY 查询通过 asyncpg 的参数绑定发送，
    不做任何字符串拼接，过滤键因此无法被 DSN / 命令行注入。
    """

    name: str
    predicate: str


# 父表在前：documents 必须先于所有以 document_id 关联的表；
# chunks 先于 graph_entity_mentions；parse_jobs 先于 parse_segments。
# 顺序错会在保留的「搬迁集内部外键」上直接报错（fail-closed，不静默跳表）。
MIGRATION_ORDER: tuple[TableSpec, ...] = (
    TableSpec("documents", '"kb_id" = $1'),
    TableSpec("chunks", '"kb_id" = $1'),
    TableSpec("kb_terms", '"kb_id" = $1'),
    TableSpec("graph_entities", '"kb_id" = $1'),
    TableSpec(
        "graph_entity_mentions",
        '"kb_id" = $1',
    ),
    TableSpec("graph_relations", '"kb_id" = $1'),
    TableSpec("wiki_pages", '"kb_id" = $1'),
    TableSpec(
        "parse_jobs",
        '"document_id" IN (SELECT "id" FROM "documents" WHERE "kb_id" = $1)',
    ),
    TableSpec(
        "parse_segments",
        '"job_id" IN ('
        'SELECT j."id" FROM "parse_jobs" j'
        ' JOIN "documents" d ON d."id" = j."document_id"'
        ' WHERE d."kb_id" = $1)',
    ),
    TableSpec(
        "governance_jobs",
        '"document_id" IN (SELECT "id" FROM "documents" WHERE "kb_id" = $1)',
    ),
)

assert {spec.name for spec in MIGRATION_ORDER} == MIGRATION_TABLES, (
    "搬迁顺序必须与 scene_db_schema.MIGRATION_TABLES 一一对应"
)

# chunks 额外校验正文：行数/id 相同但正文被截断的情况，id 摘要发现不了。
_CONTENT_TABLE: Final[str] = "chunks"

COLUMN_SQL: Final[str] = """
SELECT column_name
FROM information_schema.columns
WHERE table_schema = current_schema()
  AND table_name = $1
  AND is_generated = 'NEVER'
ORDER BY ordinal_position
"""
"""可显式写入的列（排除生成列）；源/目标两侧必须得到同一结果。"""

PROBE_SQL: Final[str] = """
SELECT count(*)::bigint AS row_count,
       COALESCE(md5(string_agg("id"::text, ',' ORDER BY "id")), '') AS id_md5
FROM "chunks"
WHERE "kb_id" = $1
  AND ("content" ILIKE '%' || $2 || '%' OR "heading_path" ILIKE '%' || $2 || '%')
"""
"""关键词探测：与检索**关键词路**同款 ILIKE 子串谓词（不是整条检索链路等价），
按 id 固定顺序取摘要。"""


def _describe(dsn: str) -> str:
    """`host:port/dbname`，绝不带账号口令（可安全贴进工单）。"""
    parts = urlsplit(dsn)
    host = parts.hostname or "?"
    port = parts.port or 5432
    database = (parts.path or "").lstrip("/") or "?"
    return f"{host}:{port}/{database}"


async def server_identity(
    connection: asyncpg.Connection,
) -> tuple[str, str, int]:
    """连接实际落到的 (库名, 服务端地址, 端口)。

    ★ 用服务端视角判定「是否同一个库」而不是比较 DSN 字符串：
      `localhost` / `127.0.0.1` / 容器名指向同一实例时必须判为同库，
      否则 `--scene-dsn` 误填控制库时 TRUNCATE 会清掉控制库数据面。
    """
    row = await connection.fetchrow(
        "SELECT current_database() AS db,"
        "       COALESCE(inet_server_addr()::text, '') AS addr,"
        "       COALESCE(inet_server_port(), 0) AS port"
    )
    return (row["db"], row["addr"], int(row["port"]))



async def column_list(connection: asyncpg.Connection, table: str) -> tuple[str, ...]:
    """表上可显式写入的列（排除生成列），按 ordinal_position 排序。

    ★ 生成列（`chunks.fts_vec` / `graph_entities.name_vec`）显式写入会报错，
      必须排除；否则 COPY 在目标库直接失败。
    """
    rows = await connection.fetch(COLUMN_SQL, table)
    return tuple(row["column_name"] for row in rows)


def _metrics_sql(spec: TableSpec) -> str:
    """逐表校验 SQL：行数 + id 摘要（chunks 另加正文长度与正文摘要）。"""
    if spec.name == _CONTENT_TABLE:
        return f"""
        SELECT count(*)::bigint AS row_count,
               COALESCE(md5(string_agg("id"::text, ',' ORDER BY "id")), '') AS id_md5,
               COALESCE(sum(length("content")), 0)::bigint AS content_len,
               COALESCE(md5(string_agg(
                   substr(md5("content"), 1, 12),
                   ',' ORDER BY "document_id", "chunk_index", "id"
               )), '') AS content_md5
        FROM "{spec.name}"
        WHERE {spec.predicate}
        """
    return f"""
    SELECT count(*)::bigint AS row_count,
           COALESCE(md5(string_agg("id"::text, ',' ORDER BY "id")), '') AS id_md5
    FROM "{spec.name}"
    WHERE {spec.predicate}
    """


async def table_metrics(
    connection: asyncpg.Connection,
    spec: TableSpec,
    kb_id: str,
) -> dict[str, Any]:
    """单表在给定 kb 过滤条件下的行数与摘要（源/目标两侧用同一 SQL）。"""
    row = await connection.fetchrow(_metrics_sql(spec), kb_id)
    return dict(row)


async def _copy_table(
    source: asyncpg.Connection,
    target: asyncpg.Connection,
    spec: TableSpec,
    kb_id: str,
    columns: tuple[str, ...],
) -> int:
    """把一张表在源库的过滤结果整表 COPY 进目标表，返回写入行数。

    ★ 经临时文件桥接（asyncpg 的 COPY 输出/输入都接受文件对象）：
      内存占用与表大小解耦，且文本格式对换行/反斜杠/中文是逐字节可逆的。
    """
    select_cols = ", ".join(f'"{column}"' for column in columns)
    query = (
        f'SELECT {select_cols} FROM "{spec.name}" '
        f"WHERE {spec.predicate} ORDER BY \"id\""
    )
    with tempfile.TemporaryFile() as buffer:
        status = await source.copy_from_query(query, kb_id, output=buffer)
        buffer.seek(0)
        await target.copy_to_table(
            spec.name, source=buffer, columns=list(columns)
        )
    # 状态串形如 "COPY 12"：用它做一次自校验，COPY 0 行时也无需猜。
    return int(status.split()[-1])


async def _copy_table_incremental(
    source: asyncpg.Connection,
    target: asyncpg.Connection,
    spec: TableSpec,
    kb_id: str,
    columns: tuple[str, ...],
) -> dict[str, int]:
    """回滚方向：只把目标库缺失的 id 插进去（不删、不改已有行）。

    做法：源行 COPY 进目标库的临时表（LIKE 目标表），再用
    `INSERT ... ON CONFLICT (id) DO NOTHING` 合并。幂等，可重复执行。
    """
    select_cols = ", ".join(f'"{column}"' for column in columns)
    insert_cols = select_cols
    query = (
        f'SELECT {select_cols} FROM "{spec.name}" '
        f"WHERE {spec.predicate} ORDER BY \"id\""
    )
    stage = "_s6_stage"
    with tempfile.TemporaryFile() as buffer:
        await source.copy_from_query(query, kb_id, output=buffer)
        buffer.seek(0)
        async with target.transaction():
            await target.execute(
                f'CREATE TEMP TABLE "{stage}" '
                f'(LIKE "{spec.name}" INCLUDING DEFAULTS) ON COMMIT DROP'
            )
            await target.copy_to_table(stage, source=buffer, columns=list(columns))
            stage_rows = await target.fetchval(f'SELECT count(*) FROM "{stage}"')
            missing = await target.fetchval(
                f'SELECT count(*) FROM "{stage}" s '
                f'WHERE NOT EXISTS (SELECT 1 FROM "{spec.name}" t WHERE t."id" = s."id")'
            )
            await target.execute(
                f'INSERT INTO "{spec.name}" ({insert_cols}) '
                f'SELECT {insert_cols} FROM "{stage}" '
                f'ON CONFLICT ("id") DO NOTHING'
            )
            resolved = await target.fetchval(
                f'SELECT count(*) FROM "{stage}" s '
                f'WHERE NOT EXISTS (SELECT 1 FROM "{spec.name}" t WHERE t."id" = s."id")'
            )
    if resolved:
        raise MigrateError(
            MIGRATE_VERIFY_MISMATCH,
            {"table": spec.name, "unresolved": resolved},
        )
    return {
        "stage_rows": int(stage_rows),
        "existing": int(stage_rows) - int(missing),
        "inserted": int(missing),
    }


def _probe_sql() -> str:
    return PROBE_SQL


async def probe_digest(
    connection: asyncpg.Connection,
    kb_id: str,
    term: str,
) -> dict[str, Any]:
    """在单库跑一次关键词探测，返回命中行数与 id 摘要（两库结果必须相等）。"""
    row = await connection.fetchrow(_probe_sql(), kb_id, term)
    return dict(row)


async def _ensure_distinct_targets(
    control: asyncpg.Connection,
    scene: asyncpg.Connection,
    control_dsn: str,
    scene_dsn: str,
) -> None:
    """两个连接必须落在不同库、且迁移版本一致（否则列/表口径不可信）。

    ★ 同库判定用服务端视角（库名 + 服务端地址 + 端口），不比较 DSN 字符串，
      避免 `localhost` 与 `127.0.0.1` 这类别名绕过保护。
    """
    if await server_identity(control) == await server_identity(scene):
        raise MigrateError(
            MIGRATE_SAME_DATABASE,
            {"control": _describe(control_dsn), "scene": _describe(scene_dsn)},
        )
    try:
        control_version = await control.fetchval(
            "SELECT version_num FROM alembic_version"
        )
        scene_version = await scene.fetchval("SELECT version_num FROM alembic_version")
    except asyncpg.UndefinedTableError as exc:
        raise MigrateError(
            MIGRATE_VERSION_MISMATCH,
            {"hint": "某一侧没有 alembic_version（未执行 alembic upgrade head）"},
        ) from exc
    if control_version != scene_version:
        raise MigrateError(
            MIGRATE_VERSION_MISMATCH,
            {
                "control": control_version,
                "scene": scene_version,
                "hint": "两侧先各跑 alembic upgrade head（场景库升级后重做 M-B 手术）",
            },
        )


async def migrate_forward(
    control_dsn: str,
    scene_dsn: str,
    kb_id: str,
    probe_term: str | None = None,
) -> dict[str, Any]:
    """把控制库中单个 kb 的数据面表整表搬进场景库（幂等：先 TRUNCATE 再 COPY）。

    前置（缺一即抛，不做任何降级）：
      · 场景库已完成 M-B 手术且三项校验 PASS（否则跨库外键会让 COPY 失败）；
      · 场景库控制面表全空（`tenants` 等有行 ⇒ 这不是场景库）；
      · 场景库现有数据只属于本次 kb（否则 TRUNCATE 会静默清掉别家数据）；
      · 源库该 kb 至少有 1 篇 document（0 篇通常是 --kb-id 写错）。

    搬迁窗口写检测：COPY 前后各取一次源库摘要，不等即回滚并报
    `MIGRATE_SOURCE_CHANGED`（说明停写没做到，本次结果不可信）。
    """
    control = await asyncpg.connect(control_dsn)
    scene = await asyncpg.connect(scene_dsn)
    try:
        # 同库判定必须最先做：DSN 填反时若先跑 verify/清库检查，
        # 「把控制库当场景库」只会报 NOT_PREPARED 之类的次生错误。
        await _ensure_distinct_targets(control, scene, control_dsn, scene_dsn)

        prepared = await verify(scene_dsn)
        if not prepared["ok"]:
            raise MigrateError(
                MIGRATE_TARGET_NOT_PREPARED,
                {
                    "target": _describe(scene_dsn),
                    "cross_db_fks": len(prepared["cross_db_fks"]),
                    "guard_tables": len(prepared["guard_tables"]),
                    "missing_triggers": prepared["missing_triggers"],
                },
            )

        occupied = await control_plane_usage(scene_dsn)
        if occupied:
            raise MigrateError(
                MIGRATE_TARGET_HAS_CONTROL_ROWS,
                {"target": _describe(scene_dsn), "tables": occupied},
            )

        foreign = await scene.fetch(
            'SELECT DISTINCT "kb_id"::text AS kb_id FROM "documents"'
        )
        foreign_ids = sorted({row["kb_id"] for row in foreign} - {kb_id})
        if foreign_ids:
            raise MigrateError(
                MIGRATE_TARGET_HAS_FOREIGN_KB,
                {"target": _describe(scene_dsn), "kb_ids": foreign_ids},
            )

        columns: dict[str, tuple[str, ...]] = {}
        for spec in MIGRATION_ORDER:
            source_columns = await column_list(control, spec.name)
            target_columns = await column_list(scene, spec.name)
            if source_columns != target_columns:
                raise MigrateError(
                    MIGRATE_SCHEMA_DRIFT,
                    {
                        "table": spec.name,
                        "control": list(source_columns),
                        "scene": list(target_columns),
                    },
                )
            columns[spec.name] = source_columns

        before = {
            spec.name: await table_metrics(control, spec, kb_id)
            for spec in MIGRATION_ORDER
        }
        if int(before["documents"]["row_count"]) == 0:
            raise MigrateError(
                MIGRATE_SOURCE_EMPTY,
                {"control": _describe(control_dsn), "kb_id": kb_id},
            )

        tables: list[dict[str, Any]] = []
        async with scene.transaction():
            # ★ 不写 CASCADE：搬迁集内部外键已在列表里，若将来冒出「外部表引用搬迁集」，
            #   这里必须显式失败，而不是连带清空未列出的表（§4.3 原文的 CASCADE 在此有意收窄）。
            await scene.execute(
                "TRUNCATE "
                + ", ".join(f'"{spec.name}"' for spec in MIGRATION_ORDER)
            )
            for spec in MIGRATION_ORDER:
                await _copy_table(control, scene, spec, kb_id, columns[spec.name])

            after = {
                spec.name: await table_metrics(control, spec, kb_id)
                for spec in MIGRATION_ORDER
            }
            changed = [
                name for name in after if after[name] != before[name]
            ]
            if changed:
                raise MigrateError(
                    MIGRATE_SOURCE_CHANGED,
                    {"tables": changed, "hint": "搬迁窗口内源库有写入，请停写后重跑"},
                )

            mismatched: list[str] = []
            for spec in MIGRATION_ORDER:
                target_metrics = await table_metrics(scene, spec, kb_id)
                ok = target_metrics == before[spec.name]
                if not ok:
                    mismatched.append(spec.name)
                tables.append(
                    {
                        "table": spec.name,
                        "source": before[spec.name],
                        "target": target_metrics,
                        "ok": ok,
                    }
                )
            if mismatched:
                raise MigrateError(
                    MIGRATE_VERIFY_MISMATCH,
                    {"tables": mismatched},
                )

            equivalence: dict[str, Any] | None = None
            if probe_term:
                control_probe = await probe_digest(control, kb_id, probe_term)
                scene_probe = await probe_digest(scene, kb_id, probe_term)
                equivalence = {
                    "ran": True,
                    "term": probe_term,
                    "control": control_probe,
                    "scene": scene_probe,
                    "ok": control_probe == scene_probe,
                }
                if not equivalence["ok"]:
                    raise MigrateError(
                        MIGRATE_VERIFY_MISMATCH,
                        {"table": "chunks", "probe_term": probe_term},
                    )

        return {
            "direction": "forward",
            "control": _describe(control_dsn),
            "scene": _describe(scene_dsn),
            "kb_id": kb_id,
            "tables": tables,
            "equivalence": equivalence,
            "ok": True,
        }
    finally:
        await control.close()
        await scene.close()


async def migrate_reverse(
    scene_dsn: str,
    control_dsn: str,
    kb_id: str,
) -> dict[str, Any]:
    """回滚方向：把场景库中控制库缺失的行增量回拷（§4.5 R5「补差」）。

    - 只 INSERT 缺失 id，不 DELETE / 不 UPDATE，控制库既有行一字不改；
    - 两侧必须不同库且 `alembic_version` 相同（列清单一致才允许合并）；
    - 每张表在目标事务里合并，失败即回滚该表；
    - 逐表报告「临时表行数 / 已存在 / 新增」，供 R5 差集留痕。
    """
    scene = await asyncpg.connect(scene_dsn)
    control = await asyncpg.connect(control_dsn)
    try:
        await _ensure_distinct_targets(control, scene, control_dsn, scene_dsn)

        tables: list[dict[str, Any]] = []
        for spec in MIGRATION_ORDER:
            source_columns = await column_list(scene, spec.name)
            target_columns = await column_list(control, spec.name)
            if source_columns != target_columns:
                raise MigrateError(
                    MIGRATE_SCHEMA_DRIFT,
                    {
                        "table": spec.name,
                        "scene": list(source_columns),
                        "control": list(target_columns),
                    },
                )
            result = await _copy_table_incremental(
                scene, control, spec, kb_id, source_columns
            )
            tables.append({"table": spec.name, **result})

        return {
            "direction": "reverse",
            "scene": _describe(scene_dsn),
            "control": _describe(control_dsn),
            "kb_id": kb_id,
            "tables": tables,
            "ok": True,
        }
    finally:
        await scene.close()
        await control.close()
