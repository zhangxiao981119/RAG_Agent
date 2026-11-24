from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config.settings import get_settings
from app.models import Base

config = context.config

# ── 连接串：显式注入优先，应用配置兜底 ────────────────────────────────
# ★ 这里 MUST NOT 无条件覆盖调用方设置的 URL。
#   历史教训（真事故未遂）：本行原先是
#       config.set_main_option("sqlalchemy.url", get_settings().database_url)
#   于是测试 fixture 里认真 `set_main_option(SCENARIO_TEST_DB_URL)` 的那行成了
#   死代码 —— `_migrate("0027", downgrade=True)` 实际打的是 `.env` 里的库。
#   只要 `.env` 指向某个能连通的库，跑一次测试就会把它**降级到 0027**。
#   当时没出事，只是因为 `.env` 写的是 `@postgres:5432`（容器内名字，宿主机解析不了）
#   —— 这是巧合提供的安全，不是设计提供的。
#
# ★ 通道用 alembic 官方的 `config.attributes`（`Config.attributes` 是给「调用方
#   往 env.py 里传东西」准备的官方口子，不依赖 ini 文件与环境变量的相对优先级）。
# ★ CLI 直跑（`python -m alembic upgrade head`）不设 attributes ⇒ 回落到
#   `get_settings().database_url`，与改动前行为逐字一致（生产路径不受影响）。
_injected_url = config.attributes.get("sqlalchemy_url")
# ★ ConfigParser 把 `%` 当插值语法：口令含 `%40` 这类 percent-encoding 时，
#   直接把 DSN 交给 set_main_option 会抛 `ValueError: invalid interpolation syntax`。
#   转义成 `%%` 后，get_main_option / get_section 读回时仍是原值。
_url = _injected_url or get_settings().database_url
config.set_main_option("sqlalchemy.url", _url.replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
