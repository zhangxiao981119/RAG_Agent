"""Step 7 S7-6：容量看板（纯函数 + 真库 HTTP）。

覆盖面：
  1. M_A 三个基准数字（含 control_pg 覆盖）；
  2. shared_buffers 解析（含无单位的 8kB 块口径与两种失败）；
  3. `database_name_for` 的三种路径（default / 未注册 / 已配置）；
  4. 真库 GET /api/system/capacity 的结构与自洽（total == compute_m_a(...)）；
  5. 库观测：库名集合包含当前库，default 行的大小与命中率口径；
  6. hot 声明与实际分离（声明 ≥1、实际 0、hot_unsupported）；
  7. 非管理员 403；
  8. 四条阈值的结构、calibrated 与可评估性口径。

★ 库读一律用独立连接（`committed_rows`），不读响应体自证。
"""
from __future__ import annotations

import uuid
from dataclasses import replace

import pytest

from app.api.deps import get_current_user
from app.config import placeholders
from app.config.settings import get_settings
from app.infra import metrics
from app.infra.db_router import (
    DB_ROUTE_KEY_UNKNOWN,
    DbRouteError,
    database_name_for,
)
from app.main import app
from app.services.capacity import (
    M_CONTROL_PG_LOWER_MB,
    M_SCENE_PG_MB,
    M_SHARED_DECLARED_MB,
    UNBOUNDED_SERVICES,
    _build_thresholds,
    _parse_shared_buffers_mb,
    compute_m_a,
)
from tests.scenario.pg_helpers import SCENARIO_TEST_DB_URL, _exec_committed, committed_rows

pytestmark = pytest.mark.asyncio

CAPACITY_URL = "/api/system/capacity"


# ══════════════════════════════════════════════════════════════════════
# 1. M_A 纯算术
# ══════════════════════════════════════════════════════════════════════


async def test_compute_m_a_matches_declared_constants():
    """M_A 的数字必须与三个常量的声明一致（8320 / 8920 / 8448）。"""
    assert M_SHARED_DECLARED_MB == 8192
    assert M_CONTROL_PG_LOWER_MB == 128
    assert M_SCENE_PG_MB == 300

    assert compute_m_a(0) == 8320
    assert compute_m_a(2) == 8920
    assert compute_m_a(0, control_pg_mb=256) == 8448


# ══════════════════════════════════════════════════════════════════════
# 2. shared_buffers 解析
# ══════════════════════════════════════════════════════════════════════


async def test_parse_shared_buffers_units_and_blocks():
    """带单位按单位换算；无单位按 PG 的 8kB 块数换算。"""
    assert _parse_shared_buffers_mb("128MB") == 128
    assert _parse_shared_buffers_mb("1GB") == 1024
    assert _parse_shared_buffers_mb("16384kB") == 16
    assert _parse_shared_buffers_mb("8192") == 64
    # 任务书的 16384 块口径：16384 × 8kB = 128MB
    assert _parse_shared_buffers_mb("16384") == 128


async def test_parse_shared_buffers_rejects_garbage_and_non_positive():
    """解析失败 / 结果 <= 0 一律 ValueError，不静默兜底成 128。"""
    for raw in ("", "abc", "128XB", "0MB", "-1MB"):
        with pytest.raises(ValueError):
            _parse_shared_buffers_mb(raw)


# ══════════════════════════════════════════════════════════════════════
# 3. database_name_for
# ══════════════════════════════════════════════════════════════════════


async def test_database_name_for_default_and_unknown_key():
    """default 能解析出非空库名；未注册的 kb 键 fail-closed。"""
    assert database_name_for("default")

    with pytest.raises(DbRouteError) as excinfo:
        database_name_for("kb_missing")
    assert excinfo.value.code == DB_ROUTE_KEY_UNKNOWN


async def test_database_name_for_configured_scene_key(monkeypatch):
    """已配置的场景库位，从 DSN 解析出库名（不返回 DSN 本身）。"""
    settings = get_settings()
    monkeypatch.setattr(
        settings,
        "scene_db_dsn",
        {"kb_finance": "postgresql+asyncpg://u:p@h:5432/finance"},
    )
    assert database_name_for("kb_finance") == "finance"


# ══════════════════════════════════════════════════════════════════════
# 4-8. 真库 HTTP
# ══════════════════════════════════════════════════════════════════════


async def test_capacity_endpoint_structure_and_arithmetic(api):
    """结构与算术自洽：total 必须由 inputs 里的 control_pg 现算出来。"""
    response = await api.client.get(CAPACITY_URL)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["formula"] == "M_A(N) = M_shared + M_control_pg + N_hot_effective × M_scene_pg"
    assert body["unbounded_services"] == list(UNBOUNDED_SERVICES)

    inputs = body["inputs"]
    assert set(inputs["tier_counts"]) == {"cold", "warm", "hot"}
    assert inputs["n_hot_effective"] == 0
    assert inputs["n_hot_declared"] == inputs["tier_counts"]["hot"]

    result = body["result"]
    assert result["total_lower_mb"] == compute_m_a(
        0, control_pg_mb=inputs["m_control_pg_lower_mb"]
    )
    assert result["postgres_side_lower_mb"] == inputs["m_control_pg_lower_mb"]
    assert result["calibrated"] is False
    assert result["lower_bound_only"] is True
    assert body["calibration_ref"] == ""

    assert body["hot_unsupported"] is True
    # 默认未标定：M_shared/M_scene 是声明值；M_control 来自运行时实测，恒 true。
    assert body["constants"]["M_SHARED_DECLARED_MB"]["calibrated"] is False
    assert body["constants"]["M_SCENE_PG_MB"]["calibrated"] is False
    control = body["constants"]["M_CONTROL_PG_LOWER_MB"]
    assert control["calibrated"] is True
    assert control["value"] == inputs["m_control_pg_lower_mb"]

    assert body["latency"]["metric"] == "http.request.duration"
    assert body["latency"]["scope"] == "process-wide"
    assert body["latency"]["retrieval_metric"] == "rag.retrieve.total"
    # 覆盖范围必须显式写清：只埋 chat 主链路，search/评测直连 retrieve() 不计入
    assert "retrieve_chain" in body["latency"]["retrieval_scope"]
    assert "/integrations/search" in body["latency"]["retrieval_scope"]
    assert "不计入" in body["latency"]["retrieval_scope"]
    assert "/integrations/search" in body["latency"]["note"]


async def test_capacity_databases_observe_current_db(api, monkeypatch):
    """库观测覆盖当前库；default 行的大小 > 0，命中率按块计数算。

    ★ 为什么要 monkeypatch `database_url`：本用例的库是 `SCENARIO_TEST_DB_URL`
      指向的测试库，而 `.env` 的 `DATABASE_URL` 指向编排里的 `kagent` ——
      不覆盖就看板观测的是另一个库，`current_database()` 永远对不上。
      （这是测试环境的库名差异，不是端点的口径问题。）
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "database_url", SCENARIO_TEST_DB_URL)

    response = await api.client.get(CAPACITY_URL)
    assert response.status_code == 200, response.text
    databases = response.json()["databases"]

    current = await committed_rows("SELECT current_database() AS name")
    current_name = current[0]["name"]
    assert current_name in {item["database"] for item in databases}

    default_row = next(item for item in databases if item["db_key"] == "default")
    assert default_row["database"] == current_name
    assert default_row["size_bytes"] > 0

    denominator = default_row["blks_hit"] + default_row["blks_read"]
    if denominator > 0:
        assert default_row["hit_rate"] == default_row["blks_hit"] / denominator
    else:
        assert default_row["hit_rate"] is None


async def test_capacity_separates_declared_hot_from_effective_hot(api):
    """库里声明了 hot（tier='hot'）时，看板仍按 0 个有效热库计算。"""
    key = f"s7cap{uuid.uuid4().hex[:8]}"
    created = await api.client.post("/api/scenarios", json={"scenario_key": key, "name": "容量看板场景"})
    assert created.status_code == 201, created.text
    scenario_id = uuid.UUID(created.json()["id"])

    # CHECK 要求 warm/hot 必须有 db_binding，所以先 warm 再升 hot
    await _exec_committed(
        "UPDATE scenarios SET tier='warm', db_binding='kb_finance' WHERE id=$1",
        scenario_id,
    )
    await _exec_committed("UPDATE scenarios SET tier='hot' WHERE id=$1", scenario_id)

    response = await api.client.get(CAPACITY_URL)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["inputs"]["n_hot_declared"] >= 1
    assert body["inputs"]["n_hot_effective"] == 0
    assert body["hot_unsupported"] is True
    # 有效热库为 0，所以公式里不能悄悄把 hot 算成 warm
    assert body["result"]["postgres_side_lower_mb"] == body["inputs"]["m_control_pg_lower_mb"]


async def test_capacity_forbidden_for_non_admin(api):
    """clearance < 40 → 403（require_admin 口径）。"""
    low_user = replace(api.user, clearance=20)
    app.dependency_overrides[get_current_user] = lambda: low_user

    response = await api.client.get(CAPACITY_URL)
    assert response.status_code == 403, response.text


async def test_capacity_thresholds_structure(api):
    """四条阈值齐全；默认未标定，缺输入的阈值 over 必须 None。"""
    response = await api.client.get(CAPACITY_URL)
    assert response.status_code == 200, response.text
    thresholds = {item["name"]: item for item in response.json()["thresholds"]}
    assert set(thresholds) == {
        "db_cache_hit_rate",
        "db_size_vs_instance_memory",
        "retrieval_p95_vs_baseline",
        "independent_rate_limit",
    }

    required = {"name", "threshold", "unit", "calibrated", "evaluable", "over", "reason"}
    for item in thresholds.values():
        assert required <= set(item)

    # 默认 ref 为空 → 三条数值阈值未标定；独立限流永远未标定。
    assert thresholds["db_cache_hit_rate"]["calibrated"] is False
    assert thresholds["db_size_vs_instance_memory"]["calibrated"] is False
    assert thresholds["retrieval_p95_vs_baseline"]["calibrated"] is False
    assert thresholds["independent_rate_limit"]["calibrated"] is False

    # 默认数值与 settings 默认值一致。
    assert thresholds["db_cache_hit_rate"]["threshold"] == 0.95
    assert thresholds["db_size_vs_instance_memory"]["threshold"] == 0.30
    assert thresholds["retrieval_p95_vs_baseline"]["threshold"] == 1.5

    # 实例内存未提供 → 大小占比不可评估。
    size = thresholds["db_size_vs_instance_memory"]
    assert size["evaluable"] is False
    assert size["over"] is None
    assert "CAPACITY_INSTANCE_MEMORY_MB" in size["reason"]

    # 检索基线未提供 → 检索 P95 不可评估。
    p95 = thresholds["retrieval_p95_vs_baseline"]
    assert p95["evaluable"] is False
    assert p95["over"] is None
    assert "CAPACITY_RETRIEVAL_P95_BASELINE_MS" in p95["reason"]

    assert thresholds["independent_rate_limit"]["evaluable"] is False
    assert thresholds["independent_rate_limit"]["over"] is None

    hit_rate = thresholds["db_cache_hit_rate"]
    assert hit_rate["evaluable"] == (hit_rate["observed"] is not None)
    if hit_rate["evaluable"]:
        assert hit_rate["over"] == (hit_rate["observed"] < 0.95)
        assert hit_rate["database"]
    else:
        assert hit_rate["over"] is None


# ══════════════════════════════════════════════════════════════════════
# 9. 容量常量/阈值可配置 + 标定记录
# ══════════════════════════════════════════════════════════════════════


async def test_capacity_settings_override_constants_and_result(api, monkeypatch):
    """CAPACITY_M_SHARED_MB / CAPACITY_M_SCENE_PG_MB 覆盖 → 常量、inputs、result 同步。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "capacity_m_shared_mb", 4096)
    monkeypatch.setattr(settings, "capacity_m_scene_pg_mb", 512)

    response = await api.client.get(CAPACITY_URL)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["constants"]["M_SHARED_DECLARED_MB"]["value"] == 4096
    assert body["constants"]["M_SCENE_PG_MB"]["value"] == 512
    assert body["inputs"]["m_shared_declared_mb"] == 4096
    assert body["inputs"]["m_scene_pg_mb"] == 512
    assert body["result"]["total_lower_mb"] == compute_m_a(
        0,
        m_shared_mb=4096,
        m_scene_pg_mb=512,
        control_pg_mb=body["inputs"]["m_control_pg_lower_mb"],
    )


async def test_capacity_calibration_ref_controls_calibrated(api, monkeypatch):
    """ref 为空 → 未标定；非空 → 已标定；independent_rate_limit 永远 false。"""
    settings = get_settings()

    monkeypatch.setattr(settings, "capacity_calibration_ref", "")
    empty = (await api.client.get(CAPACITY_URL)).json()
    assert empty["calibration_ref"] == ""
    assert empty["result"]["calibrated"] is False
    assert empty["constants"]["M_SHARED_DECLARED_MB"]["calibrated"] is False
    assert empty["constants"]["M_SCENE_PG_MB"]["calibrated"] is False
    for name in (
        "db_cache_hit_rate",
        "db_size_vs_instance_memory",
        "retrieval_p95_vs_baseline",
        "independent_rate_limit",
    ):
        threshold = next(item for item in empty["thresholds"] if item["name"] == name)
        assert threshold["calibrated"] is False

    monkeypatch.setattr(
        settings, "capacity_calibration_ref", "2026-11-24/host-a/reports/cap.md"
    )
    marked = (await api.client.get(CAPACITY_URL)).json()
    assert marked["calibration_ref"] == "2026-11-24/host-a/reports/cap.md"
    assert marked["result"]["calibrated"] is True
    assert marked["constants"]["M_SHARED_DECLARED_MB"]["calibrated"] is True
    assert marked["constants"]["M_SCENE_PG_MB"]["calibrated"] is True
    for name in (
        "db_cache_hit_rate",
        "db_size_vs_instance_memory",
        "retrieval_p95_vs_baseline",
    ):
        threshold = next(item for item in marked["thresholds"] if item["name"] == name)
        assert threshold["calibrated"] is True
    independent = next(
        item for item in marked["thresholds"] if item["name"] == "independent_rate_limit"
    )
    assert independent["calibrated"] is False


def _threshold_kwargs(**overrides):
    """`_build_thresholds` 的默认入参（未提供输入全部按「不可评估」构造）。"""
    kwargs = {
        "calibrated": False,
        "hit_rate_min": 0.95,
        "size_ratio_max": 0.30,
        "p95_ratio_max": 1.5,
        "instance_memory_mb": 0,
        "retrieval_p95_ms": None,
        "retrieval_baseline_ms": 0.0,
    }
    kwargs.update(overrides)
    return kwargs


async def test_build_thresholds_size_uses_largest_database():
    """实例内存 >0 时取最大库算占比；=0 时不可评估且 reason 指向配置。"""
    databases = [
        {"database": "small", "hit_rate": 0.99, "size_mb": 100.0},
        {"database": "large", "hit_rate": 0.97, "size_mb": 900.0},
    ]

    unset = {
        item["name"]: item
        for item in _build_thresholds(databases, **_threshold_kwargs())
    }
    size_unset = unset["db_size_vs_instance_memory"]
    assert size_unset["evaluable"] is False
    assert size_unset["over"] is None
    assert "CAPACITY_INSTANCE_MEMORY_MB" in size_unset["reason"]

    sized = {
        item["name"]: item
        for item in _build_thresholds(
            databases, **_threshold_kwargs(instance_memory_mb=1000)
        )
    }
    size = sized["db_size_vs_instance_memory"]
    assert size["evaluable"] is True
    assert size["largest_database"] == "large"
    assert size["largest_db_size_mb"] == 900.0
    assert size["observed"] == 0.9
    assert size["over"] is (0.9 > 0.30)


async def test_build_thresholds_retrieval_requires_baseline_and_sample():
    """基线为 0 → 不可评估；有基线无样本 → 不可评估；两者齐备 → 算 ratio。"""
    databases = [{"database": "default", "hit_rate": 0.99, "size_mb": 10.0}]

    no_baseline = {
        item["name"]: item
        for item in _build_thresholds(
            databases, **_threshold_kwargs(retrieval_p95_ms=120.0)
        )
    }["retrieval_p95_vs_baseline"]
    assert no_baseline["evaluable"] is False
    assert no_baseline["over"] is None
    assert "CAPACITY_RETRIEVAL_P95_BASELINE_MS" in no_baseline["reason"]

    no_sample = {
        item["name"]: item
        for item in _build_thresholds(
            databases, **_threshold_kwargs(retrieval_baseline_ms=100.0)
        )
    }["retrieval_p95_vs_baseline"]
    assert no_sample["evaluable"] is False
    assert no_sample["over"] is None
    assert "rag.retrieve.total" in no_sample["reason"]

    evaluated = {
        item["name"]: item
        for item in _build_thresholds(
            databases,
            **_threshold_kwargs(retrieval_p95_ms=120.0, retrieval_baseline_ms=100.0),
        )
    }["retrieval_p95_vs_baseline"]
    assert evaluated["evaluable"] is True
    assert evaluated["observed"] == 1.2
    assert evaluated["observed_p95_ms"] == 120.0
    assert evaluated["baseline_ms"] == 100.0
    assert evaluated["over"] is False


async def test_build_thresholds_override_flips_over():
    """阈值覆盖必须改变 over 判定（不是只改展示数字）。"""
    databases = [
        {"database": "default", "hit_rate": 0.90, "size_mb": 400.0},
    ]

    strict = {
        item["name"]: item
        for item in _build_thresholds(
            databases,
            **_threshold_kwargs(
                instance_memory_mb=1000,
                retrieval_p95_ms=120.0,
                retrieval_baseline_ms=100.0,
            ),
        )
    }
    assert strict["db_cache_hit_rate"]["over"] is True
    assert strict["db_size_vs_instance_memory"]["over"] is True
    assert strict["retrieval_p95_vs_baseline"]["over"] is False

    loose = {
        item["name"]: item
        for item in _build_thresholds(
            databases,
            **_threshold_kwargs(
                hit_rate_min=0.80,
                size_ratio_max=0.50,
                p95_ratio_max=1.0,
                instance_memory_mb=1000,
                retrieval_p95_ms=120.0,
                retrieval_baseline_ms=100.0,
            ),
        )
    }
    assert loose["db_cache_hit_rate"]["over"] is False
    assert loose["db_size_vs_instance_memory"]["over"] is False
    assert loose["retrieval_p95_vs_baseline"]["over"] is True


async def test_capacity_retrieval_p95_uses_rag_total_metric(api, monkeypatch):
    """基线 >0 时取 rag.retrieve.total 的 P95 与基线比较；无样本则不可评估。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "capacity_retrieval_p95_baseline_ms", 100.0)

    def _fake_quantile(name, q, **tags):  # noqa: ANN001
        if name == "rag.retrieve.total":
            return 120.0
        return 50.0

    monkeypatch.setattr(metrics, "quantile_ms", _fake_quantile)
    body = (await api.client.get(CAPACITY_URL)).json()
    assert body["latency"]["retrieval_p95_ms"] == 120.0
    threshold = next(
        item for item in body["thresholds"] if item["name"] == "retrieval_p95_vs_baseline"
    )
    assert threshold["evaluable"] is True
    assert threshold["observed"] == 1.2
    assert threshold["over"] is False

    # 无检索样本（P95 为 None）→ 不可评估，但仍保留基线。
    monkeypatch.setattr(
        metrics,
        "quantile_ms",
        lambda name, q, **tags: None if name == "rag.retrieve.total" else 50.0,
    )
    no_sample = (await api.client.get(CAPACITY_URL)).json()
    threshold = next(
        item
        for item in no_sample["thresholds"]
        if item["name"] == "retrieval_p95_vs_baseline"
    )
    assert threshold["evaluable"] is False
    assert threshold["over"] is None
    assert threshold["baseline_ms"] == 100.0
    assert "rag.retrieve.total" in threshold["reason"]


async def test_capacity_placeholders_and_banner_defaults_match_settings():
    """站位值镜像仍与默认声明值一致；settings 默认与横幅默认同源。"""
    assert placeholders.value_of("CAPACITY_M_SHARED_DECLARED_MB") == M_SHARED_DECLARED_MB
    assert placeholders.value_of("CAPACITY_M_CONTROL_PG_LOWER_MB") == M_CONTROL_PG_LOWER_MB
    assert placeholders.value_of("CAPACITY_M_SCENE_PG_MB") == M_SCENE_PG_MB

    defaults = type(get_settings()).model_fields
    assert defaults["capacity_m_shared_mb"].default == M_SHARED_DECLARED_MB
    assert defaults["capacity_m_scene_pg_mb"].default == M_SCENE_PG_MB
    assert defaults["capacity_instance_memory_mb"].default == 0
    assert defaults["capacity_retrieval_p95_baseline_ms"].default == 0.0
    assert defaults["capacity_calibration_ref"].default == ""
    assert defaults["capacity_db_hit_rate_min"].default == 0.95
    assert defaults["capacity_db_size_ratio_max"].default == 0.30
    assert defaults["capacity_retrieval_p95_ratio_max"].default == 1.5


async def test_capacity_settings_reject_invalid_values():
    """8 项 CAPACITY_* 必须有真实约束：非法值在 Settings 构造期即失败。"""
    from pydantic import ValidationError

    from app.config.settings import Settings

    bad = {
        "capacity_m_shared_mb": 0,
        "capacity_m_scene_pg_mb": 0,
        "capacity_instance_memory_mb": -1,
        "capacity_retrieval_p95_baseline_ms": -1.0,
        "capacity_calibration_ref": "x" * 201,
        "capacity_db_hit_rate_min": 0,
        "capacity_db_size_ratio_max": 0,
        "capacity_retrieval_p95_ratio_max": 0,
    }
    for field, value in bad.items():
        with pytest.raises(ValidationError) as excinfo:
            Settings(**{field: value})
        locs = [tuple(err.get("loc", ())) for err in excinfo.value.errors()]
        # 必须是被测字段本身被拒，不能是别的字段顺带报错（防假绿）。
        assert any(field in loc for loc in locs), (field, locs)

    # 命中率上限 1：>1 必须拒绝。
    with pytest.raises(ValidationError) as excinfo:
        Settings(capacity_db_hit_rate_min=1.2)
    locs = [tuple(err.get("loc", ())) for err in excinfo.value.errors()]
    assert any("capacity_db_hit_rate_min" in loc for loc in locs), locs


async def test_capacity_calibration_ref_whitespace_is_uncalibrated(api, monkeypatch):
    """纯空白 ref 仍是「未标定」；顶层原值回显，result.calibrated 与之一致。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "capacity_calibration_ref", "   ")

    body = (await api.client.get(CAPACITY_URL)).json()
    assert body["calibration_ref"] == "   "
    assert body["result"]["calibrated"] is False
    assert body["constants"]["M_SHARED_DECLARED_MB"]["calibrated"] is False
    assert body["constants"]["M_SCENE_PG_MB"]["calibrated"] is False
    for name in (
        "db_cache_hit_rate",
        "db_size_vs_instance_memory",
        "retrieval_p95_vs_baseline",
    ):
        threshold = next(item for item in body["thresholds"] if item["name"] == name)
        assert threshold["calibrated"] is False
    independent = next(
        item for item in body["thresholds"] if item["name"] == "independent_rate_limit"
    )
    assert independent["calibrated"] is False


async def test_build_thresholds_size_uses_max_not_order():
    """最大库即使排在首位也取它：按 size_mb 取 max，不是取首位/末位。"""
    databases = [
        {"database": "huge", "hit_rate": 0.99, "size_mb": 900.0},
        {"database": "tiny", "hit_rate": 0.99, "size_mb": 10.0},
    ]
    size = {
        item["name"]: item
        for item in _build_thresholds(
            databases, **_threshold_kwargs(instance_memory_mb=1000)
        )
    }["db_size_vs_instance_memory"]
    assert size["largest_database"] == "huge"
    assert size["largest_db_size_mb"] == 900.0
    assert size["observed"] == 0.9
    assert size["over"] is True
