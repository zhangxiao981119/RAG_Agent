"""Step 3 接线（F.3 NO-DB）：唯一折叠点的取值语义 + 「不许有第二个读点」的结构守卫。

两个硬指标在这里：

  **Z3-1 反自证**：把平台常量 monkeypatch 成不可能撞上的值，断言 `eff` 跟着变。
  不许写成 `assert eff.recall_top_k == 50` —— 那是「今天恰好等于 50」，
  平台常量改成 30 它照样绿（两边都是抓取值），证明不了「现读」。

  **Z3-4 文本守卫**：`retrieve/__init__.py` 里平台召回条数 / 平台取用条数 /
  平台阈值选择函数的直接引用出现 **0 次**，平台重排开关 **恰好 1 次**。
  「读两次配置」的典型后果不是立刻出错，而是半年后有人改了 A 处忘了 B 处，
  表现是「某场景的阈值只对 search 生效、对 ask 不生效」，**且没有任何报错**。
  这条守卫是本次改动里唯一能在**结构层面**拦住它的东西。

★ 为什么用文本计数而不是 AST：这几处的形式极其固定（属性访问），文本断言更窄更稳；
  AST 会把注释里的同名字符串放过或误伤。代价是**注释里也不能写那几个完整属性访问形式**
  —— 这条已写进 `retrieve/__init__.py` 的文件头。
"""
from __future__ import annotations

import pathlib

import pytest

from app.config import decisions
from app.services.scenario import policy_spec
from app.services.scenario.effective import (
    ACTIVE_KEYS,
    WIRED_RETRIEVAL_STEP,
    EffectiveRetrieval,
    effective_retrieval,
)
from app.services.scenario.snapshot import SCENARIO_POLICY_KEYS, ScenarioPolicy

_API_ROOT = pathlib.Path(__file__).resolve().parents[2]
_RETRIEVE_SRC = _API_ROOT / "app" / "services" / "retrieve" / "__init__.py"
_EFFECTIVE_SRC = _API_ROOT / "app" / "services" / "scenario" / "effective.py"


# ══════════════════════════════════════════════════════════════════════
# Z3-1：反自证（**Step 3 的硬指标**）
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("platform_rerank", [False, True])
def test_z3_1_default_policy_equals_platform_constants(monkeypatch, platform_rerank):
    """policy 全 None 时，有效值 == 平台**当前**值（证明是现读，不是抄常量）。

    37 / 5 / 0.61 / 0.62 / 99 这五个数刻意与平台默认（50 / 8 / 0.5x）撞不上，
    也刻意不是「合法范围内的漂亮数字」—— 它们能被读到，只可能来自 `decisions`。
    """
    monkeypatch.setattr(decisions, "TOP_K_RECALL", 37)
    monkeypatch.setattr(decisions, "TOP_K_RERANK", 5)
    monkeypatch.setattr(decisions, "RELEVANCE_THRESHOLD_VECTOR", 0.61)
    monkeypatch.setattr(decisions, "RELEVANCE_THRESHOLD_RERANK", 0.62)
    monkeypatch.setattr(policy_spec, "RECALL_TOP_K_MAX", 99)

    eff = effective_retrieval(ScenarioPolicy(), platform_rerank_enabled=platform_rerank)

    assert eff.recall_top_k == 37
    assert eff.final_top_n == 5
    assert eff.threshold_vector == 0.61
    assert eff.threshold_rerank == 0.62
    assert eff.rerank_enabled is platform_rerank
    assert eff.clamped == ()


def test_z3_1_survives_policy_is_none(monkeypatch):
    """`policy=None` 与 `policy=ScenarioPolicy()` 完全等价（调用方不必判空）。"""
    monkeypatch.setattr(decisions, "TOP_K_RECALL", 37)
    assert effective_retrieval(None, platform_rerank_enabled=False) == effective_retrieval(
        ScenarioPolicy(), platform_rerank_enabled=False
    )


# ══════════════════════════════════════════════════════════════════════
# 成对测试：四键 × 配了 / 没配
# ══════════════════════════════════════════════════════════════════════


def test_recall_top_k_override_wins(monkeypatch):
    """配了就赢（且与平台值不同，避免退化成「等于平台值」的假绿）。"""
    monkeypatch.setattr(decisions, "TOP_K_RECALL", 37)
    eff = effective_retrieval(ScenarioPolicy(recall_top_k=20), platform_rerank_enabled=False)
    assert eff.recall_top_k == 20
    assert eff.recall_top_k != decisions.TOP_K_RECALL


def test_recall_top_k_platform_when_unset(monkeypatch):
    monkeypatch.setattr(decisions, "TOP_K_RECALL", 37)
    eff = effective_retrieval(ScenarioPolicy(), platform_rerank_enabled=False)
    assert eff.recall_top_k == decisions.TOP_K_RECALL == 37


def test_rerank_scenario_can_only_tighten():
    """单向 AND 的四组合 —— 第 4 组是关键：平台关时场景开，仍是 F。

    ★ 为什么必须单向：rerank 的开启成本由**平台**承担（模型常驻 / CPU 50 条 66s）。
      若场景能自行打开，一个场景的配置就能把整台机器的检索 P95 拖垮。
    """
    cases = [
        (True, None, True),
        (True, False, False),
        (False, None, False),
        (False, True, False),
    ]
    for platform, declared, expected in cases:
        eff = effective_retrieval(
            ScenarioPolicy(rerank_enabled=declared), platform_rerank_enabled=platform
        )
        assert eff.rerank_enabled is expected, f"platform={platform} declared={declared}"


def test_threshold_vector_override_isolated(monkeypatch):
    """只覆盖 vector 时，rerank 口径仍取平台值（两键互不影响）。"""
    monkeypatch.setattr(decisions, "RELEVANCE_THRESHOLD_RERANK", 0.62)
    eff = effective_retrieval(
        ScenarioPolicy(relevance_threshold_vector=0.7), platform_rerank_enabled=True
    )
    assert eff.threshold_vector == 0.7
    assert eff.threshold_rerank == 0.62


def test_threshold_rerank_override_isolated(monkeypatch):
    monkeypatch.setattr(decisions, "RELEVANCE_THRESHOLD_VECTOR", 0.61)
    eff = effective_retrieval(
        ScenarioPolicy(relevance_threshold_rerank=0.3), platform_rerank_enabled=True
    )
    assert eff.threshold_rerank == 0.3
    assert eff.threshold_vector == 0.61


def test_threshold_for_follows_actual_score_source():
    """`threshold_for` 按**降级之后**的真实口径取 —— 钉住「降级必须切回 vector 口径」。"""
    eff = effective_retrieval(
        ScenarioPolicy(relevance_threshold_vector=0.7, relevance_threshold_rerank=0.3),
        platform_rerank_enabled=True,
    )
    assert eff.threshold_for(use_rerank=True) == 0.3
    assert eff.threshold_for(use_rerank=False) == 0.7


def test_effective_threshold_matches_active_threshold(monkeypatch):
    """全 None 时 `eff.threshold_for` MUST 等于 `decisions.active_threshold`。

    ★ 再 monkeypatch 两个平台阈值后仍相等 —— 两边都是现读，不是两个抄来的常量。
      （这条同时登记了 G3-4：两处是同一口径的两份实现，靠本断言钉住。
       将来若要合并，正确方向是 `ScenarioPolicy.effective_threshold` 的 None 分支
       委托 `active_threshold`。）
    """
    for use_rerank in (True, False):
        eff = effective_retrieval(ScenarioPolicy(), platform_rerank_enabled=True)
        assert eff.threshold_for(use_rerank=use_rerank) == decisions.active_threshold(use_rerank)

    monkeypatch.setattr(decisions, "RELEVANCE_THRESHOLD_VECTOR", 0.61)
    monkeypatch.setattr(decisions, "RELEVANCE_THRESHOLD_RERANK", 0.62)
    for use_rerank in (True, False):
        eff = effective_retrieval(ScenarioPolicy(), platform_rerank_enabled=True)
        assert eff.threshold_for(use_rerank=use_rerank) == decisions.active_threshold(use_rerank)


# ══════════════════════════════════════════════════════════════════════
# clamp：只对 recall_top_k，且必须可见
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("declared", "expected", "ceiling"),
    [
        (999, 100, 100),   # 上限被下调后，历史行越界 → 裁到平台上限
        (1001, 200, 200),  # 默认上限 200
        (0, 1, 200),       # 下界：0 会让切片为空 → 全量拒答
        (-5, 1, 200),      # 下界（负值同族）
    ],
)
def test_clamp_upper_and_lower(monkeypatch, declared, expected, ceiling):
    monkeypatch.setattr(policy_spec, "RECALL_TOP_K_MAX", ceiling)
    eff = effective_retrieval(ScenarioPolicy(recall_top_k=declared), platform_rerank_enabled=False)
    assert eff.recall_top_k == expected
    # ★ 收窄 must 可见：没有这条就是静默降级（「我配了 999、实际跑 200」）
    assert eff.clamped == ("recall_top_k",)


def test_no_clamp_inside_range():
    eff = effective_retrieval(ScenarioPolicy(recall_top_k=99), platform_rerank_enabled=False)
    assert eff.recall_top_k == 99
    assert eff.clamped == ()


def test_threshold_out_of_range_is_NOT_clamped():
    """S-4b：阈值越界**不裁**（裁掉的错值裁不出对的判据）。

    `0.0` 悄悄拉到 0.01 仍然是「永不拒答」—— 只让日志更干净，没有修好任何东西。
    """
    eff = effective_retrieval(
        ScenarioPolicy(relevance_threshold_vector=0.0), platform_rerank_enabled=False
    )
    assert eff.threshold_vector == 0.0
    assert eff.clamped == ()


# ══════════════════════════════════════════════════════════════════════
# 三方相等 + 步骤号语义
# ══════════════════════════════════════════════════════════════════════


def test_three_way_keyset_equality():
    """写入闭集 == 快照可承载 == 检索层真正读。

    三者任一不等 = 「能写不能读」（写进库静默 no-op）或「能读不能用」。
    """
    assert ACTIVE_KEYS == policy_spec.WIRED_KEYS == set(SCENARIO_POLICY_KEYS)


def test_wired_retrieval_step_matches_field_horizons():
    """`WIRED_RETRIEVAL_STEP` 必须等于接管线键的最大生效步骤，且不超前于闭集。"""
    horizon = max(
        field.wired_in_step
        for field in policy_spec.POLICY_FIELDS
        if field.name in ACTIVE_KEYS
    )
    assert WIRED_RETRIEVAL_STEP == horizon == 3
    # 接线不能超前于闭集：否则「闭集说还不能写、检索却在读」，自相矛盾
    assert WIRED_RETRIEVAL_STEP <= policy_spec.POLICY_CLOSURE_STEP


def test_step_numbers_are_three_and_explicit():
    """三个步骤号必须**分别**钉住 —— 它们来源不同，合并任意两个都会让第三个失真。

    ★ Step 4A 起 `CURRENT_STEP` 由 2 改为 4（DQ4-2）：交付已推进到「场景写接口」，
      而检索接线与写入闭集都停在 3。三者从「2/3/3」变成「4/3/3」——
      这正好证明它们是**三个独立事实**而不是一个数的三种写法。
    ★ 断言里 `WIRED_RETRIEVAL_STEP == 3` 是**故意写死的字面量**：Step 4A 只加写接口，
      一行检索接线都没改。若有人顺手把它改成 4，这条会立刻红。
    """
    assert policy_spec.CURRENT_STEP == 4
    assert policy_spec.POLICY_CLOSURE_STEP == 3
    assert WIRED_RETRIEVAL_STEP == 3


def test_effective_retrieval_is_frozen():
    """有效值对象不可变：请求内任何下游都不能把它改掉（改了就是跨读点不一致）。"""
    import dataclasses

    eff = effective_retrieval(None, platform_rerank_enabled=False)
    assert isinstance(eff, EffectiveRetrieval)
    with pytest.raises(dataclasses.FrozenInstanceError):
        eff.recall_top_k = 1  # type: ignore[misc]


# ══════════════════════════════════════════════════════════════════════
# Z3-4：结构性守卫（「不许有第二个读点」）
# ══════════════════════════════════════════════════════════════════════


def test_z3_4_no_second_read_of_platform_constants():
    """`retrieve/__init__.py` 里三个平台取值入口直接引用 **0 次**，重排开关**恰好 1 次**。

    ★ 0 次：已全部由 `eff.*` 取代。任何一处漏改，都会让「场景覆盖」在某个读点上
      被静默绕过（8 个读点里漏一个，表现是「策略只对 search 生效、对 ask 不生效」）。
    ★ 恰好 1 次：平台重排开关只该读一次并传给纯折叠函数；读两次就是两条真源。
    ★ 注释里也不能写这几个完整属性访问形式 —— 本断言是文本计数，会连注释一起数。
      要解释时写「平台召回条数常量」而不是原文（已写进文件头）。
    """
    src = _RETRIEVE_SRC.read_text(encoding="utf-8")
    assert src.count("decisions.TOP_K_RECALL") == 0, "每路召回条数必须经 eff.recall_top_k"
    assert src.count("decisions.TOP_K_RERANK") == 0, "取用条数必须经 eff.final_top_n"
    assert src.count("decisions.active_threshold") == 0, "阈值必须经 eff.threshold_for"
    assert src.count("get_settings().rerank_enabled") == 1, "平台重排开关只能读一次"


def test_effective_module_does_not_from_import_constants():
    """`effective.py` MUST NOT from-import 常量（否则 monkeypatch 锁不住口径）。

    ★ 这条也是 Z3-1 能成立的前提：若写成
      `from ...policy_spec import RECALL_TOP_K_MAX`，值在 import 期被拷进本模块命名
      空间，`monkeypatch.setattr(policy_spec, ...)` 不会影响它 —— Z3-1 会红。
      纪律由测试强制，不靠 review。
    """
    src = _EFFECTIVE_SRC.read_text(encoding="utf-8")
    assert "from app.config.decisions import" not in src
    assert "import RECALL_TOP_K_MAX" not in src


def test_retrieve_signature_has_keyword_only_policy():
    """`retrieve` / `retrieve_chain` 的 `policy` 必须是 **keyword-only**。

    ★ 多个调用点按 6 个**位置参数**传（chat 的链式入口、integrations 的 search、
      eval_service、标定脚本）。改成位置参数会静默改变实参含义 —— 这类错不会报错，
      只会让某条链路拿错参数。
    """
    import inspect

    from app.services.retrieve import RetrievalService

    for name in ("retrieve", "retrieve_chain"):
        params = inspect.signature(getattr(RetrievalService, name)).parameters
        assert "policy" in params, f"{name} 缺少 policy 形参"
        assert params["policy"].kind is inspect.Parameter.KEYWORD_ONLY, (
            f"{name}.policy 必须是 keyword-only"
        )
        assert params["policy"].default is None


def test_folding_statement_is_after_authorized_kb_early_return():
    """结构守卫：`eff = effective_retrieval(...)` MUST 在「空授权范围早退」之后。

    ★ 为什么较真位置：折叠块会读 `get_settings().rerank_enabled`。上移到早退之前
      等于让「空范围的请求」也去读平台参数，且会把读取时点从召回段提到方法入口 ——
      `tests/retrieve/` 里现存的 settings 替身只提供部分字段，上移会让那一整类桩
      全部以 AttributeError 变红（而它们与本步无关）。
    ★ 行为层面的对应证据：`test_empty_kb_scope_returns_before_reading_platform_settings`
      （`test_policy_wiring.py`，把 settings 削到连 `rerank_enabled` 都没有）。
    """
    src = _RETRIEVE_SRC.read_text(encoding="utf-8")
    early = src.index("if not authorized_kb_ids:")
    fold = src.index("eff = effective_retrieval(")
    assert early < fold, "折叠点 MUST 在 `if not authorized_kb_ids` 早退之后"
