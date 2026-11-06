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

★ 本脚本的上下文口径（与 admin 门禁 `POST /api/admin/eval/run` 不同，别搞混）：
  它走的是 `/api/chat/ask` **生产链路**，因此四类上下文（命中片 + 表格回捞 +
  章节补全 + 图召回）本来就已经注入，且走的是 `retrieve_chain()`。
  换句话说「评测只喂命中片」那个断层**不存在于本脚本**，所以这里**不提供**
  `--with-recall-chunks` 开关 —— 提供了也只会是个骗人的空开关（生产链路没有
  请求级的上下文裁剪开关，设计上也不允许有）。
  需要「只喂命中片」的旧口径来自 admin 门禁（`EvalService.run_eval` 的
  `with_recall_chunks=False`），两条链路的口径不能混比，报告里会打印口径
  （禁令原文见 `eval_service.CROSS_PATH_NOTE`，与门禁报告共用同一份文本）。

★ 图召回的量化（§14.6 可测化）：开图时图召回片本来就进了上下文，所以
  「开图 vs 关图」跑两次就能得到诚实对比。为此本脚本按**轮**聚合 SSE 上的
  `path_stats.graph_covered / graph_unique` 与 `graph_degraded_reason`
  （这两个字段由 chat.py 追加到 `done` / 检索后的 `refused` 事件载荷里），
  既写进当次运行的 metrics 与 eval_runs 记录，也打印在「上下文口径」块里。
  ⚠ 图路两键的口径已按 v1.2 契约（`d6ff0f4`）定稿，参照系是**交付上下文**：
  `graph_unique` = 最终进入上下文的图召回片数（不是候选池分母）；
  `graph_covered` = 图邻居中已在最终命中片的条数（预算裁剪前统计）。
  v1.1 以三路候选池为分母的旧口径已被真库实测证伪 —— 候选池可达 150 个 id，
  远大于最终 top-K，会让 `graph_unique` 结构性恒 0。报告里 MUST NOT 再拿
  `graph_unique=0` 声称「图没用」。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter

import httpx
from sqlalchemy import select

from app.config import decisions
from app.config.settings import get_settings
from app.database import SessionLocal
from app.models import EvalCase, KnowledgeBase, Tenant, User
from app.services.auth import create_access_token
from app.services.caliber import snapshot_for_message
from app.services.eval_service import (
    CROSS_PATH_NOTE,
    case_set_fingerprint,
    latest_eval_run,
    save_eval_run,
)


def _accumulate_graph_observation(acc: dict, ask_result: dict) -> None:
    """把单轮的图召回观测累加进本次运行的聚合桶（§14.6 可测化）。

    acc 结构：`{"covered": int, "unique": int, "degraded": Counter[str], "rounds": int}`

    ★ 为什么按**轮**累加、而不是只统计「有答案的轮」：图召回是检索侧行为，
      与这轮最后答不答得出来无关。只统计成功轮会让 `graph_degraded_reason`
      偏向某一类问题（能拒答的往往是"库里没有"，而那批恰好可能是图路最该起作用的），
      统计量随之失真。
    ★ 缺字段按 0 计、不抛：观测字段缺失（老版本服务端 / 检索前就拒答的轮）不该
      让整轮评测失败 —— 它不是正确性数据，是量化数据。
    """
    stats = ask_result.get("path_stats") or {}
    acc["covered"] += int(stats.get("graph_covered", 0) or 0)
    acc["unique"] += int(stats.get("graph_unique", 0) or 0)
    reason = ask_result.get("graph_degraded_reason") or ""
    if reason:
        acc["degraded"][reason] += 1
    acc["rounds"] += 1


async def _ask_one(
    client: httpx.AsyncClient,
    base_url: str,
    question: str,
    kb_ids: list[str],
    token: str = "",
) -> dict:
    """调 /api/chat/ask SSE，返回 {refused, citations, text, path_stats, graph_degraded_reason}。

    ★ 全业务 API 需要 JWT，脚本必须自带 token，否则 401。
    ★ path_stats / graph_degraded_reason 由 chat.py 追加在 `done` 与「检索之后」的
      `refused` 载荷里（§14.6 可测化）。检索**之前**就拒答的轮（敏感词 / 澄清 /
      视觉不可用 / 检索异常）没有这两个键 —— 按空值处理，不视为错误。
    """
    refused = False
    refuse_reason = ""
    citations: list[dict] = []
    text_parts: list[str] = []
    path_stats: dict = {}
    graph_degraded_reason = ""

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
                    # 检索之后的拒答也带观测字段；检索之前的拒答没有 → 保留原值
                    path_stats = payload.get("path_stats") or path_stats
                    graph_degraded_reason = (
                        payload.get("graph_degraded_reason") or graph_degraded_reason
                    )
                elif event == "citations":
                    citations = payload.get("citations", [])
                elif event == "delta":
                    text_parts.append(payload.get("text", ""))
                elif event == "done":
                    path_stats = payload.get("path_stats") or path_stats
                    graph_degraded_reason = (
                        payload.get("graph_degraded_reason") or graph_degraded_reason
                    )

    return {
        "refused": refused,
        "refuse_reason": refuse_reason,
        "citations": citations,
        "text": "".join(text_parts),
        "path_stats": path_stats,
        "graph_degraded_reason": graph_degraded_reason,
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


def _warn_caliber_drift(base_caliber: dict, now_caliber: dict) -> None:
    """基线与本轮的口径不一致时**大声提示**（只提示，不改变 --baseline 的判定）。

    比对的两个键：
      · `graph_enabled`   —— 开图 / 关图的两条基线本来就不该直接比
      · `context_caliber` —— 不同上下文口径（生产链路 vs 门禁路径）更不能比

    ★ 为什么只提示不改判定：口径变了不代表代码退步，把口径变化算成回退会制造
      假红灯，运维很快学会把 --max-regression 调大 —— 那道门就白设了。
    ★ 为什么又必须提示：完全不提示更糟。「开图前 vs 开图后」的两条基线比出来的
      分数差，会被当成某次代码改动的效果，而这恰恰是最容易得出的错误结论。
    ★ 缺失键按「未记录」处理：旧基线（本字段引入之前落的行）确实没有这些键，
      把它说成 False 等于替历史编造事实。
    """
    for key, label in (
        ("graph_enabled", "图能力开关"),
        ("context_caliber", "上下文口径"),
    ):
        old = base_caliber.get(key, "<未记录>")
        new = now_caliber.get(key, "<未记录>")
        if str(old) != str(new):
            print(
                f"⚠ 口径漂移 {label}: 基线 {old} -> 本次 {new}；"
                f"两者的分数差异不能归因于代码改动，--baseline 判定仅供参考"
            )


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
    # 图召回观测（§14.6 可测化）：按轮累加，与上面四个答案质量计数**分开**
    # —— 「答得对不对」和「图补了多少」是两个维度，混进同一个计数器就都解释不清
    graph_acc: dict = {"covered": 0, "unique": 0, "degraded": Counter(), "rounds": 0}

    async with httpx.AsyncClient() as client:
        # 可答
        for case in answerable:
            result = await _ask_one(client, base_url, case.question, kb_ids, token)
            _accumulate_graph_observation(graph_acc, result)
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
            _accumulate_graph_observation(graph_acc, result)
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

    # ── 上下文口径（必须能从报告本身看出来，不能只留在命令行历史里）──
    context_caliber = "production_chat_path"
    degraded = dict(graph_acc["degraded"])
    print("\n=== 上下文口径 ===")
    print("来源: /api/chat/ask 生产链路（命中片 + 表格回捞 + 章节补全 + 图召回，"
          "检索走 retrieve_chain）")
    print(f"图能力: GRAPH_ENABLED={settings.graph_enabled}")
    # 图路量化：按轮累加（口径见模块 docstring）
    print(
        f"图召回观测（{graph_acc['rounds']} 轮累加）: "
        f"graph_covered={graph_acc['covered']} graph_unique={graph_acc['unique']}"
    )
    if degraded:
        print(f"图召回降级原因: {degraded}")
    else:
        print("图召回降级原因: 无（未开启图能力时这是预期结果，不等于「图没问题」）")
    if settings.graph_enabled:
        print(
            "声明: 本次评测已启用图能力且四类上下文全部注入，"
            "报告覆盖图召回对答案质量的影响；量化看上面的 graph_covered / graph_unique。"
        )
    else:
        print(
            "声明: 本次评测未启用图能力（GRAPH_ENABLED=false），图召回为 0 路，"
            "故本报告**不覆盖**图召回对答案质量的影响；"
            "禁止用分数未变化来宣称「图无副作用」。"
        )
    print(f"★ 口径禁令: {CROSS_PATH_NOTE}")

    metrics = {
        "refusal_rate": round(refusal_rate, 4),
        "miss_rate": round(miss_rate, 4),
        "hit_rate": round(hit_rate, 4),
        "answerable": len(answerable),
        "unanswerable": len(unanswerable),
        "judged": judged,
        # ── 图召回召回侧观测（按轮累加，与答案质量指标并列存放）──
        # ★ 三个键与管理员门禁报告（EvalReport.graph_*）同名同义，
        #   但**口径不同**（见上面的 CROSS_PATH_NOTE），禁止跨链路比。
        "graph_covered": graph_acc["covered"],
        "graph_unique": graph_acc["unique"],
        "graph_degraded_reasons": degraded,
        "graph_rounds": graph_acc["rounds"],
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
        # 口径快照：记录本批指标是在什么口径下得到的（含配置指纹，见项3）
        # ★ 图口径（graph_enabled / graph_extractor_model / graph_prompt_version）
        #   现在随 build_caliber_snapshot() 一起来 —— 「这条基线开没开图」是可查询的
        #   事实，不必再靠 note 里写的人话。
        # ★ 先算口径再比基线：基线对比要拿它判断「两次运行是不是同一个口径」。
        caliber = await snapshot_for_message(tenant_id)
        # 上下文口径是本批次特有的（同一份代码可能跑两套口径），必须单独留痕 ——
        # 否则事后看 eval_runs 只看到两行不同的分数，无法判断能不能比
        caliber["context_caliber"] = context_caliber
        # 跨链路口径禁令随记录落库（与门禁报告共用同一份文本，见 CROSS_PATH_NOTE）：
        # 只看 eval_runs 的人也必须看到「本行分数不能和 admin 门禁的分数比」
        caliber["context_note"] = CROSS_PATH_NOTE

        if baseline:
            base_row = await latest_eval_run(session, tenant_id, fingerprint)
            if base_row is None:
                print(f"基线：无同用例集（{fingerprint}）的历史运行，本次作为首个基线")
            else:
                _warn_caliber_drift(base_row.caliber or {}, caliber)
                failed |= _compare_baseline(base_row.metrics, metrics, max_regression)
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
