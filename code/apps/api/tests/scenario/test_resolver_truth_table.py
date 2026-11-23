"""F.2（NO-DB 层）：§C.3.1 六行解析真值表 R1–R6 + 求值顺序 + fail-closed。

全部经 `resolve_scenario()` 走真实调用路径（假 Session 只实现 `execute → all`），
因此这里验证的不只是 `build_snapshot`，而是**入口函数**的行为。
"""
from __future__ import annotations

import dataclasses

import pytest

from app.services.scenario import ScenarioCode, ScenarioError, resolve_scenario
from app.services.scenario.snapshot import ScenarioSnapshot
from tests.scenario.conftest import (
    KB_A,
    KB_B,
    OTHER_TENANT_ID,
    SCENARIO_ID,
    TENANT_ID,
    FakeSession,
    build_row,
    make_application,
)

pytestmark = pytest.mark.asyncio


def _identity_and_source(snapshot: ScenarioSnapshot) -> dict:
    """取出「除 scenario_id / source 之外」的全部字段（R5 与 R6 的等价性比较用）。"""
    return {
        field.name: getattr(snapshot, field.name)
        for field in dataclasses.fields(snapshot)
        if field.name not in {"scenario_id", "source"}
    }


# ── R1：绑定了场景却解析不出 → 500，绝不退回默认 ──────────────────────


async def test_truth_table_r1_referenced_scenario_missing_fails_closed():
    app = make_application(scenario_id=SCENARIO_ID)
    with pytest.raises(ScenarioError) as exc_info:
        await resolve_scenario(
            FakeSession([]), tenant_id=TENANT_ID, application=app,
            requested_kb_ids=[KB_A],
        )
    error = exc_info.value
    assert error.code is ScenarioCode.UNRESOLVABLE
    assert error.http_status == 500
    assert error.message == "场景配置不可解析"
    # diagnostic 只进日志：带内部 ID，但不进响应文案
    assert error.diagnostic["scenario_id"] == str(SCENARIO_ID)
    assert str(SCENARIO_ID) not in error.message


async def test_missing_published_version_fails_closed():
    """场景行存在但没有 published 版本 ⇒ JOIN 出 0 行 ⇒ R1（不是「未配置」）。"""
    app = make_application(scenario_id=SCENARIO_ID)
    # 只有 draft 版：JOIN 条件 v.state='published' 不成立 → 查询结果为空
    with pytest.raises(ScenarioError) as exc_info:
        await resolve_scenario(
            FakeSession([]), tenant_id=TENANT_ID, application=app, requested_kb_ids=[KB_A]
        )
    assert exc_info.value.code is ScenarioCode.UNRESOLVABLE


async def test_cross_tenant_scenario_unresolvable():
    """指向另一租户的场景：SQL 的 tenant 条件查不到 → R1。

    另加一层纯函数兜底（万一 SQL 被改坏）：行不属于本租户也判 R1。
    """
    app = make_application(scenario_id=SCENARIO_ID)
    foreign = build_row(tenant_id=OTHER_TENANT_ID, scenario_id=SCENARIO_ID)
    with pytest.raises(ScenarioError) as exc_info:
        await resolve_scenario(
            FakeSession([foreign]), tenant_id=TENANT_ID, application=app,
            requested_kb_ids=[KB_A],
        )
    assert exc_info.value.code is ScenarioCode.UNRESOLVABLE


async def test_cross_tenant_builtin_row_unresolvable():
    """内置分支查出别租户的内置行 = 数据损坏（SQL 已过滤，纯函数再兜一次）。"""
    app = make_application(scenario_id=None)
    foreign_builtin = build_row(tenant_id=OTHER_TENANT_ID, is_builtin=True)
    with pytest.raises(ScenarioError) as exc_info:
        await resolve_scenario(
            FakeSession([foreign_builtin]), tenant_id=TENANT_ID, application=app,
            requested_kb_ids=[KB_A],
        )
    assert exc_info.value.code is ScenarioCode.UNRESOLVABLE


# ── R2：空请求集合 → 不做场景门禁，交由既有三重交集得空集 → 403 ────────


async def test_truth_table_r2_empty_request_skips_scenario_gate():
    """空 `kb_ids` 时**不判 status**：否则可用空请求探测停用场景的边界（400 vs 403）。"""
    app = make_application(scenario_id=SCENARIO_ID)
    disabled = build_row(status="disabled", binding_mode="explicit", kb_ids=(str(KB_A),))
    snapshot = await resolve_scenario(
        FakeSession([disabled]), tenant_id=TENANT_ID, application=app, requested_kb_ids=[]
    )
    assert snapshot.kb_ids == ()
    # 空请求下两个分支结局相同：内置（unrestricted）与非内置（explicit）都是空集
    builtin_snapshot = await resolve_scenario(
        FakeSession([]), tenant_id=TENANT_ID, application=make_application(), requested_kb_ids=[]
    )
    assert builtin_snapshot.kb_ids == ()


# ── R3：status 先于范围判断 ────────────────────────────────────────────


async def test_truth_table_r3_disabled_scenario_not_allowed():
    app = make_application(scenario_id=SCENARIO_ID)
    row = build_row(status="disabled", is_builtin=False, binding_mode="explicit",
                    kb_ids=(str(KB_A),))
    with pytest.raises(ScenarioError) as exc_info:
        await resolve_scenario(
            FakeSession([row]), tenant_id=TENANT_ID, application=app, requested_kb_ids=[KB_A]
        )
    error = exc_info.value
    assert error.code is ScenarioCode.NOT_ALLOWED
    assert error.http_status == 400
    assert error.message == "场景不可用"
    # 不区分「停用」与「草稿」：细分即信息泄露
    assert "disabled" not in error.message
    assert "draft" not in error.message


async def test_status_checked_before_scope():
    """停用场景 + 必然 mismatch 的 K_r ⇒ 必须是 NOT_ALLOWED 而不是 MISMATCH。

    若先判范围，攻击者可用「提交一个必然 mismatch 的 kb_ids」探测停用场景的边界。
    """
    app = make_application(scenario_id=SCENARIO_ID)
    row = build_row(status="disabled", is_builtin=False, binding_mode="explicit",
                    kb_ids=(str(KB_A),))
    with pytest.raises(ScenarioError) as exc_info:
        await resolve_scenario(
            FakeSession([row]), tenant_id=TENANT_ID, application=app,
            requested_kb_ids=[KB_B],  # 必然不在场景声明里
        )
    assert exc_info.value.code is ScenarioCode.NOT_ALLOWED


# ── R4：scope 与 200 分支 ──────────────────────────────────────────────


async def test_truth_table_r4_request_outside_scenario_scope():
    app = make_application(scenario_id=SCENARIO_ID)
    row = build_row(status="active", is_builtin=False, binding_mode="explicit",
                    kb_ids=(str(KB_A),))
    with pytest.raises(ScenarioError) as exc_info:
        await resolve_scenario(
            FakeSession([row]), tenant_id=TENANT_ID, application=app, requested_kb_ids=[KB_B]
        )
    error = exc_info.value
    assert error.code is ScenarioCode.MISMATCH
    assert error.http_status == 400
    assert error.message == "请求的知识库不在场景范围内"
    # 不回显场景真实集合，否则可被逐位枚举
    assert str(KB_A) not in error.message


# ── R5 / R6：正常解析 ──────────────────────────────────────────────────


async def test_truth_table_r5_unbound_uses_builtin_row():
    app = make_application(scenario_id=None)
    row = build_row(is_builtin=True, binding_mode="unrestricted", kb_ids=())
    snapshot = await resolve_scenario(
        FakeSession([row]), tenant_id=TENANT_ID, application=app, requested_kb_ids=[KB_A]
    )
    assert snapshot.source == "builtin_row"
    assert snapshot.scenario_id == SCENARIO_ID
    assert snapshot.is_builtin is True
    assert snapshot.kb_ids == (KB_A,)  # 场景不额外限制请求集合


async def test_truth_table_r6_bound_builtin_scenario_is_application_source():
    """显式绑定内置场景：source='application'，其余与 R5 全等。"""
    bound = make_application(scenario_id=SCENARIO_ID)
    row = build_row(is_builtin=True, binding_mode="unrestricted", kb_ids=())
    bound_snapshot = await resolve_scenario(
        FakeSession([row]), tenant_id=TENANT_ID, application=bound, requested_kb_ids=[KB_A]
    )
    assert bound_snapshot.source == "application"
    assert bound_snapshot.scenario_id == SCENARIO_ID

    unbound_snapshot = await resolve_scenario(
        FakeSession([row]), tenant_id=TENANT_ID, application=make_application(),
        requested_kb_ids=[KB_A],
    )
    # 除 source / scenario_id 外全等 —— 这是「回填零收益」的证明（§A.4）
    assert _identity_and_source(bound_snapshot) == _identity_and_source(unbound_snapshot)


async def test_truth_table_r6_explicit_scope_accepts_subset():
    """explicit 场景：请求集合是声明集合的子集 ⇒ 通过，且 kb_ids 原样保留顺序。"""
    app = make_application(scenario_id=SCENARIO_ID)
    row = build_row(status="active", is_builtin=False, binding_mode="explicit",
                    kb_ids=(str(KB_A), str(KB_B)))
    snapshot = await resolve_scenario(
        FakeSession([row]), tenant_id=TENANT_ID, application=app,
        requested_kb_ids=[KB_B, KB_A],
    )
    assert snapshot.source == "application"
    assert snapshot.binding_mode == "explicit"
    assert snapshot.binding_kb_ids == (KB_A, KB_B)  # 声明顺序
    assert snapshot.kb_ids == (KB_B, KB_A)          # 请求顺序（下游直接用）


async def test_truth_table_sources_are_pairwise_distinguishable():
    """三种 source 都可达且互不混淆（否则「种子行丢了」永远查不出来）。

    三个分支刻意用**不同租户**：行缓存按 (tenant_id, scenario_id) 分键，
    同一租户连跑两次内置分支会命中缓存 —— 那是缓存的正常行为，不该混进本断言。
    """
    import uuid as _uuid

    third_tenant = _uuid.UUID("66666666-6666-6666-6666-666666666666")

    with_builtin_row = await resolve_scenario(
        FakeSession([build_row(tenant_id=third_tenant, is_builtin=True)]),
        tenant_id=third_tenant,
        application=make_application(tenant_id=third_tenant),
        requested_kb_ids=[KB_A],
    )
    fallback = await resolve_scenario(
        FakeSession([]),
        tenant_id=OTHER_TENANT_ID,
        application=make_application(tenant_id=OTHER_TENANT_ID),
        requested_kb_ids=[KB_A],
    )
    bound = await resolve_scenario(
        FakeSession([build_row(is_builtin=True)]),
        tenant_id=TENANT_ID,
        application=make_application(scenario_id=SCENARIO_ID),
        requested_kb_ids=[KB_A],
    )
    assert {with_builtin_row.source, fallback.source, bound.source} == {
        "builtin_row",
        "builtin_fallback",
        "application",
    }


# ══════════════════════════════════════════════════════════════════════
# 行合法性不变量：`unrestricted` 只允许内置场景（守卫 MUST 先于 R2）
# ══════════════════════════════════════════════════════════════════════
#
# 这一族守的是设计文档 §A.3 那句「unrestricted 只允许内置场景使用」。
# 它不是风格约定，而是本抽象唯一的 fail-open 出口：
#   R4 的范围检查**只在 explicit 下执行**。于是一行
#   `is_builtin=false, binding_mode='unrestricted'` 的非内置场景，
#   R4 被整条跳过 —— 「忘了给场景配知识库」直接变成「所有知识库都放开」，
#   而日志、响应、指标里**没有任何信号**表明范围检查没跑。
# DB 层无法表达这条约束（`is_builtin` 在 scenarios、`binding_mode` 在
# scenario_versions，跨表 CHECK 不存在），所以读侧是唯一防线。
# Step 1–2 没有写入路径；Step 4 一旦开写接口，这里就是最后一道闸。


async def test_non_builtin_unrestricted_is_unresolvable():
    """非内置 + unrestricted ⇒ 不可解析（500），绝不是「放行全部范围」。"""
    app = make_application(scenario_id=SCENARIO_ID)
    row = build_row(
        status="active", is_builtin=False, binding_mode="unrestricted", kb_ids=()
    )
    with pytest.raises(ScenarioError) as exc_info:
        await resolve_scenario(
            FakeSession([row]), tenant_id=TENANT_ID, application=app,
            requested_kb_ids=[KB_A],
        )
    error = exc_info.value
    assert error.code is ScenarioCode.UNRESOLVABLE
    assert error.http_status == 500
    assert error.message == "场景配置不可解析"
    # diagnostic 里能定位到具体场景与原因（运维改库要看得懂），但不进响应
    assert error.diagnostic["reason"] == "non_builtin_unrestricted"
    assert error.diagnostic["scenario_id"] == str(SCENARIO_ID)
    assert str(SCENARIO_ID) not in error.message


async def test_builtin_unrestricted_is_allowed():
    """内置 + unrestricted = 合法形态：请求集合原样通过（正向对照）。

    ★ 与上一条**只差 `is_builtin` 一个字段** —— 两条一起才说明闸门钉的
      是「非内置」，不是「unrestricted 这个词」。
    """
    app = make_application(scenario_id=SCENARIO_ID)
    row = build_row(is_builtin=True, binding_mode="unrestricted", kb_ids=())
    snapshot = await resolve_scenario(
        FakeSession([row]), tenant_id=TENANT_ID, application=app,
        requested_kb_ids=[KB_A, KB_B],
    )
    assert snapshot.binding_mode == "unrestricted"
    assert snapshot.kb_ids == (KB_A, KB_B)
    assert snapshot.source == "application"


async def test_row_legality_guard_runs_before_r2_empty_request():
    """空请求也 MUST 被拦 —— 这条钉的是**守卫的位置**（先于 R2）。

    若把守卫挪到 R2/R3/R4 之后，空请求会在 R2 处直接返回快照，一个非法
    `binding_mode` 就原样进了 `ScenarioSnapshot.binding_mode`
    （该字段声明为 `Literal["explicit","unrestricted"]`）——
    快照的这条不变量本该**全路径**成立，而不是「非空请求时成立」。
    """
    app = make_application(scenario_id=SCENARIO_ID)
    row = build_row(is_builtin=False, binding_mode="unrestricted", kb_ids=())
    with pytest.raises(ScenarioError) as exc_info:
        await resolve_scenario(
            FakeSession([row]), tenant_id=TENANT_ID, application=app,
            requested_kb_ids=[],  # R2 分支
        )
    assert exc_info.value.code is ScenarioCode.UNRESOLVABLE
    assert exc_info.value.diagnostic["reason"] == "non_builtin_unrestricted"


async def test_unknown_binding_mode_is_unresolvable():
    """`binding_mode` 落在两个合法值之外 ⇒ 不可解析（不猜、不当 unrestricted）。"""
    app = make_application(scenario_id=SCENARIO_ID)
    row = build_row(is_builtin=True, binding_mode="wildcard")
    with pytest.raises(ScenarioError) as exc_info:
        await resolve_scenario(
            FakeSession([row]), tenant_id=TENANT_ID, application=app,
            requested_kb_ids=[KB_A],
        )
    error = exc_info.value
    assert error.code is ScenarioCode.UNRESOLVABLE
    assert error.diagnostic["reason"] == "invalid_binding_mode"
    # 脏值本身不进日志：只记类型
    assert error.diagnostic["actual_type"] == "str"
    assert "wildcard" not in str(error.diagnostic)
