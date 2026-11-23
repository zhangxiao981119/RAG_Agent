"""Step 5 只读盘点脚本单测（纯单测，不连库）。

覆盖 6 项判定的正反例，以及 DSN 主机渲染的脱敏口径：
    ① 存储层默认态   ② 视图恒等   ③ kb 冲突
    ④ 注册表自洽     ⑤ 连接预算   ⑥ 启用态键覆盖

★ 判定与取数分离：`evaluate` 全参数注入，用例不需要 monkeypatch，也不连库。
★ 期望值一律字面量（不复用被测模块的常量），避免实现错了断言跟着错。
"""
from __future__ import annotations

from app.infra.db_router import RouterReport
from scripts.db_routing_report import _format_dsn_hosts, evaluate

# 索引与六项判定顺序一一对应，字面量写出，避免"顺序改了断言还绿"
_INDEX_DEFAULT_STATE = 0
_INDEX_VIEW_IDENTITY = 1
_INDEX_KB_CONFLICT = 2
_INDEX_REGISTRY = 3
_INDEX_BUDGET = 4
_INDEX_KEY_COVERAGE = 5


def _report(
    *,
    keys: tuple[str, ...] = ("default",),
    created_engines: int = 0,
    needed: int = 50,
    budget: int = 90,
) -> RouterReport:
    """构造一次注册后的路由快照（字段与 db_router.RouterReport 一一对应）。"""
    return RouterReport(
        bucket_count=1,
        per_bucket_connections=40,
        total_connections=40,
        needed_connections=needed,
        budget=budget,
        keys=keys,
        created_engines=created_engines,
        notes=(),
    )


def _evaluate(**overrides):
    """按健康关闭态打底，覆盖任一参数后调 evaluate（返回六项结果）。"""
    kwargs = {
        "split_enabled": False,
        "bound_count": 0,
        "view_count": 3,
        "scenario_count": 3,
        "conflict_rows": [],
        "report": _report(),
        "bound_keys": set(),
        "registered_keys": ("default",),
    }
    kwargs.update(overrides)
    return evaluate(**kwargs)


def _statuses(results) -> list[str]:
    return [result.status for result in results]


def test_healthy_closed_all_pass():
    """健康关闭态：六项全 PASS（④ 有结论，⑥ 为 N/A）。"""
    assert _statuses(_evaluate()) == [
        "PASS",
        "PASS",
        "PASS",
        "PASS",
        "PASS",
        "N/A",
    ]


def test_bound_count_non_zero_fails_default_state():
    """① 有场景绑了库位 → FAIL，且 action 给出「改回 NULL」的处置。"""
    results = _evaluate(bound_count=1)

    assert results[_INDEX_DEFAULT_STATE].status == "FAIL"
    assert "bound_count=1" in results[_INDEX_DEFAULT_STATE].detail
    assert "db_binding=NULL" in results[_INDEX_DEFAULT_STATE].action


def test_view_count_mismatch_fails_identity():
    """② 视图行数与场景行数不等 → FAIL，detail 带两个数字。"""
    results = _evaluate(view_count=4, scenario_count=3)

    result = results[_INDEX_VIEW_IDENTITY]
    assert result.status == "FAIL"
    assert "4" in result.detail
    assert "3" in result.detail


def test_conflict_row_fails_kb_conflict():
    """③ 一行 kb 冲突 → FAIL，detail 列出 tenant/kb_id/db_keys。"""
    results = _evaluate(
        conflict_rows=[
            {
                "tenant_id": "11111111-1111-1111-1111-111111111111",
                "kb_id": "kb-a",
                "db_keys": ["kb_ab", "kb_cd"],
            }
        ]
    )

    result = results[_INDEX_KB_CONFLICT]
    assert result.status == "FAIL"
    assert "11111111-1111-1111-1111-111111111111" in result.detail
    assert "kb-a" in result.detail
    assert "kb_ab" in result.detail


def test_closed_state_registry_with_extra_key_or_engine_fails():
    """④ 关闭态多登记一个库位、或多建一个引擎 → FAIL。"""
    extra_key = _evaluate(registered_keys=("default", "kb_ab"))
    extra_engine = _evaluate(report=_report(created_engines=1))

    assert extra_key[_INDEX_REGISTRY].status == "FAIL"
    assert extra_engine[_INDEX_REGISTRY].status == "FAIL"


def test_enabled_state_registry_check_is_not_applicable():
    """④ 启用态不适用（PASS/FAIL 都无从谈起）。"""
    results = _evaluate(
        split_enabled=True,
        report=_report(keys=("default", "kb_ab"), created_engines=1, needed=90),
        registered_keys=("default", "kb_ab"),
    )

    assert results[_INDEX_REGISTRY].status == "N/A"
    assert results[_INDEX_REGISTRY].detail == "启用态不适用"


def test_over_budget_fails_and_action_forbids_bigger_max_connections():
    """⑤ needed > budget → FAIL，action 三选一且明确禁止调大 max_connections。"""
    results = _evaluate(report=_report(needed=170, budget=90))

    result = results[_INDEX_BUDGET]
    assert result.status == "FAIL"
    assert "needed=170" in result.detail
    assert "budget=90" in result.detail
    assert "DB_POOL_SIZE" in result.action
    assert "不要调大 max_connections" in result.action


def test_budget_boundary_equal_passes():
    """⑤ 边界 needed == budget 必须 PASS（预算本身就是恰好用完的上限）。"""
    results = _evaluate(report=_report(needed=90, budget=90))

    assert results[_INDEX_BUDGET].status == "PASS"
    assert results[_INDEX_BUDGET].detail == "needed=90 budget=90"


def test_enabled_missing_registered_key_fails_coverage():
    """⑥ 启用态：DB 绑了 kb_cd 但注册表没有 → FAIL，detail 含缺口键。"""
    results = _evaluate(
        split_enabled=True,
        report=_report(keys=("default", "kb_ab"), created_engines=1, needed=90),
        registered_keys=("default", "kb_ab"),
        bound_keys={"kb_ab", "kb_cd"},
    )

    result = results[_INDEX_KEY_COVERAGE]
    assert result.status == "FAIL"
    assert "kb_cd" in result.detail
    assert "SCENE_DB_DSN" in result.action


def test_enabled_full_coverage_passes():
    """⑥ 启用态全覆盖 → PASS。"""
    results = _evaluate(
        split_enabled=True,
        report=_report(keys=("default", "kb_ab"), created_engines=1, needed=90),
        registered_keys=("default", "kb_ab"),
        bound_keys={"kb_ab"},
    )

    assert results[_INDEX_KEY_COVERAGE].status == "PASS"


def test_closed_state_coverage_is_not_applicable():
    """⑥ 分流关闭 → N/A（键覆盖只在启用态才有意义）。"""
    results = _evaluate(bound_keys={"kb_ab"})

    assert results[_INDEX_KEY_COVERAGE].status == "N/A"
    assert results[_INDEX_KEY_COVERAGE].detail == "分流关闭"


def test_format_dsn_hosts_masks_credentials():
    """DSN 主机渲染只给 key → host:port，绝不带账号密码。"""
    text = _format_dsn_hosts(
        {"kb_ab": "postgresql+asyncpg://alice:secret@db-host:5432/kagent_finance"}
    )

    assert text == "kb_ab → db-host:5432"
    assert "alice" not in text
    assert "secret" not in text
