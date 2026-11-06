"""第 4 步 · 段 C 观测代码的可跑单测（无 DB，替身 / 纯函数）。

钉住设计 §2.6 ② / ②-b / §4 段 C 的四件事：
  1. 假绿前置门槛是**三态**：不满足 ⇒ 「本轮评测结论无效」，既不是通过也不是失败；
     `WIKI_NAV_MIN_HIT_CASES=20` 是 N3 先验值，不许因某轮评测不达标临时调整
  2. 三个数的聚合口径：hit_cases / expanded_cases 只统计可答类（20 是相对 111 条
     可答类设的），expanded_terms 全量轮累加；多轮求和值用 `> 0` 判「至少一次」
  3. `_warn_caliber_drift` 的 `wiki_enabled` 漂移键真的会报警（F14）——否则
     开/关 wiki 两条基线会被直接互比，正是双口径禁令要防的
  4. 三个 `wiki_nav_*` 字段是**只记录**：`EvalReport` / `EvalReportResponse` 有默认值、
     累加不抛；`_compare_baseline()` 仍只比三项既有指标，wiki_* 不进方向表

★ 诚实边界：这些是观测代码的契约测试，**不能**替代 217 条真库 A/B；
  真库探针（ACL / EXPLAIN / P95 / 合成页清理）在 `test_nav_acl.py` 与验收报告里。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api.eval import EvalReportResponse
from app.services import eval_service
from app.services.eval_service import EvalReport, _accumulate_wiki_nav_stats


@pytest.fixture(scope="module")
def run_eval_mod():
    """`scripts/` 不是包（无 __init__.py），按文件路径离线加载。"""
    path = Path(__file__).resolve().parents[2] / "scripts" / "run_eval.py"
    spec = importlib.util.spec_from_file_location("run_eval_under_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _acc(run_eval_mod) -> dict:
    return {
        "hit_cases": 0,
        "expanded_cases": 0,
        "expanded_terms": 0,
        "degraded_reasons": run_eval_mod.Counter(),
        "rounds": 0,
        "answerable_rounds": 0,
    }


def _ask(hits: int = 0, terms: int = 0) -> dict:
    return {"path_stats": {"wiki_nav_hits": hits, "wiki_nav_terms": terms}}


# ── ① 门槛三态与先验值 ──────────────────────────────────────
def test_min_hit_cases_is_the_frozen_prior(run_eval_mod) -> None:
    """N3：20 是先验值，首轮实测后重设；禁止因某轮评测不达标临时调整。"""
    assert run_eval_mod.WIKI_NAV_MIN_HIT_CASES == 20


@pytest.mark.parametrize(
    ("hit_cases", "expanded_cases", "ok"),
    [
        (0, 0, False),       # 扩展一次都没触发 → 典型关闭态假绿
        (19, 1, False),      # 差一条也不算（门槛是 >= 20）
        (20, 0, False),      # 命中了页但一个词都没扩出来 → 仍是假绿
        (20, 1, True),       # 唯一有效态
        (21, 3, True),
    ],
)
def test_gate_ok_is_a_three_state_judgement(run_eval_mod, hit_cases, expanded_cases, ok) -> None:
    assert run_eval_mod._wiki_nav_gate_ok(hit_cases, expanded_cases) is ok


# ── ② 聚合口径 ─────────────────────────────────────────────
def test_accumulates_only_answerable_cases_into_hit_and_expanded(run_eval_mod) -> None:
    acc = _acc(run_eval_mod)
    mod = run_eval_mod
    mod._accumulate_wiki_nav_observation(acc, _ask(hits=1, terms=2), answerable=True)
    mod._accumulate_wiki_nav_observation(acc, _ask(hits=0, terms=0), answerable=True)
    # 不可答轮：expanded_terms 照样累计，但 hit/expanded_cases 不计
    mod._accumulate_wiki_nav_observation(acc, _ask(hits=1, terms=2), answerable=False)

    assert acc["hit_cases"] == 1
    assert acc["expanded_cases"] == 1
    assert acc["expanded_terms"] == 4
    assert acc["rounds"] == 3
    assert acc["answerable_rounds"] == 2


def test_multi_round_summed_counters_count_as_one_case(run_eval_mod) -> None:
    """§7-U-E：retrieve_chain 下两键是按轮求和值，>0 判「至少一次」而非 ==1。"""
    acc = _acc(run_eval_mod)
    run_eval_mod._accumulate_wiki_nav_observation(
        acc, _ask(hits=3, terms=5), answerable=True
    )
    assert acc["hit_cases"] == 1 and acc["expanded_cases"] == 1
    assert acc["expanded_terms"] == 5


@pytest.mark.parametrize("result", [{}, {"path_stats": None}, {"path_stats": {}}])
def test_missing_fields_count_as_zero_without_failing(run_eval_mod, result) -> None:
    """老版本服务端 / 检索前就拒答的轮没有观测字段 —— 不是正确性数据，不该让评测失败。"""
    acc = _acc(run_eval_mod)
    run_eval_mod._accumulate_wiki_nav_observation(acc, result, answerable=True)
    assert acc["hit_cases"] == 0 and acc["expanded_cases"] == 0
    assert acc["expanded_terms"] == 0 and acc["rounds"] == 1


@pytest.mark.parametrize(
    ("wiki_enabled", "hit_cases", "expanded_cases", "expected_verdict"),
    [
        (True, 0, 0, "本轮评测结论无效"),     # 扩展一次都没触发 = 关闭态假绿
        (True, 20, 1, "门槛已满足"),
        (False, 20, 1, "不覆盖"),            # 关 wiki 时三数照打，但明确不覆盖本步
    ],
)
def test_report_always_prints_the_three_numbers(
    run_eval_mod, capsys, wiki_enabled, hit_cases, expanded_cases, expected_verdict
) -> None:
    """§2.6 ②-b：报告 MUST 打印三个数，且门槛三态的措辞不许被悄悄改掉。"""
    acc = _acc(run_eval_mod)
    acc.update(
        hit_cases=hit_cases,
        expanded_cases=expanded_cases,
        expanded_terms=3,
        rounds=25,
        answerable_rounds=20,
    )
    run_eval_mod._print_wiki_nav_observation(acc, wiki_enabled)
    out = capsys.readouterr().out

    for key in ("hit_cases=", "expanded_cases=", "expanded_terms="):
        assert key in out, f"报告漏打了 {key}"
    assert f"hit_cases={hit_cases}" in out
    assert f"expanded_cases={expanded_cases}" in out
    assert "expanded_terms=3" in out
    assert expected_verdict in out


# ── ③ 漂移键与方向表 ────────────────────────────────────────
def test_warn_caliber_drift_detects_wiki_toggle(run_eval_mod, capsys) -> None:
    """F14：开/关 wiki 两条基线互比时 MUST 大声提示。"""
    base = {"graph_enabled": True, "context_caliber": "production_chat_path",
            "wiki_enabled": True}
    now = {**base, "wiki_enabled": False}
    run_eval_mod._warn_caliber_drift(base, now)
    out = capsys.readouterr().out
    assert "wiki 能力开关" in out
    assert "基线 True" in out and "本次 False" in out


def test_warn_caliber_drift_is_silent_when_caliber_matches(run_eval_mod, capsys) -> None:
    caliber = {"graph_enabled": True, "context_caliber": "production_chat_path",
               "wiki_enabled": True}
    run_eval_mod._warn_caliber_drift(caliber, dict(caliber))
    assert capsys.readouterr().out == ""


def test_compare_baseline_ignores_wiki_counters(run_eval_mod, capsys) -> None:
    """「只记录、不进方向表」：wiki_* 计数变化不许影响 --baseline 判定。"""
    base = {"refusal_rate": 0.9, "hit_rate": 0.8, "miss_rate": 0.1, "judged": 10,
            "wiki_nav_hit_cases": 0, "wiki_nav_expanded_cases": 0}
    now = {**base, "wiki_nav_hit_cases": 30, "wiki_nav_expanded_cases": 12}
    assert run_eval_mod._compare_baseline(base, now, 0.02) == 0
    out = capsys.readouterr().out
    assert "wiki_nav" not in out, "方向表只该出现三项既有指标"
    assert out.count("基线对比") == 3


# ── ④ EvalReport / 响应模型的三个「只记录」字段 ──────────────
def test_eval_report_three_fields_default_to_zero() -> None:
    report = EvalReport()
    assert report.wiki_nav_hit_cases == 0
    assert report.wiki_nav_expanded_cases == 0
    assert report.wiki_nav_expanded_terms == 0


def test_eval_report_response_exposes_three_fields() -> None:
    fields = EvalReportResponse.model_fields
    for name in ("wiki_nav_hit_cases", "wiki_nav_expanded_cases",
                 "wiki_nav_expanded_terms"):
        assert name in fields, f"管理端报告看不到 {name} = 字段没真正落地"


def test_accumulate_wiki_nav_stats_is_record_only() -> None:
    report = EvalReport()
    _accumulate_wiki_nav_stats(
        report, SimpleNamespace(path_stats={"wiki_nav_hits": 1, "wiki_nav_terms": 2})
    )
    _accumulate_wiki_nav_stats(
        report, SimpleNamespace(path_stats={"wiki_nav_hits": 0, "wiki_nav_terms": 0})
    )
    _accumulate_wiki_nav_stats(report, SimpleNamespace(path_stats={}))
    assert report.wiki_nav_hit_cases == 1
    assert report.wiki_nav_expanded_cases == 1
    assert report.wiki_nav_expanded_terms == 2
    # 只记录：不改变任何通过/失败判定字段
    assert report.passed == 0 and report.failed == 0 and report.total == 0


def test_eval_service_exports_the_accumulator() -> None:
    """确保 run_eval() 里真的调用了它（防止「加了字段但没接线」的假完成）。"""
    import inspect

    src = inspect.getsource(eval_service.run_eval)
    assert "_accumulate_wiki_nav_stats(report, result)" in src
