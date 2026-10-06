"""RRF 融合单元测试。

覆盖点：多路召回的融合正确性、排序稳定性、空输入。
这是「混合检索」能力的核心算法，此前 0 测试。
"""
from __future__ import annotations

import uuid

from app.config import decisions
from app.services.retrieve import _build_rrf_scores


def _ids(n: int) -> list[uuid.UUID]:
    return [uuid.UUID(int=i + 1) for i in range(n)]


def test_single_path_scores_are_reciprocal_rank():
    """单路：分数应为 1/(k+rank+1)。"""
    a, b, c = _ids(3)
    scores = _build_rrf_scores([a, b, c])
    k = decisions.RRF_K
    assert scores[a] == 1.0 / (k + 1)
    assert scores[b] == 1.0 / (k + 2)
    assert scores[c] == 1.0 / (k + 3)


def test_two_paths_accumulate():
    """双路命中同一文档时分数累加（这是 RRF 的核心价值）。"""
    a, b = _ids(2)
    # 第一路：a 排第 1；第二路：a 排第 2
    scores = _build_rrf_scores([a, b], [b, a])
    k = decisions.RRF_K
    assert scores[a] == 1.0 / (k + 1) + 1.0 / (k + 2)
    assert scores[b] == 1.0 / (k + 2) + 1.0 / (k + 1)


def test_three_paths_are_supported():
    """三路召回（向量 / tsquery / ILIKE）必须能被融合。"""
    a, b = _ids(2)
    scores = _build_rrf_scores([a], [a], [b])
    k = decisions.RRF_K
    assert scores[a] == 2.0 / (k + 1)
    assert scores[b] == 1.0 / (k + 1)


def test_multi_path_hit_outranks_single_path_hit():
    """被多路召回的文档，应排在只被单路召回的文档之前。"""
    multi, single = _ids(2)
    scores = _build_rrf_scores([multi, single], [multi], [multi])
    assert scores[multi] > scores[single]


def test_empty_input_returns_empty():
    assert _build_rrf_scores() == {}
    assert _build_rrf_scores([], []) == {}


def test_duplicate_within_one_path_is_not_double_counted_by_caller():
    """同一路径内若传入重复 id，函数按位次累计（调用方负责去重）。

    本测试锁定当前语义：函数不做去重，去重由调用方的 all_chunks 字典保证。
    若将来改为内部去重，本测试会失败 —— 那是有意的信号。
    """
    a = _ids(1)[0]
    scores = _build_rrf_scores([a, a])
    k = decisions.RRF_K
    assert scores[a] == 1.0 / (k + 1) + 1.0 / (k + 2)
