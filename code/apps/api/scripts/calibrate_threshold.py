#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""RELEVANCE_THRESHOLD 标定脚本。

用途：用 eval_cases（含可答/不可答标注）跑**纯检索层**（不调 LLM），
收集每条用例的 top1 final_score，扫描候选阈值，给出
「拒答正确率 / 漏答率 / F1」曲线，用于把 decisions.RELEVANCE_THRESHOLD
从经验值改成**标定值**。

用法（容器内）：
    python scripts/calibrate_threshold.py
"""

from __future__ import annotations

import asyncio
import sys
import uuid

from sqlalchemy import select

from app.config import decisions
from app.config.settings import get_settings
from app.database import SessionLocal
from app.models import EvalCase, KnowledgeBase, Tenant, User
from app.services.acl import load_principal_from_db
from app.services.retrieve import get_retrieval_service


async def _collect() -> tuple[list[tuple[str, bool, float]], float]:
    """返回 [(question, expected_answerable, top1_score)] 与当前阈值。"""
    settings = get_settings()
    retrieval = get_retrieval_service()

    # ★ 关键：必须先把 L1 闸门阈值置 0 再采集分数。
    #   否则不可答问题会先被当前阈值拒答（retrieve 返回 refused=True、无 chunks），
    #   采集到的是 0.0 而不是真实相似度 —— 用被自己截断的数据标定阈值＝循环论证。
    original = decisions.RELEVANCE_THRESHOLD
    decisions.RELEVANCE_THRESHOLD = 0.0
    print(f"[采集] 阈值临时置 0（原值 {original}），以取得未截断的原始分数")

    rows: list[tuple[str, bool, float]] = []

    async with SessionLocal() as session:
        tenant = await session.scalar(
            select(Tenant).where(Tenant.code == settings.tenant_code)
        )
        if tenant is None:
            print("租户未初始化", file=sys.stderr)
            return [], decisions.RELEVANCE_THRESHOLD

        admin = await session.scalar(
            select(User).where(User.username == "admin")
        )
        if admin is None:
            print("未找到 admin 用户", file=sys.stderr)
            return [], decisions.RELEVANCE_THRESHOLD

        kbs = (
            await session.execute(
                select(KnowledgeBase).where(
                    KnowledgeBase.tenant_id == tenant.id
                )
            )
        ).scalars().all()
        kb_ids = [k.id for k in kbs]

        # 用真实 ACL 解析口径取 clearance / subjects（含双向展开 + 公开库自动成员）
        principal, _epoch, _dept = await load_principal_from_db(
            session, None, admin.id, tenant.id
        )
        clearance = int(principal.clearance)
        subjects = sorted(principal.subjects)

        cases = (
            await session.execute(
                select(EvalCase).where(EvalCase.tenant_id == tenant.id)
            )
        ).scalars().all()

        if not cases:
            print("eval_cases 为空，先跑 scripts/seed_eval.py", file=sys.stderr)
            return [], decisions.RELEVANCE_THRESHOLD

        for case in cases:
            try:
                result = await retrieval.retrieve(
                    session,
                    case.question,
                    tenant.id,
                    kb_ids,
                    clearance,
                    subjects,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"检索异常 case={case.question}: {exc}", file=sys.stderr)
                continue

            if result.refused or not getattr(result, "chunks", None):
                top1 = 0.0
            else:
                top1 = float(result.chunks[0].final_score)

            rows.append((case.question, bool(case.expected_answerable), top1))

    decisions.RELEVANCE_THRESHOLD = original  # 还原，避免污染同进程后续逻辑
    return rows, original


def _sweep(
    rows: list[tuple[str, bool, float]],
) -> list[tuple[float, float, float, float]]:
    """扫描阈值，返回 [(threshold, refusal_acc, miss_rate, f1)]。"""
    out: list[tuple[float, float, float, float]] = []
    answerable = [s for _, ans, s in rows if ans]
    unanswerable = [s for _, ans, s in rows if not ans]
    if not answerable or not unanswerable:
        return out

    t = 0.00
    while t <= 0.90001:
        # 拒答正确率：不可答且 top1 < t
        correct_refusal = sum(1 for s in unanswerable if s < t)
        # 漏答率：可答却被拒（top1 < t）
        miss = sum(1 for s in answerable if s < t)

        refusal_acc = correct_refusal / len(unanswerable)
        miss_rate = miss / len(answerable)
        if refusal_acc + (1 - miss_rate) > 0:
            f1 = (
                2
                * refusal_acc
                * (1 - miss_rate)
                / (refusal_acc + (1 - miss_rate))
            )
        else:
            f1 = 0.0
        out.append((round(t, 2), refusal_acc, miss_rate, f1))
        t += 0.05
    return out


async def main() -> int:
    rows, current = await _collect()
    if not rows:
        return 1

    ans_scores = sorted((s for _, a, s in rows if a), reverse=True)
    unans_scores = sorted((s for _, a, s in rows if not a), reverse=True)

    print("=== 样本 ===")
    print(f"总用例: {len(rows)} | 可答: {len(ans_scores)} | 不可答: {len(unans_scores)}")
    print(f"当前 RELEVANCE_THRESHOLD = {current}")

    print("\n=== 可答组 top1 分数（降序） ===")
    print(", ".join(f"{s:.3f}" for s in ans_scores))
    print(f"min={min(ans_scores):.3f} median={ans_scores[len(ans_scores)//2]:.3f}")

    print("\n=== 不可答组 top1 分数（降序） ===")
    print(", ".join(f"{s:.3f}" for s in unans_scores))
    if unans_scores:
        print(
            f"max={max(unans_scores):.3f} "
            f"median={unans_scores[len(unans_scores)//2]:.3f}"
        )

    print("\n=== 阈值扫描 ===")
    print(f"{'阈值':>6} | {'拒答正确率':>10} | {'漏答率':>8} | {'F1':>6}")
    best = (0.0, 0.0, 0.0, -1.0)
    for thr, acc, miss, f1 in _sweep(rows):
        flag = "  <== 当前" if abs(thr - current) < 1e-6 else ""
        print(f"{thr:>6.2f} | {acc:>9.2%} | {miss:>7.2%} | {f1:>6.3f}{flag}")
        if f1 > best[3]:
            best = (thr, acc, miss, f1)

    print(
        f"\n建议阈值 = {best[0]:.2f}"
        f"（拒答正确率 {best[1]:.2%}，漏答率 {best[2]:.2%}，F1 {best[3]:.3f}）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
