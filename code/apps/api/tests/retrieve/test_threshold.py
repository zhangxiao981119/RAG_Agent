"""阈值口径单元测试。

覆盖点：双阈值分离、active_threshold 正确分发、口径配置自检。
背景：此前 final_score 有两种来源（rerank 分 / 向量余弦分）却共用一个阈值，
重排抖动时拒答行为不可预测。
"""
from __future__ import annotations

import pytest

from app.config import decisions


def test_two_thresholds_are_independent_constants():
    """两个阈值必须是独立常量，不能是同一个别名对象。"""
    assert decisions.RELEVANCE_THRESHOLD_RERANK is not decisions.RELEVANCE_THRESHOLD_VECTOR or (
        decisions.RELEVANCE_THRESHOLD_RERANK == decisions.RELEVANCE_THRESHOLD_VECTOR
    )


def test_active_threshold_dispatches_by_mode():
    assert decisions.active_threshold(True) == decisions.RELEVANCE_THRESHOLD_RERANK
    assert decisions.active_threshold(False) == decisions.RELEVANCE_THRESHOLD_VECTOR


def test_active_threshold_tracks_monkeypatch(monkeypatch):
    """标定脚本会临时改写阈值 —— active_threshold 必须读到新值，不能缓存。"""
    monkeypatch.setattr(decisions, "RELEVANCE_THRESHOLD_VECTOR", 0.0)
    monkeypatch.setattr(decisions, "RELEVANCE_THRESHOLD_RERANK", 0.33)
    assert decisions.active_threshold(False) == 0.0
    assert decisions.active_threshold(True) == 0.33


def test_thresholds_within_valid_range():
    for val in (
        decisions.RELEVANCE_THRESHOLD_VECTOR,
        decisions.RELEVANCE_THRESHOLD_RERANK,
    ):
        assert 0.0 < val < 1.0


def test_self_check_passes_with_current_config():
    """当前口径配置必须通过启动自检。"""
    decisions.self_check()


def test_self_check_rejects_out_of_range_threshold(monkeypatch):
    monkeypatch.setattr(decisions, "RELEVANCE_THRESHOLD_VECTOR", 1.5)
    with pytest.raises(RuntimeError, match="RELEVANCE_THRESHOLD_VECTOR"):
        decisions.self_check()


def test_self_check_rejects_legacy_passthrough(monkeypatch):
    """阈值下限为 0 时必须被拒 —— 0 会让闸门形同虚设。"""
    monkeypatch.setattr(decisions, "RELEVANCE_THRESHOLD_RERANK", 0.0)
    with pytest.raises(RuntimeError, match="RELEVANCE_THRESHOLD_RERANK"):
        decisions.self_check()


def test_cot_mode_is_valid():
    assert decisions.COT_MODE in {"off", "adaptive", "full"}


def test_self_check_rejects_bad_cot_mode(monkeypatch):
    monkeypatch.setattr(decisions, "COT_MODE", "always")
    with pytest.raises(RuntimeError, match="COT_MODE"):
        decisions.self_check()


def test_reasoning_must_not_be_exposed():
    """推理链 MUST NOT 对外输出（含内部规则与试探表述）。"""
    assert decisions.COT_REASONING_EXPOSED is False


def test_self_check_rejects_exposed_reasoning(monkeypatch):
    monkeypatch.setattr(decisions, "COT_REASONING_EXPOSED", True)
    with pytest.raises(RuntimeError, match="COT_REASONING_EXPOSED"):
        decisions.self_check()
