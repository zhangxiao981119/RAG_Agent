"""启动期 schema 版本闸门（`app/infra/schema_guard.py`）的 NO-DB 用例 + 接线守卫。

★ 为什么这一层不连库：闸门的两个判据（代码 heads / 库的 `alembic_version`）都能用
  替身精确构造。真库语义（真的 `migrate("0030")` 后拒绝）另在
  `tests/scenario/test_step7_schema_guard_pg.py`，那里才付连库成本。

★ 替身只实现闸门用到的那一面（async with + execute → scalar_one_or_none）：
  若实现里私自加了 commit / 迁移调用，这里会直接 AttributeError，而不是悄悄写库。
"""
from __future__ import annotations

import ast
import logging
import pathlib
from types import SimpleNamespace

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

import app.database
from app.infra import schema_guard
from app.infra.schema_guard import assert_schema_matches_code

_API_ROOT = pathlib.Path(__file__).resolve().parents[1]
_LOGGER_NAME = schema_guard.__name__

# heads 用**真实** alembic 配置（当前 0031），不伪造：伪造等于让闸门与迁移链脱钩，
# 迁移链分叉或多头时本文件仍会绿。
_CODE_HEADS = ScriptDirectory.from_config(
    Config(str(_API_ROOT / "alembic.ini"))
).get_heads()

_OLD_REVISION = "0030"


class _FakeResult:
    def __init__(self, value):  # noqa: D107 - 测试替身
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    """只记录语句并返回预设版本；`error` 非空时在 execute 处抛出。"""

    def __init__(self, revision, *, error=None):
        self._revision = revision
        self._error = error
        self.statements: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def execute(self, statement, params=None):
        self.statements.append(statement)
        if self._error is not None:
            raise self._error
        return _FakeResult(self._revision)


def _install(monkeypatch, *, governed: bool, revision=None, error=None) -> _FakeSession:
    """装替身：`app.database.SessionLocal` 是**函数内 import**，故 patch 模块属性即可。"""
    session = _FakeSession(revision, error=error)
    monkeypatch.setattr(app.database, "SessionLocal", lambda: session)
    monkeypatch.setattr(
        schema_guard,
        "get_settings",
        lambda: SimpleNamespace(tenant_governance_enabled=governed),
    )
    return session


def _records(caplog, level: int):
    return [
        record
        for record in caplog.records
        if record.name == _LOGGER_NAME and record.levelno == level
    ]


@pytest.mark.asyncio
async def test_governed_with_old_revision_refuses_startup(monkeypatch):
    """治理开启 + 库停在 0030 → 拒绝启动，文案带版本、heads 与升级动作。"""
    session = _install(monkeypatch, governed=True, revision=_OLD_REVISION)

    with pytest.raises(RuntimeError) as exc_info:
        await assert_schema_matches_code(context="api")

    message = str(exc_info.value)
    assert "api" in message
    assert _OLD_REVISION in message
    assert "alembic upgrade head" in message
    assert "0031" in message
    assert session.statements, "未发出任何查询 —— 闸门退化成常量判断"


@pytest.mark.asyncio
async def test_governed_with_head_passes_and_logs_info(monkeypatch, caplog):
    """治理开启 + 版本正好是代码 head → 放行并记录 INFO。"""
    head = _CODE_HEADS[0]
    _install(monkeypatch, governed=True, revision=head)

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await assert_schema_matches_code(context="worker")

    infos = _records(caplog, logging.INFO)
    assert any(
        "schema 版本一致" in record.getMessage() and record.revision == head
        for record in infos
    ), f"缺少版本一致的 INFO 记录：{[r.getMessage() for r in infos]}"


@pytest.mark.asyncio
async def test_switch_off_mismatch_only_warns(monkeypatch, caplog):
    """治理关闭 + 版本不匹配 → 不抛，保持既有 warning 口径（关闭态行为不变）。"""
    _install(monkeypatch, governed=False, revision=_OLD_REVISION)

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        await assert_schema_matches_code(context="api")

    warnings = [
        record
        for record in _records(caplog, logging.WARNING)
        if "数据库 schema 版本与代码不一致" in record.getMessage()
    ]
    assert warnings, "关闭态未落下版本不一致的 warning"
    record = warnings[0]
    assert record.db_revision == _OLD_REVISION
    assert record.code_heads == sorted(_CODE_HEADS)
    assert record.app == "api"
    assert record.hint


@pytest.mark.asyncio
async def test_governed_probe_error_refuses_and_keeps_cause(monkeypatch):
    """治理开启 + 校验本身失败（连接 / 表缺失）→ 拒绝启动且保留根因。"""
    boom = RuntimeError("连接失败")
    _install(monkeypatch, governed=True, error=boom)

    with pytest.raises(RuntimeError) as exc_info:
        await assert_schema_matches_code(context="api")

    assert str(exc_info.value) == "无法校验 schema 版本，拒绝在治理开启时启动"
    assert exc_info.value.__cause__ is boom


@pytest.mark.asyncio
async def test_switch_off_probe_error_only_warns(monkeypatch, caplog):
    """治理关闭 + 校验失败 → 只 warning（与改动前的兜底一致）。

    ★ 必须连 `exc_info` 一起断言：只比对消息文本的话，「吞掉异常只留一句提示」
      与「带堆栈的 warning」完全同形 —— 而前者让根因彻底不可追溯。
    """
    boom = RuntimeError("连接失败")
    _install(monkeypatch, governed=False, error=boom)

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        await assert_schema_matches_code(context="worker")

    records = [
        record
        for record in _records(caplog, logging.WARNING)
        if "schema 版本启动检查跳过" in record.getMessage()
    ]
    assert records, "关闭态未落下「跳过」warning"
    record = records[0]
    assert record.exc_info is not None, "warning 未带 exc_info —— 根因不可追溯"
    assert record.exc_info[1] is boom, f"exc_info 里的异常不是原异常：{record.exc_info[1]!r}"
    assert record.app == "worker"


@pytest.mark.asyncio
async def test_switch_off_is_read_only_single_probe(monkeypatch):
    """关闭态只做一次 `SELECT version_num`：不迁移、不写库。"""
    session = _install(monkeypatch, governed=False, revision=_OLD_REVISION)

    await assert_schema_matches_code(context="api")

    assert len(session.statements) == 1
    assert "SELECT version_num FROM alembic_version" in str(session.statements[0])


# ══════════════════════════════════════════════════════════════════════
# 接线守卫：顺序（在任何治理查询之前）与两侧调用点
# ══════════════════════════════════════════════════════════════════════


def _function_node(path: pathlib.Path, name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name
    )


def _call_nodes(node: ast.AST, func_names: set[str]) -> list[ast.Call]:
    """按源码位置排序的调用节点（`ast.walk` 是广度优先，不能直接用其顺序）。"""
    return sorted(
        (
            child
            for child in ast.walk(node)
            if isinstance(child, ast.Call)
            and getattr(child.func, "id", None) in func_names
        ),
        key=lambda child: (child.lineno, child.col_offset),
    )


def _keyword(call: ast.Call, name: str):
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _is_awaited(root: ast.AST, call: ast.Call) -> bool:
    """`call` 的直接父节点是否为 `ast.Await`。

    ★ 为什么必须断言这一层：删掉 `await` 后调用语句仍在、顺序仍对、context 仍对，
      但协程从未被执行 —— 闸门等于不存在，而所有文本/位置断言照样绿。
    """
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(root):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    return isinstance(parents.get(id(call)), ast.Await)


def test_api_guard_runs_before_scenario_data_check():
    """`main.py` 里闸门必须被 await，且出现在 `check_scenario_data` 之前。"""
    main_path = _API_ROOT / "app" / "main.py"
    lifespan = _function_node(main_path, "lifespan")
    calls = _call_nodes(lifespan, {"assert_schema_matches_code", "check_scenario_data"})

    guard_calls = [c for c in calls if c.func.id == "assert_schema_matches_code"]
    assert len(guard_calls) == 1, f"main.py 的闸门调用点应为 1 个，实际 {len(guard_calls)}"
    context = _keyword(guard_calls[0], "context")
    assert isinstance(context, ast.Constant) and context.value == "api"
    assert _is_awaited(lifespan, guard_calls[0]), (
        "闸门调用未被 await 包裹：协程从未执行，等于没有闸门（假绿）"
    )

    first = calls[0].func.id
    assert first == "assert_schema_matches_code", (
        "闸门不在任何治理查询之前："
        f"lifespan 内首个相关调用是 {first}（check_scenario_data 会读 0031 列）"
    )


def test_api_has_no_leftover_schema_check():
    """旧的 warning-only schema 检查块必须整体移除（否则会出现两套判定）。"""
    source = (_API_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "SELECT version_num FROM alembic_version" not in source


def test_worker_startup_calls_guard_after_budget_check():
    """worker 的 `on_startup` 在连接预算闸门之后调用闸门，context 为 worker。"""
    worker_path = _API_ROOT / "app" / "workers" / "parse_job.py"
    on_startup = _function_node(worker_path, "on_startup")
    guard_calls = _call_nodes(on_startup, {"assert_schema_matches_code"})
    assert len(guard_calls) == 1, f"worker 的闸门调用点应为 1 个，实际 {len(guard_calls)}"

    context = _keyword(guard_calls[0], "context")
    assert isinstance(context, ast.Constant) and context.value == "worker"
    assert _is_awaited(on_startup, guard_calls[0]), (
        "worker 的闸门调用未被 await 包裹：协程从未执行，等于没有闸门（假绿）"
    )

    budget_calls = _call_nodes(on_startup, {"check_connection_budget"})
    assert budget_calls, "worker 启动路径缺少 check_connection_budget"
    assert budget_calls[0].lineno < guard_calls[0].lineno, (
        "闸门必须位于 check_connection_budget 之后"
    )
