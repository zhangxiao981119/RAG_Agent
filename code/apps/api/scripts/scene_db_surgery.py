#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""场景库 M-B 手术与守卫（S6-2）。

用法（后端根 code/apps/api）：
    python scripts/scene_db_surgery.py --dsn postgresql://user:pass@host:5432/kagent_finance
    python scripts/scene_db_surgery.py --dsn <场景库 DSN> --verify-only
    python scripts/scene_db_surgery.py --dsn <场景库 DSN> --json

前置条件（缺一不可）：
    1. 场景库已执行 `alembic upgrade head`（全链到当前 head，表结构与控制库一致）；
    2. **每次升级后都必须重跑本脚本，再执行 `--verify-only`** —— 新迁移可能把
       跨库外键加回来，只跑一次手术不能保证长期成立。

行为：
    默认先手术（删「搬迁集 → 控制面集」的全部跨库外键 + 装 5 张表的禁写触发器），
    再执行三项校验；`--verify-only` 只校验、不改 schema。
退出码：
    0 = 三项校验全 PASS；1 = 任一项 FAIL 或执行出错。

★ 永不打印完整 DSN：只输出 host:port/dbname，账号与口令一律脱敏。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any
from urllib.parse import urlsplit

from app.infra.scene_db_schema import apply_surgery, verify


def _asyncpg_dsn(raw: str) -> str:
    """把 SQLAlchemy 的 `+asyncpg` DSN 转成 asyncpg 原生 DSN。"""
    return raw.replace("postgresql+asyncpg://", "postgresql://")


def _describe_dsn(raw: str) -> str:
    """渲染成 `host:port/dbname`（纯函数）。

    ★ 只取主机、端口与库名：账号、口令、查询参数一律不输出，
      因此这份文本可以安全贴进工单与发布记录。
    """
    parts = urlsplit(_asyncpg_dsn(raw))
    host = parts.hostname or "?"
    port = parts.port or 5432
    database = (parts.path or "").lstrip("/") or "?"
    return f"{host}:{port}/{database}"


def _print_report(report: dict[str, Any]) -> None:
    """人类可读报告：手术摘要 + 三项校验。"""
    print(f"场景库：{report['target']}")

    surgery = report["surgery"]
    if surgery is None:
        print("手术：未执行（--verify-only）")
    else:
        print(f"手术：删除跨库外键 {len(surgery['dropped'])} 条；安装触发器 {len(surgery['triggers'])} 个")
        for item in surgery["dropped"]:
            print(
                f"    - {item['table']} → {item['references']}（{item['constraint']}）"
            )

    result = report["verify"]
    cross_db_fks = result["cross_db_fks"]
    print(
        f"1 跨库外键    {'PASS' if not cross_db_fks else 'FAIL'}  "
        f"remaining={len(cross_db_fks)}"
    )
    guard_tables = result["guard_tables"]
    print(
        f"2 白名单守卫  {'PASS' if not guard_tables else 'FAIL'}  "
        f"guard_tables={len(guard_tables)}"
    )
    for item in guard_tables:
        print(f"    - {item['table_name']}（{','.join(item['columns'])}）")

    missing = result["missing_triggers"]
    installed = len(result["triggers"]) - len(missing)
    print(
        f"3 禁写触发器  {'PASS' if not missing else 'FAIL'}  "
        f"{installed}/{len(result['triggers'])}"
    )
    for table in missing:
        print(f"    - 缺少触发器：{table}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="场景库 M-B 手术与守卫（跨库外键 + 白名单守卫 + 禁写触发器）"
    )
    parser.add_argument(
        "--dsn",
        required=True,
        help="场景库 DSN（postgresql:// 或 postgresql+asyncpg://，必须已 alembic upgrade head）",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="只做三项校验，不改 schema",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 输出（默认人类可读）",
    )
    args = parser.parse_args()

    target = _describe_dsn(args.dsn)
    report: dict[str, Any] = {"target": target, "surgery": None, "verify": None}
    try:
        if not args.verify_only:
            report["surgery"] = asyncio.run(apply_surgery(_asyncpg_dsn(args.dsn)))
        report["verify"] = asyncio.run(verify(_asyncpg_dsn(args.dsn)))
    except Exception as exc:
        # 与 db_routing_report 同款：给出异常类型与消息（不含 DSN），退出码 1
        print(
            f"执行失败（{type(exc).__name__}: {exc}）",
            file=sys.stderr,
        )
        return 1

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_report(report)
    return 0 if report["verify"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
