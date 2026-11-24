"""全库一键迁移脚本（DQ2）单测：目标解析、显式声明、失败隔离与脱敏。

分层：
  · NO-DB（必跑）：目标库解析、argparse 必填、逐库失败隔离、异常脱敏。
  · Real-DB（`SCENARIO_TEST_DB_URL` 缺失则 skip）：对测试库真跑一次
    `upgrade head`，证明 `config.attributes` 通道确实把连接指到了目标库。

★ 期望值尽量用字面量；只有「当前 head」从 `head_revision()` 取，
  因为迁移号会随版本推进，写死会在下一次迁移时变成假红。
"""
from __future__ import annotations

import contextlib
import io
from types import SimpleNamespace

import pytest

from scripts.alembic_all_dbs import (
    Target,
    TargetError,
    alembic_config,
    build_targets,
    head_revision,
    main,
    run,
    safe_error,
)

_DSN_DEFAULT = "postgresql+asyncpg://user:supersecret@db-host:5433/kagent"
_DSN_SCENE = "postgresql+asyncpg://user:supersecret@db-host:5433/kb_finance"
_DSN_PERCENT = "postgresql+asyncpg://user:p%40ss@db-host:5433/kagent"


def _settings(scene_db_dsn: dict[str, str] | None = None) -> SimpleNamespace:
    """构造 build_targets 需要的两个字段（不实例化 Settings，避免读 .env）。"""
    return SimpleNamespace(
        database_url=_DSN_DEFAULT,
        scene_db_dsn=scene_db_dsn or {},
    )


# ══════════════════════════════════════════════════════════════════════
# NO-DB：目标解析
# ══════════════════════════════════════════════════════════════════════


def test_all_scenes_includes_default_first_then_sorted_keys():
    targets = build_targets(
        _settings({"kb_zzz": _DSN_SCENE, "kb_aaa": _DSN_SCENE}),
        all_scenes=True,
    )

    assert [target.db_key for target in targets] == ["default", "kb_aaa", "kb_zzz"]
    assert targets[0].dsn == _DSN_DEFAULT


def test_all_scenes_without_scene_config_only_default():
    targets = build_targets(_settings(), all_scenes=True)

    assert [target.db_key for target in targets] == ["default"]


def test_duplicate_db_key_is_deduped_preserving_first_order():
    targets = build_targets(
        _settings({"kb_a": _DSN_SCENE}),
        db_keys=["kb_a", "default", "kb_a"],
    )

    assert [target.db_key for target in targets] == ["kb_a", "default"]


def test_unknown_db_key_fails_closed_and_lists_configured():
    with pytest.raises(TargetError) as excinfo:
        build_targets(_settings({"kb_a": _DSN_SCENE}), db_keys=["kb_missing"])

    message = str(excinfo.value)
    assert "kb_missing" in message
    assert "kb_a" in message


def test_main_requires_explicit_target():
    """既不给 --db-key 也不给 --all-scenes → argparse 退出码 2。"""
    with pytest.raises(SystemExit) as excinfo:
        main([])

    assert excinfo.value.code == 2


def test_main_unknown_key_returns_one_without_touching_db(capsys):
    def forbidden_upgrade(target: Target) -> None:  # pragma: no cover - 被调用即失败
        raise AssertionError("目标解析失败时不得开始迁移")

    code = main(
        ["--db-key", "kb_missing"],
        settings=_settings({"kb_a": _DSN_SCENE}),
        upgrade=forbidden_upgrade,
    )

    assert code == 1
    assert "目标库配置错误" in capsys.readouterr().err


# ══════════════════════════════════════════════════════════════════════
# NO-DB：执行与报告
# ══════════════════════════════════════════════════════════════════════


def test_run_all_pass_returns_zero_and_reports_head(capsys):
    head = head_revision()
    calls: list[Target] = []

    def upgrade(target: Target) -> None:
        calls.append(target)

    code = run(
        [Target("default", _DSN_DEFAULT), Target("kb_a", _DSN_SCENE)],
        upgrade=upgrade,
        version_reader=lambda dsn: head,
    )

    assert code == 0
    assert [target.db_key for target in calls] == ["default", "kb_a"]
    output = capsys.readouterr().out
    assert output.count("PASS") == 2
    assert f"revision={head}" in output
    assert "合计 2 个库位：通过 2，失败 0" in output


def test_run_isolates_failure_and_continues(capsys):
    """第一个库失败不得阻断第二个；退出码 1，汇总计数正确。"""
    head = head_revision()
    calls: list[str] = []

    def upgrade(target: Target) -> None:
        calls.append(target.db_key)
        if target.db_key == "default":
            raise RuntimeError("连接被拒绝")

    code = run(
        [Target("default", _DSN_DEFAULT), Target("kb_a", _DSN_SCENE)],
        upgrade=upgrade,
        version_reader=lambda dsn: head,
    )

    assert code == 1
    assert calls == ["default", "kb_a"]
    output = capsys.readouterr().out
    assert "FAIL" in output
    assert "RuntimeError: 连接被拒绝" in output
    assert "合计 2 个库位：通过 1，失败 1" in output


def test_run_revision_mismatch_is_failure(capsys):
    def upgrade(target: Target) -> None:
        return None

    code = run(
        [Target("default", _DSN_DEFAULT)],
        upgrade=upgrade,
        version_reader=lambda dsn: "0000",
    )

    assert code == 1
    output = capsys.readouterr().out
    assert "revision=0000 != head=" in output


def test_safe_error_masks_password_and_full_dsn():
    """异常消息里同时出现 SQLAlchemy DSN 与 asyncpg DSN 时，两者都必须脱敏。"""
    exc = RuntimeError(
        f"could not connect to {_DSN_DEFAULT} (or {_DSN_DEFAULT.replace('+asyncpg', '')})"
    )

    message = safe_error(exc, _DSN_DEFAULT)

    assert "supersecret" not in message
    assert _DSN_DEFAULT not in message
    assert "db-host:5433/kagent" in message


def test_run_output_never_contains_password(capsys):
    def upgrade(target: Target) -> None:
        raise RuntimeError(f"boom {target.dsn}")

    run([Target("default", _DSN_DEFAULT)], upgrade=upgrade, version_reader=lambda dsn: None)

    output = capsys.readouterr().out
    assert "supersecret" not in output


# ══════════════════════════════════════════════════════════════════════
# NO-DB：DSN 含 `%` 的插值回归（P3 发现的真实缺陷）
# ══════════════════════════════════════════════════════════════════════


def test_config_accepts_percent_encoded_password():
    """口令含 `%40` 时构造 Config 不得抛 ConfigParser 插值错误。"""
    config = alembic_config(_DSN_PERCENT)

    assert config.attributes["sqlalchemy_url"] == _DSN_PERCENT


def test_offline_render_accepts_percent_encoded_password():
    """离线渲染会真正执行 env.py 的 set_main_option；`%` 未转义会在这里炸。"""
    from alembic import command

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        command.upgrade(alembic_config(_DSN_PERCENT), "head", sql=True)

    assert "CREATE TABLE" in buffer.getvalue().upper()


# ══════════════════════════════════════════════════════════════════════
# Real-DB：attributes 通道真的把迁移打到目标库
# ══════════════════════════════════════════════════════════════════════


def test_real_db_upgrade_reaches_head(schema):
    """对测试库真跑 `upgrade head`：成功且版本等于代码 head。"""
    from tests.scenario.pg_helpers import SCENARIO_TEST_DB_URL

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = run([Target("default", SCENARIO_TEST_DB_URL)])

    assert code == 0, buffer.getvalue()
    assert f"revision={head_revision()}" in buffer.getvalue()
