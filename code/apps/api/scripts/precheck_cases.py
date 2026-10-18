#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""候选用例预检：阈值置 0 跑纯检索层，逐条记录 top1 分数与命中文档。

用途：批量筛选评测集候选用例。只读，不写库。
  · 可答用例：top1_score >= 目标阈值 且 top1 文档 == 期望文档 → 通过
  · 不可答用例：top1_score < 目标阈值 → 通过（否则会被 L1 放行，只能靠 grounding 兜底）

用法（容器内）：
    python scripts/precheck_cases.py --in /tmp/cases.json --out /tmp/precheck.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy import select

from app.config import decisions
from app.config.settings import get_settings
from app.database import SessionLocal
from app.models import KnowledgeBase, Tenant, User
from app.services.acl import load_principal_from_db
from app.services.retrieve import get_retrieval_service


async def run(src: Path, dst: Path, thresh: float) -> int:
    settings = get_settings()
    retrieval = get_retrieval_service()

    orig = (decisions.RELEVANCE_THRESHOLD_VECTOR, decisions.RELEVANCE_THRESHOLD_RERANK)
    decisions.RELEVANCE_THRESHOLD_VECTOR = 0.0
    decisions.RELEVANCE_THRESHOLD_RERANK = 0.0

    cases = json.loads(src.read_text(encoding="utf-8"))
    results: list[dict] = []

    try:
        async with SessionLocal() as session:
            tenant = await session.scalar(
                select(Tenant).where(Tenant.code == settings.tenant_code)
            )
            if tenant is None:
                print("租户未初始化", file=sys.stderr)
                return 1
            admin = await session.scalar(select(User).where(User.username == "admin"))
            if admin is None:
                print("未找到 admin 用户", file=sys.stderr)
                return 1
            kbs = (
                await session.execute(
                    select(KnowledgeBase).where(KnowledgeBase.tenant_id == tenant.id)
                )
            ).scalars().all()
            kb_ids = [k.id for k in kbs]

            principal, _epoch, _dept = await load_principal_from_db(
                session, None, admin.id, tenant.id
            )
            clearance = int(principal.clearance)
            subjects = sorted(principal.subjects)

            for i, c in enumerate(cases, 1):
                q = c["q"]
                try:
                    r = await retrieval.retrieve(
                        session, q, tenant.id, kb_ids, clearance, subjects
                    )
                    if r.refused or not getattr(r, "chunks", None):
                        score, fn, src_kind = 0.0, "", getattr(r, "score_source", "") or ""
                    else:
                        top = r.chunks[0]
                        score = float(top.final_score)
                        fn = getattr(top, "filename", "") or ""
                        src_kind = getattr(r, "score_source", "") or ""
                except Exception as exc:  # noqa: BLE001
                    print(f"[{i}] 异常 {q}: {exc}", file=sys.stderr)
                    results.append({**c, "top1": 0.0, "top1_file": "", "source": "error"})
                    continue

                ans = bool(c["answerable"])
                expect = c.get("expect") or []
                if isinstance(expect, str):
                    expect = [expect] if expect else []
                if ans:
                    ok = score >= thresh and (not expect or fn in expect)
                else:
                    ok = score < thresh
                results.append(
                    {**c, "top1": round(score, 4), "top1_file": fn, "source": src_kind, "pass": ok}
                )
                if i % 10 == 0:
                    print(f"  … {i}/{len(cases)}", file=sys.stderr, flush=True)
    finally:
        decisions.RELEVANCE_THRESHOLD_VECTOR, decisions.RELEVANCE_THRESHOLD_RERANK = orig

    dst.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")

    ok = [r for r in results if r.get("pass")]
    na = [r for r in results if not r["answerable"]]
    an = [r for r in results if r["answerable"]]
    print(f"总计 {len(results)} | 通过 {len(ok)}")
    print(
        f"可答 {len(an)} 条：通过 {sum(1 for r in an if r.get('pass'))}，"
        f"文档不符 {sum(1 for r in an if r.get('pass') is False and r['top1'] >= thresh)}, "
        f"低于阈值 {sum(1 for r in an if r['top1'] < thresh)}"
    )
    print(
        f"不可答 {len(na)} 条：通过 {sum(1 for r in na if r.get('pass'))}，"
        f"超阈值 {sum(1 for r in na if r['top1'] >= thresh)}"
    )
    srcs = {r.get("source") for r in results}
    print("分数量纲:", srcs)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--out", dest="dst", required=True)
    ap.add_argument("--thresh", type=float, default=0.50)
    a = ap.parse_args()
    return asyncio.run(run(Path(a.src), Path(a.dst), a.thresh))


if __name__ == "__main__":
    raise SystemExit(main())
