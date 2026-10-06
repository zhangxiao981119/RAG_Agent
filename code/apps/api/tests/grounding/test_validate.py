"""引用双向校验单元测试。

覆盖点：幻觉引用识别、超量截断、不足标记、不凭空补齐。
"""
from __future__ import annotations

from app.services.grounding.validate import (
    MAX_CITATIONS,
    extract_cited_ns,
    validate_citations,
)


def test_extract_ns_in_order_without_duplicates():
    assert extract_cited_ns("结论甲 [2]，结论乙 [1]，又见 [2]。") == [2, 1]


def test_extract_returns_empty_when_no_citations():
    assert extract_cited_ns("这是一段没有引用的文字。") == []


def test_valid_citations_pass_through():
    result = validate_citations("答案 [1][3]", retrieved_count=5)
    assert result.used_ns == [1, 3]
    assert result.hallucinated == []
    assert result.insufficient is False


def test_out_of_range_citation_is_flagged_as_hallucinated():
    """模型引用了不存在的片段编号 —— 必须被识别为幻觉引用。"""
    result = validate_citations("答案 [1][9]", retrieved_count=3)
    assert result.used_ns == [1]
    assert result.hallucinated == [9]


def test_zero_and_negative_are_hallucinated():
    """引用编号从 1 开始，[0] 不是合法引用。"""
    result = validate_citations("答案 [0]", retrieved_count=3)
    assert result.used_ns == []
    assert result.hallucinated == [0]
    assert result.insufficient is True


def test_excess_citations_are_truncated():
    """引用过多会稀释可读性，必须截断。"""
    text = "".join(f"[{i}]" for i in range(1, MAX_CITATIONS + 4))
    result = validate_citations(text, retrieved_count=20)
    assert len(result.used_ns) == MAX_CITATIONS
    assert result.dropped_ns == [MAX_CITATIONS + 1, MAX_CITATIONS + 2, MAX_CITATIONS + 3]


def test_no_retrieved_chunks_means_insufficient():
    result = validate_citations("答案 [1]", retrieved_count=0)
    assert result.used_ns == []
    assert result.insufficient is True


def test_answer_without_citation_is_insufficient():
    """有答案但零引用 —— 依据不足，调用方需要决定是否降级为拒答。"""
    result = validate_citations("这是一段没有引用的结论。", retrieved_count=5)
    assert result.used_ns == []
    assert result.insufficient is True


def test_does_not_invent_missing_citations():
    """★ 关键语义：不足时 MUST NOT 从检索结果凭空补齐。

    曾经的实现会在引用不足时「从检索结果补 top 页」，
    结果答案挂上了模型从未使用的来源 —— 那是误导，不是修复。
    """
    result = validate_citations("结论 [2]", retrieved_count=8)
    assert result.used_ns == [2]           # 只有模型真正用到的
    assert len(result.used_ns) == 1
    assert result.insufficient is False    # 1 条达到下限


def test_mixed_hallucination_and_valid():
    result = validate_citations("甲 [1] 乙 [7] 丙 [2] 丁 [99]", retrieved_count=4)
    assert result.used_ns == [1, 2]
    assert result.hallucinated == [7, 99]
