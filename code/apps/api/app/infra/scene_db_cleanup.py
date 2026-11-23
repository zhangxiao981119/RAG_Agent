"""S6-6：场景库单 kb 数据清理 + 孤儿检查（设计 §4.7 的配套件）。

背景：`documents.kb_id ON DELETE CASCADE` 只在**同一个 database 内**生效；
kb 搬到场景库后，API 的硬删除必须返回 409（已实现），先由本脚本在维护窗口内
删掉场景库的搬迁集行，再由 API 删控制库 kb（保留审计）。

两条纪律：

1. **只删数据面、逆依赖顺序、单事务**：删除顺序是 `MIGRATION_ORDER` 的逆序
   （先子表后父表），任一表失败整笔回滚；过滤谓词直接复用搬迁脚本的唯一来源，
   不再各写一套。
2. **孤儿检查两个方向，任一非零即失败**：
   · 场景库存在控制库 `knowledge_bases` 里已没有的 kb（含 documents 缺失的悬空行）；
   · 控制库数据面存在 `knowledge_bases` 已没有的行（陈旧副本，正常应为 0）。

`--apply` 删除一个控制库已不存在的 kb 时，必须显式 `--allow-missing-kb`
（孤儿清理场景），否则拒绝 —— 防止 `--kb-id` 写错时静默删掉别人的数据。
"""
from __future__ import annotations

from typing import Any, Final

import asyncpg

from app.infra.scene_db_migrate import (
    MIGRATION_ORDER,
    TableSpec,
    _describe,
    ensure_distinct_targets,
)
from app.infra.scene_db_schema import control_plane_usage, verify

CLEANUP_TARGET_NOT_PREPARED: Final[str] = "S6_CLEANUP_TARGET_NOT_PREPARED"
CLEANUP_TARGET_HAS_CONTROL_ROWS: Final[str] = "S6_CLEANUP_TARGET_HAS_CONTROL_ROWS"
CLEANUP_KB_MISSING: Final[str] = "S6_CLEANUP_KB_MISSING"
CLEANUP_VERIFY_MISMATCH: Final[str] = "S6_CLEANUP_VERIFY_MISMATCH"

__all__ = [
    "CLEANUP_TARGET_NOT_PREPARED",
    "CLEANUP_TARGET_HAS_CONTROL_ROWS",
    "CLEANUP_KB_MISSING",
    "CLEANUP_VERIFY_MISMATCH",
    "CleanupError",
    "DELETE_ORDER",
    "KB_ID_TABLES",
    "ORPHAN_PREDICATES",
    "scene_kb_counts",
    "scene_orphans",
    "control_orphans",
    "cleanup_scene_kb",
    "check_orphans",
]

# 孤儿判定：每张表「行本身是否还挂在一个存在的父实体上」。
# · kb_id 直连表：父实体是控制库 knowledge_bases（场景库里是空表，故由调用方传已知集合）；
# · parse_jobs / governance_jobs：父实体是 documents（控制库靠 FK CASCADE，正常为 0）；
# · parse_segments：父实体是 parse_jobs。
ORPHAN_PREDICATES: dict[str, str] = {
    "documents": '"kb_id" NOT IN (SELECT "id" FROM "knowledge_bases")',
    "chunks": '"kb_id" NOT IN (SELECT "id" FROM "knowledge_bases")',
    "kb_terms": '"kb_id" NOT IN (SELECT "id" FROM "knowledge_bases")',
    "graph_entities": '"kb_id" NOT IN (SELECT "id" FROM "knowledge_bases")',
    "graph_entity_mentions": '"kb_id" NOT IN (SELECT "id" FROM "knowledge_bases")',
    "graph_relations": '"kb_id" NOT IN (SELECT "id" FROM "knowledge_bases")',
    "wiki_pages": '"kb_id" NOT IN (SELECT "id" FROM "knowledge_bases")',
    "parse_jobs": '"document_id" NOT IN (SELECT "id" FROM "documents")',
    "parse_segments": '"job_id" NOT IN (SELECT "id" FROM "parse_jobs")',
    "governance_jobs": '"document_id" NOT IN (SELECT "id" FROM "documents")',
}

assert set(ORPHAN_PREDICATES) == {spec.name for spec in MIGRATION_ORDER}, (
    "孤儿判定必须覆盖全部搬迁表"
)

# kb_id 直连的 7 张表（不能用 predicate 里是否出现 "kb_id" 来判断：
# parse_jobs 的谓词经 documents 子查询同样含有 "kb_id"）。
KB_ID_TABLES: Final[frozenset[str]] = frozenset(
    {
        "documents",
        "chunks",
        "kb_terms",
        "graph_entities",
        "graph_entity_mentions",
        "graph_relations",
        "wiki_pages",
    }
)
assert KB_ID_TABLES == {
    spec.name for spec in MIGRATION_ORDER if spec.predicate == '"kb_id" = $1'
}, "KB_ID_TABLES 必须与 kb_id 直连谓词的表集合一致"

# 删除顺序：搬迁顺序的严格逆序（先子表后父表），与 MIGRATION_ORDER 同源。
DELETE_ORDER: Final[tuple[TableSpec, ...]] = tuple(reversed(MIGRATION_ORDER))

_MISSING_PARENT: Final[str] = "<missing_parent>"


class CleanupError(Exception):
    """清理/孤儿检查错误：code 机器可读，detail 可直接进工单。"""

    def __init__(self, code: str, detail: dict[str, object] | None = None) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


async def _assert_scene_target(
    control: asyncpg.Connection,
    scene: asyncpg.Connection,
    control_dsn: str,
    scene_dsn: str,
) -> None:
    """目标必须是「空的、已手术的」场景库（与搬迁前同一套身份校验）。"""
    await ensure_distinct_targets(control, scene, control_dsn, scene_dsn)

    prepared = await verify(scene_dsn)
    if not prepared["ok"]:
        raise CleanupError(
            CLEANUP_TARGET_NOT_PREPARED,
            {
                "target": _describe(scene_dsn),
                "cross_db_fks": len(prepared["cross_db_fks"]),
                "guard_tables": len(prepared["guard_tables"]),
                "missing_triggers": prepared["missing_triggers"],
            },
        )
    occupied = await control_plane_usage(scene_dsn)
    if occupied:
        raise CleanupError(
            CLEANUP_TARGET_HAS_CONTROL_ROWS,
            {"target": _describe(scene_dsn), "tables": occupied},
        )


async def scene_kb_counts(
    connection: asyncpg.Connection,
    kb_id: str,
) -> dict[str, int]:
    """场景库中该 kb 在每张搬迁表的行数（与清理用的是同一过滤谓词）。"""
    counts: dict[str, int] = {}
    for spec in MIGRATION_ORDER:
        counts[spec.name] = int(
            await connection.fetchval(
                f'SELECT count(*) FROM "{spec.name}" WHERE {spec.predicate}',
                kb_id,
            )
        )
    return counts


async def _delete_order_counts(
    connection: asyncpg.Connection,
    kb_id: str,
) -> dict[str, int]:
    """逆依赖顺序删除该 kb 的搬迁集行，返回每表删除行数（调用方负责事务）。"""
    deleted: dict[str, int] = {}
    for spec in DELETE_ORDER:
        status = await connection.execute(
            f'DELETE FROM "{spec.name}" WHERE {spec.predicate}', kb_id
        )
        deleted[spec.name] = int(status.split()[-1])
    return deleted


async def cleanup_scene_kb(
    control_dsn: str,
    scene_dsn: str,
    kb_id: str,
    apply: bool,
    allow_missing_kb: bool = False,
) -> dict[str, Any]:
    """清理场景库中单 kb 的搬迁集行；`apply=False` 时只读统计（用于工单确认）。

    - 控制库 kb 不存在且未显式 `allow_missing_kb` → 拒绝（防 --kb-id 写错）；
    - 删除在单事务内完成，删完立即复核：该 kb 的各表行数必须全部为 0；
    - 不触碰控制库任何行（kb 行由 API 删除，保留审计）。
    """
    control = await asyncpg.connect(control_dsn)
    scene = await asyncpg.connect(scene_dsn)
    try:
        await _assert_scene_target(control, scene, control_dsn, scene_dsn)

        existed = await control.fetchval(
            'SELECT EXISTS (SELECT 1 FROM "knowledge_bases" WHERE "id" = $1)', kb_id
        )
        if not existed and not allow_missing_kb:
            raise CleanupError(
                CLEANUP_KB_MISSING,
                {
                    "kb_id": kb_id,
                    "hint": "控制库无此 kb；若是清理孤儿数据请显式 --allow-missing-kb",
                },
            )

        before = await scene_kb_counts(scene, kb_id)
        deleted: dict[str, int] = {}
        if apply:
            async with scene.transaction():
                deleted = await _delete_order_counts(scene, kb_id)
            after = await scene_kb_counts(scene, kb_id)
            residual = {name: count for name, count in after.items() if count}
            if residual:
                raise CleanupError(
                    CLEANUP_VERIFY_MISMATCH,
                    {"kb_id": kb_id, "residual": residual},
                )
        else:
            after = before

        return {
            "direction": "cleanup" if apply else "inspect",
            "control": _describe(control_dsn),
            "scene": _describe(scene_dsn),
            "kb_id": kb_id,
            "existed_in_control": bool(existed),
            "tables": [
                {
                    "table": spec.name,
                    "before": before[spec.name],
                    "deleted": deleted.get(spec.name, 0),
                    "after": after[spec.name],
                }
                for spec in MIGRATION_ORDER
            ],
            "ok": True,
        }
    finally:
        await control.close()
        await scene.close()


async def scene_orphans(
    control: asyncpg.Connection,
    scene: asyncpg.Connection,
) -> dict[str, dict[str, int]]:
    """场景库中「所属 kb 已不在控制库」的行数（按表 → kb id/marker → 行数）。

    - 7 张 kb_id 直连表按 kb 分组，只保留未知 kb；
    - parse_jobs / governance_jobs 经 documents；parse_segments 经 parse_jobs →
      documents；父实体缺失的行记为 `<missing_parent>`，同样算孤儿。
    """
    known = {
        row["id"]
        for row in await control.fetch(
            'SELECT "id"::text AS id FROM "knowledge_bases"'
        )
    }
    orphans: dict[str, dict[str, int]] = {}

    for spec in MIGRATION_ORDER:
        if spec.name in KB_ID_TABLES:
            rows = await scene.fetch(
                f'SELECT "kb_id"::text AS kb, count(*)::bigint AS n '
                f'FROM "{spec.name}" GROUP BY 1'
            )
        elif spec.name == "parse_segments":
            rows = await scene.fetch(
                'SELECT COALESCE(d."kb_id"::text, $1) AS kb, count(*)::bigint AS n '
                'FROM "parse_segments" s '
                'LEFT JOIN "parse_jobs" j ON j."id" = s."job_id" '
                'LEFT JOIN "documents" d ON d."id" = j."document_id" '
                "GROUP BY 1",
                _MISSING_PARENT,
            )
        else:
            rows = await scene.fetch(
                f'SELECT COALESCE(d."kb_id"::text, $1) AS kb, count(*)::bigint AS n '
                f'FROM "{spec.name}" t '
                f'LEFT JOIN "documents" d ON d."id" = t."document_id" '
                "GROUP BY 1",
                _MISSING_PARENT,
            )
        table_orphans = {
            row["kb"]: int(row["n"])
            for row in rows
            if row["kb"] == _MISSING_PARENT or row["kb"] not in known
        }
        if table_orphans:
            orphans[spec.name] = table_orphans
    return orphans


async def control_orphans(control: asyncpg.Connection) -> dict[str, int]:
    """控制库数据面残留：父实体已不存在的行数（陈旧副本，正常必须为 0）。"""
    result: dict[str, int] = {}
    for spec in MIGRATION_ORDER:
        count = int(
            await control.fetchval(
                f'SELECT count(*) FROM "{spec.name}" WHERE {ORPHAN_PREDICATES[spec.name]}'
            )
        )
        if count:
            result[spec.name] = count
    return result


async def check_orphans(control_dsn: str, scene_dsn: str) -> dict[str, Any]:
    """双库孤儿检查（设计 §4.7 规则 3）：两侧任一非零即 `ok=False`。"""
    control = await asyncpg.connect(control_dsn)
    scene = await asyncpg.connect(scene_dsn)
    try:
        await _assert_scene_target(control, scene, control_dsn, scene_dsn)

        scene_result = await scene_orphans(control, scene)
        control_result = await control_orphans(control)
        return {
            "direction": "orphans",
            "control": _describe(control_dsn),
            "scene": _describe(scene_dsn),
            "scene_orphans": scene_result,
            "control_orphans": control_result,
            "ok": not scene_result and not control_result,
        }
    finally:
        await control.close()
        await scene.close()
