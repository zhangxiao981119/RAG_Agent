"""思维链（CoT）测试 —— 决策 / 剥离 / 流式 / 兜底 四条路径。

★ 为什么值得测：CoT 的实现**完全依赖模型是否遵守 prompt 契约**（把推理写进
  <reasoning> 标签）。而"模型没遵守"的那条兜底路径，恰恰是最不会被手工验到的
  —— 正常演示时模型通常听话，只有压力/弱模型下才会露出来。必须由测试钉住。

★ 另外钉一条硬要求：推理链 MUST NOT 推给前端（decisions.COT_REASONING_EXPOSED
  为 False），它只落库供审计。
"""
from __future__ import annotations

import uuid

import pytest

from app.config import decisions
from app.infra import metrics
from app.services import generate as gen
from app.services.retrieve.base import RetrievedChunk


def _chunk(content: str = "一线城市住宿上限 500 元") -> RetrievedChunk:
    uid = uuid.uuid4()
    return RetrievedChunk(
        chunk_id=uid,
        document_id=uid,
        kb_id=uid,
        filename="差旅制度.md",
        content=content,
        heading_path="制度/报销",
        page_no=1,
        level_rank=0,
        vector_score=0.9,
        keyword_score=None,
        rrf_score=0.03,
        rerank_score=0.8,
        final_score=0.8,
    )


class _FakeLLM:
    """按预设分片吐字，模拟 SSE 的 delta 切片。"""

    def __init__(self, pieces: list[str]) -> None:
        self.pieces = pieces

    async def stream_chat(self, messages, temperature):  # noqa: ANN001
        for piece in self.pieces:
            yield piece


def _patch_llm(monkeypatch, pieces: list[str]) -> None:
    monkeypatch.setattr(gen, "get_llm_service", lambda: _FakeLLM(pieces))


@pytest.fixture(autouse=True)
def _isolate_metrics():
    metrics.reset()
    yield
    metrics.reset()


async def _collect(monkeypatch, pieces: list[str], **kwargs):
    """跑一次流式生成，返回 (推给前端的正文, done 事件)。"""
    _patch_llm(monkeypatch, pieces)
    service = gen.StreamGenerationService()
    deltas: list[str] = []
    done = None
    async for evt in service.stream_generate(
        "住宿上限是多少", [_chunk()], tenant_id=None, **kwargs
    ):
        if evt.type == "delta":
            deltas.append(evt.text)
        elif evt.type == "done":
            done = evt
    return "".join(deltas), done


# ── 决策层：should_enable_cot ─────────────────────────────────────


@pytest.mark.parametrize("score", [None, 0, 50, 89, 90, 100])
def test_mode_off_never_enables(monkeypatch, score):
    monkeypatch.setattr(decisions, "COT_MODE", "off")
    assert decisions.should_enable_cot(score) is False


@pytest.mark.parametrize("score", [None, 0, 100])
def test_mode_full_always_enables(monkeypatch, score):
    monkeypatch.setattr(decisions, "COT_MODE", "full")
    assert decisions.should_enable_cot(score) is True


def test_adaptive_enables_below_threshold(monkeypatch):
    monkeypatch.setattr(decisions, "COT_MODE", "adaptive")
    assert decisions.should_enable_cot(decisions.QUERY_REWRITE_SCORE_THRESHOLD - 1) is True


def test_adaptive_skips_at_or_above_threshold(monkeypatch):
    monkeypatch.setattr(decisions, "COT_MODE", "adaptive")
    assert decisions.should_enable_cot(decisions.QUERY_REWRITE_SCORE_THRESHOLD) is False
    assert decisions.should_enable_cot(100) is False


def test_adaptive_without_score_stays_off(monkeypatch):
    """拿不到复杂度信号 → 不开。无差别开启正是这条口径要避免的成本。"""
    monkeypatch.setattr(decisions, "COT_MODE", "adaptive")
    assert decisions.should_enable_cot(None) is False


def test_adaptive_without_reuse_signal_degrades_to_off(monkeypatch):
    """关掉"复用改写分"后没有别的判据 —— 明确退化为不生效，而不是瞎猜。"""
    monkeypatch.setattr(decisions, "COT_MODE", "adaptive")
    monkeypatch.setattr(decisions, "COT_ADAPTIVE_REUSE_REWRITE_SCORE", False)
    assert decisions.should_enable_cot(10) is False


# ── 非流式剥离：_extract_reasoning ────────────────────────────────


def test_extract_reasoning_strips_block():
    text = "<reasoning>\n先看意图\n再找依据\n</reasoning>\n\n答案是 500 元 [1]"
    clean, reasoning = gen._extract_reasoning(text, True)

    assert "先看意图" not in clean
    assert clean.startswith("答案是 500 元")
    assert "先看意图" in reasoning


def test_extract_reasoning_unclosed_keeps_text_intact():
    """未闭合 → **不改正文**。宁可漏出推理，也不给用户空白回答。"""
    text = "<reasoning>\n模型忘了收尾\n答案是 500 元"
    clean, reasoning = gen._extract_reasoning(text, True)

    assert clean == text
    assert reasoning == ""
    assert metrics.get("rag.cot.unclosed") == 1, "未闭合必须可观测"


def test_extract_reasoning_noop_when_cot_disabled():
    text = "<reasoning>x</reasoning>正文"
    clean, reasoning = gen._extract_reasoning(text, False)

    assert clean == text
    assert reasoning == ""


# ── 流式：剥离与兜底 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_hides_reasoning_but_returns_it_in_done(monkeypatch):
    text, done = await _collect(
        monkeypatch,
        [
            "<reasoning>\n",
            "先确认问题问的是住宿上限\n",
            "再核对 [1] 是否对应\n",
            "</reasoning>\n",
            "一线城市住宿上限 500 元 [1]\n",
        ],
        rewrite_score=10,
    )

    assert "先确认问题" not in text, "推理链 MUST NOT 推给前端"
    assert "一线城市住宿上限 500 元 [1]" in text
    assert done is not None
    assert "先确认问题" in done.reasoning, "推理链 MUST 随 done 返回以供落库"


@pytest.mark.asyncio
async def test_stream_unclosed_reasoning_falls_back_to_body(monkeypatch):
    """模型漏写 </reasoning> → 内容按正文推出，而不是凭空消失。"""
    text, _ = await _collect(
        monkeypatch,
        ["<reasoning>\n", "忘了收尾标签\n", "答案是 500 元 [1]\n"],
        rewrite_score=10,
    )

    assert "答案是 500 元 [1]" in text
    assert metrics.get("rag.cot.unclosed") == 1


@pytest.mark.asyncio
async def test_stream_without_cot_is_untouched(monkeypatch):
    """不开 CoT 时行为与从前一致 —— 新增过滤不能影响主路径。"""
    text, done = await _collect(
        monkeypatch,
        ["一线城市住宿上限 500 元 [1]\n", "更高密级需审批 [1]\n"],
        rewrite_score=95,
    )

    assert "一线城市住宿上限 500 元 [1]" in text
    assert "更高密级需审批 [1]" in text
    assert done is not None
    assert done.reasoning == ""
    assert metrics.get("rag.cot.unclosed") == 0
