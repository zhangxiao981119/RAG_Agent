"""F.2（NO-DB 层）：策略规格表、写入校验（S-1/S-2/S-4）、「覆盖只能收紧」取值语义。

★ 这一步**不接线检索**：本文件只验证「规格表 + 校验」这一层的自洽性。
  `effective_*` 的取值语义在这里单测，Step 3 接线时才被检索链路调用。
"""
from __future__ import annotations

import pytest

from app.config import decisions
from app.services.scenario.effective import WIRED_RETRIEVAL_STEP
from app.services.scenario.policy_spec import (
    CURRENT_STEP,
    POLICY_CLOSURE_STEP,
    POLICY_FIELDS,
    RECALL_TOP_K_MAX,
    THRESHOLD_KEYS,
    WIRED_KEYS,
    PolicyValidationError,
    validate_policy,
)
from app.services.scenario.snapshot import SCENARIO_POLICY_KEYS, ScenarioPolicy
from tests.scenario.conftest import load_migration


# ── S-1：两份清单必须相等（迁移无法 import 应用代码，只能靠测试对齐）────


def test_policy_keyset_matches_migration():
    """`WIRED_KEYS` == 迁移 0028 的 `_POLICY_KEYS`。

    不一致 = 一类参数「能写不能读」或「能读不能写」。
    """
    assert WIRED_KEYS == set(load_migration()._POLICY_KEYS)
    assert WIRED_KEYS == {
        "recall_top_k",
        "rerank_enabled",
        "relevance_threshold_vector",
        "relevance_threshold_rerank",
    }


def test_closure_step_semantics_are_explicit():
    """闭集步骤与当前步骤是两个常量 —— 设计文档在这一处自相矛盾（见交付报告）。

    `CURRENT_STEP` 是「本次交付到第几步」（仅用于错误文案）；
    `POLICY_CLOSURE_STEP` 是「CHECK 闭集覆盖到哪一步的接线范围」。
    合并成一个值会让两条约束之一必然失真。

    ★ Step 4A 起 `CURRENT_STEP == 4`（交付到「写接口」这一步），而闭集与接线都还是
      3 —— 「交付进度」终于**追上并越过**了另外两个号。这恰好是原来那条
      `POLICY_CLOSURE_STEP > CURRENT_STEP`（闭集领先于交付）失效的地方：它描述的是
      Step 1–2 那个特定时刻的相对位置，不是不变量。**真正的不变量**是下面这条：
      闭集不允许超出「检索层真正会读」的范围 —— 超出的键就是「能写进去、却永远
      不生效」的静默 no-op。

    ★ 本函数里**故意只放不变量**：`POLICY_CLOSURE_STEP == 3` 这类字面量钉在
      `test_closure_and_wired_steps_are_pinned` 里，`闭集键的生效步骤 <= 闭集步骤`
      在 `test_closure_keys_respect_closure_step` 里。理由：这两条都会在注入矩阵的
      某些格里**先于**不变量触发，混在一起后矩阵就只能证明那两条、证明不了不变量。
    """
    assert CURRENT_STEP == 4
    assert POLICY_CLOSURE_STEP <= WIRED_RETRIEVAL_STEP, (
        f"闭集({POLICY_CLOSURE_STEP}) 超出接线({WIRED_RETRIEVAL_STEP})："
        "闭集里的键能写进库，但检索层不读 —— 静默 no-op"
    )


def test_closure_keys_respect_closure_step():
    """闭集里的每个键，其 `wired_in_step` 都不超过闭集步骤。

    ★ 与上面那条不变量的分工：不变量管**常量之间**的关系（每次交付都可能变），
      本条管**规格表与常量之间**的关系（表一改就要重新对齐）。
    """
    for field in POLICY_FIELDS:
        if field.name in WIRED_KEYS:
            assert field.wired_in_step <= POLICY_CLOSURE_STEP


def test_closure_and_wired_steps_are_pinned():
    """两个步骤号的**字面量钉**（交付进度、接线进度都必须显式留痕）。

    为什么需要它而不只靠不变量：不变量只约束相对大小，
    「闭集 2 / 接线 2」也能过 —— 但那是**闭集退化**（4 个键全被拒），
    必须由字面量钉发现。与 `test_effective.py` 的同名钉互为交叉校验。
    """
    assert CURRENT_STEP == 4
    assert POLICY_CLOSURE_STEP == 3
    assert WIRED_RETRIEVAL_STEP == 3


def test_closure_wired_invariant_matrix(monkeypatch):
    """**三格注入矩阵**：证明上面那条 `闭集 <= 接线` 真的会咬人（而不是恒真）。

    ★ 为什么需要它：`POLICY_CLOSURE_STEP`/`WIRED_RETRIEVAL_STEP` 现在都是 3，
      `3 <= 3` 这种断言在出厂值下**无论对错都成立** —— 与 Step 3 那次
      「两套阈值都等于 0.50」的假绿是同一类陷阱。必须注入非常规值才能观测到它。
    ★ 为什么直接调用上面那个**真**测试函数、而不是复制一份判据：
      复制出来的副本会独立漂移，于是「矩阵绿、真断言红」这种最坏情况反而可行。
    ★ 旧断言 `POLICY_CLOSURE_STEP > CURRENT_STEP` 在出厂值（3 > 4）下**已经是红的** ——
      它描述的是 Step 1–2 那个时刻的相对位置，从来不是不变量。矩阵第一格
      （闭集 5 / 接线 3）正是旧断言的盲区：`5 > 4` 成立（旧绿），而新断言红。

    三格：
      | 闭集 | 接线 | 预期 | 读法                                   |
      |    5 |    3 | 红   | 可写不可读 → 静默 no-op（旧断言在此为绿）|
      |    2 |    3 | 绿   | 闭集收紧于接线 → 合法                    |
      |    3 |    2 | 红   | 接线跑到闭集前面 → 「能读不能写」         |
    """
    import sys

    module = sys.modules[__name__]
    check = test_closure_step_semantics_are_explicit

    # ── 格 1：闭集 5 / 接线 3 → 必须红 ──────────────────────────
    monkeypatch.setattr(module, "POLICY_CLOSURE_STEP", 5)
    monkeypatch.setattr(module, "WIRED_RETRIEVAL_STEP", 3)
    with pytest.raises(AssertionError, match="闭集\\(5\\) 超出接线\\(3\\)"):
        check()

    # ── 格 2：闭集 2 / 接线 3 → 必须绿 ──────────────────────────
    monkeypatch.setattr(module, "POLICY_CLOSURE_STEP", 2)
    monkeypatch.setattr(module, "WIRED_RETRIEVAL_STEP", 3)
    check()  # 不抛即绿

    # ── 格 3：闭集 3 / 接线 2 → 必须红 ──────────────────────────
    monkeypatch.setattr(module, "POLICY_CLOSURE_STEP", 3)
    monkeypatch.setattr(module, "WIRED_RETRIEVAL_STEP", 2)
    with pytest.raises(AssertionError, match="闭集\\(3\\) 超出接线\\(2\\)"):
        check()


def test_policy_fields_are_unique_and_complete():
    """16 项、名字唯一、类型在闭集内、生效步骤单调可解释。"""
    names = [field.name for field in POLICY_FIELDS]
    assert len(names) == 16
    assert len(set(names)) == 16
    assert {field.type for field in POLICY_FIELDS} <= {"int", "bool", "float"}
    for field in POLICY_FIELDS:
        assert field.default_semantics, f"{field.name} 缺少缺省语义说明"
        assert field.invalidates, f"{field.name} 缺少「改后什么失效」的答案"
        assert field.wired_in_step >= 1


def test_locked_parameters_never_enter_keyset():
    """L0 锁定的两项（rrf_k / keyword_tsquery_enabled）永不可配。"""
    locked = {f.name for f in POLICY_FIELDS if f.wired_in_step > POLICY_CLOSURE_STEP}
    assert {"rrf_k", "keyword_tsquery_enabled"} <= locked
    assert "rrf_k" not in WIRED_KEYS
    assert "keyword_tsquery_enabled" not in WIRED_KEYS


def test_undefined_parameters_do_not_invent_defaults():
    """#15/#16 已有平台开关常量（默认常开），且仍未进写入闭集。"""
    spec = {f.name: f for f in POLICY_FIELDS}
    for name, const in (
        ("table_recall_enabled", "TABLE_RECALL_ENABLED"),
        ("section_recall_enabled", "SECTION_RECALL_ENABLED"),
    ):
        semantics = spec[name].default_semantics
        assert semantics.startswith("常开")
        assert f"decisions.{const}=" in semantics
        # 闭集未扩：不能写成可配置（否则会让人以为能按场景写）
        assert "不可按场景覆盖" in semantics
        assert spec[name].wired_in_step > POLICY_CLOSURE_STEP


# ── 写入校验（应用层主校验，DB CHECK 只兜底）─────────────────────────


def test_validate_policy_accepts_wired_keys():
    validated = validate_policy(
        {
            "recall_top_k": 30,
            "rerank_enabled": False,
            "relevance_threshold_vector": 0.62,
            "relevance_threshold_rerank": 0.71,
        }
    )
    assert validated["recall_top_k"] == 30
    assert validate_policy(None) == {}
    assert validate_policy({}) == {}


def test_validate_policy_rejects_unwired_key():
    """未接线的键写入必须报错，且文案指向「自哪一步起可配置」。"""
    with pytest.raises(PolicyValidationError) as exc_info:
        validate_policy({"final_top_n": 3})
    assert exc_info.value.field == "final_top_n"
    assert "Step 4" in str(exc_info.value)
    assert "尚未接线" in str(exc_info.value)


def test_validate_policy_rejects_unknown_key():
    with pytest.raises(PolicyValidationError) as exc_info:
        validate_policy({"no_such_knob": 1})
    assert "未知参数" in str(exc_info.value)


@pytest.mark.parametrize(
    "name", ["table_recall_enabled", "section_recall_enabled"]
)
def test_validate_policy_rejects_recall_switches(name):
    """G-9：两键已有平台常量（常开），但仍在写入闭集外 → 场景写入必须被拒。"""
    with pytest.raises(PolicyValidationError) as exc_info:
        validate_policy({name: True})
    assert exc_info.value.field == name
    assert "尚未接线" in str(exc_info.value)
    assert "Step 4" in str(exc_info.value)


@pytest.mark.parametrize(
    "policy",
    [
        {"recall_top_k": 0},                       # 下界
        {"recall_top_k": RECALL_TOP_K_MAX + 1},    # 代码上限（站位值 200）
        {"relevance_threshold_vector": 5.0},       # 阈值越界
        {"relevance_threshold_rerank": -0.1},
    ],
)
def test_validate_policy_rejects_out_of_range(policy):
    with pytest.raises(PolicyValidationError):
        validate_policy(policy)


@pytest.mark.parametrize("name", sorted(THRESHOLD_KEYS))
def test_validate_policy_rejects_zero_threshold(name):
    """S-4：0 阈值 = 永不拒答（比 DB CHECK 的 `>= 0.0` 更严一档）。"""
    with pytest.raises(PolicyValidationError) as exc_info:
        validate_policy({name: 0.0})
    assert "必须大于 0" in str(exc_info.value)


@pytest.mark.parametrize(
    "policy",
    [
        {"rerank_enabled": "true"},   # 字符串布尔
        {"recall_top_k": True},       # bool 是 int 的子类，必须被拒
        {"recall_top_k": 1.5},        # int 字段给了 float
        {"relevance_threshold_vector": "0.6"},
    ],
)
def test_validate_policy_rejects_type_mismatch(policy):
    with pytest.raises(PolicyValidationError):
        validate_policy(policy)


def test_validate_policy_rejects_non_object():
    with pytest.raises(PolicyValidationError):
        validate_policy([])  # type: ignore[arg-type]


# ── 取值语义：覆盖只能收紧 ────────────────────────────────────────────


def test_rerank_cannot_be_enabled_against_platform():
    """单向 AND：平台关时场景开 = 静默无效（I-2）。"""
    policy = ScenarioPolicy(rerank_enabled=True)
    assert policy.effective_rerank_enabled(False) is False
    assert policy.effective_rerank_enabled(True) is True
    # 场景关则一定关
    assert ScenarioPolicy(rerank_enabled=False).effective_rerank_enabled(True) is False
    # 不设则跟随平台
    assert ScenarioPolicy().effective_rerank_enabled(True) is True
    assert ScenarioPolicy().effective_rerank_enabled(False) is False


def test_recall_top_k_follows_platform_when_unset():
    assert ScenarioPolicy().effective_recall_top_k() == decisions.TOP_K_RECALL
    assert ScenarioPolicy(recall_top_k=30).effective_recall_top_k() == 30


def test_threshold_two_calibers():
    """两个阈值分开：只设 vector 时，rerank 口径仍取平台值。"""
    policy = ScenarioPolicy(relevance_threshold_vector=0.62)
    assert policy.effective_threshold(use_rerank=False) == 0.62
    assert policy.effective_threshold(use_rerank=True) == decisions.RELEVANCE_THRESHOLD_RERANK
    assert ScenarioPolicy().effective_threshold(use_rerank=False) == (
        decisions.RELEVANCE_THRESHOLD_VECTOR
    )


def test_declared_is_the_sparse_set():
    """`declared()` 只保留显式设置过的键 —— 落库与指纹都依赖这个语义。"""
    assert ScenarioPolicy().declared() == {}
    assert ScenarioPolicy(recall_top_k=30).declared() == {"recall_top_k": 30}
    assert ScenarioPolicy(recall_top_k=30, rerank_enabled=False).declared() == {
        "recall_top_k": 30,
        "rerank_enabled": False,
    }


# ══════════════════════════════════════════════════════════════════════
# 读取侧类型判定：MUST 与写入侧共用同一个 `type_ok`（不许各写一份）
# ══════════════════════════════════════════════════════════════════════
#
# 这一段是修 4 的回归网。缺陷形态：
#   `to_policy()` 曾用 `isinstance(value, (int, float))` 判 int 字段，
#   于是 `{"recall_top_k": 1.5}` / `{"recall_top_k": true}` 被**当成合法值**
#   塞进 `ScenarioPolicy.recall_top_k`（声明为 `int | None`）。
#   危险之处不是「值难看」，而是它一路静默：解析成功、快照正常、日志无异常，
#   直到下游 `range()` / 切片 / `top_k` 语义在很远的地方行为诡异。
#   写入侧（`validate_policy`）当时是**对的** —— 所以这类"一边严一边松"的缺陷
#   只有把两侧放在同一张表上逐格对照才拦得住。

_TYPE_BY_KEY = {field.name: field.type for field in POLICY_FIELDS}
"""`ScenarioPolicy` 键 → 期望类型（从规格表推导，与 `snapshot._build_type_map` 同源）。"""

_GOOD_VALUE = {"int": 30, "bool": False, "float": 0.6}

_BAD_VALUES: dict[str, list[object]] = {
    # 1.5 = 非整数；True/False = bool 是 int 的子类；"30" = 数字字符串
    "int": [1.5, True, False, "30"],
    # 1/0/1.0 = 非 bool 的"真假值"；"true" = 字符串布尔
    "bool": ["true", 1, 0, 1.0],
    # True/False = bool 是 int 的子类；"0.6" = 数字字符串
    "float": ["0.6", True, False],
}


def _row_with_policy(policy: dict[str, object]):
    """构造一行只关心 `policy` 的已发布版本行。"""
    import uuid as _uuid

    from app.services.scenario.snapshot import ScenarioRow

    return ScenarioRow(
        tenant_id=_uuid.UUID("11111111-1111-1111-1111-111111111111"),
        scenario_id=_uuid.UUID("33333333-3333-3333-3333-333333333333"),
        scenario_key="default",
        status="active",
        is_builtin=True,
        db_binding=None,
        version=1,
        policy=policy,
        kb_ids=(),
        binding_mode="unrestricted",
        dna_declared={},
        checksum="0123456789abcdef",
    )


@pytest.mark.parametrize("bad", [1.5, True, "30"])
def test_to_policy_rejects_non_integer_recall_top_k(bad):
    """`recall_top_k` 的三种坏值：1.5 / true / "30" ⇒ 一律判为不可解析。

    ★ 三条都必须抛 `SCENARIO_UNRESOLVABLE`（500），而不是「取整」「当 1」
      「字符串转数字」——读了库就改值，等于让库里的坏数据悄悄生效。
    """
    from app.services.scenario import ScenarioCode, ScenarioError

    with pytest.raises(ScenarioError) as exc_info:
        _row_with_policy({"recall_top_k": bad}).to_policy()
    error = exc_info.value
    assert error.code is ScenarioCode.UNRESOLVABLE
    assert error.http_status == 500
    assert error.diagnostic["reason"] == "policy_type_mismatch:recall_top_k"
    assert error.diagnostic["expected_type"] == "int"
    # 只记类型不记值（值可能很长，且类型已足够定位）
    assert error.diagnostic["actual_type"] == type(bad).__name__


def test_type_ok_is_strict_on_int():
    """`type_ok` 的判定矩阵（写入与读取共用的那一份实现）。"""
    from app.services.scenario.policy_spec import type_ok

    # int：只认真正的 int
    assert type_ok(30, "int") is True
    assert type_ok(0, "int") is True
    assert type_ok(-1, "int") is True
    assert type_ok(1.5, "int") is False   # ← 修 4 的核心：float 不是 int
    assert type_ok(1.0, "int") is False   # 看似整数但类型不同
    assert type_ok(True, "int") is False  # bool 是 int 子类
    assert type_ok(False, "int") is False
    assert type_ok("30", "int") is False

    # float：接受 int（0 / 1 是常见写法），仍拒绝 bool
    assert type_ok(0.6, "float") is True
    assert type_ok(0, "float") is True
    assert type_ok(1, "float") is True
    assert type_ok(True, "float") is False
    assert type_ok("0.6", "float") is False

    # bool：只认 bool
    assert type_ok(True, "bool") is True
    assert type_ok(0, "bool") is False
    assert type_ok("true", "bool") is False


@pytest.mark.parametrize("name", SCENARIO_POLICY_KEYS)
def test_read_and_write_side_agree_on_type_checks(name):
    """逐键逐值对照：**写入侧接受的，读取侧就必须接受；拒绝的也必须都拒绝**。

    这是「两边共用 `type_ok`」的可执行定义。任何一侧单独改动（例如把读取侧改回
    `isinstance(value, (int, float))`）都会在 `_BAD_VALUES` 的某一格上分叉而红。
    """
    from app.services.scenario import ScenarioCode, ScenarioError

    expected = _TYPE_BY_KEY[name]

    # ── 正向：合法值两侧都收，且读出的就是原值 ──
    good = _GOOD_VALUE[expected]
    assert validate_policy({name: good}) == {name: good}
    assert getattr(_row_with_policy({name: good}).to_policy(), name) == good

    # ── 反向：同一批坏值两侧都拒 ──
    assert _BAD_VALUES[expected], f"{name} 缺少坏值样本"
    for bad in _BAD_VALUES[expected]:
        with pytest.raises(PolicyValidationError):
            validate_policy({name: bad})
        with pytest.raises(ScenarioError) as exc_info:
            _row_with_policy({name: bad}).to_policy()
        assert exc_info.value.code is ScenarioCode.UNRESOLVABLE
        assert exc_info.value.diagnostic["reason"] == f"policy_type_mismatch:{name}"


def test_to_policy_ignores_unknown_keys_but_rejects_known_bad_values():
    """未知键忽略（前向兼容），已知键的坏值拒绝 —— 两种处理的界限不能混。"""
    policy = _row_with_policy({"future_key": object(), "recall_top_k": 30}).to_policy()
    assert policy.recall_top_k == 30
    assert "future_key" not in policy.declared()


def test_scenario_policy_keys_are_wired():
    """`SCENARIO_POLICY_KEYS`（能读）必须等于 `WIRED_KEYS`（能写）。

    两份清单不一致 = 一类键「能写不能读」（写进库静默无效）或「能读不能写」。
    """
    assert set(SCENARIO_POLICY_KEYS) == set(WIRED_KEYS)
    # 每个能读的键都必须在规格表里有类型（`_build_type_map` 在 import 期就是这么查的）
    assert set(SCENARIO_POLICY_KEYS) <= set(_TYPE_BY_KEY)
