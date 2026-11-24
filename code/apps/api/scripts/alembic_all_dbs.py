#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""全库一键迁移（框架 DQ2）：逐库执行 `alembic upgrade head`。

用法（后端根 code/apps/api）：
    # 只迁控制库
    python -m scripts.alembic_all_dbs --db-key default

    # 只迁某个场景库
    python -m scripts.alembic_all_dbs --db-key kb_finance

    # 控制库 + 全部已配置场景库
    python -m scripts.alembic_all_dbs --all-scenes

三条纪律：
    1. **必须显式声明目标库**：不给 `--db-key` 也不给 `--all-scenes` 时直接退出（argparse 退出码 2），
       禁止「默认只迁默认库」的隐式行为（框架 I-11）。
    2. **不建库、不改连接串**：目标库必须已存在；DSN 只从 settings 读取。
    3. **逐库隔离失败**：某库失败不阻断后续库，最终汇总并以非零退出码收尾。

退出码：0 = 全部库位迁移成功；1 = 任一库位失败或目标库配置缺失。
★ 永不打印口令：报告只出现 `db_key`、`host:port/dbname` 与异常类型/脱敏消息。
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import asyncpg
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url

from app.config.settings import Settings, get_settings
from app.infra.db_router import DEFAULT_DB_KEY
from scripts.scene_db_surgery import _asyncpg_dsn, _describe_dsn

_API_ROOT = pathlib.Path(__file__).resolve().parents[1]


class TargetError(Exception):
    """目标库配置错误（未知库位 / DSN 缺失），在开跑前 fail-closed。"""


@dataclass(frozen=True)
class Target:
    """一个迁移目标：库位键 + DSN（DSN 只进引擎，不进日志）。"""

    db_key: str
    dsn: str


def build_targets(
    settings: Settings,
    *,
    db_keys: Sequence[str] = (),
    all_scenes: bool = False,
) -> list[Target]:
    """把命令行参数解析成目标库列表（纯函数、零 IO）。

    - `all_scenes`：控制库 + `settings.scene_db_dsn` 的全部库位（排序）；
    - 否则按 `db_keys` 给定顺序，重复键只保留一次；
    - 未知库位 → `TargetError`，列出已配置库位，**不回落默认库**。
    """
    if all_scenes:
        keys = [DEFAULT_DB_KEY, *sorted(settings.scene_db_dsn)]
    else:
        keys = list(db_keys)

    seen: list[str] = []
    for key in keys:
        if key not in seen:
            seen.append(key)

    targets: list[Target] = []
    for key in seen:
        if key == DEFAULT_DB_KEY:
            targets.append(Target(key, settings.database_url))
            continue
        dsn = settings.scene_db_dsn.get(key)
        if dsn is None:
            configured = ", ".join(sorted(settings.scene_db_dsn)) or "（无）"
            raise TargetError(
                f"未知库位 {key!r}：SCENE_DB_DSN 未配置。已配置库位：{configured}"
            )
        targets.append(Target(key, dsn))
    return targets


def alembic_config(dsn: str) -> Config:
    """构造指向单个库的 alembic Config。

    ★ 生效通道是 `attributes`：`alembic/env.py` 读它并优先于 `get_settings().database_url`。
      只设 `set_main_option` 会被 env.py 的兜底覆盖，实际打到 `.env` 里的库。
    """
    config = Config()
    config.set_main_option("script_location", str(_API_ROOT / "alembic"))
    # ★ 只走 attributes 通道；由 `alembic/env.py` 统一写入 sqlalchemy.url 并转义 `%`。
    #   这里再写一次 set_main_option 会让含 `%40` 的口令在进 env.py 前就抛插值错误。
    config.attributes["sqlalchemy_url"] = dsn
    return config


def upgrade_one(target: Target) -> None:
    """对单个库执行 `upgrade head`（同步；env.py 内部自己 `asyncio.run`）。"""
    command.upgrade(alembic_config(target.dsn), "head")


async def _read_version(dsn: str) -> str | None:
    """读 `alembic_version.version_num`；表不存在 → None。"""
    connection = await asyncpg.connect(_asyncpg_dsn(dsn))
    try:
        try:
            value = await connection.fetchval("SELECT version_num FROM alembic_version")
        except asyncpg.exceptions.UndefinedTableError:
            return None
    finally:
        await connection.close()
    return str(value) if value is not None else None


def read_version(dsn: str) -> str | None:
    """同步包装：脚本主流程不在事件循环内，可直接 `asyncio.run`。"""
    return asyncio.run(_read_version(dsn))


def safe_error(exc: BaseException, dsn: str) -> str:
    """把异常渲染成不含口令的文本（异常消息可能带完整 DSN）。"""
    message = f"{type(exc).__name__}: {exc}"
    message = message.replace(dsn, _describe_dsn(dsn))
    message = message.replace(_asyncpg_dsn(dsn), _describe_dsn(dsn))
    try:
        password = make_url(dsn).password
    except Exception:  # noqa: BLE001 - DSN 解析失败时不做额外脱敏，原样返回类型与消息
        password = None
    if password:
        message = message.replace(password, "***")
    return message


def head_revision() -> str:
    """代码侧唯一 head（用于报告与断言；分叉时 alembic 自己会失败）。

    ★ 只读脚本目录，不需要也不构造任何数据库连接。
    """
    config = Config()
    config.set_main_option("script_location", str(_API_ROOT / "alembic"))
    return ScriptDirectory.from_config(config).get_current_head()


def run(
    targets: Sequence[Target],
    *,
    upgrade: Callable[[Target], None] = upgrade_one,
    version_reader: Callable[[str], str | None] = read_version,
) -> int:
    """逐库迁移并打印报告；任一失败返回 1。"""
    head = head_revision()
    failures = 0
    for target in targets:
        try:
            upgrade(target)
            version = version_reader(target.dsn)
        except Exception as exc:  # noqa: BLE001 - 逐库隔离失败，汇总后退出
            failures += 1
            print(
                f"{target.db_key:<16} FAIL  {_describe_dsn(target.dsn)}  "
                f"{safe_error(exc, target.dsn)}"
            )
            continue
        if version == head:
            print(f"{target.db_key:<16} PASS  {_describe_dsn(target.dsn)}  revision={version}")
        else:
            failures += 1
            print(
                f"{target.db_key:<16} FAIL  {_describe_dsn(target.dsn)}  "
                f"revision={version or '（空）'} != head={head}"
            )

    print(f"合计 {len(targets)} 个库位：通过 {len(targets) - failures}，失败 {failures}")
    return 1 if failures else 0


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    upgrade: Callable[[Target], None] = upgrade_one,
    version_reader: Callable[[str], str | None] = read_version,
) -> int:
    ap = argparse.ArgumentParser(
        description="逐库执行 alembic upgrade head（必须显式声明目标库）"
    )
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--all-scenes", action="store_true", help="控制库 + 全部已配置场景库")
    group.add_argument(
        "--db-key",
        action="append",
        metavar="KEY",
        help="目标库位键，可重复；default 表示控制库",
    )
    args = ap.parse_args(argv)

    resolved = settings or get_settings()
    try:
        targets = build_targets(
            resolved,
            db_keys=args.db_key or (),
            all_scenes=args.all_scenes,
        )
    except TargetError as exc:
        print(f"目标库配置错误：{exc}", file=sys.stderr)
        return 1

    return run(targets, upgrade=upgrade, version_reader=version_reader)


if __name__ == "__main__":
    raise SystemExit(main())
