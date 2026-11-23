#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Step 5 只读盘点：数据层路由当前是否处于「应该有的样子」。

用法（容器内 / 后端根 code/apps/api）：
    python scripts/db_routing_report.py
    python scripts/db_routing_report.py --show-dsn-hosts

六项检查（任一 FAIL → 退出码 1）：
    ① 存储层默认态   Step 5 只允许 db_binding=NULL
    ② 视图恒等       v_scenario_db_routing 行数 == scenarios 行数
    ③ kb 冲突        同一 kb 不得被两个场景绑到不同库位（健康态 0 行）
    ④ 注册表自洽     关闭态只登记 default 且不建任何引擎
    ⑤ 连接预算       report.needed_connections <= report.budget
    ⑥ 启用态键覆盖   启用态下 DB 里绑定的库位都必须在注册表里

★ 全程只读：只执行 SELECT / 视图查询，不建引擎之外的任何副作用；
  `register_from_settings()` 在关闭态零建引擎，启用态只建对象不连库。
★ 默认不打印任何 DSN 相关信息；`--show-dsn-hosts` 也只打印 host:port，
  永不输出账号、密码或完整 DSN。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from urllib.parse import urlsplit

from sqlalchemy import text

from app.config.settings import get_settings
from app.database import SessionLocal
from app.infra.db_router import RouterReport, register_from_settings


@dataclass(frozen=True)
class CheckResult:
    """一项检查的结果：`status` 只取 PASS / FAIL / N/A，`action` 只在 FAIL 时有值。"""

    name: str
    status: str
    detail: str
    action: str | None


# 与 check_connection_budget 的闸门同款处置口径：只允许降池 / 减库位 / 减副本，
# 调大 max_connections 是掩盖问题而不是解决问题。
_BUDGET_ACTION = (
    "处置：① 降 DB_POOL_SIZE/DB_MAX_OVERFLOW；② 减库位（SCENE_DB_DSN 条目）；"
    "③ 减副本（DB_REPLICAS_EXPECTED）。★ 不要调大 max_connections"
)


def evaluate(
    *,
    split_enabled: bool,
    bound_count: int,
    view_count: int,
    scenario_count: int,
    conflict_rows: list[dict],
    report: RouterReport,
    bound_keys: set[str],
    registered_keys: tuple[str, ...],
) -> list[CheckResult]:
    """把「配置 + 取数结果」判定成六项检查（纯函数、零 IO，便于单测）。

    `bound_count` / `view_count` / `scenario_count` 来自 count 查询；
    `conflict_rows` 是 `v_kb_db_binding_conflicts` 视图的原样行；
    `bound_keys` 是 `SELECT DISTINCT db_binding ...` 的结果（DB 侧实际绑定的库位）；
    `registered_keys` 是本进程注册表里的键（即 `report.keys`）。
    """
    results: list[CheckResult] = []

    # ① 存储层默认态：Step 5 阶段不允许任何场景静默落别库
    if bound_count == 0:
        results.append(CheckResult("存储层默认态", "PASS", "bound_count=0", None))
    else:
        results.append(
            CheckResult(
                "存储层默认态",
                "FAIL",
                f"bound_count={bound_count}（Step 5 阶段应为 0）",
                "Step 5 只允许 db_binding=NULL；若非 NULL，确认该库位 DSN 已配且"
                " Step 6 已上线，否则把它改回 NULL",
            )
        )

    # ② 视图恒等：LEFT JOIN + 唯一 published 索引决定「视图行数 == 场景行数」
    if view_count == scenario_count:
        results.append(
            CheckResult(
                "视图恒等",
                "PASS",
                f"view={view_count} scenario={scenario_count}",
                None,
            )
        )
    else:
        results.append(
            CheckResult(
                "视图恒等",
                "FAIL",
                f"view_count={view_count} != scenario_count={scenario_count}",
                "0030 视图被改坏（LEFT JOIN 被改 JOIN / 唯一 published 索引失效），"
                "重跑 alembic",
            )
        )

    # ③ kb 冲突：跨行聚合条件，CHECK/UNIQUE 表达不了，只能盘点
    if not conflict_rows:
        results.append(CheckResult("kb 冲突", "PASS", "conflicts=0", None))
    else:
        listed = "；".join(
            f"{row['tenant_id']}/{row['kb_id']}/{row['db_keys']}"
            for row in conflict_rows[:5]
        )
        if len(conflict_rows) > 5:
            listed += f"；…（共 {len(conflict_rows)} 行，只列前 5 行）"
        results.append(
            CheckResult(
                "kb 冲突",
                "FAIL",
                listed,
                "同一 kb 被两个场景绑到不同库位：把其中一个场景改回同一库位，"
                "或调整 kb_ids",
            )
        )

    # ④ 注册表自洽：关闭态「只登记 default、零建引擎」是路由层存在的全部代价
    if not split_enabled:
        consistent = registered_keys == ("default",) and report.created_engines == 0
        detail = (
            f"registered={registered_keys} created_engines={report.created_engines}"
        )
        results.append(
            CheckResult(
                "注册表自洽",
                "PASS" if consistent else "FAIL",
                detail,
                None
                if consistent
                else "关闭态不得建桶；检查 register_from_settings 是否被改动",
            )
        )
    else:
        results.append(
            CheckResult("注册表自洽", "N/A", "启用态不适用", None)
        )

    # ⑤ 连接预算：算术只在 db_router._arithmetic 里，这里只读 report 字段
    if report.needed_connections <= report.budget:
        results.append(
            CheckResult(
                "连接预算",
                "PASS",
                f"needed={report.needed_connections} budget={report.budget}",
                None,
            )
        )
    else:
        results.append(
            CheckResult(
                "连接预算",
                "FAIL",
                f"needed={report.needed_connections} > budget={report.budget}",
                _BUDGET_ACTION,
            )
        )

    # ⑥ 启用态键覆盖：DB 里绑了、本进程没配 → 取引擎时 fail-closed
    if not split_enabled:
        results.append(CheckResult("启用态键覆盖", "N/A", "分流关闭", None))
    else:
        missing = bound_keys - set(registered_keys)
        if not missing:
            results.append(
                CheckResult("启用态键覆盖", "PASS", "已绑定库位均已注册", None)
            )
        else:
            gap = "、".join(sorted(missing))
            results.append(
                CheckResult(
                    "启用态键覆盖",
                    "FAIL",
                    f"缺少库位注册：{gap}",
                    f"给这些库位配 SCENE_DB_DSN（缺 {gap}），或把 db_binding 改回 NULL",
                )
            )

    return results


def _format_dsn_hosts(scene_db_dsn: dict[str, str]) -> str:
    """把库位 DSN 渲染成 `key → host:port`（纯函数）。

    ★ 只取 hostname 与 port：账号、密码、库名、查询参数一律不输出，
      因此这份文本可以安全贴进工单与发布记录。
    """
    lines: list[str] = []
    for key in sorted(scene_db_dsn):
        parts = urlsplit(scene_db_dsn[key])
        host = parts.hostname
        if parts.port is None:
            lines.append(f"{key} → {host}")
        else:
            lines.append(f"{key} → {host}:{parts.port}")
    return "\n".join(lines)


async def run(show_dsn_hosts: bool) -> int:
    """执行盘点：注册 → 五条只读查询 → 判定 → 打印 → 返回退出码。"""
    settings = get_settings()
    try:
        # 关闭态零建引擎；启用态只建引擎对象，不建立连接
        report = register_from_settings()

        async with SessionLocal() as session:
            bound_count = int(
                await session.scalar(
                    text("SELECT count(*) FROM scenarios WHERE db_binding IS NOT NULL")
                )
            )
            scenario_count = int(
                await session.scalar(text("SELECT count(*) FROM scenarios"))
            )
            view_count = int(
                await session.scalar(text("SELECT count(*) FROM v_scenario_db_routing"))
            )
            conflict_rows = [
                dict(row)
                for row in (
                    await session.execute(
                        # ★ 视图不是函数：0030 建的是视图 `v_kb_db_binding_conflicts`，
                        #   函数名是 `fn_kb_db_binding_conflicts()`。写成 `...conflicts()`
                        #   会让真库报 UndefinedFunctionError（纯单测抓不到，只有真库能）。
                        text("SELECT * FROM v_kb_db_binding_conflicts")
                    )
                )
                .mappings()
                .all()
            ]
            bound_keys = {
                str(row[0])
                for row in (
                    await session.execute(
                        text(
                            "SELECT DISTINCT db_binding FROM scenarios "
                            "WHERE db_binding IS NOT NULL ORDER BY 1"
                        )
                    )
                ).all()
            }
    except Exception as exc:
        # 不打 traceback 也不吞真相：给出异常类型与消息（不含 DSN）
        print(
            f"盘点失败：数据库不可达或只读查询出错（{type(exc).__name__}: {exc}）",
            file=sys.stderr,
        )
        return 1

    results = evaluate(
        split_enabled=settings.scene_db_split_enabled,
        bound_count=bound_count,
        view_count=view_count,
        scenario_count=scenario_count,
        conflict_rows=conflict_rows,
        report=report,
        bound_keys=bound_keys,
        registered_keys=report.keys,
    )

    for index, result in enumerate(results, start=1):
        print(f"{index} {result.name}  {result.status}  {result.detail}")
        if result.status == "FAIL" and result.action:
            print(f"    → {result.action}")

    if show_dsn_hosts:
        print("库位 DSN 主机：")
        hosts = _format_dsn_hosts(settings.scene_db_dsn)
        print(hosts if hosts else "（SCENE_DB_DSN 未配置）")

    return 1 if any(result.status == "FAIL" for result in results) else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Step 5 数据层路由只读盘点（不改任何数据）")
    ap.add_argument(
        "--show-dsn-hosts",
        action="store_true",
        help="额外打印每个库位 DSN 的 host:port（永不打印账号密码）",
    )
    args = ap.parse_args()
    return asyncio.run(run(args.show_dsn_hosts))


if __name__ == "__main__":
    raise SystemExit(main())
