#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""补建 / 重抽实体图（离线脚本）。

用法（容器内）：
    python -m scripts.backfill_graph --check        # 只统计就绪度（未就绪退出码 1）
    python -m scripts.backfill_graph --dry-run      # 只列出将被处理的文档，不写库
    python -m scripts.backfill_graph --all          # 补建全部「未按当前口径抽过」的文档
    python -m scripts.backfill_graph --pending      # 只补 pending / running（崩溃残留）
    python -m scripts.backfill_graph --recaliber    # 只重抽口径不匹配的文档
    python -m scripts.backfill_graph --document <id>
    python -m scripts.backfill_graph --all --kb-id <id> --limit 50

幂等：每篇文档都走「先 delete_by_document 再 upsert_subgraph」，重复执行终态一致。
断点续跑：逐篇提交（每篇一个事务），中断后再跑一遍即可 —— 选中条件是**状态驱动**的
（graph_status 与口径列），不依赖任何本地进度文件（本地进度文件在容器里会丢）。

★ 为什么脚本也 MUST 走 services 的写入路径（extract_and_store）：
  脚本若自己拼一套「抽 → 写」，漏掉先删就会留下重抽前的旧边 —— 而那种残留只在
  图召回的结果里才看得出来（多出几条本不该有的邻居片），不报错。

★ 为什么不在脚本里入队 arq 任务：
  补建是**一次性、可观测、可中断**的运维动作。走队列会把「跑到第几篇了」埋进
  worker 日志，且无法用 Ctrl-C 干净地停在两篇文档之间。串行执行 + 逐篇提交
  反而是这里最合适的形态（worker 池已被 OCR 串行化占用，脚本再抢并发只会互相拖慢）。
"""
from __future__ import annotations

import argparse
import asyncio
import uuid
from typing import Any

from sqlalchemy import select, text

from app.config.graph_profile import current_graph_profile
from app.config.settings import get_settings
from app.database import SessionLocal
from app.models import Document
from app.services.graph.extract import GraphExtractSkip
from app.workers.graph_job import extract_and_store

# 待补建的定义（**唯一**一处，--check 与选择条件共用，避免两处口径漂移）：
# 「未按当前口径抽成功」—— failed / pending / running，以及 done 但口径不匹配。
_CALIBER_MATCH = """(graph_status = 'done'
    AND graph_extractor_model IS NOT DISTINCT FROM :m
    AND graph_prompt_version  IS NOT DISTINCT FROM :pv)"""


def _params() -> dict[str, Any]:
    profile = current_graph_profile()
    return {"m": profile.extractor_model, "pv": profile.prompt_version}


def _base_select(mode: str):
    """按模式构造选取语句（id + filename，便于打印进度）。"""
    stmt = select(Document.id, Document.filename).where(
        Document.is_latest.is_(True),
        Document.deleted_at.is_(None),
        # 只补建已解析完成的文档：没有 chunks 的文档抽不出任何东西，
        # 选中它只会得到一条 skipped 噪音。
        Document.status == "indexed",
    )
    if mode == "pending":
        stmt = stmt.where(Document.graph_status.in_(("pending", "running")))
    elif mode == "recaliber":
        stmt = stmt.where(Document.graph_status == "done").where(
            text(f"NOT {_CALIBER_MATCH}")
        )
    else:  # all
        stmt = stmt.where(text(f"NOT {_CALIBER_MATCH}")).where(
            # skipped 是「明确不该抽」（内容为空 / 超分片上限）：默认不再打扰，
            # 否则每次 --all 都会把同一批文档重新判一次、再刷一批同样的日志。
            Document.graph_status != "skipped"
        )
    return stmt.order_by(Document.created_at)


async def _check() -> int:
    """就绪度体检。返回退出码（0 = 就绪）。"""
    params = _params()
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text(
                    "SELECT "
                    "  count(*) AS total, "
                    f"  count(*) FILTER (WHERE {_CALIBER_MATCH}) AS done, "
                    "  count(*) FILTER (WHERE graph_status = 'pending') AS pending, "
                    "  count(*) FILTER (WHERE graph_status = 'running') AS running, "
                    "  count(*) FILTER (WHERE graph_status = 'failed') AS failed, "
                    "  count(*) FILTER (WHERE graph_status = 'skipped') AS skipped, "
                    "  count(*) FILTER (WHERE graph_status = 'done' "
                    f"       AND NOT {_CALIBER_MATCH}) AS caliber_mismatch "
                    "FROM documents WHERE is_latest = true AND deleted_at IS NULL"
                ),
                params,
            )
        ).one()
        entities = int((await session.scalar(text("SELECT count(*) FROM graph_entities"))) or 0)
        relations = int(
            (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM graph_relations "
                        "WHERE extractor_model = :m AND prompt_version = :pv"
                    ),
                    params,
                )
            )
            or 0
        )
        # 一致性巡检（设计文档 §20 U4）：标了 done 却一条提及都没有的文档。
        # 典型成因是「关图期间重解析」（chunk 级联删掉了提及，状态却还是 done）。
        empty_done = int(
            (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM documents d "
                        "WHERE d.is_latest = true AND d.deleted_at IS NULL "
                        "  AND d.graph_status = 'done' "
                        f"  AND {_CALIBER_MATCH} "
                        "  AND NOT EXISTS ("
                        "    SELECT 1 FROM graph_entity_mentions m "
                        "    WHERE m.document_id = d.id)"
                    ),
                    params,
                )
            )
            or 0
        )

    not_ready = int(row.pending) + int(row.running) + int(row.failed) + int(row.caliber_mismatch)
    print(f"文档总数      : {row.total}")
    print(f"已按当前口径完成: {row.done}")
    print(f"待补建(pending): {row.pending}")
    print(f"抽取中(running): {row.running}")
    print(f"失败(failed)   : {row.failed}")
    print(f"跳过(skipped)  : {row.skipped}")
    print(f"口径不匹配     : {row.caliber_mismatch}")
    print(f"实体 / 关系    : {entities} / {relations}")
    if empty_done:
        print(
            f"⚠ 标 done 但零提及: {empty_done} 篇 —— 常见成因是「关图期间重解析」"
            "（chunk 级联删掉了提及）。建议重抽：--recaliber 或 --document <id>"
        )
    print(f"未就绪合计     : {not_ready}")
    return 0 if not_ready == 0 else 1


async def _mark_failed(document_id: uuid.UUID) -> None:
    async with SessionLocal() as session:
        document = await session.get(Document, document_id)
        if document is not None:
            document.graph_status = "failed"
            await session.commit()


async def _mark_skipped(document_id: uuid.UUID) -> None:
    """把文档标成「明确跳过」（零分片 / 超分片上限）。

    ★ MUST 与 failed 分开落库：`--all` 的选取条件只排除 skipped，failed 会被每次重选。
      把「本来就不该抽」的文档记成 failed，等于让它在每轮补建里空跑一次并刷一行
      FAILED 日志 —— 既浪费一次抽取尝试，也让真正的失败被淹没。
    ★ `extract_and_store` 自己不管状态机（只做「抽 → 写三表 → 置 done」），
      所以这里必须由脚本写状态；arq 侧对应的是 run_graph_extract 的 skip 分支。
    """
    async with SessionLocal() as session:
        document = await session.get(Document, document_id)
        if document is not None:
            document.graph_status = "skipped"
            await session.commit()


async def _run(mode: str, args: argparse.Namespace) -> int:
    stmt = _base_select(mode)
    if args.document:
        stmt = stmt.where(Document.id == uuid.UUID(args.document))
    if args.kb_id:
        stmt = stmt.where(Document.kb_id == uuid.UUID(args.kb_id))
    if args.limit:
        stmt = stmt.limit(args.limit)

    async with SessionLocal() as session:
        rows = (await session.execute(stmt, _params())).all()
    if not rows:
        print("没有需要处理的文档（已按当前口径完成）")
        return 0

    print(f"待处理 {len(rows)} 篇（模式={mode}）")
    if args.dry_run:
        for document_id, filename in rows[: args.show]:
            print(f"  {document_id}  {filename}")
        if len(rows) > args.show:
            print(f"  …（其余 {len(rows) - args.show} 篇省略）")
        print("（dry-run）未写入任何数据")
        return 0

    ok = skipped = failed = 0
    for index, (document_id, filename) in enumerate(rows, start=1):
        try:
            status = await extract_and_store(document_id)
        except GraphExtractSkip as exc:
            # ★ MUST 排在 `except Exception` 之前：GraphExtractSkip 表达的是
            #   「这篇文档不该抽」（零分片 / 超护栏），不是「这次没抽成」。
            #   归到 failed 会让它被 --all 每轮重选、永远修不好，且 --check 的
            #   「失败 N / 跳过 0」把可忽略的跳过报成需要人工处理的失败。
            await _mark_skipped(document_id)
            skipped += 1
            print(f"  [{index}/{len(rows)}] skip   {document_id} {filename}（{exc}）")
            continue
        except Exception as exc:
            # 单篇失败不中断整轮：其余文档照常补建，失败项在结尾汇总
            #（与 arq 的「单条失败不阻塞其他文档」同一口径）。
            await _mark_failed(document_id)
            failed += 1
            print(f"  [{index}/{len(rows)}] FAILED {document_id} {filename}: "
                  f"{type(exc).__name__}: {exc}")
            continue
        if status == "succeeded":
            ok += 1
            print(f"  [{index}/{len(rows)}] ok     {document_id} {filename}")
        else:
            skipped += 1
            print(f"  [{index}/{len(rows)}] skip   {document_id} {filename}（{status}）")

    print(f"完成：成功 {ok} · 跳过 {skipped} · 失败 {failed}")
    return 0 if failed == 0 else 1


async def main(args: argparse.Namespace) -> int:
    settings = get_settings()
    # fail-closed：关闭能力时不抽取。这里 MUST 明确拒绝而不是「照抽不误」——
    # 抽了也没人读（在线召回未启用），白花 LLM 成本且让图数据与开关状态不一致。
    if not settings.graph_enabled:
        print(
            "GRAPH_ENABLED=false：图抽取未启用，脚本不执行。\n"
            "  如确需补建：置 GRAPH_ENABLED=true 与 LLM_API_KEY（并同时重启 api 与 worker），"
            "然后重跑本脚本。"
        )
        return 2

    if args.check:
        return await _check()

    mode = "all"
    if args.pending:
        mode = "pending"
    elif args.recaliber:
        mode = "recaliber"
    return await _run(mode, args)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="补建 / 重抽实体图")
    parser.add_argument("--all", action="store_true", help="补建全部未按当前口径完成的文档")
    parser.add_argument("--pending", action="store_true", help="只补 pending / running")
    parser.add_argument("--recaliber", action="store_true", help="只重抽口径不匹配的文档")
    parser.add_argument("--document", help="只处理指定文档 id")
    parser.add_argument("--kb-id", dest="kb_id", help="只处理指定知识库")
    parser.add_argument("--limit", type=int, default=0, help="本轮最多处理多少篇（0 = 不限）")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", help="只列出，不写库")
    parser.add_argument("--show", type=int, default=20, help="dry-run 最多列出多少篇")
    parser.add_argument("--check", action="store_true", help="只统计就绪度（未就绪退出码 1）")
    _args = parser.parse_args()
    raise SystemExit(asyncio.run(main(_args)))
