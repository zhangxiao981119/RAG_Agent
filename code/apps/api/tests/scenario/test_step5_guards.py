"""Step 5 静态守卫（T-5-14 … T-5-18）。

★ 本文件不连库、不依赖任何环境变量 —— 任何机器都必须能跑，且每条都有
  「破坏方式必须变红」的 docstring（设计 §D.1 的准入条件）。

T-5-17（语句多重集）不在本文件：它需要 handler 的 FakeSession 夹具，
放在 `tests/scenario/test_scenario_wiring.py` 里紧挨既有 handler 用例，
避免为了一条断言重建整套 fakes。偏差与理由写在那条用例的 docstring 里。
"""
from __future__ import annotations

import ast
import hashlib
import inspect
import pathlib

from app.config import config_profile
from app.config.settings import Settings

_API_ROOT = pathlib.Path(__file__).resolve().parents[2]
_MIGRATIONS_DIR = _API_ROOT / "alembic" / "versions"
_MODELS_DIR = _API_ROOT / "app" / "models"
_DB_ROUTER = _API_ROOT / "app" / "infra" / "db_router.py"
_ENTITIES = _MODELS_DIR / "entities.py"
_MIGRATION_0028 = _MIGRATIONS_DIR / "0028_add_scenarios.py"

# ── T-5-14 金标准 ─────────────────────────────────────────────────────
# ★ 这个值是**实现 Step 5 之前**实测得到的 `sha256(inspect.getsource(get_db))`。
#   它存在的唯一目的：把「本步没改连接获取路径」从一句承诺变成一个可执行断言。
#   正当变更流程：改 deps.get_db → 跑本用例拿到新哈希 → 更新此字面量 →
#   commit message 里引用设计 §B.8 与 C-3。
_GOLDEN_GET_DB_SHA256 = "36ad6cdf6e75687a9ce3f446651fbbc3113dd26f35712ac1a35e660d6c2f31b2"

# ── T-5-15 唯一权威列白名单 ────────────────────────────────────────────
# 只有两处允许出现「库位列」：0028 建列、entities.py 声明 ORM 字段。
# 其它任何迁移/模型新增 db_binding / db_key 列都必须先红 —— 这是 DQ5-1 的可执行形态。
_ALLOWED_COLUMN_SITES = {
    ("alembic/versions/0028_add_scenarios.py", "db_binding"),
    ("app/models/entities.py", "db_binding"),
}

# ── T-5-16 禁止出现的查询形状 ──────────────────────────────────────────
_FORBIDDEN_SQL_FRAGMENTS = (
    "current_database()",
    "from pg_database",
    "information_schema.tables",
    "kb_catalog",
)


def _string_constants(path: pathlib.Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def test_t5_14_get_db_source_is_pinned():
    """T-5-14：`get_db` 源码 sha256 逐字等于金标准。

    破坏方式（必须变红）：给 `get_db` 加 `try/finally`、加
    `if settings.scene_db_split_enabled`、或改成 `async for` → 哈希变 → 红。
    这是「关闭态逐字节等价」在连接获取路径上的唯一证据。
    """
    from app.api.deps import get_db

    actual = hashlib.sha256(inspect.getsource(get_db).encode("utf-8")).hexdigest()
    assert actual == _GOLDEN_GET_DB_SHA256, (
        "deps.get_db 被改动了 —— Step 5 的承诺是「关闭态连接获取路径逐字节不变」。"
        f"实测 {actual}；期望 {_GOLDEN_GET_DB_SHA256}。"
        "若这是 Step 6 的有意接线，按本文件头部的正当变更流程更新字面量。"
    )


def test_t5_15_no_second_db_binding_column():
    """T-5-15：除白名单两处外，任何迁移/模型都不得新增 `db_binding` / `db_key` 列。

    破坏方式（必须变红）：在 0031 里给 `knowledge_bases` 加
    `sa.Column("db_key", ...)`，或在 entities.py 给 KnowledgeBase 加
    `db_key: Mapped[...] = mapped_column(...)` → 红。
    这是「库位只有一个可写真相」的结构守卫：第二份拷贝一旦出现，
    两份值分叉后「这个 kb 在哪」会有两个都看起来对的答案。
    """
    candidates = sorted(_MIGRATIONS_DIR.glob("*.py")) + sorted(_MODELS_DIR.glob("*.py"))
    assert candidates, "没扫到迁移/模型文件 —— 守卫路径错了"

    offenders: list[str] = []
    allowed_hits: list[str] = []

    for path in candidates:
        rel = path.relative_to(_API_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            column_name: str | None = None
            # 形式一：sa.Column("db_key", ...) / mapped_column("db_key", ...)
            if isinstance(node, ast.Call):
                func = node.func
                call_name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if call_name in {"Column", "mapped_column"} and node.args:
                    first = node.args[0]
                    if isinstance(first, ast.Constant) and first.value in {"db_binding", "db_key"}:
                        column_name = first.value
            # 形式二：db_binding: Mapped[...] = mapped_column(...)（列名取属性名）
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.target.id in {"db_binding", "db_key"}:
                    column_name = node.target.id

            if column_name is None:
                continue
            site = (rel, column_name)
            if site in _ALLOWED_COLUMN_SITES:
                allowed_hits.append(f"{rel}:{column_name}:{node.lineno}")
            else:
                offenders.append(f"{rel}:{column_name}:{node.lineno}")

    assert not offenders, (
        "出现了第二份「库位」列（I-5-1：唯一可写真相是 scenarios.db_binding）。"
        f"命中：{offenders}"
    )
    # 防空转：白名单两处必须真的被扫到，否则说明 AST 匹配规则失效、守卫在空转。
    assert len(allowed_hits) == 2, (
        "白名单列没有被扫到 —— AST 匹配写了但没生效，守卫在空转。"
        f"实际扫到：{allowed_hits}"
    )


def test_t5_16_no_forbidden_sql_shapes():
    """T-5-16：迁移常量与 db_router 里不得出现「猜库」查询形状。

    破坏方式（必须变红）：
      ① 在横幅里加 `SELECT current_database()`（"打日志看看连哪个库"）→ 红；
      ② 在 db_router.py 里加 `select(...)` 或 `text("SELECT ...")` → 红；
      ③ 建 `kb_catalog` 视图/查询 → 红。
    """
    offenders: list[str] = []
    for path in sorted(_MIGRATIONS_DIR.glob("*.py")) + [_DB_ROUTER]:
        rel = path.relative_to(_API_ROOT).as_posix()
        for lineno, text in _string_constants(path):
            lowered = text.lower()
            for fragment in _FORBIDDEN_SQL_FRAGMENTS:
                if fragment in lowered:
                    offenders.append(f"{rel}:{lineno}:{fragment}")
    assert not offenders, (
        "出现了被禁止的查询形状（C-4b：路由不得为了认库而多查一次）。"
        f"命中：{offenders}"
    )

    router_source = _DB_ROUTER.read_text(encoding="utf-8").lower()
    assert "select(" not in router_source, "db_router 里出现了 select( —— 路由层不得查库"
    assert 'text("select' not in router_source, (
        'db_router 里出现了 text("SELECT —— 唯一允许的实测查询是启动期 SHOW max_connections'
    )


def test_T_5_18_cross_role_fields():
    """T-5-18：两个路由项必须在白名单里，且都能在快照里取到。

    ★ 变异：把 CROSS_ROLE_FIELDS 里任一项删掉 → 本条红。
      它拦的是「加了开关忘了进指纹」，Step 4B §E-4 记录过这个形态。
    """
    assert "scene_db_split_enabled" in config_profile.CROSS_ROLE_FIELDS
    assert "scene_db_keys" in config_profile.CROSS_ROLE_FIELDS

    snap = config_profile.runtime_config_snapshot()
    assert "scene_db_split_enabled" in snap
    assert "scene_db_keys" in snap
    missing = [key for key in config_profile.CROSS_ROLE_FIELDS if key not in snap]
    assert missing == [], f"CROSS_ROLE_FIELDS 里这些键不在快照中：{missing}"


def test_scene_db_dsn_snapshot_is_redacted(monkeypatch):
    """快照里的 DSN 必须逐值脱敏：账号密码不得出现，host:port 必须保留。

    ★ 变异：把 `_redact_url(v)` 改成 `v` → 本条红（凭据进日志）。
    """
    s = Settings(
        scene_db_dsn={
            "kb_ab": "postgresql+asyncpg://alice:secret@db-host:5432/kagent_finance"
        }
    )
    monkeypatch.setattr(config_profile, "get_settings", lambda: s)

    snap = config_profile.runtime_config_snapshot()
    assert snap["scene_db_keys"] == ["kb_ab"]
    redacted = snap["scene_db_dsn_redacted"]["kb_ab"]
    assert "alice" not in redacted
    assert "secret" not in redacted
    assert redacted == "postgresql+asyncpg://db-host:5432/kagent_finance"


def test_scene_db_split_and_keys_change_fingerprint():
    """开关或键集变化必须改变指纹（证明它们真的进了 CROSS_ROLE_FIELDS）。

    ★ 变异：把两项从白名单删掉 → 两个断言都红。
    """
    base = config_profile.runtime_config_snapshot()

    toggled = dict(base)
    toggled["scene_db_split_enabled"] = not base["scene_db_split_enabled"]
    assert config_profile.config_fingerprint(base) != config_profile.config_fingerprint(toggled)

    left = dict(base)
    right = dict(base)
    left["scene_db_keys"] = ["kb_ab"]
    right["scene_db_keys"] = ["kb_ab", "kb_cd"]
    assert config_profile.config_fingerprint(left) != config_profile.config_fingerprint(right)
