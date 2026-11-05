"""关闭态等价性测试（设计文档 §3.4 / §9.6 ①）。

钉住两件事：
  1. run_wiki_build 在 WIKI_ENABLED=false 时返回 "disabled" 且**一行代码不执行**
     （连参数解析都不做 —— 传非法 document_id 也不允许抛错，一旦走到
     uuid.UUID(document_id) 就会抛 ValueError，用这个性质证明"一行没执行"）；
  2. scripts/rebuild_wiki.py 在 WIKI_ENABLED=false 时打印 FAIL 到 stderr 并以
     退出码 1 结束 —— 关闭态下生成的页没有任何读路径，白烧 LLM 是假工作。

两个用例都不需要连库（也不起 TestClient），任何环境都能跑。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.workers.graph_job as graph_job

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
