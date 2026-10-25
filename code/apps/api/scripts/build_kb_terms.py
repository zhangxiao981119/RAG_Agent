#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""离线构建知识库领域词表 kb_terms（查询扩展依据）。

不依赖大模型，纯规则抽取两类词条：
  · model    型号/料号（XYZ-1000），aliases 为无分隔与空格两种写法
  · acronym  文档里成对出现的缩写与全称（`访问令牌（AccessToken）`的两个方向）
标识符（get_retrieval_service 这类）不抽：没有对照写法就没有扩展价值，
只会在表里堆一批永远扩不出东西的死数据。

用法（容器内）：
    python scripts/build_kb_terms.py --min-freq 1
"""
from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import delete, select

from app.config.settings import get_settings
from app.database import SessionLocal
from app.models import Chunk, KbTerm, KnowledgeBase, Tenant
from app.services.lexicon import collect_terms


async def run(min_freq: int) -> int:
    settings = get_settings()
    async with SessionLocal() as session:
        tenant = await session.scalar(
            select(Tenant).where(Tenant.code == settings.tenant_code)
        )
        if tenant is None:
            raise RuntimeError("租户未初始化，先跑 scripts/seed.py")
        kbs = (
            await session.execute(
                select(KnowledgeBase).where(KnowledgeBase.tenant_id == tenant.id)
            )
        ).scalars().all()

        total = 0
        for kb in kbs:
            rows = (
                await session.execute(
                    select(Chunk.content, Chunk.heading_path).where(
                        Chunk.kb_id == kb.id, Chunk.is_latest.is_(True)
                    )
                )
            ).all()
            texts = [row.content for row in rows] + [
                row.heading_path for row in rows if row.heading_path
            ]
            terms = {
                term: (kind, aliases, freq)
                for term, (kind, aliases, freq) in collect_terms(texts).items()
                if freq >= min_freq
            }

            # 重建式写入：词表是派生数据，不做增量合并（旧条目会一直留着失效写法）
            await session.execute(delete(KbTerm).where(KbTerm.kb_id == kb.id))
            for term, (kind, aliases, freq) in terms.items():
                session.add(
                    KbTerm(
                        tenant_id=tenant.id,
                        kb_id=kb.id,
                        term=term,
                        aliases=aliases,
                        kind=kind,
                        freq=freq,
                    )
                )
            await session.commit()
            total += len(terms)
            kinds = {}
            for kind, _aliases, _freq in terms.values():
                kinds[kind] = kinds.get(kind, 0) + 1
            print(f"kb={kb.name} chunks={len(rows)} terms={len(terms)} {kinds}")

    print(f"合计写入 {total} 条")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-freq", type=int, default=1, help="低于该出现次数的词条丢弃")
    a = ap.parse_args()
    return asyncio.run(run(a.min_freq))


if __name__ == "__main__":
    raise SystemExit(main())
