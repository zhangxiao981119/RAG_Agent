#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按当前 to_embedding_text() 口径重算全量 chunk 向量。

用途：to_embedding_text() 的改动（补 [heading_path] 前缀）等价于变更索引口径，
历史 chunk 的 embedding 是旧公式生成的，新旧向量不同构，必须全量重算，
否则向量路召回对新文档生效、对旧文档失效，且两者分数不可比。

用法（容器内）：
    python scripts/reindex_embeddings.py [--batch 32] [--dry-run]
"""

from __future__ import annotations

import argparse
import traceback
import asyncio
import sys

from sqlalchemy import select, update

from app.database import SessionLocal
from app.models import Chunk
from app.services.embedding import get_embedding_service


async def main(batch_size: int, dry_run: bool) -> int:
    embedder = get_embedding_service()

    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(
                    Chunk.id,
                    Chunk.content,
                    Chunk.heading_path,
                ).where(Chunk.is_latest.is_(True))
            )
        ).all()

    total = len(rows)
    print(f"待重算 chunk: {total}（is_latest=true）")

    if not rows:
        return 0

    if dry_run:
        for cid, content, heading in rows[:3]:
            prefix = f"[{heading}]\n" if heading else ""
            print(f"--- {cid}")
            print("  输入前 80 字:", (prefix + content)[:80].replace("\n", " / "))
        print("dry-run 结束，未写入")
        return 0

    done = 0
    for start in range(0, total, batch_size):
        part = rows[start : start + batch_size]
        texts = [
            (f"[{heading}]\n" if heading else "") + content
            for _cid, content, heading in part
        ]
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
            for (_cid, _content, _heading), vec in zip(part, vectors):
                await session.execute(
                    update(Chunk).where(Chunk.id == _cid).values(embedding=vec)
                )
            await session.commit()

        done += len(part)
        print(f"  已重算 {done}/{total}")

    print(f"完成：{done} 条 chunk 向量已按新口径重算")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    # ★ 默认 8 而非 settings.embedding_batch_size(32)：实测 32 条真实 chunk
    #   （平均 589 字符）会让 embedding 服务超过 60s 超时，16 条 OK、32 条 ReadTimeout
    parser.add_argument("--batch", type=int, default=8)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.batch, args.dry_run)))
