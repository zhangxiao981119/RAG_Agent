"""Step 5 配置层口径的纯单测（不连库、不读真实网络）。

钉住四件事：
  1. 数据层路由 6 个字段的默认值（默认关闭 = 生产行为零变化）；
  2. scene_db_dsn 的启动期校验（键口径 / 保留键 / DSN scheme）；
  3. db_router 的三个站位值登记在册且镜像值与 settings 一致；
  4. 横幅渲染确实把三个名字打出来。
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import placeholders
from app.config.settings import Settings


def test_defaults_are_off_and_conservative():
    """默认态：分流关闭、无库位、闸门参数取保守字面量。"""
    s = Settings()
    assert s.scene_db_split_enabled is False
    assert s.scene_db_dsn == {}
    assert s.db_max_connections_for_budget == 100
    assert s.db_conn_reserved == 10
    assert s.db_worker_conn_total == 10
    assert s.db_replicas_expected == 1


def test_valid_dsn_accepted():
    """合法库位键 + asyncpg DSN 应被接受。"""
    s = Settings(
        scene_db_dsn={"kb_finance": "postgresql+asyncpg://u:p@h:5432/kagent_finance"}
    )
    assert s.scene_db_dsn == {
        "kb_finance": "postgresql+asyncpg://u:p@h:5432/kagent_finance"
    }


def test_key_without_kb_prefix_rejected():
    """键缺 kb_ 前缀 → 拒绝启动。"""
    with pytest.raises(ValidationError):
        Settings(scene_db_dsn={"finance": "postgresql+asyncpg://u:p@h:5432/db"})


def test_reserved_default_key_rejected():
    """保留虚拟键 'default' 不得作为库位键。

    ★ 断言错误消息必须来自「保留键」分支：'default' 本身也不匹配键正则，
      只断言抛 ValidationError 的话，删掉保留键分支用例照样绿（假绿）。
    """
    with pytest.raises(ValidationError, match="保留虚拟键"):
        Settings(scene_db_dsn={"default": "postgresql+asyncpg://u:p@h:5432/db"})


def test_non_asyncpg_scheme_rejected():
    """DSN 必须用 postgresql+asyncpg:// 前缀。

    ★ 键必须合法（kb_x1 满足 ^kb_[a-z0-9_]{2,40}$），否则会先被正则分支拦下，
      scheme 检查永远不执行 —— 那样这条用例测不到它声称要测的东西。
    """
    with pytest.raises(ValidationError, match="postgresql\\+asyncpg"):
        Settings(scene_db_dsn={"kb_x1": "postgresql://u:p@h:5432/db"})


def test_db_router_placeholders_registered():
    """db_router 的三个站位值必须登记在册，镜像值取字面量。"""
    assert placeholders.names_for("db_router") == frozenset(
        {"DB_CONN_RESERVED", "DB_WORKER_CONN_TOTAL", "DB_REPLICAS_EXPECTED"}
    )
    assert placeholders.value_of("DB_CONN_RESERVED") == 10
    assert placeholders.value_of("DB_WORKER_CONN_TOTAL") == 10
    assert placeholders.value_of("DB_REPLICAS_EXPECTED") == 1


def test_placeholders_mirror_settings():
    """站位值镜像 MUST 等于 settings 对应字段（两边都锚定字面量）。"""
    s = Settings()
    assert placeholders.value_of("DB_CONN_RESERVED") == s.db_conn_reserved == 10
    assert placeholders.value_of("DB_WORKER_CONN_TOTAL") == s.db_worker_conn_total == 10
    assert placeholders.value_of("DB_REPLICAS_EXPECTED") == s.db_replicas_expected == 1


def test_banner_line_renders_three_names():
    """横幅行非空，且三个名字都出现。"""
    line = placeholders.render_line("db_router")
    assert line
    assert "DB_CONN_RESERVED" in line
    assert "DB_WORKER_CONN_TOTAL" in line
    assert "DB_REPLICAS_EXPECTED" in line
