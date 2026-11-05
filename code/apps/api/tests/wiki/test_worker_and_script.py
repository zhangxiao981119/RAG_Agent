"""关闭态等价性测试（设计文档 §3.4 / §9.6 ①）+ 幂等计数语义。

钉住三件事：
  1. run_wiki_build 在 WIKI_ENABLED=false 时返回 "disabled" 且**一行代码不执行**
     （连参数解析都不做 —— 传非法 document_id 也不允许抛错，一旦走到
     uuid.UUID(document_id) 就会抛 ValueError，用这个性质证明"一行没执行"）；
  2. scripts/rebuild_wiki.py 在 WIKI_ENABLED=false 时打印 FAIL 到 stderr 并以
     退出码 1 结束 —— 关闭态下生成的页没有任何读路径，白烧 LLM 是假工作；
  3. 幂等命中（hash 相同跳过）返回可区分的 "skipped_idempotent"，rebuild_wiki
     汇总计入「跳过」而非「成功」—— 运维看汇总就知道哪些文档没烧 LLM。

所有用例都不需要连库（也不起 TestClient），任何环境都能跑。
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.workers.graph_job as graph_job
from app.services.wiki import build as build_module

API_ROOT = Path(__file__).resolve().parents[2]


def _run_script(mode_args: list[str]) -> subprocess.CompletedProcess[str]:
    # scripts/ 直接跑时 sys.path[0] 是 scripts/，注入 API_ROOT 让 `import app` 可用
    env = {**os.environ, "PYTHONPATH": str(API_ROOT)}
    return subprocess.run(
        [sys.executable, str(API_ROOT / "scripts" / "rebuild_wiki.py"), *mode_args],
        capture_output=True,
        text=True,
        cwd=API_ROOT,
        env=env,
        timeout=120,
    )


def test_run_wiki_build_disabled_executes_nothing(monkeypatch):
    """WIKI_ENABLED=false → 返回 "disabled"；document_id 连 UUID 都不是也不许抛
    （抛 ValueError = 代码走到了门卫之后，违反"一行不执行"契约）。"""
    monkeypatch.setattr(
        graph_job, "get_settings", lambda: SimpleNamespace(wiki_enabled=False)
    )

    import asyncio

    result = asyncio.run(
        graph_job.run_wiki_build(ctx={}, document_id="not-even-a-uuid")
    )
    assert result == "disabled"


def test_rebuild_wiki_script_fails_closed_when_disabled():
    """WIKI_ENABLED=false（默认）→ 脚本退出码 1，stderr 说明原因。"""
    proc = _run_script(["--all"])
    assert proc.returncode == 1
    assert "WIKI_ENABLED=false" in proc.stderr
    assert "FAIL" in proc.stderr


@pytest.mark.parametrize("mode_args", [["--all"], ["--recaliber"], ["--stale-only"]])
def test_rebuild_wiki_script_all_modes_fail_closed_when_disabled(mode_args):
    """三种模式在关闭态一律 fail-closed，不存在绕过开关的模式。"""
    proc = _run_script(mode_args)
    assert proc.returncode == 1, f"{mode_args} 在关闭态不应继续执行"


# ── 幂等命中的计数语义（遗留项 1 收尾）─────────────────────────


class _CM:
    """把假 session 包成 async context manager（替代 SessionLocal）。"""

    def __init__(self, session) -> None:
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


class _ExecResult:
    def __init__(self, first=None, rows=None) -> None:
        self._first = first
        self._rows = list(rows or [])

    def first(self):
        return self._first

    def all(self):
        return self._rows


class _DbSession:
    """get(Document) → 对象；execute 按「是否查 wiki_pages」分流。"""

    def __init__(self, page_row) -> None:
        self._page_row = page_row

    async def get(self, model, pk):
        return SimpleNamespace(id=pk)

    async def execute(self, stmt, params=None):
        if "wiki_pages" in str(stmt):
            return _ExecResult(first=self._page_row)
        return _ExecResult(rows=[SimpleNamespace(id=uuid.UUID(int=n),
                                                 heading_path=f"第{n}章",
                                                 content="正文" * 20)
                                 for n in range(3)])

    def add(self, obj):
        pass

    async def commit(self):
        pass

    async def flush(self):
        pass


def _stub_wiki_env(monkeypatch):
    """给 graph_job / build 打桩：连库点全换掉，走真实的 build_and_store。"""
    import contextlib

    def _session_factory(page_row_holder):
        @contextlib.asynccontextmanager
        async def _cm():
            yield _DbSession(page_row_holder[0])

        return _cm

    monkeypatch.setattr(
        build_module, "current_wiki_profile",
        lambda: SimpleNamespace(generator_model="gen", prompt_version="wprompt-v1"),
    )
    monkeypatch.setattr(
        build_module, "get_settings",
        lambda: SimpleNamespace(wiki_input_max_chars=10**9),
    )
    monkeypatch.setattr(
        graph_job, "get_settings",
        lambda: SimpleNamespace(wiki_enabled=True, arq_max_attempts=3),
    )
    return _session_factory


def test_build_and_store_idempotent_hit_returns_distinguishable_value(monkeypatch):
    """已有 ready 页 + 同 hash 重跑 → build_and_store 返回可区分的
    "skipped_idempotent"（不是 "succeeded"）：脚本汇总才能把它计入「跳过」，
    运维看「成功 N / 跳过 M」就知道哪些文档没烧 LLM。"""
    _factory = _stub_wiki_env(monkeypatch)

    doc = uuid.uuid4()
    # 第一遍（无已有页）拿真实算出的 content_hash
    holder = [None]
    monkeypatch.setattr(graph_job, "SessionLocal", _factory(holder))
    prepared = asyncio.run(
        build_module.prepare_wiki_input(_DbSession(None), document_id=doc)
    )
    wiki_input = prepared[0]  # prepared = (WikiInput, idempotent)
    # 第二遍：库里有 ready + 同 hash 的页 → 幂等命中
    ready_row = SimpleNamespace(status="ready", generator_model="gen",
                                prompt_version="wprompt-v1",
                                content_hash=wiki_input.content_hash)
    holder[0] = ready_row
    result = asyncio.run(graph_job.build_and_store(doc))
    assert result == "skipped_idempotent"


def test_build_and_store_real_build_still_reports_succeeded(monkeypatch):
    """真实生成路径（无已有页 → 生成打桩成功 → 落库打桩）仍返回 "succeeded"
    —— 计数语义改动不许波及正常成功路径。"""
    _factory = _stub_wiki_env(monkeypatch)
    holder = [None]
    monkeypatch.setattr(graph_job, "SessionLocal", _factory(holder))

    async def _fake_write(session, *, document, wiki_input, generated):
        return None

    # build_and_store 内延迟 import write_wiki_page —— 打在 build 模块上
    monkeypatch.setattr(build_module, "write_wiki_page", _fake_write)

    async def _fake_generate(wiki_input):
        return SimpleNamespace(
            title="t", summary="s", sections=[], key_terms=[],
            source_chunk_ids=[], llm_calls=1,
        )

    monkeypatch.setattr(build_module, "generate_page_content", _fake_generate)

    doc = uuid.uuid4()
    result = asyncio.run(graph_job.build_and_store(doc))
    assert result == "succeeded"


def test_rebuild_wiki_script_counts_idempotent_as_skipped(monkeypatch, capsys):
    """脚本汇总：幂等命中 → 「跳过 1」而非「成功 1」。"""
    spec = importlib.util.spec_from_file_location(
        "rebuild_wiki_script", API_ROOT / "scripts" / "rebuild_wiki.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    doc_id = uuid.uuid4()

    class _ScriptSession:
        async def execute(self, stmt, params=None):
            return _ExecResult(rows=[(doc_id, "某文档.pdf")])

    monkeypatch.setattr(mod, "SessionLocal",
                        lambda: _CM(_ScriptSession()))

    async def _fake_build(document_id):
        return "skipped_idempotent"

    monkeypatch.setattr(mod, "build_and_store", _fake_build)

    args = SimpleNamespace(recaliber=False, stale_only=False, document=None,
                           kb_id=None, limit=None, dry_run=False, show=20)
    exit_code = asyncio.run(mod._run(args))
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "完成：成功 0 / 跳过 1 / 失败 0" in out
    assert "未调 LLM" in out


def test_rebuild_wiki_script_counts_succeeded_as_ok(monkeypatch, capsys):
    """对照组：真实生成（"succeeded"）仍计「成功」——改动不许波及正常计数。"""
    spec = importlib.util.spec_from_file_location(
        "rebuild_wiki_script2", API_ROOT / "scripts" / "rebuild_wiki.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    doc_id = uuid.uuid4()

    class _ScriptSession:
        async def execute(self, stmt, params=None):
            return _ExecResult(rows=[(doc_id, "某文档.pdf")])

    monkeypatch.setattr(mod, "SessionLocal", lambda: _CM(_ScriptSession()))

    async def _fake_build(document_id):
        return "succeeded"

    monkeypatch.setattr(mod, "build_and_store", _fake_build)

    args = SimpleNamespace(recaliber=False, stale_only=False, document=None,
                           kb_id=None, limit=None, dry_run=False, show=20)
    exit_code = asyncio.run(mod._run(args))
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "完成：成功 1 / 跳过 0 / 失败 0" in out
