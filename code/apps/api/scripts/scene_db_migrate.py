#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""存量搬迁（S6-5）：控制库单场景 kb 的数据面表 → 场景库，及回滚增量回拷。

用法（后端根 code/apps/api，用 -m 让 app / scripts 都可导入）：
    # 正向：控制库 → 场景库（场景库须先 alembic upgrade head + 已做 M-B 手术）
    python -m scripts.scene_db_migrate --control-dsn <控制库 DSN> --scene-dsn <场景库 DSN> \\
        --kb-id <UUID> [--probe-term 关键词]

    # 回滚：场景库新增行增量回拷控制库（只增不删、可重跑）
    python -m scripts.scene_db_migrate --control-dsn <控制库 DSN> --scene-dsn <场景库 DSN> \\
        --kb-id <UUID> --reverse

前置与纪律见 `02-design/第六阶段-Step6-前置裁决.md` §4.3 Phase 3 / §4.5：
    · 搬迁必须在停写窗口内执行；脚本用「COPY 前后源库摘要比对」检出窗口内写入；
    · 本脚本**不修改** `scenarios.db_binding`（Phase 4 才切流量，单独一步）；
    · 正向先 TRUNCATE 搬迁集再整表 COPY，可重复执行（幂等）；
    · 逐表校验行数 + id 摘要，chunks 另加正文长度与正文摘要；
    · 可选 `--probe-term` 在两侧各跑一次关键词 ILIKE 探测，比对命中 id 摘要
      （是关键词谓词等价，不代表整条检索链路等价）。

退出码：0 = 全部校验通过；1 = 任一项失败或执行出错。
★ 永不打印口令：报告只出现 `host:port/dbname`。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from app.infra.scene_db_migrate import (
    MigrateError,
    migrate_forward,
    migrate_reverse,
)
from scripts.scene_db_surgery import _asyncpg_dsn


def _print_forward(report: dict[str, Any]) -> None:
    print(f"方向：正向  控制库 {report['control']} → 场景库 {report['scene']}")
    print(f"kb_id：{report['kb_id']}")
    for item in report["tables"]:
        source = item["source"]
        line = (
            f"  {item['table']:<22} {'PASS' if item['ok'] else 'FAIL'}  "
            f"rows={source['row_count']} id_md5={source['id_md5'][:12] or '-'}"
        )
        if "content_len" in source:
            line += f" content_len={source['content_len']}"
        print(line)
    equivalence = report.get("equivalence")
    if equivalence is None:
        print("关键词探测等价：未执行（未提供 --probe-term）")
    else:
        state = "PASS" if equivalence["ok"] else "FAIL"
        print(
            f"关键词探测等价：{state}  term={equivalence['term']!r}  "
            f"control_hits={equivalence['control']['row_count']}  "
            f"scene_hits={equivalence['scene']['row_count']}"
        )


def _print_reverse(report: dict[str, Any]) -> None:
    print(f"方向：回滚回拷  场景库 {report['scene']} → 控制库 {report['control']}")
    print(f"kb_id：{report['kb_id']}")
    for item in report["tables"]:
        print(
            f"  {item['table']:<22} stage={item['stage_rows']}  "
            f"existing={item['existing']}  inserted={item['inserted']}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="S6-5 存量搬迁：单场景 kb 数据面表 COPY + 校验（正向/回滚）"
    )
    parser.add_argument("--control-dsn", required=True, help="控制库 DSN")
    parser.add_argument(
        "--scene-dsn",
        required=True,
        help="场景库 DSN（正向须已 alembic upgrade head 且完成 M-B 手术）",
    )
    parser.add_argument("--kb-id", required=True, help="要搬迁的知识库 UUID")
    parser.add_argument(
        "--probe-term",
        default=None,
        help="可选：关键词探测词，用于两侧关键词命中摘要比对（非整条检索链路等价）",
    )
    parser.add_argument(
        "--reverse",
        action="store_true",
        help="回滚方向：把场景库缺失行增量回拷控制库（只增不删）",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args()

    control_dsn = _asyncpg_dsn(args.control_dsn)
    scene_dsn = _asyncpg_dsn(args.scene_dsn)
    try:
        if args.reverse:
            report = asyncio.run(
                migrate_reverse(scene_dsn, control_dsn, args.kb_id)
            )
        else:
            report = asyncio.run(
                migrate_forward(
                    control_dsn, scene_dsn, args.kb_id, args.probe_term
                )
            )
    except MigrateError as exc:
        print(f"搬迁失败 {exc.code}：{json.dumps(exc.detail, ensure_ascii=False)}", file=sys.stderr)
        return 1
    except Exception as exc:
        # 与 scene_db_surgery 同款：只给异常类型与消息，绝不回显 DSN
        print(f"执行失败（{type(exc).__name__}: {exc}）", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.reverse:
        _print_reverse(report)
    else:
        _print_forward(report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
