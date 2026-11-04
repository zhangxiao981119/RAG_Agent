"""run_eval.py 的图召回观测聚合（§14.6 可测化）测试。

钉住四件事：
  1. `_ask_one` 能从 SSE 的 `done` 载荷解析 `path_stats` / `graph_degraded_reason`
     （chat.py 追加的两个键）；
  2. 「检索之后」的 `refused` 载荷同样能解析；检索**之前**的 `refused`（无这两个键）
     按空值处理，不报错；
  3. `_accumulate_graph_observation` 按**轮**累加（拒答轮也计入 rounds），
     缺字段按 0 计 —— 观测字段缺失不该让整轮评测失败；
  4. 跨链路口径禁令（`CROSS_PATH_NOTE`）是**单一常量**，门禁报告两条 note
     都引用它 —— 三份措辞一旦漂移，就等于两份报告引用不同规则。
"""
from __future__ import annotations

import json
from collections import Counter

import httpx
import pytest

from app.services.eval_service import CROSS_PATH_NOTE, _NOTE_CHUNKS_ONLY, _NOTE_WITH_RECALL
from scripts import run_eval as re_mod


def _sse_body(events: list[tuple[str, dict]]) -> bytes:
    parts = []
    for event, payload in events:
        parts.append(
            f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        )
    return "".join(parts).encode("utf-8")


async def _ask(events: list[tuple[str, dict]]) -> dict:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=_sse_body(events),
            headers={"content-type": "text/event-stream"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await re_mod._ask_one(client, "http://test", "问题", ["kb"])


# ── _ask_one：SSE 解析 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_done_path_stats_parsed():
    result = await _ask([
        ("meta", {"stage": "retrieving", "modality": "text"}),
        ("citations", {"citations": [{"doc_id": "d1"}]}),
        ("delta", {"text": "答案"}),
        ("done", {
            "finish_reason": "stopped",
            "grounding": {"stripped_sentences": 0},
            "usage": {},
            "suggestions": [],
            "path_stats": {"graph_covered": 2, "graph_unique": 1, "vector_covered": 2},
            "graph_degraded_reason": "",
        }),
    ])
    assert result["refused"] is False
    assert result["text"] == "答案"
    assert result["citations"] == [{"doc_id": "d1"}]
    assert result["path_stats"]["graph_covered"] == 2
    assert result["graph_degraded_reason"] == ""


@pytest.mark.asyncio
async def test_refused_after_retrieval_parsed():
    result = await _ask([
        ("meta", {"stage": "retrieving", "modality": "text"}),
        ("refused", {
            "reason": "NO_RELEVANT_CONTENT",
            "message": "未找到相关内容",
            "path_stats": {"graph_covered": 0, "graph_unique": 3},
            "graph_degraded_reason": "no_neighbors",
        }),
    ])
    assert result["refused"] is True
    assert result["refuse_reason"] == "NO_RELEVANT_CONTENT"
    assert result["path_stats"] == {"graph_covered": 0, "graph_unique": 3}
    assert result["graph_degraded_reason"] == "no_neighbors"


@pytest.mark.asyncio
async def test_refused_before_retrieval_defaults_empty():
    """检索之前就拒答（敏感词 / 澄清等）：载荷没有观测键 → 按空值处理，不抛。"""
    result = await _ask([
        ("meta", {"stage": "retrieving", "modality": "text"}),
        ("refused", {"reason": "SENSITIVE_INPUT", "message": "……"}),
    ])
    assert result["refused"] is True
    assert result["path_stats"] == {}
    assert result["graph_degraded_reason"] == ""


@pytest.mark.asyncio
async def test_delta_and_citations_regression():
    """回归：delta 累加与 citations 提取与旧版行为一致（追加字段不改变既有解析）。"""
    result = await _ask([
        ("citations", {"citations": [{"doc_id": "a"}, {"doc_id": "b"}]}),
        ("delta", {"text": "第一"}),
        ("delta", {"text": "段"}),
        ("done", {"finish_reason": "stopped", "grounding": {}, "usage": {},
                  "suggestions": []}),
    ])
    assert result["text"] == "第一段"
    assert len(result["citations"]) == 2


# ── _accumulate_graph_observation：按轮聚合 ─────────────────────


def test_accumulate_sums_covered_unique_and_counts_reasons():
    acc = {"covered": 0, "unique": 0, "degraded": Counter(), "rounds": 0}
    re_mod._accumulate_graph_observation(acc, {
        "path_stats": {"graph_covered": 2, "graph_unique": 1},
        "graph_degraded_reason": "",
    })
    re_mod._accumulate_graph_observation(acc, {
        "path_stats": {"graph_covered": 3, "graph_unique": 0},
        "graph_degraded_reason": "timeout",
    })
    re_mod._accumulate_graph_observation(acc, {  # 拒答轮也计（检索已完成）
        "path_stats": {"graph_covered": 0, "graph_unique": 2},
        "graph_degraded_reason": "timeout",
    })
    assert acc["covered"] == 5
    assert acc["unique"] == 3
    assert acc["degraded"] == {"timeout": 2}
    assert acc["rounds"] == 3


def test_accumulate_tolerates_missing_fields():
    """观测字段缺失（老版本服务端 / 检索前拒答）按 0 计，不抛。"""
    acc = {"covered": 0, "unique": 0, "degraded": Counter(), "rounds": 0}
    re_mod._accumulate_graph_observation(acc, {})
    re_mod._accumulate_graph_observation(acc, {"path_stats": None})
    assert acc == {"covered": 0, "unique": 0, "degraded": {}, "rounds": 2}


def test_accumulate_counts_nonzero_reason_only():
    """graph_degraded_reason 为空串（正常）不进 Counter —— 空串不是降级。"""
    acc = {"covered": 0, "unique": 0, "degraded": Counter(), "rounds": 0}
    re_mod._accumulate_graph_observation(acc, {
        "path_stats": {}, "graph_degraded_reason": "",
    })
    assert acc["degraded"] == {}


# ── 跨链路口径禁令：单一常量 ─────────────────────────────────────


def test_cross_path_note_shared_by_both_reports():
    """门禁报告的两条 note 都引用同一份 CROSS_PATH_NOTE —— 措辞不许各写各的。"""
    assert CROSS_PATH_NOTE in _NOTE_CHUNKS_ONLY
    assert CROSS_PATH_NOTE in _NOTE_WITH_RECALL
    # CLI 侧 import 的是同一份文本（不是复制品）
    assert re_mod.CROSS_PATH_NOTE is CROSS_PATH_NOTE
    assert "禁止横向比较" in CROSS_PATH_NOTE
