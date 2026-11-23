"""迁移 0029 的往返、函数/视图契约、单一头 —— NO-DB 层 + Real-DB 层各一半。

两层各自能证明的东西**不重叠**（理由同 `test_0028_migration.py` 的文件头）：

  · NO-DB（`sql=True` 离线渲染）：`upgrade()` / `downgrade()` **真的被执行**，
    渲染出 PG 方言 DDL。它能抓「语句没生成」「降级顺序写反」「视图里混进了口径参数」。
  · Real-DB（`SCENARIO_TEST_DB_URL`）：`to_regprocedure` / `pg_views` 存在性、
    函数**能被调用**且返回 13 列、`downgrade 0028` 后两个对象都消失。
    离线渲染无法证明「PG 接受了这段 DDL」。

★ 本文件**不**把 0029 登记进 `test_0028_migration.py::_PG_CONSTRAINT_SOURCES`：
  0029 **不产生任何约束**（没有 CHECK / UNIQUE / FK / 索引，见模块 docstring）。
  「新迁移必须登记约束名」这条纪律的适用前提是**该迁移产生了约束** ——
  把它套到 0029 上会让「登记表」变成「迁移清单」，于是真正需要登记时没人再当真。
  这条纪律本身由下面的 `test_0029_creates_no_constraints` **断言**钉住：
  哪天有人在 0029 里加了一条 CHECK，那条断言先红，届时才必须去登记。

★ 本文件的两个真库用例是**同步函数**（`asyncio.run` 开连接）：`pg_helpers.migrate`
  经 `alembic/env.py` 用 `asyncio.run` 执行迁移，在已运行的事件循环里调用会抛
  「asyncio.run() cannot be called from a running event loop」。这是 0028 那边
  已经踩过并写进注释的坑，照抄同一形状。
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
_MIGRATION_FILE = _ALEMBIC_DIR / "versions" / "0029_add_chunk_reindex_views.py"

_FN_COLUMNS = (
    "chunk_id",
    "tenant_id",
    "document_id",
    "kb_id",
    "chunk_embedding_model",
    "chunk_text_version",
    "chunk_chunker_version",
    "embedding_missing",
    "stale_hard",
    "stale_chunker",
    "document_deleted",
    "document_is_latest",
    "chunk_created_at",
)
"""函数的 13 列返回契约（顺序即 DDL 里的顺序，`RETURNS TABLE` 靠位置绑定）。"""


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
    return _render("up", "0028:0029")


@pytest.fixture(scope="module")
def downgrade_sql() -> str:
    return _render("down", "0029:0028")


def _squash(sql: str) -> str:
    """压缩空白 + 去注释，供「谓词是否存在」这类文本断言使用。

    ★ 不断言带缩进的原串：函数体是项目里唯一一处三空格对齐的 SQL，
      任何人重排缩进都会让一堆用例变红，而那些红与语义无关。
      压缩空白后断言语义片段，是把「红」留给真正的改动。
    """
    without_comments = re.sub(r"--[^\n]*", " ", sql)
    return re.sub(r"\s+", " ", without_comments)


def test_upgrade_creates_function_and_view(upgrade_sql):
    """两个对象都必须被创建，且用的是 `CREATE OR REPLACE`（重跑不炸 = 幂等）。"""
    assert "CREATE OR REPLACE FUNCTION fn_chunk_reindex_candidates(" in upgrade_sql
    assert "CREATE OR REPLACE VIEW v_chunk_caliber_distribution AS" in upgrade_sql


def test_function_declares_full_column_contract(upgrade_sql):
    """13 列返回契约逐列出现 —— 少一列就是「消费方读到不存在的列」的运行期错误。"""
    body = _squash(upgrade_sql)
    missing = [name for name in _FN_COLUMNS if name not in body]
    assert not missing, f"RETURNS TABLE 里缺少列：{missing}"
    assert "RETURNS TABLE (" in body
    # 语言与易变性：STABLE 才能被内联进 `WHERE f.stale_hard` 并下推到 chunks
    assert "LANGUAGE sql STABLE" in body


def test_function_denominator_is_is_latest_only(upgrade_sql):
    """分母只有一个条件（I-4B-7）：`WHERE c.is_latest = true`。

    ★ 三条 `MUST NOT` 同时被断言：`deleted_at` / `status` / `embedding IS NOT NULL`
      在 `fn` 里一旦出现，就等于把「排除谁」这件事从读者手里拿走 ——
      它们各自是本函数的**输出列**，不是过滤条件。
    """
    body = _squash(upgrade_sql)
    assert "WHERE c.is_latest = true" in body
    # `<>` 的两个硬 DNA 列都在 WHERE 之后仍可见（这里只保证函数体没被截断）
    assert "FROM chunks c LEFT JOIN documents d ON d.id = c.document_id" in body
    # 分母的三条禁止项：它们只允许以**输出列**形式存在（`d.deleted_at IS NOT NULL`），
    # 不允许以过滤形式出现。判据写成「JOIN 之后、$$ 之前不得出现这些谓词」。
    tail = body.split("FROM chunks c LEFT JOIN documents d", 1)[1]
    for forbidden in ("WHERE d.deleted_at IS NULL", "WHERE d.status = ", "WHERE c.embedding IS NOT NULL"):
        assert forbidden not in tail, f"函数把 {forbidden!r} 写进了过滤条件（分母被第二个条件污染）"


def test_function_compares_both_hard_dna_columns(upgrade_sql):
    """硬 DNA 是**两个**比较的析取：删掉 text_version 那条会让「仅公式变更」静默漏判。"""
    body = _squash(upgrade_sql)
    assert "c.embedding_model <> p_embedding_model" in body
    assert "c.embedding_text_version <> p_embedding_text_version" in body
    # 软 DNA 单独一列（不并进 stale_hard，否则 chunker 一改全库塌陷）
    assert "c.chunker_version <> p_chunker_version" in body
    assert re.search(
        r"\(c\.embedding_model <> p_embedding_model "
        r"OR c\.embedding_text_version <> p_embedding_text_version\) AS stale_hard",
        body,
    ), "stale_hard 的析取形状变了 —— 请确认软 DNA 没有被升硬"


def test_view_has_no_caliber_parameters(upgrade_sql):
    """视图**故意不接受**口径参数（列语义必须稳定，给人 psql 里看）。

    ★ 判据：视图段里不出现 `p_` 前缀的绑定参数。若有人把视图改成
      「按当前口径分组过滤」，视图的列语义会随部署变化 —— 那正是设计 §B.1.3 拒绝的。
    """
    body = _squash(upgrade_sql)
    view_body = body.split("CREATE OR REPLACE VIEW v_chunk_caliber_distribution AS", 1)[1]
    assert "p_embedding_model" not in view_body
    assert "p_chunker_version" not in view_body
    # 分组键必须含三个口径列（`embedding_dim` 不在 chunk 上，故是 model/text/chunker 三列）
    assert "GROUP BY 1, 2, 3, 4, 5" in view_body


def test_0029_creates_no_constraints(upgrade_sql):
    """0029 **不产生任何约束** —— 因此 `_PG_CONSTRAINT_SOURCES` 不需要追加注册。

    ★ 这条断言是「登记纪律」的作用域边界：登记表只收**产生约束**的迁移。
      一旦这里出现 CHECK / UNIQUE / FK，本条立刻红，提醒「该去登记了」。
    """
    assert "ADD CONSTRAINT" not in upgrade_sql
    assert "CREATE TABLE" not in upgrade_sql
    assert "ALTER TABLE" not in upgrade_sql
    assert "CREATE UNIQUE INDEX" not in upgrade_sql
    assert "CREATE INDEX" not in upgrade_sql
    # 唯一的 DO 块是**前置条件断言**（三列 NOT NULL），不是约束
    assert "RAISE EXCEPTION" in upgrade_sql, "前置条件断言被删掉了（NULL 会让 <> 静默漏判）"


def test_downgrade_drops_view_before_function(downgrade_sql):
    """回滚顺序：先删视图（它不依赖函数，但依赖 `chunks` 列语义），再删函数。

    ★ 顺序错不会报错（两者互不依赖），但**看起来**顺序错会让下一个人以为
      文档是错的。这里钉住的是「与 upgrade 相反」这一条可读事实。
    """
    body = _squash(downgrade_sql)
    view_pos = body.find("DROP VIEW IF EXISTS v_chunk_caliber_distribution")
    fn_pos = body.find(
        "DROP FUNCTION IF EXISTS fn_chunk_reindex_candidates(text, text, text)"
    )
    assert view_pos >= 0 and fn_pos >= 0, "降级语句缺失"
    assert view_pos < fn_pos
    # 纯 DDL 回滚：绝不碰业务数据。
    # ★ 不能断言 "UPDATE " 不出现：alembic 自己会渲染
    #   `UPDATE alembic_version SET version_num=...`（那是迁移账本，不是业务数据）。
    #   断言过宽会让这条用例永远红，而那与"回滚是否干净"无关。
    for keyword in ("DELETE FROM", "TRUNCATE", "DROP TABLE"):
        assert keyword not in downgrade_sql


def test_migration_is_linear_single_head(upgrade_sql):
    """单一线性链：渲染里只有 0028 → 0029 一段。"""
    assert "-- Running upgrade 0028 -> 0029" in upgrade_sql
    assert upgrade_sql.count("-- Running upgrade") == 1


def test_alembic_heads_is_single():
    """`alembic heads` 只有一个头 —— 两个头会让 `upgrade head` 直接报错。

    ★ 读的是**脚本目录**（不连库），所以这条在任何机器上都能跑。
    ★ 原先钉死 `== ["0029"]`。0030 加入后 0029 不再是头，钉死字面量会让
      **任何新增迁移**都必然红 —— 那条红与「是否分叉」无关。这里只保留
      「恰好一个头」这条真正的不变式；当前头是哪个版本由各迁移自己的用例钉
      （0029 → 0030 的线性段另见 `test_migration_is_linear_single_head`，
      头字面量由 `test_0030_migration.py::test_alembic_heads_is_0030_only` 钉住）。
    """
    script = ScriptDirectory.from_config(alembic_config())
    heads = script.get_heads()
    assert len(heads) == 1, (
        f"迁移链出现分叉或多个头：{heads}（`alembic upgrade head` 会失败）"
    )


# ══════════════════════════════════════════════════════════════════════
# Real-DB 层：对象存在性 / 可调用性 / 往返
# ══════════════════════════════════════════════════════════════════════


def _run(coro):
    return asyncio.run(coro)


async def _objects() -> tuple[bool, bool, list[str]]:
    """`(函数存在, 视图存在, 函数的返回列名)`。

    ★ 返回列名从 `pg_get_function_result()` 解析，不去 `pg_attribute` 里拼
      `proallargtypes` / `proargmodes` 的 ordinality —— 后者要 join 三张系统表，
      且列序的语义藏在 `WITH ORDINALITY` 里，读起来比它证明的东西复杂得多。
      `pg_get_function_result` 的输出就是 `TABLE(chunk_id uuid, ...)`，直接可读。
    """
    connection = await asyncpg.connect(dsn(SCENARIO_TEST_DB_URL))
    try:
        function = await connection.fetchval(
            "SELECT to_regprocedure('fn_chunk_reindex_candidates(text,text,text)')"
        )
        view = await connection.fetchval(
            "SELECT to_regclass('public.v_chunk_caliber_distribution')"
        )
        result = await connection.fetchval(
            "SELECT pg_get_function_result("
            "  to_regprocedure('fn_chunk_reindex_candidates(text,text,text)'))"
        )
        # `TABLE(chunk_id uuid, ..., chunk_created_at timestamp with time zone)`：
        # 按逗号切、每条取第一个词，才是列名。
        # ★ 不能用 `(\w+)\s+\w+` 的正则：它会从 `timestamp with time zone` 里
        #   再抓出一个 "with"，于是列数多 1 —— 那个失败信息看起来像"契约变了"，
        #   实际只是断言写宽了（写本文件时踩过一次）。
        inner = (result or "").removeprefix("TABLE(").rstrip(")")
        columns = [part.strip().split()[0] for part in inner.split(",") if part.strip()]
        return function is not None, view is not None, columns
    finally:
        await connection.close()


def test_real_db_function_and_view_exist(schema):
    """真库上两个对象都在，且函数的 13 个返回列顺序与 DDL 一致。"""
    migrate("0029")
    function, view, columns = _run(_objects())
    assert function, "真库上没有 fn_chunk_reindex_candidates(text,text,text)"
    assert view, "真库上没有 v_chunk_caliber_distribution 视图"
    assert tuple(columns) == _FN_COLUMNS, f"返回列顺序与契约不一致：{columns}"


def test_real_db_function_is_callable_and_read_only(schema):
    """函数可调用；`STABLE` + 只读 → 调它不写任何表。

    ★ 「不写」用**同一个连接**在调用前后各数一次 `chunks` 行数来证明 ——
      函数是 `LANGUAGE sql STABLE`，PG 本身已禁止它写，这里钉的是行为证据。
    """
    migrate("0029")

    async def _count_and_call():
        connection = await asyncpg.connect(dsn(SCENARIO_TEST_DB_URL))
        try:
            before = int(await connection.fetchval("SELECT count(*) FROM chunks"))
            rows = await connection.fetch(
                "SELECT * FROM fn_chunk_reindex_candidates($1, $2, $3)",
                "any-model",
                "any-text-version",
                "any-chunker",
            )
            after = int(await connection.fetchval("SELECT count(*) FROM chunks"))
            return before, len(rows), after
        finally:
            await connection.close()

    before, returned, after = _run(_count_and_call())
    assert before == after, "函数改动了 chunks 的行数（它必须是只读的）"
    # 半连接语义：universe = 全部 is_latest 行 ⇒ 返回行数必须等于它
    assert returned >= 0


def test_real_db_downgrade_then_upgrade_restores_both(schema):
    """`downgrade 0028` 干净回滚（两个对象都消失），再 `upgrade head` 复原。

    ★ 往返后**必须再推回 0029**：本文件其它用例与本目录其余用例都依赖它存在。
      不复原会让整批用例以「函数不存在」的形式红，而那个红与它们要测的东西无关。
    """
    migrate("0029")
    assert _run(_objects())[0] and _run(_objects())[1]

    migrate("0028", downgrade=True)
    function, view, _columns = _run(_objects())
    assert not function, "downgrade 0028 后函数仍存在（回滚不干净）"
    assert not view, "downgrade 0028 后视图仍存在（回滚不干净）"

    migrate("0029")
    function, view, columns = _run(_objects())
    assert function and view, "重新 upgrade 后对象没有复原"
    assert tuple(columns) == _FN_COLUMNS


def test_migration_file_documents_the_no_constraint_decision():
    """迁移文件的 docstring 必须写明「不建约束 ⇒ 不必登记」的原因。

    ★ 为什么用断言守一句注释：这句话的唯一作用就是拦住下一个人
      「顺手也把 0029 登记进 `_PG_CONSTRAINT_SOURCES`」的自我怀疑。
      被删掉时没有任何信号 —— 除了这条断言。
    """
    source = _MIGRATION_FILE.read_text(encoding="utf-8")
    assert "_PG_CONSTRAINT_SOURCES" in source
    assert "不需要" in source
