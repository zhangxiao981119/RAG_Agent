"""补建脚本对「跳过」与「失败」的区分（回归测试）。

背景（独立验证时实测暴露）：
    `GraphExtractSkip` 原本被脚本里的 `except Exception` 兜住 → 落成 `failed`。
    后果两个：
      1. 零分片文档跑完是 failed 而不是 skipped，`--check` 报「失败 1 / 跳过 0」；
      2. `--all` 的选取条件只排除 skipped → 这篇文档**每轮都被重选重跑**，永远修不好。
    归因很清楚：「本来就不该抽」被记成了「这次没抽成」。

★ 本文件用假 session 覆盖状态机（无需数据库），并静态校验 `--all` 的选取谓词确实
  排除 skipped。真实库上的端到端（第二轮是否真的不再选中）仍属「未验证」，
  已在回传中标注。
"""
from __future__ import annotations

import argparse
import uuid

import pytest

from app.services.graph.extract import GraphExtractSkip
from scripts import backfill_graph as bg


class _FakeDocument:
    def __init__(self, status: str = "pending") -> None:
        self.graph_status = status


class _FakeSession:
    """只实现脚本用到的三个方法：execute / get / commit。"""

    def __init__(self, documents: dict, rows: list[tuple]) -> None:
        self._documents = documents
        self._rows = rows

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    async def execute(self, _stmt, _params=None):
        rows = self._rows

        class _Result:
            def all(self_inner):
                return rows

        return _Result()

    async def get(self, _model, pk):
        return self._documents.get(pk)

    async def commit(self) -> None:
        return None


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("GRAPH_ENABLED", "true")
    from app.config.settings import get_settings

    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()
    monkeypatch.undo()


def _args() -> argparse.Namespace:
    return argparse.Namespace(
        document=None, kb_id=None, limit=0, dry_run=False, show=20
    )


def _install(monkeypatch, documents: dict, rows: list[tuple]) -> None:
    monkeypatch.setattr(bg, "SessionLocal", lambda: _FakeSession(documents, rows))


@pytest.mark.asyncio
async def test_skip_is_recorded_as_skipped_not_failed(env, monkeypatch, capsys):
    doc_id = uuid.uuid4()
    documents = {doc_id: _FakeDocument("pending")}
    _install(monkeypatch, documents, [(doc_id, "empty.pdf")])

    async def _raise_skip(_document_id):
        raise GraphExtractSkip("文档无已索引分片（内容为空或尚未解析）")

    monkeypatch.setattr(bg, "extract_and_store", _raise_skip)

    exit_code = await bg._run("all", _args())

    assert exit_code == 0, "跳过不是失败：整轮退出码必须为 0"
    assert documents[doc_id].graph_status == "skipped", (
        "零分片文档 MUST 落 skipped —— 落 failed 会被 --all 每轮重选、永远修不好"
    )
    output = capsys.readouterr().out
    assert "跳过 1 · 失败 0" in output
    assert "FAILED" not in output


@pytest.mark.asyncio
async def test_real_failure_still_recorded_as_failed(env, monkeypatch, capsys):
    """真正的异常（LLM/写库）仍 MUST 落 failed 并让退出码非 0。"""
    doc_id = uuid.uuid4()
    documents = {doc_id: _FakeDocument("pending")}
    _install(monkeypatch, documents, [(doc_id, "broken.pdf")])

    async def _raise_error(_document_id):
        raise RuntimeError("LLM 输出非法 JSON")

    monkeypatch.setattr(bg, "extract_and_store", _raise_error)

    exit_code = await bg._run("all", _args())

    assert exit_code == 1
    assert documents[doc_id].graph_status == "failed"
    assert "跳过 0 · 失败 1" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_success_is_recorded_as_done(env, monkeypatch, capsys):
    """成功路径不受影响（extract_and_store 自己置 done，脚本只计数）。"""
    doc_id = uuid.uuid4()
    documents = {doc_id: _FakeDocument("pending")}
    _install(monkeypatch, documents, [(doc_id, "ok.pdf")])

    async def _ok(_document_id):
        return "succeeded"

    monkeypatch.setattr(bg, "extract_and_store", _ok)

    exit_code = await bg._run("all", _args())

    assert exit_code == 0
    assert "成功 1 · 跳过 0 · 失败 0" in capsys.readouterr().out


def test_all_mode_excludes_skipped_documents():
    """`--all` 的选取谓词 MUST 排除 skipped —— 否则被跳过的文档每轮都被重跑。"""
    compiled = bg._base_select("all").compile()
    sql = str(compiled)
    assert "graph_status !=" in sql
    assert "skipped" in compiled.params.values(), (
        "选取条件里找不到 'skipped' —— 被跳过的文档会在每轮 --all 里重新被选中"
    )


def test_recaliber_mode_only_targets_done_with_stale_caliber():
    """`--recaliber` 只挑 done 且口径不匹配的；不得把 pending 也卷进来。"""
    compiled = bg._base_select("recaliber").compile()
    sql = str(compiled)
    assert "graph_status = " in sql  # done 过滤
    assert "graph_extractor_model" in sql and "graph_prompt_version" in sql


def test_pending_mode_ignores_skipped_and_failed():
    """`--pending` 只扫 pending/running（崩溃残留），不重跑 failed/skipped。"""
    sql = str(bg._base_select("pending").compile())
    assert "graph_status IN" in sql
    assert "skipped" not in sql
    assert "failed" not in sql
