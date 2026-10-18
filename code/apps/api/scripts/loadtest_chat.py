"""问答链路并发压测（P1-5）—— 纯 asyncio + httpx，不引 locust/k6。

设计要点（先看这段，否则数字会被误读）：

1. **为什么自己写而不用 locust**：仓库已依赖 httpx，locust 要另装一套运行时；
   本脚本要的只有「N 个并发连接 + 分位数 + 错误分类」，200 行足够，
   且能直接打进容器用同一套登录逻辑。

2. **两个时间点必须分开记**：
   · `ttft_ms`  = 发出请求 → 第一个 `delta` 事件。这是**用户感知的首字延迟**。
   · `retrieve_ms` = 发出请求 → `citations` 事件（检索 + 重排 + 组装完成）。
   上游 LLM 的排队时间体现在 `ttft_ms - retrieve_ms` 的差值里，
   压测时把它单列，才能判「慢是慢在检索还是慢在模型」。

3. **思考时间（think time）默认 5s**：聊天类应用说「20 并发」指的是 20 个**同时在用的人**，
   不是 20 个死循环打接口。两者差一个数量级的 QPS。要测饱和能力就传 `--think 0`。

4. **拒答（refused）算成功不算失败**：拒答是产品行为，HTTP 200 + refused 事件。
   算进失败率会把「拒答率」错当「错误率」。分列统计。

用法（容器内，api 已在 8000 监听）：
    # 阶梯爬坡：1/5/10/20/30 并发，各 120 秒
    python scripts/loadtest_chat.py --kb <kb_id> --stages 1:120,5:120,10:120,20:120,30:120 \\
        --out /tmp/lt_ramp.json

    # 稳态：20 并发 × 30 分钟，思考 5s
    python scripts/loadtest_chat.py --kb <kb_id> --concurrency 20 --duration 1800 \\
        --think 5 --out /tmp/lt_steady.json
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import random
import statistics
import sys
import time
from dataclasses import dataclass, field

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

API = "http://localhost:8000/api"
ADMIN, PASSWORD = "admin", "ChangeMe123!"

# 问题池：覆盖事实型 / 流程型 / 长尾型，避免全是同一句导致缓存命中失真
QUESTIONS = [
    "报销流程分几步？每一步的责任人是谁？",
    "试用期员工的年假怎么计算？",
    "采购合同需要哪几级审批？",
    "数据导出有哪些权限限制？",
    "月度考核的评分标准是什么？",
    "差旅住宿标准按城市怎么分档？",
    "离职交接需要提交哪些材料？",
    "知识库文档多久需要复审一次？",
]


@dataclass
class Rec:
    """单次请求的观测值。失败的请求只填 status/error。"""

    status: int = 0
    ok: bool = False
    refused: bool = False
    meta_ms: float = 0.0       # → meta 事件
    retrieve_ms: float = 0.0   # → citations 事件（检索完成）
    ttft_ms: float = 0.0       # → 首个 delta
    total_ms: float = 0.0      # → done 事件
    chars: int = 0
    error: str = ""
    stage_ms: dict[str, float] = field(default_factory=dict)


def _encrypt(pub_b64: str, plain: str) -> str:
    pub = serialization.load_der_public_key(base64.b64decode(pub_b64))
    ct = pub.encrypt(
        plain.encode(),
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    return base64.b64encode(ct).decode()


async def login(client: httpx.AsyncClient) -> str:
    pk = (await client.get(f"{API}/auth/public-key")).json()["public_key"]
    r = await client.post(
        f"{API}/auth/login", json={"username": ADMIN, "password": _encrypt(pk, PASSWORD)}
    )
    r.raise_for_status()
    return r.json()["token"]


async def one_ask(
    client: httpx.AsyncClient, token: str, kb: str, question: str, timeout: float
) -> Rec:
    """发一次 /chat/ask，边读 SSE 边打时间戳。"""
    rec = Rec()
    t0 = time.perf_counter()
    payload = {"kb_ids": [kb], "question": question}
    try:
        async with client.stream(
            "POST",
            f"{API}/chat/ask",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        ) as resp:
            rec.status = resp.status_code
            if resp.status_code != 200:
                body = (await resp.aread()).decode(errors="replace")[:200]
                rec.error = f"HTTP {resp.status_code}: {body}"
                rec.total_ms = (time.perf_counter() - t0) * 1000
                return rec
            event = ""
            async for line in resp.aiter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                now = (time.perf_counter() - t0) * 1000
                data = line[5:].strip()
                if event == "meta" and not rec.meta_ms:
                    rec.meta_ms = now
                elif event == "stage":
                    try:
                        s = json.loads(data)
                        rec.stage_ms[s.get("stage", "?")] = float(s.get("ms", 0))
                    except json.JSONDecodeError:
                        pass
                elif event == "citations" and not rec.retrieve_ms:
                    rec.retrieve_ms = now
                elif event == "refused":
                    rec.refused = True
                    rec.ok = True
                elif event == "delta" and not rec.ttft_ms:
                    rec.ttft_ms = now
                    rec.chars += len(data)
                elif event == "delta":
                    rec.chars += len(data)
                elif event == "done":
                    rec.total_ms = now
                    rec.ok = True
                elif event == "error":
                    rec.error = data[:200]
    except (httpx.HTTPError, asyncio.TimeoutError) as exc:
        rec.error = f"{type(exc).__name__}: {exc}"[:200]
        rec.total_ms = (time.perf_counter() - t0) * 1000
        return rec
    if not rec.total_ms:
        rec.total_ms = (time.perf_counter() - t0) * 1000
    rec.ok = rec.ok or rec.status == 200
    # 检索完成但没等到 delta（拒答路径）→ ttft 归零，不参与 TTFT 分位数
    return rec


async def vu(
    idx: int, token: str, kb: str, deadline: float, think: float, timeout: float, out: list[Rec]
) -> None:
    """一个虚拟用户：循环提问直到到达 deadline。"""
    rnd = random.Random(1000 + idx)
    async with httpx.AsyncClient(timeout=None, limits=httpx.Limits(max_connections=4)) as client:
        while time.perf_counter() < deadline:
            q = rnd.choice(QUESTIONS)
            out.append(await one_ask(client, token, kb, q, timeout))
            if think > 0 and time.perf_counter() < deadline:
                await asyncio.sleep(think * rnd.uniform(0.7, 1.3))


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((p / 100) * (len(s) - 1)))))
    return s[k]


def summarize(label: str, recs: list[Rec], wall: float) -> dict:
    ok = [r for r in recs if r.ok]
    fail = [r for r in recs if not r.ok]
    answered = [r for r in ok if not r.refused]
    ttft = [r.ttft_ms for r in answered if r.ttft_ms]
    ret = [r.retrieve_ms for r in ok if r.retrieve_ms]
    tot = [r.total_ms for r in recs if r.total_ms]
    return {
        "label": label,
        "wall_s": round(wall, 1),
        "requests": len(recs),
        "ok": len(ok),
        "refused": sum(1 for r in ok if r.refused),
        "failed": len(fail),
        "qps": round(len(recs) / wall, 2) if wall else 0,
        "error_rate_pct": round(100 * len(fail) / len(recs), 2) if recs else 0,
        "ttft_p50": round(pct(ttft, 50), 1),
        "ttft_p95": round(pct(ttft, 95), 1),
        "ttft_p99": round(pct(ttft, 99), 1),
        "retrieve_p50": round(pct(ret, 50), 1),
        "retrieve_p95": round(pct(ret, 95), 1),
        "total_p50": round(pct(tot, 50), 1),
        "total_p95": round(pct(tot, 95), 1),
        "avg_chars": round(statistics.mean([r.chars for r in answered]), 1) if answered else 0,
        "errors": sorted({r.error for r in fail})[:5],
    }


async def run_stage(
    concurrency: int, seconds: float, think: float, kb: str, timeout: float
) -> dict:
    async with httpx.AsyncClient(timeout=None) as client:
        token = await login(client)
    recs: list[Rec] = []
    deadline = time.perf_counter() + seconds
    t0 = time.perf_counter()
    await asyncio.gather(
        *[vu(i, token, kb, deadline, think, timeout, recs) for i in range(concurrency)]
    )
    return summarize(f"c={concurrency} think={think}s", recs, time.perf_counter() - t0)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb", required=True, help="知识库 id")
    ap.add_argument("--stages", default="", help="阶梯模式，如 1:120,5:120,10:120")
    ap.add_argument("--concurrency", type=int, default=20)
    ap.add_argument("--duration", type=float, default=1800, help="秒，稳态模式用")
    ap.add_argument("--think", type=float, default=5.0, help="请求间隔秒数，0=饱和压")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    results = []
    if args.stages:
        for spec in args.stages.split(","):
            c, _, sec = spec.partition(":")
            r = await run_stage(int(c), float(sec), args.think, args.kb, args.timeout)
            results.append(r)
            print(json.dumps(r, ensure_ascii=False), flush=True)
    else:
        r = await run_stage(args.concurrency, args.duration, args.think, args.kb, args.timeout)
        results.append(r)
        print(json.dumps(r, ensure_ascii=False), flush=True)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
