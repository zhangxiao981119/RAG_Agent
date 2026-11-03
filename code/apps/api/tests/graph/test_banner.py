"""启动横幅与能力快照文案的回归测试。

钉住三件事：
  1. 关闭态横幅逐字包含设计文档 §12.1 的「能力全关」文案（含「既有链路不变」一句）——
     这些字是运维读的，改一个字都可能让人以为开了什么；
  2. 启用态必须显示口径、在线参数、就绪度与「改了要重做什么」清单；
  3. 能力关闭时快照里的数据字段为 None（MUST NOT 用 0 冒充「没有数据」）。
"""
from __future__ import annotations

import pytest

from app.config import capabilities
from app.config.settings import get_settings


@pytest.fixture
def env(monkeypatch):
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()
    monkeypatch.undo()


def test_disabled_banner_matches_spec(env):
    env.setenv("GRAPH_ENABLED", "false")
    env.setenv("WIKI_ENABLED", "false")
    get_settings.cache_clear()

    text = capabilities._render_text(
        role="api",
        app_env="prod",
        fingerprint="a1b2c3d4e5f6",
        graph_enabled=False,
        wiki_enabled=False,
        stats=None,
    )
    assert " kagent 能力快照 · app_env=prod · config_fingerprint=a1b2c3d4e5f6" in text
    assert " 实体图召回（GraphRAG） : 未启用（GRAPH_ENABLED=false）" in text
    assert " LLM wiki 导航         : 未启用（WIKI_ENABLED=false）" in text
    assert "向量 + 词元 tsquery + 词元 ILIKE 三路 + RRF（既有链路不变）" in text
    assert "同时重建 api 与 worker 两个镜像" in text
    # 关闭态不该出现「已启用」或就绪度统计
    assert "已启用" not in text
    assert "图数据就绪度" not in text


def test_enabled_banner_shows_caliber_and_readiness(env):
    env.setenv("GRAPH_ENABLED", "true")
    env.setenv("WIKI_ENABLED", "true")
    env.setenv("GRAPH_EXTRACTOR_MODEL", "")
    env.setenv("LLM_MODEL", "deepseek-v4-flash")
    env.setenv("WIKI_SCOPE", "doc")
    get_settings.cache_clear()

    stats = {
        "documents_total": 120,
        "documents_done": 118,
        "documents_pending": 2,
        "documents_failed": 0,
        "documents_skipped": 0,
        "entities": 8421,
        "relations": 15033,
    }
    text = capabilities._render_text(
        role="worker",
        app_env="prod",
        fingerprint="a1b2c3d4e5f6",
        graph_enabled=True,
        wiki_enabled=True,
        stats=stats,
    )
    assert " 实体图召回（GraphRAG） : 已启用   backend=postgres" in text
    assert "口径 = deepseek-v4-flash | gprompt-v1" in text
    assert "口径 = deepseek-v4-flash | wprompt-v1" in text
    assert "timeout 800ms" in text
    assert "图数据就绪度           : 118/120 篇已抽取" in text
    assert "待补建 2" in text
    # ⚠ 设计文档 §12.1 的样例写「118/120 …（92.5% 覆盖）」，但 118/120 = 98.3%
    #   —— 样例数值自相矛盾。覆盖率一律由代码按 done/total 计算（格式照样例），
    #   所以这里断言的是算出来的值，不是样例里那个手写数字。
    assert "98.3% 覆盖" in text
    assert "图规模                : 实体 8421 · 关系 15033" in text
    # 一致性命中：worker 侧提示的「另一侧」必须是 api
    assert "另一侧（api）的启动横幅" in text


def test_snapshot_data_is_none_when_disabled(env):
    env.setenv("GRAPH_ENABLED", "false")
    env.setenv("WIKI_ENABLED", "false")
    get_settings.cache_clear()

    snapshot = capabilities.capability_snapshot("fingerprint00", None)
    graph = next(c for c in snapshot["capabilities"] if c["key"] == "graph")
    wiki = next(c for c in snapshot["capabilities"] if c["key"] == "wiki")
    assert graph["enabled"] is False
    assert graph["data"]["documents_total"] is None  # None ≠ 0
    assert graph["data"]["entities"] is None
    assert wiki["enabled"] is False
    assert snapshot["config_fingerprint"] == "fingerprint00"


def test_snapshot_exposes_runtime_constants(env):
    env.setenv("GRAPH_ENABLED", "true")
    get_settings.cache_clear()

    snapshot = capabilities.capability_snapshot("fingerprint00", None)
    graph = next(c for c in snapshot["capabilities"] if c["key"] == "graph")
    assert graph["runtime"]["timeout_ms"] == get_settings().graph_retrieve_timeout_ms
    assert graph["runtime"]["max_hops"] >= 1
    assert graph["caliber"]["prompt_version"] == "gprompt-v1"
    # 部署期固定的能力 MUST 在快照里明示，避免管理端把它画成可切换开关
    assert graph["deployment_fixed"] is True
