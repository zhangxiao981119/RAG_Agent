#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""S6-6：场景库单 kb 数据清理 + 双库孤儿检查（设计 §4.7）。

用法（后端根 code/apps/api）：
    # 只看行数（默认不删）
    python -m scripts.scene_db_cleanup --control-dsn <控制库 DSN> --scene-dsn <场景库 DSN> \\
        --kb-id <UUID>

    # 执行清理（维护窗口内；控制库 kb 仍由 API 删除以保留审计）
    python -m scripts.scene_db_cleanup --control-dsn <控制库 DSN> --scene-dsn <场景库 DSN> \\
        --kb-id <UUID> --apply

    # 孤儿检查（场景库存在控制库已删除的 kb，或控制库残留陈旧数据面行）
    python -m scripts.scene_db_cleanup --control-dsn <控制库 DSN> --scene-dsn <场景库 DSN>

退出码：0 = 报告 ok（清理完成且无残留 / 无孤儿）；1 = 有孤儿、清理残留或执行出错。
★ 永不打印口令：报告只出现 `host:port/dbname`。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from app.infra.scene_db_cleanup import (
    CleanupError,
    check_orphans,
    cleanup_scene_kb,
)
from app.infra.scene_db_migrate import MigrateError
from scripts.scene_db_surgery import _asyncpg_dsn


def _print_kb_report(report: dict[str, Any]) -> None:
    action = "清理" if report["direction"] == "cleanup" else "只读统计"
    print(
        f"方向：{action}  场景库 {report['scene']}  kb={report['kb_id']}  "
        f"控制库存在={'是' if report['existed_in_control'] else '否'}"
    )
    for item in report["tables"]:
        print(
            f"  {item['table']:<22} before={item['before']}  "
            f"deleted={item['deleted']}  after={item['after']}"
        )


def _print_orphans(report: dict[str, Any]) -> None:
    print(f"方向：孤儿检查  控制库 {report['control']}  场景库 {report['scene']}")
    scene_orphans = report["scene_orphans"]
    if scene_orphans:
        print("场景库孤儿（kb 已不在控制库）：")
        for table, by_kb in scene_orphans.items():
            detail = ", ".join(f"{kb}={count}" for kb, count in sorted(by_kb.items()))
            print(f"  {table:<22} {detail}")
    else:
        print("场景库孤儿：无")
    control_orphans = report["control_orphans"]
    if control_orphans:
        print("控制库陈旧数据面残留：")
        for table, count in control_orphans.items():
            print(f"  {table:<22} {count}")
    else:
        print("控制库陈旧数据面残留：无")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="S6-6 场景库单 kb 数据清理与孤儿检查（跨库 CASCADE 失效的配套件）"
    )
    parser.add_argument("--control-dsn", required=True, help="控制库 DSN")
    parser.add_argument("--scene-dsn", required=True, help="场景库 DSN")
    parser.add_argument(
        "--kb-id", default=None, help="要统计/清理的知识库 UUID；缺省则只做孤儿检查"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正删除场景库中该 kb 的搬迁集行（默认只统计）",
    )
    parser.add_argument(
        "--allow-missing-kb",
        action="store_true",
        help="允许清理控制库已不存在的 kb（孤儿清理场景，必须显式开启）",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args()

    control_dsn = _asyncpg_dsn(args.control_dsn)
    scene_dsn = _asyncpg_dsn(args.scene_dsn)
    try:
        if args.kb_id:
            report = asyncio.run(
                cleanup_scene_kb(
                    control_dsn,
                    scene_dsn,
                    args.kb_id,
                    apply=args.apply,
                    allow_missing_kb=args.allow_missing_kb,
                )
            )
        else:
            report = asyncio.run(check_orphans(control_dsn, scene_dsn))
    except (CleanupError, MigrateError) as exc:
        print(
            f"执行失败 {exc.code}：{json.dumps(exc.detail, ensure_ascii=False)}",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        # 与其它运维脚本同款：只给异常类型与消息，绝不回显 DSN
        print(f"执行失败（{type(exc).__name__}: {exc}）", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.kb_id:
        _print_kb_report(report)
    else:
        _print_orphans(report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
