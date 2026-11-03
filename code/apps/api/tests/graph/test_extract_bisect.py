"""抽取输出被 max_tokens 截断时的二分重试与错误语义（不连库、不调真 LLM）。

背景（真实故障）：22 篇文档里 10 篇 `graph_status=failed`，报错是
「LLM 输出非法 JSON」。实际输出是**合法 JSON 的前缀**，在
`{"name": "Webpack", "` 中途断掉 —— 是 4 分片/请求的批量输出把
max_tokens 用光了，`json.loads` 失败后又被当成「模型乱写」。

本文件钉住三件事：
  1. 批内解析失败 → **二分拆批**再要一次，合并两侧结果（不丢边）；
  2. 单分片仍失败且输出长度撞上限 → GraphExtractTruncated，message 直接指向
     该调的环境变量（而不是让人去改 prompt）；
  3. 单分片输出短且无 JSON → 仍是 GraphExtractError「非法 JSON」，
     MUST NOT 被误报成「截断」—— 两者修法完全不同，混报会让排查方向全错。
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

import app.services.graph.extract as extract_mod
from app.services.graph.extract import (
    GraphExtractError,
    GraphExtractTruncated,
    _ChunkRow,
    _extract_batch,
)

# 真实故障里的形态：合法 JSON 的前缀，在字符串中途断掉（无闭合外层 `{`）
_TRUNCATED = (
    '{\n"entities": [\n'
    '{"name": "前端性能优化终极手册", "type": "TERM", "aliases": []},\n'
    '{"name": "React", "type": "PRODUCT", "aliases": []},\n'
    '{"name": "Webpack", "'
)


def _chunks(count: int) -> list[_ChunkRow]:
    return [
        _ChunkRow(
            id=uuid.UUID(int=index + 1),
            heading_path=f"章节{index}",
            content=f"第 {index} 片的正文",
        )
        for index in range(count)
    ]


class _FakeLLM:
    """替身 LLMService：按 prompt 里的分片数决定返回什么，并记录每次调用参数。"""

    def __init__(self, responder) -> None:
        self._responder = responder
        self.calls: list[dict] = []

    async def chat(
        self,
        messages,
        temperature,
        *,
        model=None,
        timeout_seconds=None,
        max_tokens=None,
    ) -> str:
        prompt = messages[0]["content"]
        self.calls.append(
            {
                "prompt": prompt,
                "model": model,
                "timeout_seconds": timeout_seconds,
                "max_tokens": max_tokens,
            }
        )
        return self._responder(prompt)


def _install(monkeypatch, responder) -> _FakeLLM:
    fake = _FakeLLM(responder)
    monkeypatch.setattr(extract_mod, "get_llm_service", lambda: fake)
    return fake


def _present_ids(prompt: str, chunks: list[_ChunkRow]) -> list[str]:
    """本次 prompt 里到底塞了几个分片（= 二分走到了哪一层）。"""
    ids = [str(chunk.id) for chunk in chunks]
    return [cid for cid in ids if cid in prompt]


def _one_chunk_json(chunk_id: str) -> str:
    name = f"实体{chunk_id[:8]}"
    return json.dumps(
        {
            "entities": [{"name": name, "type": "TERM", "aliases": []}],
            "mentions": [
                {"name": name, "chunk_id": chunk_id, "surface": name, "confidence": 0.9}
            ],
            "relations": [],
        },
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_multi_chunk_failure_bisects_and_keeps_both_sides(monkeypatch):
    """4 分片批解析失败 → 二分到单分片成功，左右两侧实体都进合并结果。"""
    chunks = _chunks(4)

    def responder(prompt: str) -> str:
        present = _present_ids(prompt, chunks)
        if len(present) > 1:
            return _TRUNCATED  # 多分片 → 输出撞上限，返回的必然是前缀
        return _one_chunk_json(present[0])

    fake = _install(monkeypatch, responder)

    result = await _extract_batch(
        chunks, model="qwen-test", timeout_seconds=30.0, max_tokens=8_000
    )

    names = {entity["name"] for entity in result["entities"]}
    head = f"实体{str(chunks[0].id)[:8]}"
    tail = f"实体{str(chunks[-1].id)[:8]}"
    assert head in names and tail in names  # 二分左右两侧都被合并进来
    assert names == {f"实体{str(chunk.id)[:8]}" for chunk in chunks}
    assert len(result["mentions"]) == 4
    assert result["relations"] == []
    # 二分确实发生了：4 分片批（首次+重试=2）→ 2 个 2 分片批（各 2）→ 4 个单分片（各 1）
    assert len(fake.calls) == 2 + 2 * 2 + 4
    assert fake.calls[-1]["max_tokens"] == 8_000  # 递归调用沿用同一上限


@pytest.mark.asyncio
async def test_single_chunk_truncation_raises_truncated_with_actionable_hint(
    monkeypatch,
):
    """单分片输出撞上限 → GraphExtractTruncated，message 指向该调的环境变量。"""
    chunks = _chunks(1)
    _install(monkeypatch, lambda p: "{" + "甲" * 300)

    with pytest.raises(GraphExtractTruncated) as excinfo:
        await _extract_batch(
            chunks, model="qwen-test", timeout_seconds=30.0, max_tokens=100
        )

    message = str(excinfo.value)
    assert "截断" in message
    assert "GRAPH_EXTRACT_MAX_OUTPUT_TOKENS" in message
    # 继承关系：调用方的 `except GraphExtractError` 仍能兜住（job 层无需改动）
    assert isinstance(excinfo.value, GraphExtractError)


@pytest.mark.asyncio
async def test_single_chunk_garbage_stays_plain_invalid_json(monkeypatch):
    """单分片输出短且无 JSON → 仍报「非法 JSON」，MUST NOT 报成截断。"""
    chunks = _chunks(1)
    _install(monkeypatch, lambda p: "抱歉，我无法完成这个抽取任务。")

    with pytest.raises(GraphExtractError) as excinfo:
        await _extract_batch(
            chunks, model="qwen-test", timeout_seconds=30.0, max_tokens=8_000
        )

    message = str(excinfo.value)
    assert "非法 JSON" in message
    assert "截断" not in message
    assert not isinstance(excinfo.value, GraphExtractTruncated)


@pytest.mark.asyncio
async def test_max_tokens_is_forwarded_to_llm_call(monkeypatch):
    """调用方传的上限 MUST 一路传到 llm.chat，否则「按调用覆盖」等于没改。"""
    chunks = _chunks(1)
    fake = _install(
        monkeypatch,
        lambda p: '{"entities": [], "mentions": [], "relations": []}',
    )

    await _extract_batch(
        chunks, model="qwen-test", timeout_seconds=12.5, max_tokens=1_234
    )

    assert fake.calls[0]["max_tokens"] == 1_234
    assert fake.calls[0]["timeout_seconds"] == 12.5
    assert fake.calls[0]["model"] == "qwen-test"


def test_extract_document_uses_configured_upper_bound():
    """extract_document MUST 用配置项，而不是被锁在 4K 的全局 llm_max_output_tokens。"""
    from app.config.settings import get_settings

    assert get_settings().graph_extract_max_output_tokens >= 8_000
    source = (
        Path(__file__).resolve().parents[2]
        / "app"
        / "services"
        / "graph"
        / "extract.py"
    ).read_text(encoding="utf-8")
    assert "max_tokens=settings.graph_extract_max_output_tokens" in source
