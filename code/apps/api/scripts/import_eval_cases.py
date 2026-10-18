#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按预检结果导入评测集（覆盖式）。

  · 先备份现有 eval_cases 到 eval_cases_backup_<tag>
  · 清空后用预检通过的用例重建
  · expected_doc_ids 按 expect 文件名反查 indexed 文档 id（同名多份取并集）

用法：
    python scripts/import_eval_cases.py --in /tmp/precheck.json --tag 1018
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import select, text

from app.config.settings import get_settings
from app.database import SessionLocal
from app.models import Document, EvalCase, KnowledgeBase, Tenant


async def run(src: Path, tag: str, keep_failed: bool) -> int:
    settings = get_settings()
    cases = json.loads(src.read_text(encoding="utf-8"))
    if not keep_failed:
        cases = [c for c in cases if c.get("pass")]

    async with SessionLocal() as session:
        tenant = await session.scalar(
            select(Tenant).where(Tenant.code == settings.tenant_code)
        )
        if tenant is None:
            raise RuntimeError("租户未初始化")
        kb = await session.scalar(
            select(KnowledgeBase).where(KnowledgeBase.tenant_id == tenant.id).limit(1)
        )
        kb_id = kb.id if kb else None

        # 备份
        backup = f"eval_cases_backup_{tag}"
        await session.execute(text(f'DROP TABLE IF EXISTS "{backup}"'))
        await session.execute(
            text(f'CREATE TABLE "{backup}" AS SELECT * FROM eval_cases')
        )
        n_old = await session.scalar(text(f'SELECT count(*) FROM "{backup}"'))
        await session.execute(text("DELETE FROM eval_cases"))
        await session.commit()

        # 文件名 → 文档 id
        docs = (
            await session.execute(
                select(Document).where(
                    Document.tenant_id == tenant.id, Document.status == "indexed"
                )
            )
        ).scalars().all()
        by_name: dict[str, list] = {}
        for d in docs:
            by_name.setdefault(d.filename, []).append(d.id)

        inserted = 0
        missing: dict[str, int] = {}
        for c in cases:
            expect = c.get("expect") or []
            if isinstance(expect, str):
                expect = [expect]
            doc_ids: list = []
            for fn in expect:
                ids = by_name.get(fn)
                if not ids:
                    missing[fn] = missing.get(fn, 0) + 1
                    continue
                doc_ids.extend(ids)
            session.add(
                EvalCase(
                    tenant_id=tenant.id,
                    kb_id=kb_id,
                    question=c["q"],
                    expected_answerable=bool(c["answerable"]),
                    expected_doc_ids=list(dict.fromkeys(doc_ids)),
                    note=f"[{c.get('cat','-')}] 预检 top1={c.get('top1')} {c.get('top1_file','')}".strip(),
                )
            )
            inserted += 1

        await session.commit()

    print(f"备份 {n_old} 条 → {backup}")
    print(f"导入 {inserted} 条（keep_failed={keep_failed}）")
    if missing:
        print("未匹配到文档的期望文件名:", missing)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--tag", default="1018")
    ap.add_argument("--keep-failed", action="store_true", help="连预检未通过的也导入")
    a = ap.parse_args()
    return asyncio.run(run(Path(a.src), a.tag, a.keep_failed))


if __name__ == "__main__":
    raise SystemExit(main())
