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
from app.config.settings import get_settings
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

    assert body["hot_unsupported"] is True
    for name in ("M_SHARED_DECLARED_MB", "M_CONTROL_PG_LOWER_MB", "M_SCENE_PG_MB"):
        assert body["constants"][name]["calibrated"] is False

    assert body["latency"]["metric"] == "http.request.duration"
    assert body["latency"]["scope"] == "process-wide"


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
    """四条阈值齐全、全部未标定；不可评估的两条 over 必须 None。"""
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
        assert item["calibrated"] is False

    assert thresholds["retrieval_p95_vs_baseline"]["evaluable"] is False
    assert thresholds["retrieval_p95_vs_baseline"]["over"] is None
    assert thresholds["independent_rate_limit"]["evaluable"] is False
    assert thresholds["independent_rate_limit"]["over"] is None

    hit_rate = thresholds["db_cache_hit_rate"]
    assert hit_rate["threshold"] == 0.95
    assert hit_rate["evaluable"] == (hit_rate["observed"] is not None)
    if hit_rate["evaluable"]:
        assert hit_rate["over"] == (hit_rate["observed"] < 0.95)
        assert hit_rate["database"]
    else:
        assert hit_rate["over"] is None
