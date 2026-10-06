"""指标收集与重排降级单元测试。

覆盖点：计数器/直方图行为、标签隔离、reset 语义；
以及重排服务在未配置时的降级行为（此前降级是静默的）。
"""
from __future__ import annotations

import pytest

from app.infra import metrics
from app.services.rerank import RerankService, RerankTimeout


@pytest.fixture(autouse=True)
def _clean_metrics():
    """每个用例前后清空指标，避免互相污染。"""
    metrics.reset()
    yield
    metrics.reset()


# ── 指标 ──────────────────────────────────────────────


def test_counter_increments():
    metrics.incr("rag.test.hit")
    metrics.incr("rag.test.hit")
    assert metrics.get("rag.test.hit") == 2


def test_counter_accepts_value():
    metrics.incr("rag.test.batch", value=5)
    assert metrics.get("rag.test.batch") == 5


def test_tags_are_isolated():
    metrics.incr("rag.rerank.degraded", reason="timeout")
    metrics.incr("rag.rerank.degraded", reason="timeout")
    metrics.incr("rag.rerank.degraded", reason="error")

    assert metrics.get("rag.rerank.degraded", reason="timeout") == 2
    assert metrics.get("rag.rerank.degraded", reason="error") == 1
    # 无边界的读取应返回 0，而不是把带标签的合计
    assert metrics.get("rag.rerank.degraded") == 0


def test_tag_order_does_not_split_series():
    """标签书写顺序不同不应产生两条时间序列。"""
    metrics.incr("x", a="1", b="2")
    metrics.incr("x", b="2", a="1")
    assert metrics.get("x", a="1", b="2") == 2


def test_histogram_tracks_count_sum_min_max():
    metrics.observe_ms("rag.retrieve.duration", 100.0)
    metrics.observe_ms("rag.retrieve.duration", 300.0)
    metrics.observe_ms("rag.retrieve.duration", 200.0)

    snap = metrics.snapshot()
    hist = snap["histograms"]["rag.retrieve.duration"]
    assert hist["count"] == 3
    assert hist["sum_ms"] == 600.0
    assert hist["avg_ms"] == 200.0
    assert hist["min_ms"] == 100.0
    assert hist["max_ms"] == 300.0


def test_snapshot_shape():
    metrics.incr("a")
    snap = metrics.snapshot()
    assert set(snap.keys()) == {"counters", "histograms"}
    assert snap["counters"]["a"] == 1


def test_reset_clears_everything():
    metrics.incr("a")
    metrics.observe_ms("b", 1.0)
    metrics.reset()
    snap = metrics.snapshot()
    assert snap["counters"] == {}
    assert snap["histograms"] == {}


# ── 重排降级 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rerank_raises_timeout_when_url_not_configured(monkeypatch):
    """未配置 RERANK_BASE_URL 时必须抛 RerankTimeout（走降级），而不是崩。"""
    from app.config import settings as settings_mod

    class _S:
        rerank_base_url = ""
        rerank_model = "bge-reranker-v2-m3"
        rerank_timeout_seconds = 3.0

    monkeypatch.setattr(settings_mod, "get_settings", lambda: _S())

    with pytest.raises(RerankTimeout):
        await RerankService().rerank("q", ["d"], top_n=1)


@pytest.mark.asyncio
async def test_healthcheck_returns_false_when_unavailable(monkeypatch):
    """探活必须走真实调用路径 —— 容器 healthy ≠ 接口可用。"""
    from app.config import settings as settings_mod

    class _S:
        rerank_base_url = ""  # 未配置 → 必然不可用
        rerank_model = "bge-reranker-v2-m3"
        rerank_timeout_seconds = 0.1

    monkeypatch.setattr(settings_mod, "get_settings", lambda: _S())

    assert await RerankService().healthcheck() is False


@pytest.mark.asyncio
async def test_healthcheck_returns_true_when_ok(monkeypatch):
    svc = RerankService()

    async def _fake_rerank(query, documents, top_n):
        return [(0, 0.9)]

    monkeypatch.setattr(svc, "rerank", _fake_rerank)
    assert await svc.healthcheck() is True
