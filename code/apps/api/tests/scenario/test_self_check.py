"""Step 3 自检 S-2 ~ S-10：判据 / 动作分流 / 计数（**无 PG、无模型**）。

三条主线：

  1. **常量类（T1）违反 → RuntimeError**：S-2a / S-4a / S-6。
     它们判断的是「代码与代码」的关系，错了就是代码 bug，每台机器启动都必现。
  2. **数据类（T2）违反 → 只 WARN + metric，永不 raise**：S-2b/S-3/S-4b/S-5/S-7/S-8/S-9/S-10。
     这是与 Step 1–2 原稿**相反**的决定（附录 A-1/A-2/A-3 登记）：数据是运维改出来的，
     让它拒绝启动 = 一个场景的错配置拖垮整个实例，且形成死锁
     （坏数据在库里 → 实例起不来 → 接口不可用 → 只能手写 SQL 改库）。
  3. **T4 发布期拒绝**：`check_policy_publishable()` —— 唯一能真正阻止坏配置进系统的
     位置。本步只有测试调它，函数现在就交付，Step 4 的写接口直接调。

数据层用 `tests/scenario/conftest.py` 现成的 `FakeSession` + `build_row`：
它们只实现 `execute → all → _mapping`，而 `_load_all_for_scan` 恰好只需要这一面。
**不需要 PG，也不需要改 conftest。**
"""
from __future__ import annotations

import ast
import dataclasses
import logging
import pathlib
import re
import uuid
from types import SimpleNamespace

import pytest

from app.config import decisions, placeholders
from app.services.scenario import policy_spec, self_check
from tests.scenario.conftest import FakeSession, build_row, load_migration

_API_ROOT = pathlib.Path(__file__).resolve().parents[2]

# 平台开关的可变状态：由 `default_settings(...)` 写入，被测代码无参 `get_settings()` 读出。
_STATE: dict[str, bool] = {
    "rerank_enabled": False,
    "tenant_governance_enabled": False,
}


@pytest.fixture(autouse=True)
def default_settings(monkeypatch):
    """默认把平台重排与租户治理开关都置为**关**（本机真实默认），需要开的用例自己覆盖。

    ★ 不用真实 `get_settings()`：那会读本仓 `.env`，让「平台开/关」两条判据的
      结果取决于跑测试的机器 —— 这类测试一旦随环境变，就会在某台机器上假绿。
    ★ `tenant_governance_enabled` 必须出现在替身里：`_load_all_for_scan` 会读它
      决定扫描口径，缺失会让所有用例落进「查询失败」分支。
    """

    _STATE["rerank_enabled"] = False  # 每条用例都从「平台关」这个真实默认起步
    _STATE["tenant_governance_enabled"] = False  # 扫描口径默认按关闭态

    def _settings(
        *,
        rerank_enabled: bool | None = None,
        tenant_governance_enabled: bool | None = None,
    ):
        """设置并**返回**一个 settings 替身（无参调用只读当前状态）。

        ★ 必须带状态：`default_settings(rerank_enabled=True)` 之后再让被测函数
          无参 `get_settings()` 时也要看到 True，否则「平台开」用例断言的
          其实还是「平台关」的旧状态 —— 那是一条永远绿不了/绿错位置的假测试。
        """
        if rerank_enabled is not None:
            _STATE["rerank_enabled"] = rerank_enabled
        if tenant_governance_enabled is not None:
            _STATE["tenant_governance_enabled"] = tenant_governance_enabled
        return SimpleNamespace(
            rerank_enabled=_STATE["rerank_enabled"],
            tenant_governance_enabled=_STATE["tenant_governance_enabled"],
        )

    monkeypatch.setattr(self_check, "get_settings", _settings)
    return _settings


@pytest.fixture(params=[["not-a-mapping"], "abc", [1, 2]], ids=["list-str", "str", "list-int"])
def bad_dna(request):
    """三种非 JSON 对象的 jsonb 值：`dict()` 分别抛 ValueError / ValueError / TypeError。"""
    return request.param


def _dna(**overrides: object) -> dict:
    """构造一份 dna_declared（默认取迁移里的种子值，逐字）。"""
    dna = dict(load_migration()._SEED_DNA)
    dna.update(overrides)
    return dna


async def _scan(rows, *, max_step: int = 3, settings=None) -> dict[str, int]:
    return await self_check.check_scenario_data(FakeSession(rows), max_step=max_step)


# ══════════════════════════════════════════════════════════════════════
# T1：常量类（违反 → RuntimeError）
# ══════════════════════════════════════════════════════════════════════


def test_constants_pass_on_shipped_code():
    """出厂状态必须通过（否则每次启动都炸）。"""
    self_check.check_scenario_constants()


def test_s2a_raises_when_ceiling_below_one(monkeypatch):
    """S-2a：上限 < 1 ⇒ 候选池恒空 ⇒ 全量拒答。"""
    monkeypatch.setattr(policy_spec, "RECALL_TOP_K_MAX", 0)
    with pytest.raises(RuntimeError) as exc_info:
        self_check.check_scenario_constants()
    assert "RECALL_TOP_K_MAX" in str(exc_info.value)
    assert "S-2a" in str(exc_info.value)


def test_s2a_raises_when_ceiling_above_db_check(monkeypatch):
    """S-2a：代码上限 > DB CHECK 上限 ⇒ 合法配置会先被数据库拒（两处口径分裂）。"""
    monkeypatch.setattr(
        policy_spec, "RECALL_TOP_K_MAX", self_check.RECALL_TOP_K_DB_CEILING + 1
    )
    with pytest.raises(RuntimeError) as exc_info:
        self_check.check_scenario_constants()
    assert str(self_check.RECALL_TOP_K_DB_CEILING) in str(exc_info.value)
    # 报错必须点名那条 DB 约束 —— 运维要能直接去核对迁移
    assert "ck_scenario_versions_policy_recall_top_k" in str(exc_info.value)


def test_s2a_allows_ceiling_equal_to_db_check(monkeypatch):
    """边界：恰好等于 DB 上限是合法的（正向对照，防「一律拒绝」的假绿）。"""
    monkeypatch.setattr(
        policy_spec, "RECALL_TOP_K_MAX", self_check.RECALL_TOP_K_DB_CEILING
    )
    self_check.check_scenario_constants()


def _with_threshold_field(monkeypatch, name: str, **changes) -> None:
    """把某个阈值字段替换成改过的副本（`PolicyField` 是 frozen，只能整体替换）。"""
    patched = tuple(
        dataclasses.replace(field, **changes) if field.name == name else field
        for field in policy_spec.POLICY_FIELDS
    )
    monkeypatch.setattr(policy_spec, "POLICY_FIELDS", patched)


def test_s4a_raises_when_threshold_min_drifts(monkeypatch):
    """S-4a②：阈值下界被改成非 0.0 ⇒ 规格表与「必须 > 0」的第二道校验分叉。

    ★ 这与设计文档的字面判据（`min_value > 0.0`）不同，理由见交付报告：
      出厂值就是 0.0，按字面实现会**在已验收基线上启动即失败**。真正的不变量是
      「两侧不许分叉」，见 `_s4a_threshold_spec_range` 的 docstring。
    """
    _with_threshold_field(monkeypatch, "relevance_threshold_vector", min_value=0.5)
    with pytest.raises(RuntimeError) as exc_info:
        self_check.check_scenario_constants()
    assert "relevance_threshold_vector" in str(exc_info.value)
    assert "S-4a" in str(exc_info.value)


def test_s4a_raises_when_threshold_max_out_of_domain(monkeypatch):
    """S-4a①：上界超出 (0, 1] ⇒ 允许了一个不可能被满足的阈值。"""
    _with_threshold_field(monkeypatch, "relevance_threshold_rerank", max_value=1.5)
    with pytest.raises(RuntimeError) as exc_info:
        self_check.check_scenario_constants()
    assert "relevance_threshold_rerank" in str(exc_info.value)


def test_s4a_raises_when_validator_stops_rejecting_zero(monkeypatch):
    """S-4a③：有人把「阈值必须 > 0」这道校验删了 ⇒ 自检必须炸。

    ★ 这条守的是「规格表下界 0.0 + 校验器拒 0」这对**有意的不对称**：
      规格表是闭区间写法，表达不了 strict > 0，所以严格性全靠校验器那一道。
      校验器一旦失守，前端按规格表填 0 就会静默生效（= 永不拒答）。
    """
    monkeypatch.setattr(policy_spec, "validate_policy", lambda policy: dict(policy or {}))
    with pytest.raises(RuntimeError) as exc_info:
        self_check.check_scenario_constants()
    assert "永不拒答" in str(exc_info.value)


def test_s6_scenario_placeholders_registered():
    """S-6 正向：两个场景站位值都已登记，且镜像值与真源一致。"""
    self_check.check_scenario_constants()
    names = {item.name for item in placeholders.for_owner("scenario")}
    assert names == {"RECALL_TOP_K_MAX", "SCENARIO_CACHE_TTL_SECONDS"}
    # 镜像 == 真源（import 期断言就是这条；这里再按值核对一次，防断言被删）
    from app.services.scenario import resolver

    assert placeholders.value_of("RECALL_TOP_K_MAX") == policy_spec.RECALL_TOP_K_MAX
    assert placeholders.value_of("SCENARIO_CACHE_TTL_SECONDS") == resolver.CACHE_TTL_SECONDS


def test_s6_raises_when_scenario_placeholders_missing(monkeypatch):
    """S-6 反向：登记表被清空 ⇒ RuntimeError（否则横幅少一行且无人发现）。"""
    monkeypatch.setattr(placeholders, "PLACEHOLDERS", ())
    with pytest.raises(RuntimeError) as exc_info:
        self_check.check_scenario_constants()
    assert "S-6" in str(exc_info.value)


def test_s6_raises_when_one_placeholder_missing(monkeypatch):
    """S-6：只漏一个也要炸（只查「非空」会漏掉单条漏登记）。"""
    kept = tuple(item for item in placeholders.PLACEHOLDERS if item.name != "SCENARIO_CACHE_TTL_SECONDS")
    monkeypatch.setattr(placeholders, "PLACEHOLDERS", kept)
    with pytest.raises(RuntimeError) as exc_info:
        self_check.check_scenario_constants()
    assert "SCENARIO_CACHE_TTL_SECONDS" in str(exc_info.value)


def test_placeholders_render_line_is_not_empty():
    """横幅那一行必须真的能渲染出内容（S-6 的可执行形态）。"""
    line = placeholders.render_line("scenario")
    assert line.startswith("含站位值参数:")
    assert "RECALL_TOP_K_MAX=200" in line
    assert "SCENARIO_CACHE_TTL_SECONDS=60.0" in line


# ══════════════════════════════════════════════════════════════════════
# T2：数据类（只 WARN + 计数，**永不 raise**）
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_clean_row_produces_all_zero(monkeypatch):
    """正向对照：一行干净的种子形态 ⇒ 八个判据全 0，且扫到 1 行。

    ★ 没有这条，所有「计数 == 1」的用例都可能因为「什么都命中」而假绿。
    """
    counts = await _scan([build_row(policy={}, dna_declared=_dna())])
    assert counts["scanned_versions"] == 1
    for key, value in counts.items():
        if key == "scanned_versions":
            continue
        assert value == 0, f"{key} 不该命中：{counts}"


@pytest.mark.asyncio
async def test_s2b_counts_recall_below_final_top_n(monkeypatch):
    """S-2b：`recall_top_k` 小于取用条数 ⇒ 计数 1，**且不抛**（数据类不拒绝启动）。"""
    from app.infra import metrics

    before = metrics.get("rag.scenario.recall_below_final_top_n")
    counts = await _scan([build_row(policy={"recall_top_k": 3}, dna_declared=_dna())])
    assert counts["recall_below_final_top_n"] == 1
    assert metrics.get("rag.scenario.recall_below_final_top_n") == before + 1
    # 其它判据不因这行被连带命中
    assert counts["threshold_out_of_range"] == 0


@pytest.mark.asyncio
async def test_s2b_uses_clamped_value(monkeypatch):
    """S-2b 判的是**裁后**的值：上限下调到 4 时，历史行 recall=6 不该再报 WARN。"""
    monkeypatch.setattr(policy_spec, "RECALL_TOP_K_MAX", 4)
    monkeypatch.setattr(decisions, "TOP_K_RERANK", 8)
    counts = await _scan([build_row(policy={"recall_top_k": 6}, dna_declared=_dna())])
    assert counts["recall_below_final_top_n"] == 1  # min(6,4)=4 < 8 → 仍命中

    monkeypatch.setattr(decisions, "TOP_K_RERANK", 3)
    counts2 = await _scan([build_row(policy={"recall_top_k": 6}, dna_declared=_dna())])
    assert counts2["recall_below_final_top_n"] == 0  # min(6,4)=4 >= 3 → 不命中


@pytest.mark.asyncio
async def test_s3_counts_rerank_open_but_platform_off(default_settings):
    """S-3：平台关 + 场景声明开 ⇒ 计数 1，且有效值仍是 False（单向 AND）。"""
    from app.infra import metrics
    from app.services.scenario.effective import effective_retrieval
    from app.services.scenario.snapshot import ScenarioPolicy

    default_settings(rerank_enabled=False)
    before = metrics.get("rag.scenario.rerank_open_but_platform_off")
    counts = await _scan(
        [build_row(policy={"rerank_enabled": True}, dna_declared=_dna())]
    )
    assert counts["rerank_open_but_platform_off"] == 1
    assert metrics.get("rag.scenario.rerank_open_but_platform_off") == before + 1
    eff = effective_retrieval(
        ScenarioPolicy(rerank_enabled=True), platform_rerank_enabled=False
    )
    assert eff.rerank_enabled is False


@pytest.mark.asyncio
async def test_s3_silent_when_platform_on(default_settings):
    """S-3 反向：平台开着就不该报（否则运维会习惯性忽略这条告警）。"""
    default_settings(rerank_enabled=True)
    counts = await _scan(
        [build_row(policy={"rerank_enabled": True}, dna_declared=_dna())]
    )
    assert counts["rerank_open_but_platform_off"] == 0


@pytest.mark.parametrize("bad", [0.0, 1.5, -0.1])
@pytest.mark.asyncio
async def test_s4b_counts_out_of_range_threshold(bad):
    """S-4b：阈值越界 ⇒ 计数 1（只可能来自绕过写入路径）。

    ★ 不 clamp：裁掉的错值裁不出对的判据（`0.0 → 0.01` 仍然是永不拒答）。
      这条由 `test_effective.py::test_threshold_out_of_range_is_NOT_clamped` 另钉一次。
    """
    counts = await _scan(
        [build_row(policy={"relevance_threshold_vector": bad}, dna_declared=_dna())]
    )
    assert counts["threshold_out_of_range"] == 1


@pytest.mark.asyncio
async def test_s4b_silent_for_valid_threshold():
    counts = await _scan(
        [build_row(policy={"relevance_threshold_vector": 0.62}, dna_declared=_dna())]
    )
    assert counts["threshold_out_of_range"] == 0


@pytest.mark.asyncio
async def test_s5_counts_rerank_threshold_when_platform_off(default_settings):
    """S-5：设了 rerank 阈值但平台关着 ⇒ 计数 1（该键现在**休眠**）。"""
    default_settings(rerank_enabled=False)
    counts = await _scan(
        [build_row(policy={"relevance_threshold_rerank": 0.71}, dna_declared=_dna())]
    )
    assert counts["rerank_threshold_uncalibrated"] == 1


@pytest.mark.asyncio
async def test_s5_silent_when_platform_on(default_settings):
    default_settings(rerank_enabled=True)
    counts = await _scan(
        [build_row(policy={"relevance_threshold_rerank": 0.71}, dna_declared=_dna())]
    )
    assert counts["rerank_threshold_uncalibrated"] == 0


@pytest.mark.asyncio
async def test_s7_counts_db_binding():
    """S-7：`db_binding` 已配但接线只到 Step 3 ⇒ 计数 1（配了不生效）。"""
    counts = await _scan(
        [build_row(db_binding="kb_hr", policy={}, dna_declared=_dna())], max_step=3
    )
    assert counts["db_binding_configured"] == 1


@pytest.mark.asyncio
async def test_s7_silent_after_step_six():
    """S-7 反向：接线到 Step 6 之后就不该再报（判据用的是 max_step，不是写死的常量）。"""
    counts = await _scan(
        [build_row(db_binding="kb_hr", policy={}, dna_declared=_dna())], max_step=6
    )
    assert counts["db_binding_configured"] == 0


@pytest.mark.asyncio
async def test_s8_detects_builtin_policy_not_empty():
    """S-8：内置场景的 policy 非空 ⇒ 计数 1（只可能来自直写 SQL）。"""
    counts = await _scan(
        [build_row(is_builtin=True, policy={"recall_top_k": 9}, dna_declared=_dna())]
    )
    assert counts["builtin_policy_not_empty"] == 1


@pytest.mark.asyncio
async def test_s8_ignores_non_builtin_policy():
    """S-8 反向：非内置场景配了 policy 是**正常**的，不该报。"""
    counts = await _scan(
        [build_row(is_builtin=False, binding_mode="unrestricted",
                   policy={"recall_top_k": 9}, dna_declared=_dna())]
    )
    assert counts["builtin_policy_not_empty"] == 0


@pytest.mark.asyncio
async def test_s9_detects_dna_drift():
    """S-9：内置行的 dna_declared 与当前部署口径不一致 ⇒ 计数 1。"""
    counts = await _scan(
        [build_row(is_builtin=True, policy={},
                   dna_declared=_dna(embedding_text_version="etext-evil"))]
    )
    assert counts["dna_declared_drift"] == 1


@pytest.mark.asyncio
async def test_s9_clean_seed_does_not_drift():
    """S-9 正向：迁移写死的 4 个常量与当前 profile 一致 ⇒ 不报。"""
    counts = await _scan([build_row(is_builtin=True, policy={}, dna_declared=_dna())])
    assert counts["dna_declared_drift"] == 0


@pytest.mark.asyncio
async def test_s9_expands_env_bound_names_to_current_deployment():
    """S-9 的「展开」语义：`env_bound` 只声明**字段名**，不落值 ⇒ 不参与漂移判定。

    ★ 若 `env_bound` 的字段名在 dna_declared 里带了具体值，就与当前部署比对 ——
      这正是「声明过期」的可能来源（迁移里 lock 了一个模型名，换模型后没人改）。
    """
    # 只声明字段名（种子形态）→ 不报
    assert (await _scan([build_row(policy={}, dna_declared=_dna())]))["dna_declared_drift"] == 0

    # 带了一个与当前部署不同的具体值 → 报
    from app.config.index_profile import current_index_profile

    stale = _dna(embedding_model="text-embedding-v1-ancient")
    assert stale["embedding_model"] != current_index_profile().embedding_model
    counts = await _scan([build_row(policy={}, dna_declared=stale)])
    assert counts["dna_declared_drift"] == 1


@pytest.mark.asyncio
async def test_data_check_survives_unparsable_row():
    """一行坏数据不许让自检崩，也不许静默跳过（计入 `selfcheck_unparsable`）。"""
    counts = await _scan(
        [build_row(policy={"recall_top_k": "abc"}, dna_declared=_dna())]
    )
    assert counts["selfcheck_unparsable"] == 1


@pytest.mark.asyncio
async def test_bad_dna_declared_never_raises(bad_dna):  # noqa: D401
    """`dna_declared` 不是 JSON 对象 ⇒ **不抛**、计入 unparsable、S-9 跳过。

    ★ 这是 P0-3 钉的那条：jsonb 的 `jsonb_typeof(dna_declared)='object'` CHECK 只
      挡正常写入；旧库 / 恢复库 / 约束被绕过时，`dict(['not-a-mapping'])` 抛
      ValueError、`dict([1,2])` 抛 TypeError，会一路冒到 lifespan ——
      **一行坏数据让整个实例起不来**，正是设计文档要用 WARN 分流来避免的死锁。
    ★ 三种坏值分别覆盖 str / list[str] / list[int]：前两者 ValueError、后者 TypeError。
    ★ 断言 `dna_declared_drift == 0`：跳过不等于「算了个漂移出来」。
    """
    row = build_row(dna_declared=_dna())
    row["dna_declared"] = bad_dna  # 绕过 conftest 的 dict(...) 包装，模拟库里的脏值

    counts = await _scan([row])

    assert counts["selfcheck_unparsable"] == 1
    assert counts["dna_declared_drift"] == 0
    assert counts["scanned_versions"] == 1


@pytest.mark.asyncio
async def test_bad_dna_declared_does_not_stop_scanning():
    """同一行的其它判据必须照跑；后面的行也必须照扫。

    ★ 这条是「字段级兜底」与「整行一把兜」的**分界线**：若把坏 dna 交给最外层
      的 `except Exception` 处理，整行都会被跳过 —— 那么一个 dna 脏值就顺带让
      S-2b/S-3/S-4b/S-5/S-7/S-8 全部失明。断言「坏 dna 的行仍然报 S-2b」才能
      锁住「只跳过 S-9、其余判据照跑」这个语义。
    """
    bad = build_row(scenario_id=uuid.uuid4(), scenario_key="bad",
                    policy={"recall_top_k": 3})  # 3 < TOP_K_RERANK=8 → S-2b 命
    bad["dna_declared"] = ["not-a-mapping"]
    good = build_row(
        scenario_id=uuid.uuid4(), scenario_key="good",
        policy={"recall_top_k": 4},  # 同样触发 S-2b
    )

    counts = await _scan([bad, good])

    assert counts["selfcheck_unparsable"] == 1
    assert counts["recall_below_final_top_n"] == 2, (
        "两行都配了 recall_top_k < 取用条数：坏 dna 那行的 S-2b 也必须照跑"
        "（整行一把兜会漏成 1）"
    )
    assert counts["scanned_versions"] == 2


@pytest.mark.asyncio
async def test_unexpected_check_exception_is_contained(monkeypatch):
    """最后一道网：任一判据抛出意料之外的异常，也只让**这一行**计入 unparsable。

    ★ 契约是「永不 raise」，不能只靠每个字段各自 try 一圈 —— 将来新增判据读了
      别的列、而那列出现意外类型时，这里保证实例仍然起得来。
    """
    def _boom(*args, **kwargs):  # noqa: ANN001, ARG001
        raise RuntimeError("模拟意料之外的判据异常")

    monkeypatch.setattr(self_check, "_check_builtin_policy", _boom)

    counts = await _scan([build_row(policy={}, dna_declared=_dna())])

    assert counts["selfcheck_unparsable"] == 1
    assert counts["scanned_versions"] == 1


@pytest.mark.asyncio
async def test_data_check_counts_scan_size():
    """`scanned_versions` 让「全绿」与「一行都没扫到」可区分。

    ★ 没有它，8 个 0 既可能是「配置没问题」，也可能是「自检没生效」。
    """
    empty = await _scan([])
    assert empty["scanned_versions"] == 0
    assert set(empty) == set(self_check._EMPTY_COUNTS)

    two = await _scan([
        build_row(policy={}, dna_declared=_dna()),
        build_row(scenario_id=__import__("uuid").uuid4(), policy={}, dna_declared=_dna()),
    ])
    assert two["scanned_versions"] == 2


@pytest.mark.asyncio
async def test_data_check_query_failure_is_not_green(monkeypatch):
    """查询失败 ⇒ 单独计 `selfcheck_query_failed`，**不能**伪装成「全 0 = 全绿」。"""

    class _Broken:
        async def execute(self, stmt):  # noqa: ANN001, ARG002
            raise RuntimeError("relation \"scenarios\" does not exist")

    counts = await self_check.check_scenario_data(_Broken(), max_step=3)
    assert counts["selfcheck_query_failed"] == 1
    assert counts["scanned_versions"] == 0


@pytest.mark.asyncio
async def test_data_check_never_raises_across_all_violations(default_settings):
    """**全部判据同时违反**也不许抛（这是「数据类不拒绝启动」的总证据）。"""
    default_settings(rerank_enabled=False)
    row = build_row(
        is_builtin=True,
        db_binding="kb_hr",
        policy={
            "recall_top_k": 3,
            "rerank_enabled": True,
            "relevance_threshold_vector": 1.5,
            "relevance_threshold_rerank": 0.71,
        },
        dna_declared=_dna(chunker_version="c-changed"),
    )
    counts = await _scan([row])
    hits = {k: v for k, v in counts.items() if v and k != "scanned_versions"}
    assert set(hits) == {
        "recall_below_final_top_n",
        "rerank_open_but_platform_off",
        "threshold_out_of_range",
        "rerank_threshold_uncalibrated",
        "db_binding_configured",
        "builtin_policy_not_empty",
        "dna_declared_drift",
    }, hits


# ══════════════════════════════════════════════════════════════════════
# Step 7：扫描口径随治理开关分两套 + S-10（hot 声明不生效）
# ══════════════════════════════════════════════════════════════════════


def _row_with_tier(tier: str, **kwargs) -> dict:
    """`build_row` 之外补一个 `tier` 键（`ScenarioRow.tier` 读的就是它）。"""
    row = build_row(**kwargs)
    row["tier"] = tier
    return row


def _last_sql(session: FakeSession) -> str:
    assert session.statements, "自检没有执行任何 SQL"
    return str(session.statements[-1])


@pytest.mark.parametrize("tier, expected", [("hot", 1), ("warm", 0), ("cold", 0)])
@pytest.mark.asyncio
async def test_s10_counts_hot_declared_but_unsupported(tier, expected, caplog):
    """S-10：`tier='hot'` ⇒ 计数 1 + WARN（hot 是保留枚举，本期没有实例注册表）。

    ★ warm / cold 必须为 0：否则「一律命中」的实现也能过，等于没测。
    """
    caplog.set_level(logging.WARNING, logger="app.services.scenario.self_check")
    key = f"sc_{tier}"

    counts = await _scan([_row_with_tier(tier, scenario_key=key, policy={}, dna_declared=_dna())])

    assert counts["tier_hot_unsupported"] == expected
    hot_records = [
        record
        for record in caplog.records
        if record.name == "app.services.scenario.self_check"
        and getattr(record, "tier", None) == "hot"
    ]
    if expected:
        assert hot_records, "tier=hot 必须留下 WARN（配了不生效不能静默）"
        assert hot_records[0].scenario_key == key
        assert "不会生效" in hot_records[0].getMessage()
        assert "n_hot_declared" in hot_records[0].getMessage()
    else:
        assert not hot_records, f"tier={tier} 不该报 hot 告警"


@pytest.mark.asyncio
async def test_s10_fires_even_when_policy_is_broken():
    """S-10 必须在 `to_policy()` **之前**执行：policy 坏行也要能报 hot。

    ★ 这条守的是调用位置，不是判据本身：把 `_check_tier_hot` 挪到 `to_policy()`
      之后，坏行会先 return，hot 声明就永远沉默。
    """
    row = _row_with_tier("hot", policy={"recall_top_k": "abc"}, dna_declared=_dna())

    counts = await _scan([row])

    assert counts["tier_hot_unsupported"] == 1
    assert counts["selfcheck_unparsable"] == 1


@pytest.mark.asyncio
async def test_s10_reads_tier_even_when_governance_on(monkeypatch):
    """tier 是物理列：治理开启时也必须读出（`_row_from` 不按开关裁剪 tier）。

    ★ 反向守护：关闭态用例对「只在 governed=False 时读 tier」同样会绿，
      只有本用例能抓住「把 tier 也塞进 governed 分支」的回归（承诺 3 的双向性）。
    """
    monkeypatch.setattr(
        self_check,
        "get_settings",
        lambda: SimpleNamespace(tenant_governance_enabled=True, rerank_enabled=False),
    )

    counts = await _scan([_row_with_tier("hot", policy={}, dna_declared=_dna())])

    assert counts["tier_hot_unsupported"] == 1
    assert counts["scanned_versions"] == 1


@pytest.mark.asyncio
async def test_governance_off_scan_sql_is_published_only():
    """治理关闭：扫描 SQL 不含钉定列，但必须选 `s.tier`（S-10 的数据来源）。"""
    session = FakeSession([build_row(policy={}, dna_declared=_dna())])

    counts = await self_check.check_scenario_data(session, max_step=3)

    sql = _last_sql(session)
    assert "pinned_version" not in sql
    assert "'archived'" not in sql
    assert "s.tier" in sql
    assert counts["scanned_versions"] == 1


@pytest.mark.asyncio
async def test_governance_on_scan_sql_uses_pinned_rule(monkeypatch):
    """治理开启：扫描 SQL 同时含钉定列、archived 分支与四项 caps 列。"""
    monkeypatch.setattr(
        self_check,
        "get_settings",
        lambda: SimpleNamespace(tenant_governance_enabled=True, rerank_enabled=False),
    )
    session = FakeSession([build_row(policy={}, dna_declared=_dna())])

    await self_check.check_scenario_data(session, max_step=3)

    sql = _last_sql(session)
    assert "pinned_version" in sql
    assert "'archived'" in sql
    for column in (
        "recall_top_k_max",
        "threshold_vector_min",
        "threshold_rerank_min",
        "rerank_allowed",
    ):
        assert column in sql, f"开启态扫描必须选 {column}"


@pytest.mark.asyncio
async def test_governance_on_folds_archived_pinned_row(monkeypatch):
    """钉定到归档版本的行必须能正常折叠并被扫描（不自检成 unparsable）。"""
    monkeypatch.setattr(
        self_check,
        "get_settings",
        lambda: SimpleNamespace(tenant_governance_enabled=True, rerank_enabled=False),
    )
    row = build_row(policy={}, dna_declared=_dna())
    row.update(
        {
            "pinned_version": 1,
            "version_state": "archived",
            "recall_top_k_max": 50,
            "threshold_vector_min": 0.42,
            "threshold_rerank_min": 0.51,
            "rerank_allowed": False,
        }
    )
    session = FakeSession([row])

    folded = await self_check._load_all_for_scan(session)

    assert len(folded) == 1
    assert folded[0].version_state == "archived"
    assert folded[0].pinned_version == 1
    caps = folded[0].to_caps()
    assert caps.recall_top_k_max == 50
    assert caps.threshold_vector_min == 0.42
    assert caps.threshold_rerank_min == 0.51
    assert caps.rerank_allowed is False

    counts = await self_check.check_scenario_data(session, max_step=3)
    assert counts["scanned_versions"] == 1
    assert counts["selfcheck_unparsable"] == 0


@pytest.mark.asyncio
async def test_governance_off_ignores_governance_columns_even_if_fed():
    """开关兜底不被 SQL 选择绕过：关闭态即便喂来钉定/caps 键也必须全部忽略。"""
    row = build_row(policy={}, dna_declared=_dna())
    row.update(
        {
            "pinned_version": 3,
            "version_state": "archived",
            "recall_top_k_max": 5,
            "threshold_vector_min": 0.9,
            "threshold_rerank_min": 0.9,
            "rerank_allowed": True,
        }
    )

    folded = await self_check._load_all_for_scan(FakeSession([row]))

    assert folded[0].version_state == "published"
    assert folded[0].pinned_version is None
    caps = folded[0].to_caps()
    assert caps.recall_top_k_max is None
    assert caps.threshold_vector_min is None
    assert caps.threshold_rerank_min is None
    assert caps.rerank_allowed is None


# ══════════════════════════════════════════════════════════════════════
# T4：发布期拒绝（唯一能真正阻止坏配置进系统的位置）
# ══════════════════════════════════════════════════════════════════════


def test_publishable_accepts_valid_policy():
    """正向对照：合法的覆盖集原样通过（返回可落库 dict）。"""
    checked = self_check.check_policy_publishable(
        {"recall_top_k": 20, "relevance_threshold_vector": 0.62},
        platform_rerank_enabled=False,
    )
    assert checked == {"recall_top_k": 20, "relevance_threshold_vector": 0.62}


def test_publishable_accepts_empty_policy():
    assert self_check.check_policy_publishable({}, platform_rerank_enabled=False) == {}


def test_publishable_rejects_recall_below_final_top_n():
    """T4：把 S-2b 的硬拦提前到**写入期**（坏配置根本进不了 published）。"""
    with pytest.raises(policy_spec.PolicyValidationError) as exc_info:
        self_check.check_policy_publishable(
            {"recall_top_k": 3}, platform_rerank_enabled=False
        )
    assert exc_info.value.field == "recall_top_k"
    assert str(decisions.TOP_K_RERANK) in str(exc_info.value)


def test_publishable_rejects_rerank_when_platform_off():
    """T4：把「静默无效」提前到写入期（平台关时不许写 rerank_enabled=true）。"""
    with pytest.raises(policy_spec.PolicyValidationError) as exc_info:
        self_check.check_policy_publishable(
            {"rerank_enabled": True}, platform_rerank_enabled=False
        )
    assert exc_info.value.field == "rerank_enabled"
    assert "RERANK_ENABLED=false" in str(exc_info.value)


def test_publishable_allows_rerank_when_platform_on():
    """正向对照：平台开着就允许写（否则这个键永远写不进去）。"""
    checked = self_check.check_policy_publishable(
        {"rerank_enabled": True}, platform_rerank_enabled=True
    )
    assert checked == {"rerank_enabled": True}


def test_publishable_reuses_validate_policy():
    """T4 MUST 复用 `validate_policy`，不重写判据（两份判据必然分叉）。"""
    for bad in ({"final_top_n": 3}, {"recall_top_k": 0}, {"rerank_enabled": "true"}):
        with pytest.raises(policy_spec.PolicyValidationError):
            self_check.check_policy_publishable(bad, platform_rerank_enabled=True)


# ══════════════════════════════════════════════════════════════════════
# 调用点：自检真的被启动流程调用了吗（AST，不执行启动）
# ══════════════════════════════════════════════════════════════════════


def _called_names(path: pathlib.Path) -> set[str]:
    """抽出文件里所有「被调用的函数名」（`f(...)` 形式的 Name 调用）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def test_lifespan_calls_both_checks():
    """api 侧 MUST 调 T1 + T2（T2 只在 api 侧跑）。

    ★ 为什么用 AST 而不是跑 `TestClient(app)`：后者会真的连 DB / 起依赖。
      AST 断言证明的是「调用点存在」，这与「判据存在」是两件事，缺一不可 ——
      自检函数写得再对，没人调它等于没有。
    """
    called = _called_names(_API_ROOT / "app" / "main.py")
    assert "check_scenario_constants" in called
    assert "check_scenario_data" in called


def test_worker_calls_only_constants_check():
    """worker 侧 MUST 只调 T1：它不检索，T2 的全场景扫描是纯加成本。"""
    called = _called_names(_API_ROOT / "app" / "workers" / "parse_job.py")
    assert "check_scenario_constants" in called
    assert "check_scenario_data" not in called


def test_lifespan_passes_wired_step_not_a_literal():
    """启动期 `max_step` MUST 传 `WIRED_RETRIEVAL_STEP`，不许写死常量值。

    ★ 为什么较真这个参数：`max_step` 决定 S-7（`db_binding` 配了不生效）是否看得见。
      写成 `max_step=6` 会让 S-7 **永久失明** —— 而它恰恰是唯一会喊出「你配了
      db_binding、但现在还不生效」的判据，失明的后果是运维以为配好了。
      验证者实测：把 `main.py` 里那行改成 `max_step=6`，177 条测试全绿。
    ★ 断言三层：① 关键字名是 `max_step`；② 值是对 `WIRED_RETRIEVAL_STEP` 的引用
      （不是字面量 int）；③ 该常量当前确实是 3（改常量必须显式改这里，不能顺手漂）。
    """
    tree = ast.parse((_API_ROOT / "app" / "main.py").read_text(encoding="utf-8"))

    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "check_scenario_data"
    ]
    assert calls, "lifespan 里找不到 check_scenario_data 调用"

    kwargs = {kw.arg: kw.value for kw in calls[0].keywords}
    assert "max_step" in kwargs, "check_scenario_data MUST 显式传 max_step"
    assert isinstance(kwargs["max_step"], ast.Name), (
        "max_step 不许写字面量（写死 6 会让 S-7 永久失明）"
    )
    assert kwargs["max_step"].id == "WIRED_RETRIEVAL_STEP"

    from app.services.scenario.effective import WIRED_RETRIEVAL_STEP

    assert WIRED_RETRIEVAL_STEP == 3, "检索接线进度常量变了，本用例必须同步改（不许静默漂）"


def test_scan_sql_columns_match_migration_and_lookup():
    """两套扫描 SQL 的每一列都必须在迁移里真实存在，且与对应的运行时 SQL 对齐。

    ★ 为什么需要静态守护：这条 SQL **从未在真库上跑过**（自检的测试全用 FakeSession）。
      列名写错（例如 `v.checksum` → `v.checksum_xx`）的唯一表现是
      `selfcheck_query_failed=1` —— 也就是 T2 **静默失明**：判据全部不报，
      日志里只有一条「查询失败」，运维很容易当成临时抖动。
    ★ 三层对账：
      ① 关闭态扫描 = `LOOKUP_SQL` 的列 + 自检专用的 `tier`（运行时解析不读 tier）；
      ② 开启态扫描 = `LOOKUP_GOVERNED_SQL` 的列 + `tier`；
      ③ 两处「钉定 / 归档」的版本选择条件必须**逐字同规则** —— 否则自检会漏扫
         「钉定到归档版本、请求期能正常解析」的场景（本判据存在的理由）。
    ★ 列来源必须同时解析 0028 与 0031：`scenarios.tier` 是 0031 加的，只解析 0028
      会把一条合法列误判成「迁移里不存在」。
    """
    from app.services.scenario import resolver

    columns = _migration_columns()

    def _columns_of(sql: str) -> dict[str, str]:
        """{输出别名: 列名}，并校验 `alias.column AS out` 里的 alias 已声明。"""
        aliases = dict(
            (alias, table) for table, alias in re.findall(r"(?:FROM|JOIN)\s+(\w+)\s+(\w+)", sql)
        )
        assert aliases, f"没解析出表别名：{sql[:60]}"
        out: dict[str, str] = {}
        for alias, column, name in re.findall(r"(\w+)\.(\w+)\s+AS\s+(\w+)", sql):
            assert alias in aliases, f"未知别名 {alias}（正文：{sql[:60]}）"
            table = aliases[alias]
            assert column in columns[table], (
                f"{table}.{column} 在迁移里不存在 —— SQL 只会在真库上失败，"
                f"表现为 selfcheck_query_failed=1（T2 静默失明）"
            )
            out[name] = column
        return out

    scan_closed = _columns_of(self_check.SCAN_SQL)
    lookup_closed = _columns_of(resolver.LOOKUP_SQL)
    assert scan_closed == {**lookup_closed, "tier": "tier"}, (
        "关闭态 SCAN_SQL 必须是 LOOKUP_SQL 的列 + 自检专用的 tier\n"
        f"只在 SCAN：{set(scan_closed) - set(lookup_closed)}\n"
        f"只在 LOOKUP：{set(lookup_closed) - set(scan_closed)}\n"
        f"列不同：{ {k: (scan_closed[k], lookup_closed[k]) for k in set(scan_closed) & set(lookup_closed) if scan_closed[k] != lookup_closed[k]} }"
    )

    scan_governed = _columns_of(self_check.SCAN_GOVERNED_SQL)
    lookup_governed = _columns_of(resolver.LOOKUP_GOVERNED_SQL)
    assert scan_governed == {**lookup_governed, "tier": "tier"}, (
        "开启态 SCAN_GOVERNED_SQL 必须是 LOOKUP_GOVERNED_SQL 的列 + 自检专用的 tier\n"
        f"只在 SCAN：{set(scan_governed) - set(lookup_governed)}\n"
        f"只在 LOOKUP：{set(lookup_governed) - set(scan_governed)}\n"
        f"列不同：{ {k: (scan_governed[k], lookup_governed[k]) for k in set(scan_governed) & set(lookup_governed) if scan_governed[k] != lookup_governed[k]} }"
    )

    # ★ 版本选择「逐字同规则」：自检不得自创第三种口径
    version_rule = (
        "(s.pinned_version IS NULL AND v.state = 'published') "
        "OR (s.pinned_version IS NOT NULL AND v.version = s.pinned_version "
        "AND v.state IN ('published','archived'))"
    )
    assert version_rule in " ".join(self_check.SCAN_GOVERNED_SQL.split())
    assert version_rule in " ".join(resolver.LOOKUP_GOVERNED_SQL.split())


def _migration_columns() -> dict[str, set[str]]:
    """0028 + 0031 里两张场景表的真实列。

    0028 用 `sa.Column` 建表，0031 用 `ALTER TABLE ... ADD COLUMN` 字符串加列 ——
    两种形态都要解析，否则 0031 的列会被误判成不存在。
    """
    columns: dict[str, set[str]] = {}
    for path in (
        _API_ROOT / "alembic" / "versions" / "0028_add_scenarios.py",
        _API_ROOT / "alembic" / "versions" / "0031_step7_tenant_governance.py",
    ):
        source = path.read_text(encoding="utf-8")
        for table, column in re.findall(r"ALTER TABLE (\w+) ADD COLUMN (\w+)", source):
            columns.setdefault(table, set()).add(column)
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            fname = getattr(node.func, "attr", None)
            if fname not in ("create_table", "add_column"):
                continue
            args = node.args
            if fname == "create_table":
                table = _const_str(args[0])
                if table is None:
                    continue
                cols = columns.setdefault(table, set())
                # create_table 里一般隐式带 id 主键，但本迁移是显式 sa.Column("id", ...)
                for extra in args[1:]:
                    name = _column_name(extra)
                    if name:
                        cols.add(name)
            else:  # add_column
                table = _const_str(args[0])
                name = _column_name(args[1]) if len(args) > 1 else None
                if table and name:
                    columns.setdefault(table, set()).add(name)
    assert "scenarios" in columns and "scenario_versions" in columns, columns
    return columns


def _const_str(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _column_name(node: ast.AST) -> str | None:
    """`sa.Column("name", ...)` → "name"（只认字符串字面量列名）。"""
    if not isinstance(node, ast.Call) or getattr(node.func, "attr", None) != "Column":
        return None
    if not node.args:
        return None
    return _const_str(node.args[0])


@pytest.mark.asyncio
async def test_s2b_judges_the_clamped_value(monkeypatch):
    """S-2b MUST 按**裁后**值判（`min(effective, RECALL_TOP_K_MAX)`）。

    ★ 这组数（recall=6 / 平台上限=4 / 取用条数=5）是刻意构造的分歧点：
      · 裁后 4 < 5 → 命中（现网判 1）
      · 不裁 6 < 5 → 不命中（去掉 `min(...)` 判 0）
      既有用例（7 / 0 …）在两种实现下结果相同，抓不到这处。
    ★ 为什么必须按裁后判：`effective.py` 会在取值层把超限值裁到上限；按原值判会
      让「平台上限下调后的所有历史行」全部报 WARN —— 噪音把真问题埋掉。
    """
    monkeypatch.setattr(policy_spec, "RECALL_TOP_K_MAX", 4)  # 低于取用条数 8
    monkeypatch.setattr(decisions, "TOP_K_RERANK", 5)

    counts = await _scan([build_row(policy={"recall_top_k": 6}, dna_declared=_dna())])

    assert counts["recall_below_final_top_n"] == 1, (
        "6 被裁到 4，4 < 5 ⇒ 必须命中；不裁则 6 < 5 不成立 ⇒ 0（这是分歧点）"
    )


@pytest.mark.asyncio
async def test_s2b_silent_when_clamped_value_is_enough(monkeypatch):
    """反向对照：裁后值够大时不报（避免「一律命中」的假实现也能过）。"""
    monkeypatch.setattr(policy_spec, "RECALL_TOP_K_MAX", 20)
    monkeypatch.setattr(decisions, "TOP_K_RERANK", 5)

    counts = await _scan([build_row(policy={"recall_top_k": 6}, dna_declared=_dna())])

    assert counts["recall_below_final_top_n"] == 0


@pytest.mark.asyncio
async def test_warn_payload_policy(caplog):
    """WARN 载荷口径（P3-3）：**允许数值**、**禁止业务数据** —— 与 docstring 逐条对齐。

    ★ 这是一条「注释说真话」的测试。原名文档写「输出的诊断信息只含 tenant_id /
      scenario_key / 字段名，不含 policy 其它业务值」，但实现里 S-4b 记了阈值数值、
      S-5 记了阈值、S-9 记了 drift 的新旧值 —— 注释在说谎。
      两条路只能选一条：收紧日志（运维看到「阈值越界」却不知道是哪个键什么值，
      只能去翻库）或改注释。这里选**改注释 + 本用例锁住实际口径**，理由：
      policy 的四个键都是非敏感数值型配置，数值的敏感度远低于「告警无法定位」的成本。
    ★ 真正必须禁止的是业务数据（kb_ids / 文档内容 / 用户标识）—— 那才是跨租户泄露。
    """
    caplog.set_level(logging.WARNING, logger="app.services.scenario.self_check")

    counts = await _scan([
        build_row(policy={"relevance_threshold_vector": 1.5}, dna_declared=_dna()),
        build_row(scenario_id=uuid.uuid4(), scenario_key="other",
                  policy={"relevance_threshold_rerank": 0.71}, dna_declared=_dna()),
    ])
    assert counts["threshold_out_of_range"] == 1

    records = [r for r in caplog.records if r.name == "app.services.scenario.self_check"]
    assert records, "越界必须留下 WARN（否则就是静默）"

    # ① 数值必须出现：否则告警无法定位
    s4b = [r for r in records if hasattr(r, "fields")]
    assert s4b, "S-4b 的 WARN 必须带 fields"
    assert 1.5 in s4b[0].fields.values(), "必须带上越界的**值**，否则运维只能翻库"

    # ② 业务数据必须一个都没有
    banned = {"kb_ids", "policy", "documents", "content", "question", "user_id"}
    for record in records:
        leaked = banned & set(record.__dict__)
        assert not leaked, f"日志 extra 出现禁止字段：{leaked}"


def test_banner_follows_placeholder_registry(monkeypatch):
    """横幅那行 MUST 由登记表**现算**，不是硬编码串。

    ★ 变异证据：把 `placeholders.render_line` 改成 `return "含站位值参数: ..."` 固定串，
      只断言「含 RECALL_TOP_K_MAX=200」的老用例照样绿（固定串恰好命中）。
      本用例改登记表内容 / 清空登记表，两种做法都会让固定串露馅。
    """
    from app.config import capabilities

    def _render() -> str:
        return capabilities._render_text(
            role="api", app_env="local", fingerprint="f" * 12,
            graph_enabled=False, wiki_enabled=False, stats=None,
        )

    tweaked = tuple(
        dataclasses.replace(item, value=137) if item.name == "RECALL_TOP_K_MAX" else item
        for item in placeholders.PLACEHOLDERS
    )
    monkeypatch.setattr(placeholders, "PLACEHOLDERS", tweaked)
    text = _render()
    assert "RECALL_TOP_K_MAX=137" in text, "改登记表的值 ⇒ 横幅必须跟着变"
    assert "RECALL_TOP_K_MAX=200" not in text

    # 清空 scenario 组 ⇒ 整行消失（`if _placeholder_line:` 分支的另一半）
    others = tuple(item for item in placeholders.PLACEHOLDERS if item.owner != "scenario")
    monkeypatch.setattr(placeholders, "PLACEHOLDERS", others)
    assert "含站位值参数" not in _render()


def test_banner_renders_placeholder_line():
    """横幅 MUST 打印站位值一行（S-6 的唯一可见出口）。"""
    from app.config.capabilities import _render_text

    text = _render_text(
        role="api", app_env="local", fingerprint="f" * 12,
        graph_enabled=False, wiki_enabled=False, stats=None,
    )
    assert "含站位值参数" in text
    assert "RECALL_TOP_K_MAX=200" in text
