"""query_rewrite 全分支测试 —— 本模块此前**零测试**。

为什么值得补：它坐在 chat 的主路径上。阈值
`QUERY_REWRITE_SCORE_THRESHOLD = 90`，而 prompt 的评分标定里 90-100 才是
「自包含、无歧义」，70-89 只是「基本可检索」—— 所以「score < 90 → 走改写
分支」是**主路径而非边缘路径**。分支又多（含 4 个异常/兜底分支），漏测代价高。

覆盖 9 个返回标签：
  disabled / skip_high_score / rewritten / rewrite_contract_violated /
  rewrite_no_change / parse_error / timeout / llm_error / error
"""
from __future__ import annotations

import asyncio

import pytest

from app.config import decisions
from app.infra import metrics
from app.services import query_rewrite as qr
from app.services.llm import LLMError


class _FakeLLM:
    """替身：返回预设文本，或抛预设异常。"""

    def __init__(self, raw: str = "", exc: Exception | None = None) -> None:
        self.raw = raw
        self.exc = exc
        self.calls = 0

    async def chat(self, messages, temperature):  # noqa: ANN001
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.raw


def _patch_llm(monkeypatch, raw: str = "", exc: Exception | None = None) -> _FakeLLM:
    fake = _FakeLLM(raw, exc)
    monkeypatch.setattr(qr, "get_llm_service", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def _isolate_metrics():
    """指标是全局计数器，用例之间必须隔离，否则计数断言会互相污染。"""
    metrics.reset()
    yield
    metrics.reset()


# ── 开关与高分短路 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disabled_never_touches_llm(monkeypatch):
    """关闭改写时**不应调用 LLM** —— 用会抛异常的替身证明它没被碰过。"""
    monkeypatch.setattr(decisions, "QUERY_REWRITE_ENABLED", False)
    fake = _patch_llm(monkeypatch, exc=AssertionError("disabled 时不应调用 LLM"))

    query, status, score = await qr.rewrite_query("任意问题")

    assert (query, status, score) == ("任意问题", "disabled", 100)
    assert fake.calls == 0


@pytest.mark.asyncio
async def test_high_score_skips_even_if_query_given(monkeypatch):
    """高分优先：score >= 阈值就不改写，哪怕 LLM 同时给了 query。"""
    _patch_llm(
        monkeypatch,
        raw='{"score": 95, "need_rewrite": true, "query": "一个改写版"}',
    )

    query, status, score = await qr.rewrite_query("这个是自包含的问题")

    assert status == "skip_high_score"
    assert query == "这个是自包含的问题"
    assert score == 95


# ── 正常改写路径 ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_low_score_with_query_rewrites(monkeypatch):
    _patch_llm(
        monkeypatch,
        raw='{"score": 45, "need_rewrite": true, '
            '"query": "差旅报销标准 一线城市 住宿费上限", "intent": "报销"}',
    )

    query, status, score = await qr.rewrite_query("那个标准是多少")

    assert status == "rewritten"
    assert query == "差旅报销标准 一线城市 住宿费上限"
    assert score == 45


@pytest.mark.asyncio
async def test_trusts_query_even_when_need_rewrite_false(monkeypatch):
    """score 低 → 信任 score：LLM 给了改写版就用，即使它标了 need_rewrite=false。

    这是有意的设计判断（分数低说明检索不友好，改写版总比原问好）。
    本用例把行为钉死，避免日后被"更严格地遵循 need_rewrite"改掉 ——
    那样会降低改写率，反而让检索质量变差。
    """
    _patch_llm(
        monkeypatch,
        raw='{"score": 78, "need_rewrite": false, "query": "文档上传流程"}',
    )

    query, status, _ = await qr.rewrite_query("怎么上传")

    assert status == "rewritten"
    assert query == "文档上传流程"


# ── 新增：契约违反 vs 合法未改写 的区分 ────────────────────────────


@pytest.mark.asyncio
async def test_contract_violation_when_no_query_given(monkeypatch):
    """★ 本组核心：need_rewrite=true 却拿不到 query → 单独标记 + 记指标。

    原实现把它混进 rewrite_no_change 并打日志「改写结果未变化」——
    那句话是**错的**，LLM 根本没给改写结果。混在一起会让"漏改"永远静默：
    看日志的人会以为"改写不起作用"，而非"LLM 没履行输出契约"。
    """
    _patch_llm(monkeypatch, raw='{"score": 40, "need_rewrite": true}')

    query, status, score = await qr.rewrite_query("那个东西怎么弄")

    assert status == "rewrite_contract_violated"
    assert query == "那个东西怎么弄"  # 主流程行为不变：无内容可改，回退原问题
    assert score == 40
    assert metrics.get("rag.rewrite.contract_violation") == 1


@pytest.mark.asyncio
async def test_no_change_when_need_rewrite_false_and_no_query(monkeypatch):
    """合法未改写：LLM 说不改、也没给内容 → rewrite_no_change，且**不计**违约。"""
    _patch_llm(monkeypatch, raw='{"score": 75, "need_rewrite": false}')

    query, status, _ = await qr.rewrite_query("请说明上传步骤")

    assert status == "rewrite_no_change"
    assert query == "请说明上传步骤"
    assert metrics.get("rag.rewrite.contract_violation") == 0


@pytest.mark.asyncio
async def test_no_change_when_rewritten_equals_question(monkeypatch):
    """给了改写但与原问逐字相同 → rewrite_no_change。

    这**不算**契约违反 —— LLM 履行了格式契约（给了内容），
    只是判定无需实质变更。与"没给内容"是两件事。
    """
    _patch_llm(
        monkeypatch,
        raw='{"score": 60, "need_rewrite": true, "query": "同一个问题"}',
    )

    _, status, _ = await qr.rewrite_query("同一个问题")

    assert status == "rewrite_no_change"
    assert metrics.get("rag.rewrite.contract_violation") == 0


# ── 容错与降级 ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_json_wrapped_in_markdown_fence_still_parsed(monkeypatch):
    """prompt 要求不给围栏，但模型常加 —— _extract_json 必须容错。"""
    _patch_llm(
        monkeypatch,
        raw='```json\n{"score": 50, "need_rewrite": true, "query": "补全后的问题"}\n```',
    )

    query, status, _ = await qr.rewrite_query("它是什么")

    assert status == "rewritten"
    assert query == "补全后的问题"


@pytest.mark.asyncio
async def test_parse_error_uses_rule_fallback_score(monkeypatch):
    """无 JSON → parse_error；规则兜底给分（≤5 字 → 50），但不改写（无内容可改）。"""
    _patch_llm(monkeypatch, raw="这不是 JSON")

    query, status, score = await qr.rewrite_query("那个")

    assert status == "parse_error"
    assert query == "那个"
    assert score == 50


@pytest.mark.asyncio
async def test_parse_error_without_rule_hit_reports_full_score(monkeypatch):
    """规则兜底未命中 → 返回 100，表示不触发改写。"""
    _patch_llm(monkeypatch, raw="随便一段没有 JSON 的文本")

    _, status, score = await qr.rewrite_query("请详细说明文档上传的完整操作步骤")

    assert status == "parse_error"
    assert score == 100


@pytest.mark.asyncio
async def test_invalid_score_value_treated_as_full(monkeypatch):
    """score 字段不是数字 → 视为 100 → 跳过改写（宁可少改，也不错改）。"""
    _patch_llm(
        monkeypatch,
        raw='{"score": "很高", "need_rewrite": true, "query": "改写版"}',
    )

    _, status, score = await qr.rewrite_query("测试问题")

    assert status == "skip_high_score"
    assert score == 100


@pytest.mark.asyncio
async def test_timeout_falls_back_to_original(monkeypatch):
    _patch_llm(monkeypatch, exc=asyncio.TimeoutError())

    query, status, score = await qr.rewrite_query("超时的问题")

    assert (query, status, score) == ("超时的问题", "timeout", 100)


@pytest.mark.asyncio
async def test_llm_error_falls_back_to_original(monkeypatch):
    _patch_llm(monkeypatch, exc=LLMError("上游 500"))

    query, status, _ = await qr.rewrite_query("失败的问题")

    assert (query, status) == ("失败的问题", "llm_error")


@pytest.mark.asyncio
async def test_unexpected_error_falls_back_to_original(monkeypatch):
    """未预期异常也不得让请求失败 —— 改写是增强，不是必需环节。"""
    _patch_llm(monkeypatch, exc=RuntimeError("意料之外"))

    query, status, _ = await qr.rewrite_query("异常的问题")

    assert (query, status) == ("异常的问题", "error")
