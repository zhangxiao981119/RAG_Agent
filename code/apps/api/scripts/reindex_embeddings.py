#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按当前索引口径重算**过期** chunk 的向量。

过期定义：chunk 落库的 (embedding_model, embedding_text_version) 与当前口径不一致。
  · 换 embedding 模型      → 全库过期（不同模型向量空间不同构，不可混用）
  · 改 to_embedding_text 公式并 bump EMBEDDING_TEXT_VERSION → 全库过期
  · 仅 chunker_version 变化 → **不**触发重算：分块粒度不影响向量空间，
    它只作为「是否建议重切」的提示（见 app/config/index_profile.py）。

用法（容器内）：
    python scripts/reindex_embeddings.py             # 重算所有过期 chunk
    python scripts/reindex_embeddings.py --check     # 只统计过期数（过期>0 退出码为 1）
    python scripts/reindex_embeddings.py --dry-run   # 只打印前几条输入，不写入

★ 「过期」的判定谓词**不本文件里**：唯一实现是迁移 0029 的
  `fn_chunk_reindex_candidates(...)`，本脚本与 `GET /api/admin/needs-reindex`、
  发布快照、巡检视图都调它。理由写在那个迁移的 docstring 里（I-4B-2）：
  两处各写一遍 predicate，第一次分叉就会产生「视图说 0 条、脚本说 300 条」，
  而这类分叉没有任何单一行为用例能同时覆盖两边。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import traceback

from sqlalchemy import text, update

from app.config.index_profile import current_index_profile
from app.database import SessionLocal
from app.models import Chunk
from app.services.chunk import ChunkData, to_embedding_text
from app.services.embedding import get_embedding_service


async def _load_stale() -> list:
    """取出所有硬口径与当前不一致的 is_latest chunk。

    ★ 谓词来自 `fn_chunk_reindex_candidates`（迁移 0029），**不在这里重写**：
      视图 / 端点 / 发布快照与 `--check` 门禁必须同源，否则「视图说 0 条、
      脚本说 300 条」这类分叉无从发现（I-4B-2）。守卫：`T-4B-10`。
    ★ 本函数只投影重算所需的五列：**判定的形状归函数，取数的列归调用方**。
    ★ 加了 `ORDER BY` 不是顺手：原实现无排序，行序由堆表决定 → 分批重算时
      "哪 8 条先进一批"不可复现；一批失败后重跑，批次划分与上次不同 →
      "上次是哪几条失败"无法还原。确定性排序是零成本的。
    """
    profile = current_index_profile()
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT c.id, c.content, c.heading_path, c.token_count, c.page_no "
                    "FROM chunks c "
                    "JOIN fn_chunk_reindex_candidates(:m, :tv, :cv) f ON f.chunk_id = c.id "
                    "WHERE f.stale_hard "
                    "ORDER BY f.chunk_created_at, f.chunk_id"
                ),
                {
                    "m": profile.embedding_model,
                    "tv": profile.embedding_text_version,
                    "cv": profile.chunker_version,
                },
            )
        ).all()
    return list(rows)


def _row_text(row) -> str:
    """用与生产写入完全相同的 to_embedding_text 口径构造 embedding 输入。"""
    _cid, content, heading, token_count, page_no = row
    return to_embedding_text(
        ChunkData(
            content=content,
            token_count=token_count,
            heading_path=heading or "",
            page_no=page_no,
        )
    )


async def main(batch_size: int, dry_run: bool, check: bool) -> int:
    profile = current_index_profile()
    rows = await _load_stale()
    total = len(rows)
    print(
        f"过期 chunk（口径 != 当前 {profile.embedding_model} / "
        f"{profile.embedding_text_version}）: {total}"
    )

    if check:
        # 供 CI / 运维门禁：有过期即非 0 退出码
        return 1 if total else 0
    if total == 0:
        return 0

    if dry_run:
        for row in rows[:3]:
            print(f"--- {row[0]}")
            print("  输入前 80 字:", _row_text(row)[:80].replace("\n", " / "))
        print("dry-run 结束，未写入")
        return 0

    embedder = get_embedding_service()
    done = 0
    for start in range(0, total, batch_size):
        part = rows[start : start + batch_size]
        texts = [_row_text(row) for row in part]
        try:
            vectors = await embedder.embed(texts)
        except Exception:  # noqa: BLE001
            print(
                f"批次 {start} embedding 失败"
                f"（{len(part)} 条，总字符 {sum(len(t) for t in texts)}）",
                file=sys.stderr,
            )
            traceback.print_exc()
            return 1

        async with SessionLocal() as session:
            for row, vec in zip(part, vectors, strict=True):
                await session.execute(
                    update(Chunk)
                    .where(Chunk.id == row[0])
                    # ★ 这里**只**写三列 —— 全部是"本次真的重算过"的事实。
                    #   `chunker_version` **不写**：本脚本只重算向量、**没有重切**，
                    #   `row` 里的 `content` / `token_count` 仍是旧分块边界下的产物。
                    #   写新值等于把它标成"按新参数切的"，而那是个假事实 ——
                    #   它会让「建议重切」这个唯一用途在跑一次重算后永久消失
                    #   （`index_profile.py:19-21`），4B 的 `chunker_only` 也就
                    #   成了永远不会说话的观测项。守卫：`T-4B-6`。
                    .values(
                        embedding=vec,
                        embedding_model=profile.embedding_model,
                        embedding_text_version=profile.embedding_text_version,
                    )
                )
            await session.commit()

        done += len(part)
        print(f"  已重算 {done}/{total}")

    print(f"完成：{done} 条 chunk 向量已按新口径重算")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    # ★ 默认 8 而非 settings.embedding_batch_size：实测 32 条真实 chunk（平均 589 字符）
    #   会让 embedding 服务超过 60s 超时，16 条 OK、32 条 ReadTimeout
    parser.add_argument("--batch", type=int, default=8)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--check", action="store_true", help="只统计过期数量（过期>0 退出码为 1）"
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.batch, args.dry_run, args.check)))