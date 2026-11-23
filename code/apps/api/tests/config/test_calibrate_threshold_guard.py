"""修 6 回归：标定脚本的口径硬闸 —— 口径对不上就 MUST 不输出建议值。

缺陷现场（`scripts/calibrate_threshold.py::main`）：
    `mode = dominant if args.mode == "auto" else args.mode` —— 之后既没有校验
    `args.mode` 与 `dominant` 是否一致，也没有校验 `dominant` 本身是否可信，
    于是 `--mode vector` 却采到重排分时，脚本只多打一行
    「主导口径: rerank | 本次标定口径: vector」，然后**照常给出**
    `decisions.RELEVANCE_THRESHOLD_VECTOR` 的建议值。

为什么这比"报错退出"危险得多：
  · 建议值是要被手抄进 `decisions.py` 的，中间没有第二个人复核；
  · 错口径得到的数字与正确的那份**长得完全一样**（同为 0.00–0.90 的阈值 +
    漂亮的拒答正确率/F1），事后从数字本身分辨不出来；
  · 后果是全量请求的拒答率 / 漏答率整体偏移，而日志里只有一行容易被滑过的
    「主导口径 / 本次标定口径」。
故：口径不可信 ⇒ 非零退出（2）、不输出报告、只说明原因。

本文件用假 `_collect` 替换采集层：脚本的价值在**判定与出口**，不在跑库。
（真实采集需要 PG + 检索服务，属标定手册的手工流程，不在单测里跑。）
"""
from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys

import pytest

_SCRIPT_PATH = (
    pathlib.Path(__file__).resolve().parents[2] / "scripts" / "calibrate_threshold.py"
)


def _load_script():
    """按文件路径导入脚本（`scripts/` 不是包，不能按模块名 import）。"""
    spec = importlib.util.spec_from_file_location("calibrate_threshold_under_test", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script():
    return _load_script()


def _sample_rows(sources: list[str]) -> list[tuple[str, bool, float, str]]:
    """构造采集结果：可答 3 条 / 不可答 2 条，分离度 0.2（阈值扫描有唯一最优）。

    两组都必须非空：`_sweep` 在缺组时返回空表，最优阈值会退化成 (0.0, -1.0)，
    那样"是否输出了建议值"的断言就失去意义。
    """
    answerable = [("q1", True, 0.80, "vector"), ("q2", True, 0.70, "vector"),
                  ("q3", True, 0.60, "vector")]
    unanswerable = [("q4", False, 0.20, "vector"), ("q5", False, 0.10, "vector")]
    rows = answerable + unanswerable
    return [(q, a, s, sources[i % len(sources)]) for i, (q, a, s, _) in enumerate(rows)]


def _run_capture(script, monkeypatch, capsys, mode: str, rows, dominant: str):
    async def fake_collect():
        return rows, dominant

    monkeypatch.setattr(script, "_collect", fake_collect)
    monkeypatch.setattr(sys, "argv", ["calibrate_threshold.py", "--mode", mode])
    rc = asyncio.run(script.main())
    out, err = capsys.readouterr()
    return rc, out, err


# ── 硬闸：拒绝的三种情形 ────────────────────────────────────────────────


def test_mode_mismatch_refuses_to_emit_recommendation(script, monkeypatch, capsys):
    """`--mode vector` 采到 `rerank` 口径 ⇒ 退出码 2，且**一个字建议值都不给**。"""
    rc, out, err = _run_capture(
        script, monkeypatch, capsys,
        mode="vector", rows=_sample_rows(["rerank"]), dominant="rerank",
    )
    assert rc == 2, "口径不符时必须非零退出（1 留给「采不到样本」）"
    assert "建议值" not in out
    # 整份报告都不打：错口径下的分数明细同样会被误读
    assert "阈值扫描" not in out
    assert "=== 样本 ===" not in out
    # 原因说清楚到能照着改：指出实际口径、指出会被写错的常量
    assert "vector" in err and "rerank" in err
    assert "RELEVANCE_THRESHOLD_VECTOR" in err


def test_mixed_caliber_refuses_even_with_auto(script, monkeypatch, capsys):
    """采集期间混口径 ⇒ 样本本身无效，`--mode auto` 也 MUST 拒绝。

    此时 `dominant` 只是"较多的那个"，把它当口径等于让抖动决定阈值。
    """
    rows = _sample_rows(["vector", "rerank"])
    rc, out, err = _run_capture(
        script, monkeypatch, capsys, mode="auto", rows=rows, dominant="vector"
    )
    assert rc == 2
    assert "建议值" not in out
    assert "混口径" in err
    assert "rerank" in err and "vector" in err


def test_unknown_dominant_refuses(script, monkeypatch, capsys):
    """拿不到口径（`score_source` 未填充）⇒ 写任何常量都是瞎猜，MUST 拒绝。"""
    rc, out, err = _run_capture(
        script, monkeypatch, capsys,
        mode="auto", rows=_sample_rows(["unknown"]), dominant="unknown",
    )
    assert rc == 2
    assert "建议值" not in out
    assert "score_source" in err


# ── 正向对照：口径对得上时照常给建议值（闸门不能把工具焊死）────────────


def test_matching_forced_mode_emits_recommendation(script, monkeypatch, capsys):
    """`--mode rerank` 且采到 `rerank` ⇒ 退出码 0，建议值指向重排常量。"""
    rc, out, err = _run_capture(
        script, monkeypatch, capsys,
        mode="rerank", rows=_sample_rows(["rerank"]), dominant="rerank",
    )
    assert rc == 0, err
    assert "建议值" in out
    assert "decisions.RELEVANCE_THRESHOLD_RERANK" in out
    assert "RELEVANCE_THRESHOLD_VECTOR" not in out


def test_auto_mode_follows_dominant(script, monkeypatch, capsys):
    """`--mode auto` ⇒ 跟随实际口径，建议值指向对应常量。"""
    rc, out, err = _run_capture(
        script, monkeypatch, capsys,
        mode="auto", rows=_sample_rows(["vector"]), dominant="vector",
    )
    assert rc == 0, err
    assert "建议值" in out
    assert "decisions.RELEVANCE_THRESHOLD_VECTOR" in out
    assert "RELEVANCE_THRESHOLD_RERANK" not in out


def test_empty_collection_exits_1_without_recommendation(script, monkeypatch, capsys):
    """采不到任何样本 ⇒ 退出码 1（与"口径不可信"的 2 区分开），也不给建议值。"""
    rc, out, err = _run_capture(
        script, monkeypatch, capsys, mode="auto", rows=[], dominant="vector"
    )
    assert rc == 1
    assert "建议值" not in out
