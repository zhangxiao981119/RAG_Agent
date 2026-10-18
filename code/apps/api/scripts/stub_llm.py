"""本地 OpenAI 兼容 LLM 桩 —— 压测专用，用来把「上游时延」从「自建链路时延」里剥掉。

为什么需要它：
  · 真上游（DeepSeek / 任何第三方 API）在 20 并发下，P95 主要由**对方排队**决定，
    测出来的是别人的容量，不是本项目的代码容量；真被限流还会让错误率失去意义。
  · 压测要回答的是「检索 + 重排 + prompt 组装 + SSE + 落库这条链路能扛多少并发」，
    上游必须可控、可复现。

★ 按 prompt 特征分三档作答（这点很关键，否则时延结构是假的）：
  一次问答链路会打 **2~3 次** LLM，如果不区分，按同一个 profile 回，
  改写那一步会被灌进 120 token 的输出，retrieve 时间被凭空放大一倍。
  判别依据是提示词里稳定存在的角色句（见 app/config/prompts.py）：
    · "检索优化器" → 改写/评分，短 JSON
    · "信息抽取器" → 用户记忆抽取，短 JSON
    · 其余         → 正文生成，输出带 [n] 引用的答案

用法（独立容器，不打进生产镜像）：
    docker run -d --name llm-stub --network code_default \
        -v <abs>/stub_llm.py:/tmp/stub_llm.py:ro code-api \
        python /tmp/stub_llm.py
    # 再把 api 的 LLM_BASE_URL 指到 http://llm-stub:8099/v1
    # 整套编排见 docker-compose.loadtest.yml

可调参数（环境变量）：
    STUB_TTFT_MS        生成档：首 token 前等待，默认 400
    STUB_TOKEN_MS       生成档：每 token 间隔，默认 15
    STUB_TOKENS         生成档：输出 token 数，默认 120
    STUB_JITTER_MS      每 token 间隔的 ±随机抖动，默认 8（避免所有流完全同步）
    STUB_FAIL_RATE      返回 500 的概率，默认 0（用来验证客户端错误处理）
    STUB_REWRITE_SCORE  改写档返回的评分，默认 75（<90 → 走 CoT 重路径，贴近生产）
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

TTFT_MS = int(os.getenv("STUB_TTFT_MS", "400"))
TOKEN_MS = int(os.getenv("STUB_TOKEN_MS", "15"))
TOKENS = int(os.getenv("STUB_TOKENS", "120"))
JITTER_MS = int(os.getenv("STUB_JITTER_MS", "8"))
FAIL_RATE = float(os.getenv("STUB_FAIL_RATE", "0"))
REWRITE_SCORE = int(os.getenv("STUB_REWRITE_SCORE", "75"))

app = FastAPI(title="llm-stub")

# 正文答案：句子里带 [1][2]，否则 grounding 判定「无引用」→ 直接拒答，
# 压测就只测到检索、测不到生成与落库（实测踩过：不加引用 12/12 全 refused）。
_ANSWER = (
    "根据知识库中的记录[1]，该事项的处理分为三步："
    "先确认适用范围与前置条件，再按规范执行并留存凭证[2]，"
    "最后在系统中登记结果并同步相关方。以上流程应在规定时限内完成。"
)

# 改写档 / 记忆抽取档的固定短输出（贴近真实 token 量：20~40 token）
_REWRITE_JSON = json.dumps(
    {"score": REWRITE_SCORE, "need_rewrite": True,
     "query": "知识库文档处理流程与审批要求", "intent": "流程咨询"},
    ensure_ascii=False,
)
_MEMORY_JSON = json.dumps({"facts": []}, ensure_ascii=False)


def _flatten(body: dict) -> str:
    """把 messages 拍平成一段文本，用于判别这是哪一类调用。"""
    out: list[str] = []
    for m in body.get("messages", []):
        c = m.get("content")
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):  # 多模态 content 数组
            for item in c:
                if isinstance(item, dict) and item.get("type") == "text":
                    out.append(item.get("text", ""))
    return "\n".join(out)


def _profile(prompt: str) -> tuple[str, int, int]:
    """返回 (文本, ttft_ms, 每 token 毫秒)。

    ★ 改写/拆解/抽取档给固定短时延：这类调用生产上就是 0.3~0.6s 返回几十 token 的 JSON，
      用生成档的 profile 会把 retrieve 时间放大到 2s+，属于自欺。
      实测踩过：漏掉拆解档时 retrieve_ms 从 163ms 虚增到 2435ms，
      因为拆解拿到的 120 字被当成正文 JSON 解析失败后降级单趟 —— 时延凭空多 2.2s。
    """
    if "检索优化器" in prompt:
        return _REWRITE_JSON, 150, 4
    if "检索问题拆解器" in prompt:
        # 空 subquestions 列表 = 「问题已足够聚焦」，normalize 会保留原问题 → 单趟检索
        return '{"subquestions": []}', 120, 4
    if "信息抽取器" in prompt:
        return _MEMORY_JSON, 120, 4
    return _fit(_ANSWER, TOKENS), TTFT_MS, TOKEN_MS


def _fit(text: str, n: int) -> str:
    """把固定答案补/截到 n 字，用来控制流式长度。"""
    if len(text) >= n:
        return text[:n]
    filler = "以上要求需在制度规定的时限内完成并留痕[1]，具体口径以最新制度版本为准。"
    while len(text) < n:
        text += filler
    return text[:n]


def _chunk(content: str, finish: str | None = None) -> str:
    return "data: " + json.dumps(
        {
            "id": "stub",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": "stub",
            "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": finish}],
        },
        ensure_ascii=False,
    ) + "\n\n"


async def _stream(text: str, ttft_ms: int, token_ms: int):
    await asyncio.sleep(ttft_ms / 1000)
    for ch in text:
        delay = (token_ms + random.uniform(-JITTER_MS, JITTER_MS) / 10) / 1000
        if delay > 0:
            await asyncio.sleep(delay)
        yield _chunk(ch)
    yield _chunk("", "stop")
    yield "data: [DONE]\n\n"


@app.post("/v1/chat/completions")
async def completions(request: Request):
    if FAIL_RATE > 0 and random.random() < FAIL_RATE:
        return JSONResponse({"error": {"message": "stub injected failure"}}, status_code=500)
    body = await request.json()
    text, ttft, tok = _profile(_flatten(body))
    if not body.get("stream"):
        return JSONResponse(
            {
                "id": "stub",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "stub",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": len(text),
                          "total_tokens": len(text)},
            }
        )
    return StreamingResponse(_stream(text, ttft, tok), media_type="text/event-stream")


@app.get("/health")
async def health():
    return {"ok": True, "ttft_ms": TTFT_MS, "token_ms": TOKEN_MS,
            "tokens": TOKENS, "rewrite_score": REWRITE_SCORE}


if __name__ == "__main__":
    import uvicorn

    # 必须绑 0.0.0.0：桩是独立容器，绑 127.0.0.1 时 api 容器连不进来（只在本容器内可见）
    uvicorn.run(app, host=os.getenv("STUB_HOST", "0.0.0.0"),
                port=int(os.getenv("STUB_PORT", "8099")), log_level="warning")
