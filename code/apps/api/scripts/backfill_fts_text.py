#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""回填 chunks.fts_text（中文重叠 2-gram + ASCII 整词）。

背景：迁移 0020 只加列与索引，已有 chunk 的 fts_text 为 NULL；查询侧切到
idx_chunk_fts_text 之前必须回填。切分口径 MUST 用
app/services/keyword_text.to_fts_text —— 在 SQL 里另写一份 bigram 逻辑必然与查询侧
不一致，表现为静默 0 召回（不报错、不告警）。

用法（容器内）：
    python scripts/backfill_fts_text.py --check    # 只统计未回填数（>0 退出码 1）
    python scripts/backfill_fts_text.py --dry-run  # 打印前几条转换结果，不写库
    python scripts/backfill_fts_text.py            # 分批回填全部
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import bindparam, func, select, update

from app.database import SessionLocal
from app.models import Chunk
from app.services.keyword_text import to_fts_text

# 分批大小：单条 fts_text 约为原文 1.8 倍字符数，500 条一批的 SQL 体量在数百 KB 级
_BATCH = 500


async def _pending() -> int:
    """统计尚未回填（fts_text IS NULL）的 chunk 数。"""
    async with SessionLocal() as session:
        value = await session.scalar(
            select(func.count()).select_from(Chunk).where(Chunk.fts_text.is_(None))
        )
    return int(value or 0)


async def main(batch: int, dry_run: bool, check: bool) -> int:
    if check:
        pending = await _pending()
        print(f"未回填：{pending} 条")
        return 1 if pending else 0

    # 只更新两列，走主键；不触碰 content 等原文（INV-M-11）。
    # 用 Core Table 而非 ORM 实体：ORM 的批量 UPDATE 会因 "WHERE 带额外条件" 要求
    # synchronize_session 策略，而回填脚本根本不需要回写内存对象。
    table = Chunk.__table__
    stmt = (
        update(table)
        .where(table.c.id == bindparam("b_id"))
        .values(fts_text=bindparam("b_fts_text"))
    )

    done = 0
    async with SessionLocal() as session:
        while True:
            rows = (
                await session.execute(
                    select(Chunk.id, Chunk.content, Chunk.heading_path)
                    .where(Chunk.fts_text.is_(None))
                    .limit(batch)
                )
            ).all()
            if not rows:
                break

            payload = [
                {"b_id": row.id, "b_fts_text": to_fts_text(row.content, row.heading_path)}
                for row in rows
            ]
            if dry_run:
                for item in payload[:5]:
                    print(f"{item['b_id']} -> {item['b_fts_text'][:160]}")
                print(f"（dry-run）本批待写 {len(payload)} 条")
                return 0

            await session.execute(stmt, payload)
            await session.commit()
            done += len(payload)
            print(f"  已回填 {done} 条")
            if len(payload) < batch:
                break

    print(f"完成：{done} 条 chunk 已写入 fts_text")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", type=int, default=_BATCH)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--check", action="store_true", help="只统计未回填数量（>0 退出码为 1）"
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.batch, args.dry_run, args.check)))
