"""生成层拒答守卫与重试测试（D+A）。

钉住四件事：
  1. 拒答文案在确认前不外发 —— 前端不会看到"先拒答、再吐答案"的拼接
  2. 上下文充足却输出契约拒答 ⇒ 自动重试一次，重试成功即正常返回
  3. 上下文不足（< GENERATION_REFUSAL_RETRY_MIN_CHUNKS）不重试
  4. 两次都拒答 ⇒ 返回拒答，并打点 refusal_with_context / retry / after_retry
"""
from __future__ import annotations

import uuid

import pytest

from app.config import decisions
from app.config.prompts import NO_ANSWER_TEXT
from app.services import generate as generate_mod
from app.services.generate import (
    StreamGenerationService,
    is_possible_refusal_prefix,
)
from app.services.retrieve.base import RetrievedChunk

ANSWER_OK = "Netty 的 Channel 封装了 NIO 多路复用，传统 IO 流是阻塞式的字节读写 [1]。"
ANSWER_OK_2 = "Channel 基于事件驱动，传统 IO 流基于阻塞读写，两者模型不同 [1]。"


def _chunk(n: int = 1, score: float = 0.7) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        kb_id=uuid.uuid4(),
        filename="JAVA面试核心知识点整理.pdf",
        content="Netty 的 Channel 是对 NIO 的封装，传统 IO 流是阻塞式读写。",
        heading_path="Netty",
        page_no=n,
        level_rank=20,
        vector_score=score,
        keyword_score=None,
        rrf_score=0.03,
        rerank_score=None,
        final_score=score,
    )


def _chunks(count: int, score: float = 0.7) -> list[RetrievedChunk]:
    return [_chunk(i, score) for i in range(1, count + 1)]


class FakeLLM:
    """按调用次序回放脚本；每次调用记录一次。"""

    def __init__(self, scripts: list[list[str]]) -> None:
        self.scripts = scripts
        self.calls = 0

    async def stream_chat(self, messages, temperature):  # noqa: ARG002
        script = self.scripts[min(self.calls, len(self.scripts) - 1)]
        self.calls += 1
        for piece in script:
            yield piece


@pytest.fixture
def counters(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """拦截生成模块的指标打点，返回记录列表。"""
    seen: list[str] = []
    monkeypatch.setattr(generate_mod.metrics, "incr", lambda name, value=1, **tags: seen.append(name))
    return seen


def _service(monkeypatch: pytest.MonkeyPatch, llm: FakeLLM) -> StreamGenerationService:
    monkeypatch.setattr(generate_mod, "get_llm_service", lambda: llm)
    return StreamGenerationService()


async def _collect(service: StreamGenerationService, chunks: list[RetrievedChunk]) -> list:
    return [
        evt
        async for evt in service.stream_generate("Netty 的 Channel 和传统 IO 流有什么区别？", chunks)
    ]


# ── 守卫判定（纯函数）─────────────────────────────────────


def test_prefix_guard_holds_refusal_and_headings() -> None:
    assert is_possible_refusal_prefix("")
    assert is_possible_refusal_prefix("知识库")
    assert is_possible_refusal_prefix(NO_ANSWER_TEXT)
    assert is_possible_refusal_prefix("#### 说明")
    assert is_possible_refusal_prefix("关于报销范围，")


def test_prefix_guard_releases_normal_answer() -> None:
    assert not is_possible_refusal_prefix(ANSWER_OK)
    assert not is_possible_refusal_prefix("Netty 的 Channel 是异步的。")


# ── 重试行为 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retry_recovers_when_context_sufficient(monkeypatch, counters) -> None:
    llm = FakeLLM([[NO_ANSWER_TEXT], [ANSWER_OK]])
    events = await _collect(_service(monkeypatch, llm), _chunks(3))

    assert llm.calls == 2
    assert [e.type for e in events][-1] == "done"
    assert "Channel" in events[-1].full_text
    # 拒答文案一个字都没发给前端
    assert all(NO_ANSWER_TEXT not in e.text for e in events)
    assert "rag.generate.refusal_with_context" in counters
    assert "rag.generate.refusal_retry" in counters
    assert "rag.generate.refusal_retry_recovered" in counters


@pytest.mark.asyncio
async def test_no_retry_when_context_too_small(monkeypatch, counters) -> None:
    llm = FakeLLM([[NO_ANSWER_TEXT]])
    events = await _collect(
        _service(monkeypatch, llm),
        _chunks(decisions.GENERATION_REFUSAL_RETRY_MIN_CHUNKS - 1),
    )

    assert llm.calls == 1
    assert events[-1].type == "refused"
    assert events[-1].refuse_reason == "NO_RELEVANT_CONTENT"
    assert "rag.generate.refusal_retry" not in counters


@pytest.mark.asyncio
async def test_no_retry_when_top1_score_low(monkeypatch, counters) -> None:
    """条数够但 top1 分低于门槛 ⇒ 不重试（不可答问题也有 8 条相邻上下文）。"""
    llm = FakeLLM([[NO_ANSWER_TEXT], [ANSWER_OK]])
    events = await _collect(
        _service(monkeypatch, llm),
        _chunks(3, score=decisions.GENERATION_REFUSAL_RETRY_MIN_SCORE - 0.1),
    )

    assert llm.calls == 1
    assert events[-1].type == "refused"
    # 条数达标仍记发生率，但不重试
    assert "rag.generate.refusal_with_context" in counters
    assert "rag.generate.refusal_retry" not in counters


@pytest.mark.asyncio
async def test_refusal_after_retry_returns_refused(monkeypatch, counters) -> None:
    llm = FakeLLM([[NO_ANSWER_TEXT], [NO_ANSWER_TEXT]])
    events = await _collect(_service(monkeypatch, llm), _chunks(3))

    assert llm.calls == 2
    assert events[-1].type == "refused"
    assert "rag.generate.refusal_after_retry" in counters
    # 两次尝试只记一次发生率，避免指标翻倍
    assert counters.count("rag.generate.refusal_with_context") == 1


@pytest.mark.asyncio
async def test_normal_answer_not_retried(monkeypatch, counters) -> None:
    llm = FakeLLM([[ANSWER_OK]])
    events = await _collect(_service(monkeypatch, llm), _chunks(3))

    assert llm.calls == 1
    assert events[-1].type == "done"
    assert "".join(e.text for e in events if e.type == "delta").strip() == ANSWER_OK
    assert "rag.generate.refusal_retry" not in counters
