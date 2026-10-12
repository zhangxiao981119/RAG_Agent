"""评估集标定脚本 —— 手册 §7.3 + §7.4。

用法：
  python scripts/run_eval.py [--assert-refusal-rate 0.9] [--assert-miss-rate 0.1]
                             [--baseline] [--max-regression 0.02] [--note "发布 v1.2"]

跑 eval_cases 表里所有 case，调 /api/chat/ask（SSE），统计：
  · 拒答正确率 = 不可答问题中正确拒答的比例（目标 >= 0.9）
  · 漏答率     = 可答问题中被错误拒答的比例（目标 <= 0.1）
  · 命中率     = 可答问题中引用到期望文档的比例（目标 >= 0.8）

★ 用于标定 RELEVANCE_THRESHOLD（手册 §3.3.2 阈值标定）。

项2（评估基线版本）：每次运行都会落一条 eval_runs 记录（指标 + 用例集指纹 + 运行口径）。
  --baseline 与最近一次**同用例集指纹**的运行比对，任一指标回退超过 --max-regression
  即以非零退出码阻断发布。用例集指纹变化（增删改用例）时自动跳过对比。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

import httpx
from sqlalchemy import select

from app.config import decisions
from app.config.settings import get_settings
from app.database import SessionLocal
from app.models import EvalCase, KnowledgeBase, Tenant, User
from app.services.auth import create_access_token
from app.services.caliber import snapshot_for_message
from app.services.eval_service import (
    case_set_fingerprint,
    latest_eval_run,
    save_eval_run,
)


async def _ask_one(
    client: httpx.AsyncClient,
    base_url: str,
    question: str,
    kb_ids: list[str],
    token: str = "",
) -> dict:
    """调 /api/chat/ask SSE，返回 {refused, citations, text}。

    ★ 全业务 API 需要 JWT，脚本必须自带 token，否则 401。
    """
    refused = False
    refuse_reason = ""
    citations: list[dict] = []
    text_parts: list[str] = []

    async with client.stream(
        "POST",
        f"{base_url}/api/chat/ask",
        json={"question": question, "kb_ids": kb_ids, "conversation_id": None},
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
        timeout=120,
    ) as response:
        response.raise_for_status()
        buffer = ""
        async for chunk in response.aiter_text():
            buffer += chunk
            blocks = buffer.split("\n\n")
            buffer = blocks.pop() or ""
            for block in blocks:
                event = ""
                data = ""
                for line in block.split("\n"):
                    if line.startswith("event: "):
                        event = line[7:].strip()
                    elif line.startswith("data: "):
                        data = line[6:]
                if not event or not data:
                    continue
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if event == "refused":
                    refused = True
                    refuse_reason = payload.get("reason", "")
                elif event == "citations":
                    citations = payload.get("citations", [])
                elif event == "delta":
                    text_parts.append(payload.get("text", ""))

    return {
        "refused": refused,
        "refuse_reason": refuse_reason,
        "citations": citations,
        "text": "".join(text_parts),
    }


def _compare_baseline(base: dict, now: dict, max_regression: float) -> int:
    """把本次指标与基线比对，返回是否有回退（0/1）。

    方向：refusal_rate / hit_rate 越高越好，miss_rate 越低越好。
    ★ hit_rate 只在两次都「可判定」（judged > 0）时比对 —— 基线不可判定时其值恒为 0，
      硬比会把「本次已可判定」误判成回退。
    """
    directions = (("refusal_rate", 1), ("hit_rate", 1), ("miss_rate", -1))
    failed = 0
    for name, higher_better in directions:
        if name == "hit_rate" and (base.get("judged", 0) <= 0 or now.get("judged", 0) <= 0):
            print(f"基线对比 {name}: 有一次不可判定（judged=0），跳过比对")
            continue
        b = float(base.get(name, 0.0))
        n = float(now.get(name, 0.0))
        regress = (b - n) if higher_better == 1 else (n - b)
        print(f"基线对比 {name}: 基线 {b:.2%} -> 本次 {n:.2%}，回退量 {regress:+.2%}")
        if regress > max_regression:
            failed = 1
    if failed:
        print(f"FAIL: 指标回退超过阈值 {max_regression:.2%}（--max-regression）")
    return failed


async def run_eval(
    assert_refusal_rate: float | None = None,
    assert_miss_rate: float | None = None,
    baseline: bool = False,
    max_regression: float = 0.02,
    note: str | None = None,
) -> int:
    settings = get_settings()
    base_url = "http://localhost:8000"

    async with SessionLocal() as session:
        tenant = await session.scalar(
            select(Tenant).where(Tenant.code == settings.tenant_code)
        )
        if tenant is None:
            print("租户未初始化", file=sys.stderr)
            return 1

        cases = (
            await session.execute(
                select(EvalCase).where(EvalCase.tenant_id == tenant.id)
            )
        ).scalars().all()
        if not cases:
            print("eval_cases 为空，先跑 scripts/seed_eval.py", file=sys.stderr)
            return 1

        # 取所有知识库 id（M2 无权限，全部参与）
        kbs = (
            await session.execute(
                select(KnowledgeBase).where(KnowledgeBase.tenant_id == tenant.id)
            )
        ).scalars().all()
        kb_ids = [str(kb.id) for kb in kbs]

        # 项2：用例集指纹在本次会话内算好 —— 落库与基线取数共用同一个值
        tenant_id = tenant.id
        fingerprint = case_set_fingerprint(cases)

        # 全业务 API 需 JWT：签发 admin token，否则 /api/chat/ask 返回 401
        admin = await session.scalar(
            select(User).where(User.tenant_id == tenant.id, User.username == "admin")
        )
        token = ""
        if admin is None:
            print("未找到 admin 用户，请求不带 token，预计全部 401", file=sys.stderr)
        else:
            token = create_access_token(admin.id, tenant.id, admin.username)

    answerable = [c for c in cases if c.expected_answerable]
    unanswerable = [c for c in cases if not c.expected_answerable]

    correct_refusal = 0
    wrong_refusal = 0  # 可答问题被拒答（漏答）
    hit_docs = 0
    unjudged = 0  # 未标注 expected_doc_ids，命中率不可判定的用例数
    total_citations = 0

    async with httpx.AsyncClient() as client:
        # 可答
        for case in answerable:
            result = await _ask_one(client, base_url, case.question, kb_ids, token)
            if result["refused"]:
                wrong_refusal += 1
                print(f"[漏答] {case.question} -> refused({result['refuse_reason']})")
            else:
                # 命中率：引用的 doc_id 是否在 expected_doc_ids
                cited_doc_ids = {c.get("doc_id") for c in result["citations"]}
                expected = {str(d) for d in case.expected_doc_ids}
                if not expected:
                    # ★ 未标注 expected_doc_ids 的用例不可判定，计入 unjudged，
                    #   既不进分子也不进分母——否则命中率恒为 100%，指标失真
                    unjudged += 1
                elif cited_doc_ids & expected:
                    hit_docs += 1
                total_citations += len(result["citations"])
                print(f"[答] {case.question} -> {len(result['citations'])} 引用")

        # 不可答
        for case in unanswerable:
            result = await _ask_one(client, base_url, case.question, kb_ids, token)
            if result["refused"]:
                correct_refusal += 1
                print(f"[拒答] {case.question} -> {result['refuse_reason']}")
            else:
                print(f"[错答] {case.question} -> {result['text'][:80]}")

    refusal_rate = correct_refusal / len(unanswerable) if unanswerable else 0
    miss_rate = wrong_refusal / len(answerable) if answerable else 0
    # 命中率只在「已回答（未被拒答）且有标注」的用例上统计：
    # 漏答的不进统计（它已经计入漏答率），未标注的不可判定
    judged = len(answerable) - wrong_refusal - unjudged
    hit_rate = hit_docs / judged if judged else 0

    print("\n=== 评估结果 ===")
    print(f"拒答正确率: {refusal_rate:.2%}（目标 >= 0.90）")
    print(f"漏答率:     {miss_rate:.2%}（目标 <= 0.10）")
    print(f"命中率:     {hit_rate:.2%}（目标 >= 0.80）")
    if judged == 0:
        print(
            f"  ⚠ 命中率不可判定：{len(answerable)} 条可答用例中 "
            f"{wrong_refusal} 条被拒答、{unjudged} 条未标注 expected_doc_ids，"
            f"可判定样本为 0 —— 请先校准用例标注（scripts/seed_eval.py）"
        )
    elif unjudged:
        print(
            f"  ⚠ {unjudged} 条用例未标注 expected_doc_ids，"
            f"命中率仅在 {judged} 条可判定的用例上统计"
        )
    print(
        f"阈值口径 = vector:{decisions.RELEVANCE_THRESHOLD_VECTOR} / "
        f"rerank:{decisions.RELEVANCE_THRESHOLD_RERANK}"
    )

    metrics = {
        "refusal_rate": round(refusal_rate, 4),
        "miss_rate": round(miss_rate, 4),
        "hit_rate": round(hit_rate, 4),
        "answerable": len(answerable),
        "unanswerable": len(unanswerable),
        "judged": judged,
    }

    failed = 0
    if assert_refusal_rate is not None and refusal_rate < assert_refusal_rate:
        print(f"FAIL: 拒答正确率 {refusal_rate:.2%} < {assert_refusal_rate:.2%}")
        failed = 1
    if assert_miss_rate is not None and miss_rate > assert_miss_rate:
        print(f"FAIL: 漏答率 {miss_rate:.2%} > {assert_miss_rate:.2%}")
        failed = 1

    # ── 项2：基线对比 + 落库 ──────────────────────────────
    async with SessionLocal() as session:
        if baseline:
            base_row = await latest_eval_run(session, tenant_id, fingerprint)
            if base_row is None:
                print(f"基线：无同用例集（{fingerprint}）的历史运行，本次作为首个基线")
            else:
                failed |= _compare_baseline(base_row.metrics, metrics, max_regression)
        # 口径快照：记录本批指标是在什么口径下得到的（含配置指纹，见项3）
        caliber = await snapshot_for_message(tenant_id)
        await save_eval_run(
            session,
            tenant_id=tenant_id,
            metrics=metrics,
            case_fingerprint=fingerprint,
            case_count=len(cases),
            caliber=caliber,
            passed=(failed == 0),
            note=note,
        )
    print(
        f"已落库评估基线: fingerprint={fingerprint} "
        f"case_count={len(cases)} passed={failed == 0}"
    )
    return failed


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--assert-refusal-rate", type=float, default=None)
    parser.add_argument("--assert-miss-rate", type=float, default=None)
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="与最近一次同用例集运行比对，指标回退超过 --max-regression 即非零退出",
    )
    parser.add_argument(
        "--max-regression",
        type=float,
        default=0.02,
        help="允许的最大回退幅度（比率，默认 0.02 即 2 个百分点）",
    )
    parser.add_argument("--note", type=str, default=None, help="本次运行的备注（如版本号）")
    args = parser.parse_args()
    sys.exit(
        asyncio.run(
            run_eval(
                args.assert_refusal_rate,
                args.assert_miss_rate,
                args.baseline,
                args.max_regression,
                args.note,
            )
        )
    )
