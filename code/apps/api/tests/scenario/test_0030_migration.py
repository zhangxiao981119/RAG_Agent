"""迁移 0030 的往返、视图/函数契约、单一头 —— NO-DB 层 + Real-DB 层各一半。

两层各自能证明的东西**不重叠**（理由同 `test_0029_migration.py` 的文件头）：

  · NO-DB（`sql=True` 离线渲染）：`upgrade()` / `downgrade()` **真的被执行**，
    渲染出 PG 方言 DDL。它能抓「语句没生成」「降级顺序写反」「视图漏了 LEFT JOIN」。
  · Real-DB（`SCENARIO_TEST_DB_URL`）：对象存在性、视图行数恒等、
    冲突视图健康态 0 行、`downgrade 0029` 后对象全部消失、再 upgrade 复原。
    离线渲染无法证明「PG 接受了这段 DDL」，也无法证明行数恒等式。

★ 本文件**不**把 0030 登记进 `test_0028_migration.py::_PG_CONSTRAINT_SOURCES`：
  0030 **不产生任何约束**（没有 CHECK / UNIQUE / FK / 索引，见模块 docstring）。
  这条作用域边界由下面的 `test_0030_creates_no_constraints` 断言钉住：
  哪天有人在 0030 里加了一条 CHECK，那条断言先红，届时才必须去登记。

★ 真库用例是**同步函数**（`asyncio.run` 开连接）：`pg_helpers.migrate` 经
  `alembic/env.py` 用 `asyncio.run` 执行迁移，在已运行的事件循环里调用会抛
  「asyncio.run() cannot be called from a running event loop」。照抄 0029 的形状。
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import pathlib
import re

import asyncpg
import pytest

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from tests.scenario.pg_helpers import SCENARIO_TEST_DB_URL, alembic_config, dsn, migrate

_API_ROOT = pathlib.Path(__file__).resolve().parents[2]
_ALEMBIC_DIR = _API_ROOT / "alembic"
_MIGRATION_FILE = _ALEMBIC_DIR / "versions" / "0030_add_db_routing_views.py"

_FN_COLUMNS = ("tenant_id", "kb_id", "db_keys", "scene_count")
"""冲突函数的 4 列返回契约（顺序即 DDL 里的顺序，`RETURNS TABLE` 靠位置绑定）。"""


# ══════════════════════════════════════════════════════════════════════
# NO-DB 层：离线渲染 upgrade / downgrade
# ══════════════════════════════════════════════════════════════════════


def _render(direction: str, revision_range: str) -> str:
    """离线渲染迁移 SQL（不建连；URL 只用于解析方言）。

    刻意用**不带 ini 文件**的 `Config()`：`alembic.ini` 的 `fileConfig()` 会重配
    全局 logging，污染其它用例的 caplog（`env.py` 已用 `config_file_name is not None`
    守住这条分支）。
    """
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
    return _render("up", "0029:0030")


@pytest.fixture(scope="module")
def downgrade_sql() -> str:
    return _render("down", "0030:0029")


def _squash(sql: str) -> str:
    """压缩空白 + 去注释，供「谓词是否存在」这类文本断言使用。"""
    without_comments = re.sub(r"--[^\n]*", " ", sql)
    return re.sub(r"\s+", " ", without_comments)


def test_upgrade_creates_two_views_and_function(upgrade_sql):
    """三个对象都必须被创建，且都用 `CREATE OR REPLACE`（重跑不炸 = 幂等）。"""
    assert "CREATE OR REPLACE VIEW v_scenario_db_routing AS" in upgrade_sql
    assert "CREATE OR REPLACE FUNCTION fn_kb_db_binding_conflicts(" in upgrade_sql
    assert "CREATE OR REPLACE VIEW v_kb_db_binding_conflicts AS" in upgrade_sql
    # 先函数后包装视图：视图引用了函数，顺序反过来会在真库上失败
    assert upgrade_sql.find("CREATE OR REPLACE FUNCTION fn_kb_db_binding_conflicts(") < (
        upgrade_sql.find("CREATE OR REPLACE VIEW v_kb_db_binding_conflicts AS")
    )


def test_scenario_routing_view_keeps_left_join_and_single_published_caliber(upgrade_sql):
    """行数恒等式的三条前提：LEFT JOIN / 双端 tenant_id / state='published'。"""
    body = _squash(upgrade_sql)
    view_body = body.split("CREATE OR REPLACE VIEW v_scenario_db_routing AS", 1)[1]
    assert "FROM scenarios s LEFT JOIN scenario_versions v" in view_body, (
        "改成 INNER JOIN 会让无已发布版本的场景从盘点里消失"
    )
    assert "v.scenario_id = s.id" in view_body
    assert "v.tenant_id = s.tenant_id" in view_body, "漏掉双端 tenant_id = 第二真源"
    assert "v.state = 'published'" in view_body, "版本口径必须只有 published"
    # 默认库位由 COALESCE 在展示层给出，存储层保持 NULL
    assert "COALESCE(s.db_binding, 'default')" in view_body
    assert "CASE WHEN s.db_binding IS NULL THEN 'default' ELSE 'bound' END" in view_body
    # draft / validated / archived 都不该被当成版本口径写进过滤条件
    for forbidden in ("v.state <>", "v.state IN", "v.state != 'archived'"):
        assert forbidden not in view_body


def test_kb_conflict_function_has_teeth_predicates(upgrade_sql):
    """冲突判据的三条硬谓词：只判非默认库位、按库位去重、需要已发布版本。"""
    body = _squash(upgrade_sql)
    fn_body = body.split("CREATE OR REPLACE FUNCTION fn_kb_db_binding_conflicts(", 1)[1]
    assert "jsonb_array_elements_text(v.kb_ids)" in fn_body, "必须用 _text 变体（脏数据不该让盘点 500）"
    assert "WHERE s.db_binding IS NOT NULL" in fn_body, "默认库位不该参与冲突判定"
    assert "HAVING count(DISTINCT db_key) > 1" in fn_body, (
        "判据必须是「≥2 个不同库位」，不是「≥2 个场景」"
    )
    assert "v.state = 'published'" in fn_body
    missing = [name for name in _FN_COLUMNS if name not in fn_body]
    assert not missing, f"RETURNS TABLE 里缺少列：{missing}"
    assert "RETURNS TABLE (" in fn_body
    assert "LANGUAGE sql STABLE" in fn_body
    # 函数体必须用 `$$;` 收尾 —— 漏掉它 PG 会报 unterminated dollar-quoted string
    # （离线渲染看不出这个错，但文本断言能看出收尾丢了）
    assert "HAVING count(DISTINCT db_key) > 1; $$;" in fn_body
    # DO 块同样必须闭合
    assert "END $$;" in body


def test_upgrade_asserts_db_binding_check_still_exists(upgrade_sql):
    """只读前置断言：CHECK 缺失/口径被改时迁移必须失败（不是一句注释）。"""
    body = _squash(upgrade_sql)
    assert "pg_get_constraintdef" in body
    assert "ck_scenarios_db_binding_format" in body
    assert "RAISE EXCEPTION" in body
    # 断言段必须在建视图之前：先验证前提，再依赖它
    assert body.find("ck_scenarios_db_binding_format") < body.find(
        "CREATE OR REPLACE VIEW v_scenario_db_routing AS"
    )


def test_0030_creates_no_constraints(upgrade_sql):
    """0030 **不产生任何约束** —— 因此 `_PG_CONSTRAINT_SOURCES` 不需要追加注册。"""
    assert "ADD CONSTRAINT" not in upgrade_sql
    assert "CREATE TABLE" not in upgrade_sql
    assert "ALTER TABLE" not in upgrade_sql
    assert "CREATE UNIQUE INDEX" not in upgrade_sql
    assert "CREATE INDEX" not in upgrade_sql
    assert "CREATE TRIGGER" not in upgrade_sql


def test_downgrade_drops_all_three_objects(downgrade_sql):
    """回滚顺序：先删包装视图，再删路由视图，最后删函数（被依赖者最后删）。"""
    body = _squash(downgrade_sql)
    conflicts_view = body.find("DROP VIEW IF EXISTS v_kb_db_binding_conflicts")
    routing_view = body.find("DROP VIEW IF EXISTS v_scenario_db_routing")
    function = body.find("DROP FUNCTION IF EXISTS fn_kb_db_binding_conflicts()")
    assert conflicts_view >= 0 and routing_view >= 0 and function >= 0, "降级语句缺失"
    assert conflicts_view < routing_view < function
    # 纯 DDL 回滚：绝不碰业务数据
    for keyword in ("DELETE FROM", "TRUNCATE", "DROP TABLE", "DROP COLUMN"):
        assert keyword not in downgrade_sql


def test_migration_is_linear_single_head(upgrade_sql):
    """单一线性链：渲染里只有 0029 → 0030 一段。"""
    assert "-- Running upgrade 0029 -> 0030" in upgrade_sql
    assert upgrade_sql.count("-- Running upgrade") == 1


def test_alembic_heads_is_0030_only():
    """`alembic heads` 只有一个头 0030 —— 两个头会让 `upgrade head` 直接报错。

    ★ 读的是**脚本目录**（不连库），所以这条在任何机器上都能跑。
    """
    script = ScriptDirectory.from_config(alembic_config())
    assert script.get_heads() == ["0030"], (
        f"迁移链出现分叉或多个头：{script.get_heads()}（`alembic upgrade head` 会失败）"
    )


def test_migration_file_documents_the_no_constraint_decision():
    """迁移文件 docstring 必须写明「不建约束 ⇒ 不必登记」的原因。"""
    source = _MIGRATION_FILE.read_text(encoding="utf-8")
    assert "_PG_CONSTRAINT_SOURCES" in source
    assert "不需要" in source


def test_migration_file_adds_no_column_to_knowledge_bases():
    """0030 不得给 knowledge_bases 加列（DQ5-1：scenarios.db_binding 单点权威）。

    ★ 只断言**DDL 形态**，不断言文件名里不出现 `knowledge_bases`：
      docstring 里正面写了「不给 knowledge_bases 加库位列」这条裁决，
      把它一并禁掉会让「解释为什么」变成不可能。
    """
    source = _MIGRATION_FILE.read_text(encoding="utf-8")
    assert "ALTER TABLE knowledge_bases" not in source
    assert "sa.Column" not in source
    assert "add_column" not in source


# ══════════════════════════════════════════════════════════════════════
# Real-DB 层：对象存在性 / 行数恒等 / 冲突健康态 / 往返
# ══════════════════════════════════════════════════════════════════════


def _run(coro):
    return asyncio.run(coro)


async def _objects() -> tuple[bool, bool, bool, list[str]]:
    """`(路由视图存在, 冲突视图存在, 函数存在, 函数返回列名)`。

    列名从 `pg_get_function_result()` 解析（与 0029 同一写法）：它的输出就是
    `TABLE(tenant_id uuid, kb_id text, db_keys text[], scene_count bigint)`，直接可读。
    """
    connection = await asyncpg.connect(dsn(SCENARIO_TEST_DB_URL))
    try:
        routing = await connection.fetchval(
            "SELECT to_regclass('public.v_scenario_db_routing')"
        )
        conflicts = await connection.fetchval(
            "SELECT to_regclass('public.v_kb_db_binding_conflicts')"
        )
        function = await connection.fetchval(
            "SELECT to_regprocedure('fn_kb_db_binding_conflicts()')"
        )
        result = await connection.fetchval(
            "SELECT pg_get_function_result("
            "  to_regprocedure('fn_kb_db_binding_conflicts()'))"
        )
        inner = (result or "").removeprefix("TABLE(").rstrip(")")
        columns = [part.strip().split()[0] for part in inner.split(",") if part.strip()]
        return routing is not None, conflicts is not None, function is not None, columns
    finally:
        await connection.close()


async def _counts() -> tuple[int, int, int, int]:
    """`(路由视图行数, scenarios 行数, 冲突行数, 非默认库位场景数)`。"""
    connection = await asyncpg.connect(dsn(SCENARIO_TEST_DB_URL))
    try:
        routing_rows = int(await connection.fetchval("SELECT count(*) FROM v_scenario_db_routing"))
        scenario_rows = int(await connection.fetchval("SELECT count(*) FROM scenarios"))
        conflict_rows = int(
            await connection.fetchval("SELECT count(*) FROM v_kb_db_binding_conflicts")
        )
        bound_rows = int(
            await connection.fetchval(
                "SELECT count(*) FROM scenarios WHERE db_binding IS NOT NULL"
            )
        )
        return routing_rows, scenario_rows, conflict_rows, bound_rows
    finally:
        await connection.close()


def test_real_db_objects_exist_with_column_contract(schema):
    """真库上三个对象都在，且冲突函数的 4 个返回列顺序与 DDL 一致。"""
    migrate("head")
    routing, conflicts, function, columns = _run(_objects())
    assert routing, "真库上没有 v_scenario_db_routing 视图"
    assert conflicts, "真库上没有 v_kb_db_binding_conflicts 视图"
    assert function, "真库上没有 fn_kb_db_binding_conflicts()"
    assert tuple(columns) == _FN_COLUMNS, f"返回列顺序与契约不一致：{columns}"


def test_real_db_routing_view_row_count_equals_scenarios(schema):
    """行数恒等式：`count(v_scenario_db_routing) == count(scenarios)`（I-5-8）。"""
    migrate("head")
    routing_rows, scenario_rows, _conflict_rows, _bound_rows = _run(_counts())
    assert routing_rows == scenario_rows, (
        f"路由视图行数 {routing_rows} != scenarios 行数 {scenario_rows}"
        "（LEFT JOIN 被改成 JOIN，或唯一 published 索引失效导致行数被放大）"
    )
    # 同时断言视图列语义：未绑定行必须是虚拟键 'default'，不是 NULL
    default_mismatch = _run(_default_key_mismatch())
    assert default_mismatch == 0, f"{default_mismatch} 行的 db_key 不是 'default'（COALESCE 丢了）"


async def _default_key_mismatch() -> int:
    connection = await asyncpg.connect(dsn(SCENARIO_TEST_DB_URL))
    try:
        return int(
            await connection.fetchval(
                "SELECT count(*) FROM v_scenario_db_routing "
                "WHERE db_binding_raw IS NULL "
                "  AND (db_key <> 'default' OR db_key_source <> 'default' OR NOT is_default)"
            )
        )
    finally:
        await connection.close()


def test_real_db_conflict_view_is_empty_when_all_default(schema):
    """健康态：全部场景未绑定非默认库位 ⇒ 冲突视图 0 行（§A.3.1.3）。"""
    migrate("head")
    _routing, _scenarios, conflict_rows, bound_rows = _run(_counts())
    assert bound_rows == 0, f"测试库里已有 {bound_rows} 行非默认库位，健康态前提被破坏"
    assert conflict_rows == 0, f"全默认库位的健康态下冲突视图返回了 {conflict_rows} 行"


def test_real_db_downgrade_removes_then_upgrade_restores(schema):
    """`downgrade 0029` 干净回滚（三个对象都消失），再 `upgrade head` 复原。

    ★ 往返后**必须再推回 head**：本目录其余用例依赖这些对象存在，
      不复原会让整批用例以「对象不存在」的形式红，而那与它们要测的东西无关。
    """
    migrate("head")
    assert all(_run(_objects())[:3])

    migrate("0029", downgrade=True)
    routing, conflicts, function, _columns = _run(_objects())
    assert not routing, "downgrade 0029 后路由视图仍存在（回滚不干净）"
    assert not conflicts, "downgrade 0029 后冲突视图仍存在（回滚不干净）"
    assert not function, "downgrade 0029 后冲突函数仍存在（回滚不干净）"

    migrate("head")
    routing, conflicts, function, columns = _run(_objects())
    assert routing and conflicts and function, "重新 upgrade 后对象没有复原"
    assert tuple(columns) == _FN_COLUMNS
