"""T1 启动闸门的**真库**语义：head 放行、停在 0030 拒绝、结束后必须回到 head。

★ 为什么必须真库：替身能证明分支，但不能证明「`alembic_version` 里真的是 0030 时
  闸门拒绝」—— 而这正是「治理开启但迁移没跑」的真实故障形态。

★ 同步用例 + `asyncio.run`（同 `test_0031_migration.py`）：
  `pg_helpers.migrate` 经 `alembic/env.py` 用 `asyncio.run` 执行迁移，
  在已运行的事件循环里调用会抛「asyncio.run() cannot be called from a running event loop」。

★ `finally` 无条件 `migrate("head")`：本用例会把共享测试库降级到 0030，
  不恢复就让后续模块（治理 / 0031 迁移用例）在缺列状态下失败 —— 那不是它们的问题。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.database
from app.infra import schema_guard
from app.infra.schema_guard import assert_schema_matches_code
from tests.scenario.pg_helpers import SCENARIO_TEST_DB_URL, current_revision, migrate


def _run_guard() -> None:
    asyncio.run(assert_schema_matches_code(context="scenario-test"))


def test_guard_accepts_head_and_refuses_0030(schema, monkeypatch):
    """head → 放行；降级到 0030 → RuntimeError；finally 恢复 head。"""
    engine = create_async_engine(SCENARIO_TEST_DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(app.database, "SessionLocal", factory)
    monkeypatch.setattr(
        schema_guard,
        "get_settings",
        lambda: SimpleNamespace(tenant_governance_enabled=True),
    )

    try:
        migrate("head")
        _run_guard()

        migrate("0030", downgrade=True)
        with pytest.raises(RuntimeError) as exc_info:
            _run_guard()
        message = str(exc_info.value)
        assert "0030" in message
        assert "alembic upgrade head" in message
    finally:
        migrate("head")
        asyncio.run(engine.dispose())

    assert current_revision() == "0031", "测试库未恢复到 head，后续模块会在缺列状态下失败"
