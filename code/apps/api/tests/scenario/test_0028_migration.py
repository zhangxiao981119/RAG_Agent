"""F.1（NO-DB 层）：`upgrade()` / `downgrade()` **真跑**，断言渲染出的 DDL。

★ 为什么用 alembic 离线模式（`sql=True`）而不是连 PG：
  它**真的执行** `upgrade()` / `downgrade()` 的函数体（`op.create_table` 等全部走到
  编译阶段并渲染成 PG 方言 SQL），只是不建立连接。因此它能抓到：
    · 约束名重复 / 引用了不存在的表（编译期报错）；
    · 种子 INSERT 的绑定参数渲染失败（离线模式必须 literal_binds）；
    · 降级顺序写反（先删表后删索引 → 真库上报错，这里能看出顺序）。
  真正的 PG 语义（CHECK 求值、部分唯一索引、FK RESTRICT）由
  `test_0028_pg_constraints.py` 在 `SCENARIO_TEST_DB_URL` 存在时验证。

★ 注意这不是「伪称已运行」：本文件断言的是**真实产物**（渲染出的 SQL 文本），
  并且 `upgrade()` / `downgrade()` 确实被调用过；未验证的部分（PG 求值）在报告里
  逐条列出。
"""
from __future__ import annotations

import ast
import contextlib
import io
import pathlib

import pytest

from alembic import command
from alembic.config import Config

_API_ROOT = pathlib.Path(__file__).resolve().parents[2]
_ALEMBIC_DIR = _API_ROOT / "alembic"


def _render(direction: str, revision_range: str) -> str:
    """离线渲染迁移 SQL。

    刻意用**不带 ini 文件**的 `Config()`：`alembic.ini` 里的 `fileConfig()` 会重配
    全局 logging，污染其它用例的 caplog。env.py 已用
    `if config.config_file_name is not None` 守住这条分支。
    """
    config = Config()
    config.set_main_option("script_location", str(_ALEMBIC_DIR))
    # 离线模式不建连，URL 只用于解析方言（PG 方言决定 DDL 的写法）
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
    return _render("up", "0027:0028")


@pytest.fixture(scope="module")
def downgrade_sql() -> str:
    return _render("down", "0028:0027")


def test_upgrade_creates_both_tables(upgrade_sql):
    assert "CREATE TABLE scenarios (" in upgrade_sql
    assert "CREATE TABLE scenario_versions (" in upgrade_sql


def test_upgrade_creates_all_named_constraints(upgrade_sql):
    """§A 的字段级约束**逐条**出现在 DDL 里（少一条就是漏了失败态）。"""
    expected = [
        "ck_scenarios_status",
        "ck_scenarios_key_format",
        "ck_scenarios_name_len",
        "ck_scenarios_db_binding_format",
        "uq_scenarios_tenant_key",
        "ck_scenario_versions_state",
        "ck_scenario_versions_version_positive",
        "ck_scenario_versions_binding_mode",
        "ck_scenario_versions_policy_object",        # C-A1
        "ck_scenario_versions_policy_keyset",        # C-A2
        "ck_scenario_versions_policy_recall_top_k",  # C-A3
        "ck_scenario_versions_policy_threshold_vector",   # C-A5
        "ck_scenario_versions_policy_threshold_rerank",   # C-A5
        "ck_scenario_versions_policy_rerank_bool",   # C-A4
        "ck_scenario_versions_binding_consistent",   # C-B1
        "ck_scenario_versions_kb_ids_strings",       # C-B2
        "ck_scenario_versions_dna_object",
        "ck_scenario_versions_dna_required_keys",
        "ck_scenario_versions_dna_keyset",
        "ck_scenario_versions_checksum_format",
        "ck_scenario_versions_state_timestamps",     # C-V1
        "uq_scenario_versions_scenario_version",
    ]
    missing = [name for name in expected if name not in upgrade_sql]
    assert not missing, f"DDL 里缺少约束：{missing}"


def test_upgrade_creates_partial_unique_indexes(upgrade_sql):
    """两条「聚合唯一」约束只能靠部分唯一索引表达，且 WHERE 子句必须在。"""
    assert "CREATE UNIQUE INDEX uq_scenarios_one_builtin ON scenarios (tenant_id) WHERE is_builtin" in upgrade_sql
    assert (
        "CREATE UNIQUE INDEX uq_scenario_versions_one_published ON scenario_versions "
        "(scenario_id) WHERE state = 'published'"
    ) in upgrade_sql
    assert "CREATE INDEX idx_scenario_versions_lookup" in upgrade_sql


def test_upgrade_adds_scenario_id_with_restrict_fk(upgrade_sql):
    """绑定列：可空 + ON DELETE RESTRICT + (tenant_id, scenario_id) 索引。"""
    assert "ALTER TABLE integration_applications ADD COLUMN scenario_id UUID;" in upgrade_sql
    assert (
        "ALTER TABLE integration_applications ADD CONSTRAINT fk_integration_app_scenario "
        "FOREIGN KEY(scenario_id) REFERENCES scenarios (id) ON DELETE RESTRICT"
    ) in upgrade_sql
    assert (
        "CREATE INDEX idx_integration_app_scenario ON integration_applications "
        "(tenant_id, scenario_id)"
    ) in upgrade_sql
    # 不回填（§A.4）：DDL 里 MUST NOT 出现 UPDATE integration_applications
    assert "UPDATE integration_applications" not in upgrade_sql


def test_upgrade_seed_sql_is_idempotent_and_scoped_to_tenants(upgrade_sql):
    """种子语句：从 tenants 出发、冲突即跳过、只写新建的两张表。"""
    assert "FROM tenants t" in upgrade_sql
    assert "ON CONFLICT (tenant_id, scenario_key) DO NOTHING" in upgrade_sql
    assert "'default'" in upgrade_sql
    assert "'内置默认场景'" in upgrade_sql
    assert "'unrestricted'" in upgrade_sql
    # 种子行 checksum 由 Python 计算后以字面量落库（保证与运行时同源）
    from tests.scenario.conftest import load_migration

    assert load_migration()._SEED_CHECKSUM in upgrade_sql


def test_downgrade_drops_in_reverse_dependency_order(downgrade_sql):
    """先删依赖（索引 / 外键 / 列），再删表；两张表的删除顺序也是子表在前。"""
    order = [
        "DROP INDEX idx_integration_app_scenario",
        "DROP CONSTRAINT fk_integration_app_scenario",
        "DROP COLUMN scenario_id",
        "DROP INDEX idx_scenario_versions_lookup",
        "DROP INDEX uq_scenario_versions_one_published",
        "DROP TABLE scenario_versions",
        "DROP INDEX uq_scenarios_one_builtin",
        "DROP TABLE scenarios",
    ]
    positions = [downgrade_sql.find(item) for item in order]
    assert all(pos >= 0 for pos in positions), dict(zip(order, positions))
    assert positions == sorted(positions), "降级顺序与依赖方向相反，真库上会失败"
    # 降级只动本迁移创建的对象：不碰任何业务表的数据
    for keyword in ("UPDATE chunks", "UPDATE documents", "DELETE FROM", "TRUNCATE"):
        assert keyword not in downgrade_sql


def test_migration_is_linear_no_branching(upgrade_sql):
    """单一线性链：渲染里只有 0027 → 0028 一段，没有 branch 头。"""
    assert "-- Running upgrade 0027 -> 0028" in upgrade_sql
    assert upgrade_sql.count("-- Running upgrade") == 1


# ══════════════════════════════════════════════════════════════════════
# 守护「只能靠 PG 跑的那批用例」不会退化成假绿（NO-DB 层能做的那一半）
# ══════════════════════════════════════════════════════════════════════
#
# `test_0028_pg_constraints.py` 里的负向用例断言的是**字面量约束名**，
# 而那些用例只在 `SCENARIO_TEST_DB_URL` 存在时才真跑。这就留下一个盲区：
# 约束改名 / 名字打错之后，测试在**没有 PG 的机器上永远是绿的**，
# 只有在有 PG 的机器上才红 —— 而那种机器可能已经不存在了。
# 下面两条把盲区里能静态验证的部分先钉死：
#   1. 被钉的每个约束名都必须在渲染出的 DDL 里真实存在；
#   2. 整个文件只允许有**一个** `pytest.raises`（在 `_expect_violation` 里），
#      即不存在「断言任意 CheckViolation」的旁路。

_PG_TEST_FILE = pathlib.Path(__file__).resolve().parent / "test_0028_pg_constraints.py"
_PG_HELPERS_FILE = pathlib.Path(__file__).resolve().parent / "pg_helpers.py"
_PG_STEP4_CONSTRAINT_FILE = pathlib.Path(__file__).resolve().parent / "test_step4_constraints.py"

# ★ 为什么是**一组**文件而不是一个：Step 4A 把夹具与 `expect_violation` 提取到了
#   `pg_helpers.py`（DQ4-5），约束用例也新增了 `test_step4_constraints.py`。
#   若守卫仍只扫 `test_0028_pg_constraints.py`，那么在**新文件**里写
#   `pytest.raises(CheckViolationError)` 这条最危险的回归就无人拦 ——
#   守卫的覆盖面必须跟着「口径的物理位置」走，否则它保护的是一个已经不存在的世界。
_PG_CONSTRAINT_SOURCES = (_PG_TEST_FILE, _PG_HELPERS_FILE, _PG_STEP4_CONSTRAINT_FILE)


def _pg_sources() -> list[pathlib.Path]:
    """实际存在的约束用例源文件（新文件未创建时不报错）。"""
    return [path for path in _PG_CONSTRAINT_SOURCES if path.exists()]



def _pinned_constraint_names() -> list[str]:
    """抽出 `expect_violation(conn, exc, "<约束名>", action)` 里写死的约束名。

    ★ 同时认 `_expect_violation`（`test_0028_pg_constraints.py` 里的 import 别名）
      与 `expect_violation`（`pg_helpers.py` 与 Step 4A 新文件用的原名）：
      只认一个名字的话，换了名字的那批用例会**从统计里静默消失**，
      `assert names` 仍在（有别的文件撑着），于是缺的那批没人发现。
    """
    names: list[str] = []
    for path in _pg_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", None) in {"_expect_violation", "expect_violation"}
            ):
                continue
            if len(node.args) >= 3 and isinstance(node.args[2], ast.Constant):
                names.append(node.args[2].value)
            for keyword in node.keywords:
                if keyword.arg == "constraint" and isinstance(keyword.value, ast.Constant):
                    names.append(keyword.value.value)
    return names


def test_pg_tests_pin_constraint_names_that_really_exist(upgrade_sql):
    """PG 用例钉的每个约束名都必须真实存在于 DDL —— 否则那批用例的"红"是假的。"""
    names = _pinned_constraint_names()
    assert names, "没抽到任何约束名：PG 用例的断言形态变了，本守卫需同步"
    missing = sorted({name for name in names if name not in upgrade_sql})
    assert not missing, f"PG 用例钉了 DDL 里不存在的约束：{missing}"


def test_pg_tests_have_no_unnamed_violation_assertions():
    """约束用例的全部源文件里只允许**一处** `pytest.raises`（在 `expect_violation` 内）。

    ★ 这是修 1 的根因守卫：缺陷形态是「`pytest.raises(CheckViolationError)` +
      只给部分列 INSERT」—— 先撞上 `ck_scenario_versions_binding_consistent`，
      目标约束删掉也照样绿。只要有人再写一句裸 `pytest.raises`，
      约束名就不再是被验证的对象，这条会红。
    ★ 扫描范围是 `_pg_sources()`（0028 用例 + `pg_helpers` + Step 4A 约束用例）：
      Step 4A 把 `raises` 挪进了 `pg_helpers.py`，只扫 0028 那个文件会让
      「在 helpers 里加第二个裸 raises」变成完全无人拦的退化。
    """
    raises_calls: list[ast.Call] = []
    helper_found = False
    for path in _pg_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        raises_calls.extend(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "attr", None) == "raises"
            and getattr(node.func, "value", None) is not None
            and getattr(node.func.value, "id", None) == "pytest"
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name in {
                "_expect_violation",
                "expect_violation",
            }:
                helper = node
                helper_found = True
    assert helper_found, "没找到 expect_violation 的实现（口径函数被改名/删除）"
    assert len(raises_calls) == 1, (
        f"发现 {len(raises_calls)} 处 pytest.raises；负向用例必须统一走 "
        "`expect_violation`（它会断言 constraint_name）"
    )

    outer = raises_calls[0]
    assert any(node is outer for node in ast.walk(helper)), (
        "唯一的 pytest.raises 不在 expect_violation 里"
    )


# ══════════════════════════════════════════════════════════════════════
# env.py 的连接串来源：显式注入优先，应用配置兜底
# ══════════════════════════════════════════════════════════════════════


def test_env_py_honors_injected_url_before_app_settings():
    """`alembic/env.py` MUST NOT 无条件用 `get_settings().database_url` 覆盖调用方。

    ★ 这是真事故未遂的守卫：原先那行
        `config.set_main_option("sqlalchemy.url", get_settings().database_url)`
      让测试 fixture 的 `set_main_option(SCENARIO_TEST_DB_URL)` 变成死代码 ——
      `_migrate("0027", downgrade=True)` 实际打的是 `.env` 指向的库。
      只要 `.env` 指向一个能连通的库，跑一次 PG 用例就会把它**降级到 0027**。
      当时没出事纯属巧合（`.env` 写的是容器内的 `@postgres:5432`，宿主机解析不了）。
    ★ 断言形态（AST，不用正则猜）：
      ① 全文件 `set_main_option("sqlalchemy.url", ...)` **恰好 1 处**（多一处就是第二条真源）；
      ② 该处的值是 `X or get_settings().database_url` 形式（`or` ⇒ 显式注入优先）；
      ③ `X` 必须来自 `config.attributes.get("sqlalchemy_url")`（alembic 官方通道，
         不是自造的环境变量协议）；
      ④ 兜底里仍有 `get_settings().database_url` —— 否则 CLI 直跑（不传 attributes）
         会因为 url 为空而失败，生产路径被测试需求破坏。
    """
    source = (_ALEMBIC_DIR / "env.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    url_writes: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "attr", None) != "set_main_option":
            continue
        if node.args and isinstance(node.args[0], ast.Constant) and (
            node.args[0].value == "sqlalchemy.url"
        ):
            url_writes.append(node)

    assert len(url_writes) == 1, (
        f"`sqlalchemy.url` 的赋值必须唯一（防止出现第二条真源），实际 {len(url_writes)} 处"
    )
    call = url_writes[0]
    assert len(call.args) >= 2, "set_main_option 缺少值参数"

    value = call.args[1]
    assert isinstance(value, ast.BoolOp) and isinstance(value.op, ast.Or), (
        "必须是 `注入值 or 兜底值` 的形式；直接写 get_settings().database_url 就是无条件覆盖"
    )

    # `_injected_url or ...` 里的 `_injected_url` 本身要能追到 attributes.get
    assigned: dict[str, ast.expr] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigned[target.id] = node.value

    def _resolve(node: ast.expr, depth: int = 0) -> str:
        """把 Name 追到它的赋值表达式（允许一层中转，够用且不猜）。"""
        if isinstance(node, ast.Name) and depth < 3 and node.id in assigned:
            return _resolve(assigned[node.id], depth + 1)
        return ast.unparse(node)

    injected = _resolve(value.values[0])
    assert "config.attributes.get(" in injected, (
        f"显式注入必须走 alembic 官方通道 config.attributes，实际是 {injected!r}"
    )
    assert "sqlalchemy_url" in injected, f"attributes 的键名必须是 sqlalchemy_url，实际 {injected!r}"

    fallback = _resolve(value.values[-1])
    assert "get_settings().database_url" in fallback, (
        f"兜底值必须是应用配置（否则 CLI 直跑没有 url 可用），实际 {fallback!r}"
    )


def test_pg_fixture_passes_url_through_attributes():
    """测试 fixture MUST 经 `config.attributes` 传库地址（`set_main_option` 是无效通道）。

    ★ 两处都写不算错，但**只有 attributes 生效**：若有人删掉 attributes 那行、
      只留 `set_main_option`，`env.py` 会静默回落到 `.env` —— 测试不再报错，
      而是悄悄去动应用库。这类「删一行仍然全绿」的退化必须静态拦住。
    ★ 扫描目标是 `pg_helpers.py`：Step 4A 起 `_alembic_config()` 搬到了那里
      （DQ4-5），继续扫 `test_0028_pg_constraints.py` 只会扫到一行 import。
    """
    tree = ast.parse(_PG_HELPERS_FILE.read_text(encoding="utf-8"))

    attribute_writes: list[ast.Subscript] = []
    for node in ast.walk(tree):
        # config.attributes["sqlalchemy_url"] = ...
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Subscript) and (
                    getattr(target.value, "attr", None) == "attributes"
                ):
                    key = target.slice
                    assert isinstance(key, ast.Constant) and key.value == "sqlalchemy_url", (
                        f"attributes 的键名必须与 env.py 一致，实际 {ast.unparse(key)!r}"
                    )
                    attribute_writes.append(target)

    assert attribute_writes, (
        "PG 用例的 Alembic Config 必须设置 config.attributes['sqlalchemy_url']："
        "只设 set_main_option 会被 env.py 的兜底覆盖，从而连到应用库"
    )
