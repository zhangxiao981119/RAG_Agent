"""抽取期间不得持有数据库事务（真实故障：idle in transaction 11 分钟）。

背景：`extract_document()` 先读 document + chunks，然后**在同一个会话/事务里**
进入 LLM 循环。抽取跑多久，事务就挂着多久 —— 实测一次冻结里那条会话
`idle in transaction` 挂了 11 分钟，`query_start` 停在 `SELECT chunks ...`，
把连接池与 VACUUM 一起拖住，而 who-is-blocking 完全指不到 LLM 这条真凶。

本文件钉住顺序：**先 rollback，再发第一次 LLM 请求**。顺序错了就等于没改。
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

import app.services.graph.extract as extract_mod
from app.services.graph.extract import _ChunkRow, extract_document


class _OrderRecordingSession:
    """最小 session 替身：只记录 rollback 的时序，不做任何 IO。"""

    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def get(self, model, pk):  # noqa: ANN001 - 只要非 None 即可
        return SimpleNamespace(id=pk)

    async def rollback(self) -> None:
        self.events.append("rollback")


class _OrderRecordingLLM:
    def __init__(self, events: list[str], payload: dict) -> None:
        self.events = events
        self.payload = payload

    async def chat(self, messages, temperature, **kwargs) -> str:
        self.events.append("llm.chat")
        return json.dumps(self.payload, ensure_ascii=False)


@pytest.mark.asyncio
async def test_transaction_released_before_first_llm_call(monkeypatch):
    events: list[str] = []
    chunk = _ChunkRow(id=uuid.uuid4(), heading_path="第一章", content="财务部负责预算审批。")

    async def _fake_load_chunks(session, document_id):  # noqa: ANN001
        events.append("load_chunks")
        return [chunk]

    payload = {
        "entities": [
            {"name": "财务部", "type": "ORG", "aliases": []},
            {"name": "预算", "type": "TERM", "aliases": []},
        ],
        "mentions": [
            {
                "name": "财务部",
                "chunk_id": str(chunk.id),
                "surface": "财务部",
                "confidence": 0.9,
            }
        ],
        "relations": [{"src": "财务部", "dst": "预算", "relation": "负责"}],
    }
    monkeypatch.setattr(extract_mod, "_load_chunks", _fake_load_chunks)
    monkeypatch.setattr(
        extract_mod, "get_llm_service", lambda: _OrderRecordingLLM(events, payload)
    )

    session = _OrderRecordingSession(events)
    result = await extract_document(session, document_id=uuid.uuid4())

    # 抽取本身照常成功（rollback 不该影响后续读取与产物）
    assert result.chunks_processed == 1
    assert [entity.name_key for entity in result.entities] == ["财务部", "预算"]
    assert len(result.mentions) == 1
    assert len(result.relations) == 1

    # ★ 核心断言：rollback 必须在第一次 LLM 调用之前，且只发生一次
    assert events.count("rollback") == 1
    assert events.index("rollback") < events.index("llm.chat")
    # 顺序细节：先读 chunk → 再释放事务 → 再打 LLM
    assert events.index("load_chunks") < events.index("rollback")
