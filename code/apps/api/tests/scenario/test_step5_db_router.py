"""Step 5 数据层路由单测（纯单测，不连库）。

覆盖 8 组：
    T-5-1 resolve_db_key 真值表（解析不做规范化、不查注册表）
    T-5-2 关闭态不建引擎
    T-5-3 默认引擎对象恒等（关闭态与引入路由层前等价）
    T-5-4 预算算术（唯一算术来源 _arithmetic）
    T-5-5 超预算必须 raise + 边界 needed == budget 允许
    T-5-6 未注册键 fail-closed
    T-5-7 AST：data_session 零调用点、定义只在 db_router.py 一次
    T-5-8 AST：engine_for / data_session 不读 scene_db_split_enabled

★ 期望值一律字面量：不复用被测模块的常量，避免"实现错了断言跟着错"。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from app.config.settings import Settings
from app.infra import db_router

_API_ROOT = pathlib.Path(__file__).resolve().parents[2]
_APP_ROOT = _API_ROOT / "app"
_DB_ROUTER_PATH = _APP_ROOT / "infra" / "db_router.py"

# 合法 DSN（settings 的 field_validator 只要求 postgresql+asyncpg:// 前缀）
_DSN = "postgresql+asyncpg://u:p@h:5432/demo"
# 库位键必须匹配 ^kb_[a-z0-9_]{2,40}$（既有校验器口径），"kb_a" 只有 1 个字符不合法，
# 故用例统一用 kb_ab；算术只关心库位数量，不关心键名。


@pytest.fixture(autouse=True)
def _isolate_router_state(monkeypatch):
    """每个用例独立的注册表与引擎计数，避免用例间串味。"""
    monkeypatch.setattr(db_router, "_REGISTRY", {})
    monkeypatch.setattr(db_router, "_CREATED_ENGINES", 0)


# ══════════════════════════════════════════════════════════════════════
# T-5-1 resolve_db_key 真值表
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("db_binding", "expected"),
    [
        (None, "default"),
        ("kb_finance", "kb_finance"),
        # 未注册的键也原样返回：解析与可用性分离，不在这里 raise
        ("kb_unknown", "kb_unknown"),
        # 不做小写/规范化：规范化会把"配置写错"变成"悄悄落另一个键"
        ("KB_Finance", "KB_Finance"),
    ],
)
def test_t5_1_resolve_db_key_truth_table(db_binding, expected):
    assert db_router.resolve_db_key(db_binding) == expected


# ══════════════════════════════════════════════════════════════════════
# T-5-2 / T-5-3 / T-5-6 注册与取引擎
# ══════════════════════════════════════════════════════════════════════


def test_t5_2_disabled_registers_without_building_engines(monkeypatch):
    monkeypatch.setattr(db_router, "get_settings", lambda: Settings())
    report = db_router.register_from_settings()

    assert report.created_engines == 0
    assert report.bucket_count == 1
    assert report.total_connections == 40
    assert report.keys == ("default",)


def test_t5_3_default_engine_is_same_object(monkeypatch):
    import app.database as app_database

    monkeypatch.setattr(db_router, "get_settings", lambda: Settings())
    db_router.register_from_settings()

    assert db_router.engine_for("default") is app_database.engine


def test_t5_6_unregistered_key_fails_closed(monkeypatch):
    monkeypatch.setattr(db_router, "get_settings", lambda: Settings())
    db_router.register_from_settings()

    with pytest.raises(db_router.DbRouteError) as excinfo:
        db_router.engine_for("kb_unknown")

    assert excinfo.value.code == "DB_ROUTE_KEY_UNKNOWN"


# ══════════════════════════════════════════════════════════════════════
# T-5-4 预算算术（直接调 _arithmetic，不建引擎）
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("settings_kwargs", "expected"),
    [
        # (per_bucket, k, needed, budget)
        (
            {
                "db_pool_size": 20,
                "db_max_overflow": 20,
                "db_replicas_expected": 1,
                "scene_db_split_enabled": False,
            },
            (40, 1, 50, 90),
        ),
        (
            {
                "db_pool_size": 20,
                "db_max_overflow": 20,
                "db_replicas_expected": 1,
                "scene_db_split_enabled": True,
                "scene_db_dsn": {"kb_ab": _DSN},
            },
            (40, 2, 90, 90),
        ),
        (
            {
                "db_pool_size": 10,
                "db_max_overflow": 10,
                "db_replicas_expected": 1,
                "scene_db_split_enabled": True,
                "scene_db_dsn": {"kb_ab": _DSN},
            },
            (20, 2, 50, 90),
        ),
        (
            {
                "db_pool_size": 20,
                "db_max_overflow": 20,
                "db_replicas_expected": 2,
                "scene_db_split_enabled": True,
                "scene_db_dsn": {"kb_ab": _DSN},
            },
            (40, 2, 170, 90),
        ),
    ],
)
def test_t5_4_budget_arithmetic(settings_kwargs, expected):
    assert db_router._arithmetic(Settings(**settings_kwargs)) == expected


# ══════════════════════════════════════════════════════════════════════
# T-5-5 超预算必须 raise；needed == budget 允许
# ══════════════════════════════════════════════════════════════════════


def test_t5_5_over_budget_raises_and_equal_budget_passes(monkeypatch):
    over = Settings(
        db_pool_size=20,
        db_max_overflow=20,
        db_replicas_expected=2,
        scene_db_split_enabled=True,
        scene_db_dsn={"kb_ab": _DSN},
    )
    monkeypatch.setattr(db_router, "get_settings", lambda: over)

    with pytest.raises(RuntimeError) as excinfo:
        db_router.check_connection_budget()

    message = str(excinfo.value)
    assert "170" in message
    assert "90" in message
    assert "DB_POOL_SIZE" in message

    # 边界：40×2(副本)×1(库位)+10 = 90 == 预算 90 → 允许通过
    edge = Settings(
        db_pool_size=20,
        db_max_overflow=20,
        db_replicas_expected=2,
        scene_db_split_enabled=False,
    )
    monkeypatch.setattr(db_router, "get_settings", lambda: edge)
    db_router.check_connection_budget()  # 不抛即通过


# ══════════════════════════════════════════════════════════════════════
# T-5-7 / T-5-8 AST 守卫（断言对象是源码文本，不是被测函数返回值）
# ══════════════════════════════════════════════════════════════════════


def _iter_app_trees():
    for path in sorted(_APP_ROOT.rglob("*.py")):
        yield path, ast.parse(path.read_text(encoding="utf-8"))


def _callee_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def test_t5_7_data_session_has_zero_call_sites_and_single_definition():
    call_sites: list[tuple[str, int]] = []
    definition_files: list[pathlib.Path] = []

    for path, tree in _iter_app_trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _callee_name(node.func) == "data_session":
                call_sites.append((str(path), node.lineno))
            # async def 节点是 AsyncFunctionDef，与 FunctionDef 一并计入"定义"
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "data_session"
            ):
                definition_files.append(path)

    assert call_sites == [], f"data_session 在 app/ 内必须零调用点，实际：{call_sites}"
    assert len(definition_files) == 1, f"data_session 必须只定义一次，实际：{definition_files}"
    assert definition_files[0].resolve() == _DB_ROUTER_PATH.resolve()


def _find_function(tree: ast.Module, name: str) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"db_router.py 中找不到函数 {name}")


def _switch_attr_uses(func: ast.AST) -> list[ast.Attribute]:
    return [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.Attribute) and node.attr == "scene_db_split_enabled"
    ]


def test_t5_8_switch_only_read_at_registration():
    tree = ast.parse(_DB_ROUTER_PATH.read_text(encoding="utf-8"))

    assert _switch_attr_uses(_find_function(tree, "engine_for")) == []
    assert _switch_attr_uses(_find_function(tree, "data_session")) == []
    assert len(_switch_attr_uses(_find_function(tree, "register_from_settings"))) >= 1
