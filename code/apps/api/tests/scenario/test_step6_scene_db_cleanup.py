"""S6-6：场景库单 kb 清理与双库孤儿检查。

结构：
  (a) NO-DB：删除顺序为搬迁顺序逆序；孤儿判定覆盖全部 10 张表；CLI 错误/成功路径；
  (b) Real-DB（`SCENARIO_TEST_DB_URL` 缺失即 skip）：
      迁移出数据 → 只读统计 → 清理后归零 → 孤儿检测 → 缺 kb 需显式放行 → 再清理。

真库用例直接复用 S6-5 的双库夹具（`_seed_control` / `_insert_extra_document`），
不另造一套种子数据（避免两份夹具漂移）。
"""
from __future__ import annotations

import asyncio
import sys
import uuid

import asyncpg
import pytest
from alembic import command

from app.infra.scene_db_cleanup import (
    CLEANUP_KB_MISSING,
    CLEANUP_TARGET_NOT_PREPARED,
    DELETE_ORDER,
    KB_ID_TABLES,
    ORPHAN_PREDICATES,
    CleanupError,
    check_orphans,
    cleanup_scene_kb,
)
from app.infra.scene_db_migrate import (
    MIGRATE_SAME_DATABASE,
    MIGRATION_ORDER,
    MigrateError,
    migrate_forward,
)
from app.infra.scene_db_schema import MIGRATION_TABLES, apply_surgery
from scripts import scene_db_cleanup as cli
from tests.scenario.pg_helpers import (
    SCENARIO_TEST_DB_URL,
    alembic_config,
    dsn,
    requires_db,
)
from tests.scenario.test_step6_scene_db_migrate import (
    _insert_extra_document,
    _seed_control,
    _with_database,
)


# ══════════════════════════════════════════════════════════════════════
# A. NO-DB 层
# ══════════════════════════════════════════════════════════════════════


def test_delete_order_is_exact_reverse_of_migration_order():
    """删除必须先子后父：与搬迁顺序严格逆序，且首尾分别是叶子与 documents。"""
    assert [spec.name for spec in DELETE_ORDER] == [
        spec.name for spec in reversed(MIGRATION_ORDER)
    ]
    assert DELETE_ORDER[0].name == "governance_jobs"
    assert DELETE_ORDER[-1].name == "documents"


def test_orphan_predicates_cover_all_tables_and_correct_parents():
    """孤儿判定：kb_id 表看 knowledge_bases；job 表看 documents；segments 看 parse_jobs。"""
    assert set(ORPHAN_PREDICATES) == MIGRATION_TABLES

    for table in (
        "documents",
        "chunks",
        "kb_terms",
        "graph_entities",
        "graph_entity_mentions",
        "graph_relations",
        "wiki_pages",
    ):
        assert '"kb_id" NOT IN (SELECT "id" FROM "knowledge_bases")' == (
            ORPHAN_PREDICATES[table]
        ), table

    for table in ("parse_jobs", "governance_jobs"):
        assert '"document_id" NOT IN (SELECT "id" FROM "documents")' == (
            ORPHAN_PREDICATES[table]
        ), table
    assert '"job_id" NOT IN (SELECT "id" FROM "parse_jobs")' == (
        ORPHAN_PREDICATES["parse_segments"]
    )

    # kb_id 直连表必须与搬迁谓词完全一致（不能用子串判断，parse_jobs 谓词也含 kb_id）
    assert KB_ID_TABLES == {
        spec.name for spec in MIGRATION_ORDER if spec.predicate == '"kb_id" = $1'
    }


def _argv(*extra: str) -> None:
    sys.argv = [
        "scene_db_cleanup",
        "--control-dsn",
        "postgresql://alice:s3cret@h:1/kagent",
        "--scene-dsn",
        "postgresql://alice:s3cret@h:1/kagent_finance",
        *extra,
    ]


def test_cli_returns_one_on_cleanup_error(monkeypatch, capsys):
    """CLI 遇 CleanupError：退出 1、打印错误码与 detail，不回显口令。"""

    async def boom(*args, **kwargs):
        raise CleanupError("S6_TEST_CLEANUP", {"hint": "测试"})

    monkeypatch.setattr(cli, "cleanup_scene_kb", boom)
    _argv("--kb-id", str(uuid.uuid4()), "--apply")
    assert cli.main() == 1
    captured = capsys.readouterr()
    assert "S6_TEST_CLEANUP" in captured.err
    assert "s3cret" not in captured.err + captured.out


def test_cli_prints_orphan_report_and_exit_zero(monkeypatch, capsys):
    """孤儿检查成功路径：打印「无」，退出 0。"""

    async def ok(*args, **kwargs):
        return {
            "direction": "orphans",
            "control": "h:1/kagent",
            "scene": "h:1/kagent_finance",
            "scene_orphans": {},
            "control_orphans": {},
            "ok": True,
        }

    monkeypatch.setattr(cli, "check_orphans", ok)
    _argv()
    assert cli.main() == 0
    captured = capsys.readouterr()
    assert "场景库孤儿：无" in captured.out
    assert "控制库陈旧数据面残留：无" in captured.out


def test_cli_returns_one_when_orphans_exist(monkeypatch, capsys):
    """有孤儿即退出 1（运维脚本必须能被 CI/工单判失败）。"""

    async def bad(*args, **kwargs):
        return {
            "direction": "orphans",
            "control": "h:1/kagent",
            "scene": "h:1/kagent_finance",
            "scene_orphans": {"documents": {"kb-x": 2}},
            "control_orphans": {},
            "ok": False,
        }

    monkeypatch.setattr(cli, "check_orphans", bad)
    _argv()
    assert cli.main() == 1
    assert "kb-x=2" in capsys.readouterr().out


# ══════════════════════════════════════════════════════════════════════
# B. Real-DB 层
# ══════════════════════════════════════════════════════════════════════


async def _run_roundtrip() -> None:
    base_url = SCENARIO_TEST_DB_URL or ""
    suffix = uuid.uuid4().hex[:12]
    control_name = f"clean_ctrl_{suffix}"
    scene_name = f"clean_scene_{suffix}"
    control_url = _with_database(base_url, control_name)
    scene_url = _with_database(base_url, scene_name)
    control_dsn = _with_database(dsn(base_url), control_name)
    scene_dsn = _with_database(dsn(base_url), scene_name)

    admin = await asyncpg.connect(_with_database(dsn(base_url), "postgres"))
    created: list[str] = []
    try:
        for name in (control_name, scene_name):
            try:
                await admin.execute(f'CREATE DATABASE "{name}" TEMPLATE template0')
            except asyncpg.exceptions.InsufficientPrivilegeError as exc:
                pytest.skip(f"当前账号无 CREATE DATABASE 权限（{exc}）")
            created.append(name)

        for url in (control_url, scene_url):
            await asyncio.to_thread(command.upgrade, alembic_config(url), "head")

        control = await asyncpg.connect(control_dsn)
        try:
            ids = await _seed_control(control)
        finally:
            await control.close()

        # 1) 未手术的场景库：清理必须拒绝
        with pytest.raises(CleanupError) as unprepared:
            await cleanup_scene_kb(
                control_dsn, scene_dsn, str(ids["kb1"]), apply=True
            )
        assert unprepared.value.code == CLEANUP_TARGET_NOT_PREPARED

        await apply_surgery(scene_dsn)

        # 2) 同库：必须拒绝（防止把控制库当场景库删）
        with pytest.raises(MigrateError) as same:
            await cleanup_scene_kb(control_dsn, control_dsn, str(ids["kb1"]), apply=True)
        assert same.value.code == MIGRATE_SAME_DATABASE

        # 3) 把 kb1 搬进场景库，作为清理对象
        migration = await migrate_forward(control_dsn, scene_dsn, str(ids["kb1"]))
        assert migration["ok"] is True

        # 4) 无孤儿
        orphans = await check_orphans(control_dsn, scene_dsn)
        assert orphans["ok"] is True
        assert orphans["scene_orphans"] == {}
        assert orphans["control_orphans"] == {}

        # 5) 只读统计：不删任何行
        inspect = await cleanup_scene_kb(
            control_dsn, scene_dsn, str(ids["kb1"]), apply=False
        )
        assert inspect["direction"] == "inspect"
        assert all(item["deleted"] == 0 for item in inspect["tables"])
        assert {item["table"]: item["before"] for item in inspect["tables"]}[
            "documents"
        ] == 1

        # 6) 执行清理：搬迁集全部归零
        cleanup = await cleanup_scene_kb(
            control_dsn, scene_dsn, str(ids["kb1"]), apply=True
        )
        assert cleanup["direction"] == "cleanup"
        by_table = {item["table"]: item for item in cleanup["tables"]}
        assert by_table["documents"]["deleted"] == 1
        assert by_table["chunks"]["deleted"] == 1
        assert all(item["after"] == 0 for item in cleanup["tables"])
        assert (await check_orphans(control_dsn, scene_dsn))["ok"] is True

        # 7) 场景库出现控制库已不存在的 kb：孤儿检查必须报出来
        scene = await asyncpg.connect(scene_dsn)
        try:
            ghost_kb = uuid.uuid4()
            ghost_doc = await _insert_extra_document(
                scene, ids, ghost_kb, "ghost.txt"
            )
        finally:
            await scene.close()
        orphans = await check_orphans(control_dsn, scene_dsn)
        assert orphans["ok"] is False
        assert orphans["scene_orphans"]["documents"] == {str(ghost_kb): 1}

        # 8) 清理不存在的 kb：默认拒绝，显式放行后才删
        with pytest.raises(CleanupError) as missing:
            await cleanup_scene_kb(
                control_dsn, scene_dsn, str(ghost_kb), apply=True
            )
        assert missing.value.code == CLEANUP_KB_MISSING

        ghost_cleanup = await cleanup_scene_kb(
            control_dsn,
            scene_dsn,
            str(ghost_kb),
            apply=True,
            allow_missing_kb=True,
        )
        assert ghost_cleanup["existed_in_control"] is False
        assert {item["table"]: item["deleted"] for item in ghost_cleanup["tables"]}[
            "documents"
        ] == 1
        assert (await check_orphans(control_dsn, scene_dsn))["ok"] is True

        # 9) 控制库陈旧副本：删 kb2 后其数据面行应被 FK CASCADE 清干净
        control = await asyncpg.connect(control_dsn)
        try:
            await control.execute("DELETE FROM knowledge_bases WHERE id = $1", ids["kb2"])
            assert await control.fetchval(
                "SELECT count(*) FROM documents WHERE kb_id = $1", ids["kb2"]
            ) == 0
        finally:
            await control.close()
        assert (await check_orphans(control_dsn, scene_dsn))["control_orphans"] == {}

        # ghost_doc 只用于触发孤儿；确认 id 未与迁移数据冲突
        assert ghost_doc != ids["doc1"]
    finally:
        for name in created:
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid()",
                name,
            )
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        await admin.close()


@requires_db
def test_real_db_cleanup_roundtrip():
    """临时双库：拒绝 → 迁移 → 统计 → 清理 → 孤儿 → 缺 kb 放行 → 控制库副本。"""
    asyncio.run(_run_roundtrip())
