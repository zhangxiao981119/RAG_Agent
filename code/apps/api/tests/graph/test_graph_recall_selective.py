"""图召回「按需路由」（GRAPH_RECALL_SELECTIVE）纯函数单测。

函数：`app.services.retrieve._graph_should_recall`（定义于 retrieve/__init__.py:45）。

钉住两件事：
  1. 默认关（graph_recall_selective=False）时恒 True —— 与未引入路由逐字节相同，基线零影响；
  2. 开启时：仅当 top1.final_score >= GRAPH_SKIP_SCORE(=0.70) **且**
     retrieved_n >= GRAPH_SKIP_MIN_CHUNKS(=8) 同时成立才跳过，否则仍召回。
     边界都要覆盖：刚好达标 → 跳过；任一维度差一点点 → 召回。

MUST NOT 改生产代码；本文件只测纯函数，不触 DB。每个用例用函数级 monkeypatch
固定开关，避免 off→on 之间的状态泄漏（get_settings 是单例缓存）。
"""
from __future__ import annotations

import pytest

from app.config import decisions
from app.config.settings import get_settings
from app.services.retrieve import _graph_should_recall


# ── 组 1：默认关 = 逐字节相同基线 ──
def test_default_off_always_recalls_low_score(monkeypatch):
    monkeypatch.setattr(get_settings(), "graph_recall_selective", False)
    assert _graph_should_recall(0.0, 0) is True


def test_default_off_always_recalls_high_score_and_many_chunks(monkeypatch):
    monkeypatch.setattr(get_settings(), "graph_recall_selective", False)
    assert _graph_should_recall(1.0, 100) is True


def test_default_off_always_recalls_mid_score(monkeypatch):
    monkeypatch.setattr(get_settings(), "graph_recall_selective", False)
    assert _graph_should_recall(0.5, 50) is True


# ── 组 2：selective 开启 —— 两维「且」判定，显式覆盖边界 ──
def test_selective_on_skips_at_exact_boundary(monkeypatch):
    """边界：top1 等于阈值常量、召回数等于门槛常量 → 跳过（两维同时达标）。"""
    monkeypatch.setattr(get_settings(), "graph_recall_selective", True)
    assert _graph_should_recall(decisions.GRAPH_SKIP_SCORE, decisions.GRAPH_SKIP_MIN_CHUNKS) is False


def test_selective_on_recalls_when_score_just_below(monkeypatch):
    """边界：top1 低于阈值常量一点点、召回数充足 → 仍召回。"""
    monkeypatch.setattr(get_settings(), "graph_recall_selective", True)
    assert _graph_should_recall(decisions.GRAPH_SKIP_SCORE - 1e-6, decisions.GRAPH_SKIP_MIN_CHUNKS) is True


def test_selective_on_recalls_when_chunks_just_below(monkeypatch):
    """边界：召回数比门槛常量少一个、top1 达标 → 仍召回。"""
    monkeypatch.setattr(get_settings(), "graph_recall_selective", True)
    assert _graph_should_recall(decisions.GRAPH_SKIP_SCORE, decisions.GRAPH_SKIP_MIN_CHUNKS - 1) is True


def test_selective_on_skips_when_both_above_boundary(monkeypatch):
    monkeypatch.setattr(get_settings(), "graph_recall_selective", True)
    assert _graph_should_recall(1.0, 100) is False


def test_selective_on_recalls_when_score_above_but_chunks_low(monkeypatch):
    monkeypatch.setattr(get_settings(), "graph_recall_selective", True)
    assert _graph_should_recall(0.5, 50) is True


def test_selective_on_skips_when_score_high_and_chunks_at_boundary(monkeypatch):
    """top1=0.90 高于阈值、n=8 达标 → 跳过。"""
    monkeypatch.setattr(get_settings(), "graph_recall_selective", True)
    assert _graph_should_recall(0.90, 8) is False


def test_selective_on_recalls_when_both_below(monkeypatch):
    """两维都不达标 → 召回。"""
    monkeypatch.setattr(get_settings(), "graph_recall_selective", True)
    assert _graph_should_recall(0.5, 1) is True


# ── 组 3：路由阈值与站位常量一致性 ──
def test_skip_thresholds_match_standing_constants():
    """钉住当前标定值，防止误改。"""
    assert decisions.GRAPH_SKIP_SCORE == 0.70
    assert decisions.GRAPH_SKIP_MIN_CHUNKS == 8


def test_skip_thresholds_driven_by_constants(monkeypatch):
    """用常量本身复算边界，证明函数与常量同源（不再写死字面量）。

    开启 selective 后，刚好达标 → 跳过；任一维度差一点 → 召回。"""
    monkeypatch.setattr(get_settings(), "graph_recall_selective", True)
    score = decisions.GRAPH_SKIP_SCORE
    n = decisions.GRAPH_SKIP_MIN_CHUNKS
    assert _graph_should_recall(score, n) is False
    assert _graph_should_recall(score - 1e-6, n) is True
    assert _graph_should_recall(score, n - 1) is True
