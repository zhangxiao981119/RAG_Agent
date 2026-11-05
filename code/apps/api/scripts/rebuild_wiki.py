#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""重建 wiki 导航页（离线脚本，设计文档 §10 / §7.3 第 4/7 行）。

用法（容器内）：
    python -m scripts.rebuild_wiki --all          # 全量（幂等短路：已是当前口径且内容未变的文档不调 LLM）
    python -m scripts.rebuild_wiki --recaliber    # 只重建口径不匹配的页（换模型 / bump prompt 后）
    python -m scripts.rebuild_wiki --stale-only   # 只重建 stale / failed 的页（重试没成的）
    python -m scripts.rebuild_wiki --document <id>
    python -m scripts.rebuild_wiki --all --kb-id <id> --limit 50

幂等：build_and_store 的幂等键 (document_id, generator_model, prompt_version,
content_hash) —— hash 相同直接跳过（不调 LLM），所以 --all 的实际 LLM 调用数
= 「输入或口径有变化的文档数」。

★ 为什么脚本也 MUST 走 services 的写入路径（build_and_store）：
  与 backfill_graph 同理 —— 脚本若自己拼一套「生成 → 写页」，漏掉 DELETE 旧页
  就会撞 uq_wiki_pages_document，或留下指向死 chunk 的旧页。

★ 为什么不在脚本里入队 arq：与 backfill_graph 同理 —— 补建是一次性、可观测、
  可中断的运维动作，串行执行 + 逐篇提交才可以用 Ctrl-C 干净地停在两篇之间。

★ WIKI_ENABLED=false → 直接报错退出（退出码 1）：关闭态下页不参与任何读路径，
  白烧 LLM 生成出来的页没有人能看见 —— 这是假工作。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from sqlalchemy import select, text

from app.config.settings import get_settings
from app.config.wiki_profile import current_wiki_profile
from app.database import SessionLocal
from app.models import Document
from app.services.wiki.build import WikiBuildSkip
from app.workers.graph_job import build_and_store

# 口径匹配的判定（--recaliber 的反面），与 documents 的 graph 口径同款 IS NOT DISTINCT
_CALIBER_MATCH = (
    "(generator_model IS NOT DISTINCT FROM :m AND prompt_version IS NOT DISTINCT FROM :pv)"
)


def _params() -> dict[str, str]:
    profile = current_wiki_profile()
    return {"m": profile.generator_model, "pv": profile.prompt_version}


def _base_select(mode: str):
    """按模式构造选取语句（id + filename，便于打印进度）。"""
    stmt = select(Document.id, Document.filename).where(
        Document.is_latest.is_(True),
        Document.deleted_at.is_(None),
        # 与 backfill_graph 同一分母：没有 chunks 的文档生成不出页，
        # 选中它只会得到一条 skip 噪音
        Document.status == "indexed",
    )
    if mode == "recaliber":
        # 有页且口径不匹配（没页的文档归 --all 管，否则 --recaliber 会悄悄变全量）
        stmt = stmt.where(
            text(f"EXISTS (SELECT 1 FROM wiki_pages w "
                 f"WHERE w.document_id = documents.id AND NOT {_CALIBER_MATCH})")
        )
    elif mode == "stale-only":
        # 重试没成的页：stale / failed。已 ready 的页不动 —— 那是 --all 的幂等短路管的
        stmt = stmt.where(
            text("EXISTS (SELECT 1 FROM wiki_pages w "
                 "WHERE w.document_id = documents.id "
                 "  AND w.status IN ('stale','failed'))")
        )
    # --all：全部已索引文档（幂等短路让已就绪的文档零 LLM 成本）
    return stmt.order_by(Document.created_at)


async def _run(args: argparse.Namespace) -> int:
    mode = "recaliber" if args.recaliber else ("stale-only" if args.stale_only else "all")
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
        print("没有需要处理的文档（页已按当前口径就绪）")
        return 0

    print(f"待处理 {len(rows)} 篇（模式={mode}）")
    if args.dry_run:
        for document_id, filename in rows[: args.show]:
            print(f"  {document_id}  {filename}")
        if len(rows) > args.show:
            print(f"  …（其余 {len(rows) - args.show} 篇省略）")
        print("（dry-run）未写入任何数据、未调用 LLM")
        return 0

    ok = skipped = failed = 0
    for index, (document_id, filename) in enumerate(rows, start=1):
        try:
            result = await build_and_store(document_id)
        except WikiBuildSkip:
            result = "skipped"
        except Exception as exc:  # 脚本视角：一篇失败不拖垮整批（与 backfill_graph 同款）
            print(f"[{index}/{len(rows)}] FAIL {filename}: {type(exc).__name__}: {exc}")
            failed += 1
            continue
        if result == "succeeded":
            ok += 1
            print(f"[{index}/{len(rows)}] OK   {filename}")
        elif result == "skipped_idempotent":
            # 幂等命中（hash 相同跳过，未调 LLM）—— 与「无分片」的 skip 分开计数但
            # 都进「跳过」桶：运维看汇总才知道哪些文档零 LLM 成本
            skipped += 1
            print(f"[{index}/{len(rows)}] SKIP {filename}（页已按当前口径就绪，未调 LLM）")
        elif result == "skipped":
            skipped += 1
            print(f"[{index}/{len(rows)}] SKIP {filename}（无分片）")
        else:
            # 其余非成功态按失败计（"dead" 等）
            failed += 1
            print(f"[{index}/{len(rows)}] FAIL {filename}（result={result}）")

    print(f"\n完成：成功 {ok} / 跳过 {skipped} / 失败 {failed}")
    return 0 if failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="重建 wiki 导航页")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--all", action="store_true",
                       help="全量重建（幂等短路：口径与内容未变的文档不调 LLM）")
    modes.add_argument("--recaliber", action="store_true",
                       help="只重建口径不匹配的页（换生成模型 / bump prompt 后）")
    modes.add_argument("--stale-only", action="store_true",
                       help="只重建 stale / failed 的页（重试没成的）")
    parser.add_argument("--document", type=str, default=None, help="只处理这一篇")
    parser.add_argument("--kb-id", type=str, default=None, help="只处理这个知识库")
    parser.add_argument("--limit", type=int, default=None, help="最多处理 N 篇")
    parser.add_argument("--dry-run", action="store_true", help="只列出将处理的文档")
    parser.add_argument("--show", type=int, default=20, help="dry-run 最多列出的条数")
    args = parser.parse_args()

    if not get_settings().wiki_enabled:
        print(
            "FAIL: WIKI_ENABLED=false —— wiki 能力未开启，生成的页没有任何读路径，"
            "这是假工作。先在 .env 打开 WIKI_ENABLED 并重启 worker/api。",
            file=sys.stderr,
        )
        return 1

    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
