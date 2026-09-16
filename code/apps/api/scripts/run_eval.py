"""评估集标定脚本 —— 手册 §7.3 + §7.4。

用法：
  python scripts/run_eval.py [--assert-refusal-rate 0.9] [--assert-miss-rate 0.1]

跑 eval_cases 表里所有 case，调 /api/chat/ask（SSE），统计：
  · 拒答正确率 = 不可答问题中正确拒答的比例（目标 >= 0.9）
  · 漏答率     = 可答问题中被错误拒答的比例（目标 <= 0.1）
  · 命中率     = 可答问题中引用到期望文档的比例（目标 >= 0.8）

★ 用于标定 RELEVANCE_THRESHOLD（手册 §3.3.2 阈值标定）。
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
from app.models import EvalCase, KnowledgeBase, Tenant


async def _ask_one(client: httpx.AsyncClient, base_url: str, question: str, kb_ids: list[str]) -> dict:
    """调 /api/chat/ask SSE，返回 {refused, citations, text}。"""
    refused = False
    refuse_reason = ""
    citations: list[dict] = []
    text_parts: list[str] = []

    async with client.stream(
        "POST",
        f"{base_url}/api/chat/ask",
        json={"question": question, "kb_ids": kb_ids, "conversation_id": None},
        headers={"Content-Type": "application/json"},
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


async def run_eval(
    assert_refusal_rate: float | None = None,
    assert_miss_rate: float | None = None,
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

    answerable = [c for c in cases if c.expected_answerable]
    unanswerable = [c for c in cases if not c.expected_answerable]

    correct_refusal = 0
    wrong_refusal = 0  # 可答问题被拒答（漏答）
    hit_docs = 0
    total_citations = 0

    async with httpx.AsyncClient() as client:
        # 可答
        for case in answerable:
            result = await _ask_one(client, base_url, case.question, kb_ids)
            if result["refused"]:
                wrong_refusal += 1
                print(f"[漏答] {case.question} -> refused({result['refuse_reason']})")
            else:
                # 命中率：引用的 doc_id 是否在 expected_doc_ids
                cited_doc_ids = {c.get("doc_id") for c in result["citations"]}
                expected = {str(d) for d in case.expected_doc_ids}
                if not expected or cited_doc_ids & expected:
                    hit_docs += 1
                total_citations += len(result["citations"])
                print(f"[答] {case.question} -> {len(result['citations'])} 引用")

        # 不可答
        for case in unanswerable:
            result = await _ask_one(client, base_url, case.question, kb_ids)
            if result["refused"]:
                correct_refusal += 1
                print(f"[拒答] {case.question} -> {result['refuse_reason']}")
            else:
                print(f"[错答] {case.question} -> {result['text'][:80]}")

    refusal_rate = correct_refusal / len(unanswerable) if unanswerable else 0
    miss_rate = wrong_refusal / len(answerable) if answerable else 0
    hit_rate = hit_docs / len(answerable) if answerable else 0

    print("\n=== 评估结果 ===")
    print(f"拒答正确率: {refusal_rate:.2%}（目标 >= 0.90）")
    print(f"漏答率:     {miss_rate:.2%}（目标 <= 0.10）")
    print(f"命中率:     {hit_rate:.2%}（目标 >= 0.80）")
    print(f"RELEVANCE_THRESHOLD = {decisions.RELEVANCE_THRESHOLD}")

    failed = 0
    if assert_refusal_rate is not None and refusal_rate < assert_refusal_rate:
        print(f"FAIL: 拒答正确率 {refusal_rate:.2%} < {assert_refusal_rate:.2%}")
        failed = 1
    if assert_miss_rate is not None and miss_rate > assert_miss_rate:
        print(f"FAIL: 漏答率 {miss_rate:.2%} > {assert_miss_rate:.2%}")
        failed = 1
    return failed


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--assert-refusal-rate", type=float, default=None)
    parser.add_argument("--assert-miss-rate", type=float, default=None)
    args = parser.parse_args()
    sys.exit(asyncio.run(run_eval(args.assert_refusal_rate, args.assert_miss_rate)))
