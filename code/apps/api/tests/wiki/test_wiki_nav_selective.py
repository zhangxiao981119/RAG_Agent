"""wiki 导航「按需路由」（WIKI_NAV_SELECTIVE）纯函数单测。

函数：`app.services.retrieve._wiki_should_nav`（定义于 retrieve/__init__.py:58）。

钉住两件事：
  1. 默认关（wiki_nav_selective=False）时恒 True —— 与未引入路由**逐字节相同**，基线零影响；
  2. 开启时只按词元数路由：len(ilike_terms) < WIKI_NAV_SELECTIVE_MIN_TERMS(=3) → 扩词，
     >= 3 → 跳过。边界 len==2 仍扩、len==3 起跳过。

MUST NOT 改生产代码；本文件只测纯函数，不触 DB。每个用例用函数级 monkeypatch
固定开关，避免 off→on 之间的状态泄漏（get_settings 是单例缓存）。
"""
from __future__ import annotations

import pytest

from app.config import decisions
from app.config.settings import get_settings
from app.services.retrieve import _wiki_should_nav


# ── 组 1：默认关 = 逐字节相同基线（无论其它用例是否改过开关，这里先钉回 False）──
def test_default_off_always_navigates_for_empty_terms(monkeypatch):
    monkeypatch.setattr(get_settings(), "wiki_nav_selective", False)
    assert _wiki_should_nav([]) is True


def test_default_off_always_navigates_for_one_term(monkeypatch):
    monkeypatch.setattr(get_settings(), "wiki_nav_selective", False)
    assert _wiki_should_nav(["a"]) is True


def test_default_off_always_navigates_for_two_terms(monkeypatch):
    monkeypatch.setattr(get_settings(), "wiki_nav_selective", False)
    assert _wiki_should_nav(["a", "b"]) is True


def test_default_off_always_navigates_for_three_terms(monkeypatch):
    monkeypatch.setattr(get_settings(), "wiki_nav_selective", False)
    assert _wiki_should_nav(["a", "b", "c"]) is True


def test_default_off_always_navigates_for_four_terms(monkeypatch):
    monkeypatch.setattr(get_settings(), "wiki_nav_selective", False)
    assert _wiki_should_nav(["a", "b", "c", "d"]) is True


# ── 组 2：selective 开启 —— 仅按词元数路由，且显式覆盖边界 ──
def test_selective_on_navigates_when_terms_below_threshold(monkeypatch):
    monkeypatch.setattr(get_settings(), "wiki_nav_selective", True)
    # len < 3 → 扩词
    assert _wiki_should_nav([]) is True
    assert _wiki_should_nav(["a"]) is True
    assert _wiki_should_nav(["a", "b"]) is True


def test_selective_on_skips_when_terms_at_or_above_threshold(monkeypatch):
    monkeypatch.setattr(get_settings(), "wiki_nav_selective", True)
    # len >= 3 → 跳过
    assert _wiki_should_nav(["a", "b", "c"]) is False
    assert _wiki_should_nav(["a", "b", "c", "d"]) is False


def test_selective_on_boundary_len_2_still_navigates(monkeypatch):
    """边界：len==2 仍 < WIKI_NAV_SELECTIVE_MIN_TERMS(=3) → 扩词。"""
    monkeypatch.setattr(get_settings(), "wiki_nav_selective", True)
    assert len(["a", "b"]) == 2
    assert _wiki_should_nav(["a", "b"]) is True


def test_selective_on_boundary_len_3_skips(monkeypatch):
    """边界：len==3 触发跳过（最保守的活跃值，ILIKE_MAX_TERMS=3）。"""
    monkeypatch.setattr(get_settings(), "wiki_nav_selective", True)
    assert len(["a", "b", "c"]) == 3
    assert _wiki_should_nav(["a", "b", "c"]) is False


def test_selective_on_boundary_len_4_skips(monkeypatch):
    monkeypatch.setattr(get_settings(), "wiki_nav_selective", True)
    assert len(["a", "b", "c", "d"]) == 4
    assert _wiki_should_nav(["a", "b", "c", "d"]) is False


def test_selective_threshold_matches_standing_constant(monkeypatch):
    """路由判据 MUST 与 decisions.WIKI_NAV_SELECTIVE_MIN_TERMS 同源（不要写死 3）。"""
    monkeypatch.setattr(get_settings(), "wiki_nav_selective", True)
    assert decisions.WIKI_NAV_SELECTIVE_MIN_TERMS == 3
    # 刚好少一个词元 → 扩词；刚好达到 → 跳过
    below = ["x"] * (decisions.WIKI_NAV_SELECTIVE_MIN_TERMS - 1)
    at = ["x"] * decisions.WIKI_NAV_SELECTIVE_MIN_TERMS
    assert _wiki_should_nav(below) is True
    assert _wiki_should_nav(at) is False
