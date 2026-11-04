"""口径快照的图字段（§14.6 可测化）测试。

钉住三件事：
  1. `build_caliber_snapshot()` 必须带三个**显式**图键：`graph_enabled` /
     `graph_extractor_model` / `graph_prompt_version` —— 「这条基线开没开图、
     按哪个口径抽的」必须是可查询的事实，不能只靠 config_fingerprint 间接表达。
  2. `graph_extractor_model` 取 `current_graph_profile()` 的**解析后实际值**，
     空配置时为 llm_model 的实际名 —— 不落空串（空串让「换主模型」在口径里不可见）。
  3. 关闭态（graph_enabled=False）同样有三个键：「图没开」本身就是要留痕的事实，
     缺键会让「没开图」和「老版本代码没这个字段」无法区分。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.config.graph_profile import GRAPH_PROMPT_VERSION, GraphProfile
from app.config import graph_profile
from app.services import caliber
from app.services.caliber import build_caliber_snapshot


def _patch_settings(monkeypatch, *, graph_enabled: bool, llm_model: str) -> None:
    """钉死 settings 的图相关字段，避免受宿主 .env 影响。

    ★ 必须同时 patch 两个模块的 `get_settings`：`caliber` 用它取 graph_enabled，
      `graph_profile.current_graph_profile()` 用它解析 extractor_model ——
      只 patch 一边会出现「快照说没开图、口径却是开图解析值」的错位替身。
    """
    settings = SimpleNamespace(
        graph_enabled=graph_enabled,
        graph_extractor_model="",  # 空 = 跟随 llm_model（由 graph_profile 解析）
        llm_model=llm_model,
        embedding_model="e-test",
        rerank_model="r-test",
    )
    monkeypatch.setattr(caliber, "get_settings", lambda: settings)
    monkeypatch.setattr(graph_profile, "get_settings", lambda: settings)


def test_snapshot_carries_graph_keys_when_enabled(monkeypatch):
    _patch_settings(monkeypatch, graph_enabled=True, llm_model="llm-test")
    profile = GraphProfile(extractor_model="extractor-actual", prompt_version="gp-x")
    monkeypatch.setattr(caliber, "current_graph_profile", lambda: profile)

    snap = build_caliber_snapshot()

    assert snap["graph_enabled"] is True
    assert snap["graph_extractor_model"] == "extractor-actual"
    assert snap["graph_prompt_version"] == "gp-x"


def test_snapshot_carries_graph_keys_when_disabled(monkeypatch):
    """关闭态照样写键：缺键 = 没开图与老版本无法区分。"""
    _patch_settings(monkeypatch, graph_enabled=False, llm_model="llm-test")
    monkeypatch.setattr(
        caliber, "current_graph_profile",
        lambda: GraphProfile(extractor_model="llm-test", prompt_version=GRAPH_PROMPT_VERSION),
    )

    snap = build_caliber_snapshot()

    assert snap["graph_enabled"] is False
    assert snap["graph_extractor_model"] == "llm-test"
    assert snap["graph_prompt_version"] == GRAPH_PROMPT_VERSION


def test_extractor_model_is_resolved_not_empty(monkeypatch):
    """graph_extractor_model 空配置时解析为 llm_model 实际值，不落空串。"""
    _patch_settings(monkeypatch, graph_enabled=True, llm_model="real-model-v2")

    snap = build_caliber_snapshot()

    assert snap["graph_extractor_model"] == "real-model-v2"
    assert snap["graph_extractor_model"] != ""


def test_graph_keys_do_not_regress_on_real_settings():
    """不 patch 的真实配置下，三个键也存在且 extractor_model 非空（契约级兜底）。"""
    snap = build_caliber_snapshot()
    assert isinstance(snap["graph_enabled"], bool)
    assert snap["graph_extractor_model"]
    assert snap["graph_prompt_version"]
