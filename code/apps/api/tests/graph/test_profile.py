"""抽取口径（graph_profile / wiki_profile）的回归测试。

钉住三件事：
  1. GRAPH_EXTRACTOR_MODEL 留空 → 解析为 llm_model 的**实际值**（不落空串）；
  2. 显式配置优先；
  3. 指纹是 (model|prompt) 的稳定哈希，长度与 index_profile 一致。
"""
from __future__ import annotations

import pytest

from app.config.graph_profile import (
    GRAPH_PROMPT_VERSION,
    GraphProfile,
    current_graph_profile,
)
from app.config.settings import get_settings
from app.config.wiki_profile import WIKI_PROMPT_VERSION, current_wiki_profile


@pytest.fixture
def settings_env(monkeypatch):
    """改环境变量后清缓存，并保证用例结束后恢复（否则会污染后续用例）。"""
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def test_empty_extractor_model_follows_llm_model(settings_env):
    settings_env.setenv("GRAPH_EXTRACTOR_MODEL", "")
    settings_env.setenv("LLM_MODEL", "some-model-x")
    get_settings.cache_clear()
    profile = current_graph_profile()
    assert profile.extractor_model == "some-model-x"
    assert profile.prompt_version == GRAPH_PROMPT_VERSION


def test_explicit_extractor_model_wins(settings_env):
    settings_env.setenv("GRAPH_EXTRACTOR_MODEL", "graph-special")
    settings_env.setenv("LLM_MODEL", "some-model-x")
    get_settings.cache_clear()
    assert current_graph_profile().extractor_model == "graph-special"


def test_extractor_model_is_never_empty(settings_env):
    """落库口径 MUST NOT 是空串 —— 空串会让「跟随主模型」这段历史无法回溯。"""
    settings_env.setenv("GRAPH_EXTRACTOR_MODEL", "   ")
    get_settings.cache_clear()
    assert current_graph_profile().extractor_model.strip()


def test_fingerprint_shape_and_stability():
    profile = GraphProfile(extractor_model="m", prompt_version="pv")
    assert profile.fingerprint == GraphProfile("m", "pv").fingerprint
    assert len(profile.fingerprint) == 12
    assert profile.fingerprint != GraphProfile("m2", "pv").fingerprint
    assert profile.fingerprint != GraphProfile("m", "pv2").fingerprint


def test_wiki_profile_follows_llm_model(settings_env):
    settings_env.setenv("WIKI_GENERATOR_MODEL", "")
    settings_env.setenv("LLM_MODEL", "some-model-y")
    get_settings.cache_clear()
    wiki = current_wiki_profile()
    assert wiki.generator_model == "some-model-y"
    assert wiki.prompt_version == WIKI_PROMPT_VERSION


def test_prompt_versions_are_non_empty():
    """空版本 = 口径过滤失效（检索 SQL 会匹配到所有历史的空串行）。"""
    assert GRAPH_PROMPT_VERSION.strip()
    assert WIKI_PROMPT_VERSION.strip()
