#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""L1 阈值标定脚本（支持双口径）。

背景：final_score 有两种来源 ——
  · 重排在线 → rerank 绝对相关性分
  · 重排降级 → 向量余弦分
两者分布不同，必须**分别标定**（decisions.RELEVANCE_THRESHOLD_RERANK /
RELEVANCE_THRESHOLD_VECTOR）。本脚本用 eval_cases 跑**纯检索层**（不调 LLM），
采集 top1 final_score 并扫描候选阈值。

用法（容器内）：
    python scripts/calibrate_threshold.py                # 自动识别当前口径
    python scripts/calibrate_threshold.py --mode vector  # 强制按向量口径采集
    python scripts/calibrate_threshold.py --mode rerank  # 强制按重排口径采集

★ 循环论证陷阱（务必保留此处理）：
  采集前必须把阈值置 0。否则不可答问题会先被当前阈值拒答
  （retrieve 返回 refused=True 且无 chunks），采集到 0.0 而非真实分数 ——
  等于拿被自己阈值截断的数据去标定阈值。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select

from app.config import decisions
from app.config.settings import get_settings
from app.database import SessionLocal
from app.models import EvalCase, KnowledgeBase, Tenant, User
from app.services.acl import load_principal_from_db
from app.services.retrieve import get_retrieval_service

# 分离度低于此值视为「阈值不稳」——两组分数靠得太近，样本增加后可能重叠
MIN_SEPARATION = 0.05


def _zero_thresholds() -> tuple[float, float]:
    """把两个阈值都临时置 0，返回原值用于还原。"""
    orig = (decisions.RELEVANCE_THRESHOLD_VECTOR, decisions.RELEVANCE_THRESHOLD_RERANK)
    decisions.RELEVANCE_THRESHOLD_VECTOR = 0.0
    decisions.RELEVANCE_THRESHOLD_RERANK = 0.0
    return orig


def _restore_thresholds(orig: tuple[float, float]) -> None:
    decisions.RELEVANCE_THRESHOLD_VECTOR, decisions.RELEVANCE_THRESHOLD_RERANK = orig


async def _collect() -> tuple[list[tuple[str, bool, float, str]], str]:
    """返回 ([(question, expected_answerable, top1_score, score_source)], 主导口径)。"""
    settings = get_settings()
    retrieval = get_retrieval_service()

    orig = _zero_thresholds()
    print(f"[采集] 两个阈值临时置 0（原值 vector={orig[0]} rerank={orig[1]}），以取得未截断的原始分数")

    rows: list[tuple[str, bool, float, str]] = []
    sources: list[str] = []

    async with SessionLocal() as session:
        tenant = await session.scalar(
            select(Tenant).where(Tenant.code == settings.tenant_code)
        )
        if tenant is None:
            print("租户未初始化", file=sys.stderr)
            _restore_thresholds(orig)
            return [], "vector"

        admin = await session.scalar(select(User).where(User.username == "admin"))
        if admin is None:
            print("未找到 admin 用户", file=sys.stderr)
            _restore_thresholds(orig)
            return [], "vector"

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

        cases = (
            await session.execute(
                select(EvalCase).where(EvalCase.tenant_id == tenant.id)
            )
        ).scalars().all()

        if not cases:
            print("eval_cases 为空，先跑 scripts/seed_eval.py", file=sys.stderr)
            _restore_thresholds(orig)
            return [], "vector"

        for case in cases:
            try:
                result = await retrieval.retrieve(
                    session, case.question, tenant.id, kb_ids, clearance, subjects
                )
            except Exception as exc:  # noqa: BLE001
                print(f"检索异常 case={case.question}: {exc}", file=sys.stderr)
                continue

            if result.refused or not getattr(result, "chunks", None):
                top1 = 0.0
            else:
                top1 = float(result.chunks[0].final_score)

            src = getattr(result, "score_source", "") or "unknown"
            sources.append(src)
            rows.append((case.question, bool(case.expected_answerable), top1, src))

    _restore_thresholds(orig)

    # ★ 口径一致性检查：采集期间若重排状态发生变化，样本是混口径的，标定结果不可用
    distinct = {s for s in sources if s not in ("", "unknown")}
    if len(distinct) > 1:
        print(
            f"\n[!] 警告：采集期间出现**混口径**样本 {sorted(distinct)} ——"
            " 说明重排服务在运行中状态变化，本次标定结果不可用于生产。"
            " 请修好重排服务（或临时禁用）后重跑。\n",
            file=sys.stderr,
        )

    dominant = max(set(sources), key=sources.count) if sources else "vector"
    return rows, dominant


def _sweep(rows: list[tuple[str, bool, float, str]]) -> list[tuple[float, float, float, float]]:
    """扫描阈值，返回 [(threshold, refusal_acc, miss_rate, f1)]。"""
    out: list[tuple[float, float, float, float]] = []
    answerable = [s for _, ans, s, _ in rows if ans]
    unanswerable = [s for _, ans, s, _ in rows if not ans]
    if not answerable or not unanswerable:
        return out

    t = 0.00
    while t <= 0.90001:
        correct_refusal = sum(1 for s in unanswerable if s < t)
        miss = sum(1 for s in answerable if s < t)
        refusal_acc = correct_refusal / len(unanswerable)
        miss_rate = miss / len(answerable)
        denom = refusal_acc + (1 - miss_rate)
        f1 = 2 * refusal_acc * (1 - miss_rate) / denom if denom > 0 else 0.0
        out.append((round(t, 2), refusal_acc, miss_rate, f1))
        t += 0.05
    return out


async def main() -> int:
    parser = argparse.ArgumentParser(description="L1 阈值标定（双口径）")
    parser.add_argument(
        "--mode",
        choices=["auto", "vector", "rerank"],
        default="auto",
        help="标定哪套阈值；auto 按采集时的实际口径决定",
    )
    args = parser.parse_args()

    rows, dominant = await _collect()
    if not rows:
        return 1

    mode = dominant if args.mode == "auto" else args.mode
    ans_scores = sorted((s for _, a, s, _ in rows if a), reverse=True)
    unans_scores = sorted((s for _, a, s, _ in rows if not a), reverse=True)

    print("=== 样本 ===")
    print(f"总用例: {len(rows)} | 可答: {len(ans_scores)} | 不可答: {len(unans_scores)}")
    print(f"主导口径: {dominant} | 本次标定口径: {mode}")
    if len(rows) < 50:
        print(f"[!] 样本仅 {len(rows)} 条，建议扩到 50+ 后以本结果为准")

    print("\n=== 可答组 top1 分数（降序） ===")
    print(", ".join(f"{s:.3f}" for s in ans_scores))
    print(f"min={min(ans_scores):.3f} median={ans_scores[len(ans_scores)//2]:.3f}")

    print("\n=== 不可答组 top1 分数（降序） ===")
    print(", ".join(f"{s:.3f}" for s in unans_scores))
    if unans_scores:
        print(f"max={max(unans_scores):.3f} median={unans_scores[len(unans_scores)//2]:.3f}")

    # ★ 分离度：阈值稳健性的直接指标
    sep = min(ans_scores) - max(unans_scores) if unans_scores else float("inf")
    print(f"\n=== 分离度 = 可答min - 不可答max = {sep:.3f} ===")
    if sep < MIN_SEPARATION:
        print(
            f"[!] 分离度 {sep:.3f} < {MIN_SEPARATION} —— 阈值处于「刀尖」状态，"
            "样本增加后可能失效。建议扩样本或改进检索。"
        )

    print("\n=== 阈值扫描 ===")
    print(f"{'阈值':>6} | {'拒答正确率':>10} | {'漏答率':>8} | {'F1':>6}")
    best = (0.0, 0.0, 0.0, -1.0)
    for thr, acc, miss, f1 in _sweep(rows):
        print(f"{thr:>6.2f} | {acc:>9.2%} | {miss:>7.2%} | {f1:>6.3f}")
        if f1 > best[3]:
            best = (thr, acc, miss, f1)

    target_const = (
        "RELEVANCE_THRESHOLD_RERANK" if mode == "rerank"
        else "RELEVANCE_THRESHOLD_VECTOR"
    )
    print(
        f"\n建议值：decisions.{target_const} = {best[0]:.2f}"
        f"（拒答正确率 {best[1]:.2%}，漏答率 {best[2]:.2%}，F1 {best[3]:.3f}）"
    )
    print(
        f"\n✏️ 写入 decisions.py 时 MUST 同时更新注释中的标定记录："
        f"样本数 {len(rows)}、口径 {mode}、分离度 {sep:.3f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
