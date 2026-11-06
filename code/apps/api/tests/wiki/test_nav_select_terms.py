"""`select_nav_terms` 的表驱动回归（设计 §2.2.3 / §4 段 A，≥8 例）。

纯函数、零 IO：覆盖「双向包含去重 / 原问句已有 / alias 重复 / 超 FTS 预算整词跳过 /
长词优先 / 全序可复现 / 停用词 / 空页」八类，另加 ASCII 短词、纯数字、单字、
入参不被修改等边界。

★ 期望值全部写死（不调用 `to_fts_text` 去算期望）—— 否则是在用被测链路验证
  被测链路。`to_fts_text` 是入库侧真源，本次不改它，写死期望才钉得住「选词逻辑」。
"""
from __future__ import annotations

import pytest

from app.services.wiki.navigate import select_nav_terms


# (用例 id, 页术语, query 词元, query 归一文本, 词表别名写法, 期望顶层词元, 期望索引词元)
_CASES = [
    (
        "empty_page", [], ["报销"], "报销流程", set(), [], [],
    ),
    (
        "bidirectional_dedup_term_contains_query",
        ["报销制度", "差旅费"], ["报销"], "报销", set(),
        ["差旅费"], ["差旅", "旅费"],
    ),
    (
        "bidirectional_dedup_query_contains_term",
        ["报销"], ["报销制度"], "报销制度与流程", set(),
        [], [],
    ),
    (
        "already_present_in_original_question",
        ["差旅费"], [], "请问差旅费怎么报销", set(),
        [], [],
    ),
    (
        "alias_form_duplicate",
        ["访问令牌", "accesstoken"], [], "认证怎么配置", {"访问令牌"},
        ["accesstoken"], ["accesstoken"],
    ),
    (
        "fts_budget_skips_whole_term_not_half_grams",
        ["访问控制列表", "缓存机制"], [], "系统架构", set(),
        ["访问控制列表"], ["访问", "问控", "控制", "制列", "列表"],
    ),
    (
        "longest_first_and_max_terms_cap",
        ["缓存", "访问控制列表", "差旅费"], [], "系统架构", set(),
        ["访问控制列表", "缓存"],
        ["访问", "问控", "控制", "制列", "列表", "缓存"],
    ),
    (
        "stopwords_ascii_noise_and_digits_dropped",
        ["为什么", "如何", "vs", "js", "1234", "get_data"], [], "系统架构", set(),
        ["get_data"], ["get_data"],
    ),
    (
        "single_cjk_char_below_minimum",
        ["缓存", "甲"], [], "系统架构", set(),
        ["缓存"], ["缓存"],
    ),
    (
        "ascii_minimum_length_accepted",
        ["abcd"], [], "系统架构", set(),
        ["abcd"], ["abcd"],
    ),
]


@pytest.mark.parametrize(
    ("page_terms", "query_terms", "query_norm", "alias_forms", "want_terms", "want_fts"),
    [c[1:] for c in _CASES],
    ids=[c[0] for c in _CASES],
)
def test_select_nav_terms_table_driven(
    page_terms, query_terms, query_norm, alias_forms, want_terms, want_fts,
):
    got_terms, got_fts = select_nav_terms(
        page_terms, query_terms, query_norm, alias_forms,
    )
    assert got_terms == want_terms
    assert got_fts == want_fts
    # 两个上限是代码硬上限（不因输入而破）
    assert len(got_terms) <= 2
    assert len(got_fts) <= 6


def test_full_order_is_reproducible_regardless_of_input_order():
    """页级 key_terms 是字典序 sorted；选词重排为「长度降序 + 字典序破平局」。

    同一组术语换个输入顺序 MUST 得到同一结果（主文档 §17 必答项 8 的排序键全序）。
    """
    forward = ["缓存", "访问控制列表", "差旅费", "访问令牌"]
    backward = list(reversed(forward))
    shuffled = ["访问令牌", "差旅费", "缓存", "访问控制列表"]
    baseline = select_nav_terms(forward, [], "系统架构", set())
    assert select_nav_terms(backward, [], "系统架构", set()) == baseline
    assert select_nav_terms(shuffled, [], "系统架构", set()) == baseline


def test_inputs_are_not_mutated():
    """纯函数契约：不修改任何入参（调用方还要用 `query_terms` 造 pattern / needle）。"""
    page_terms = ["访问控制列表", "缓存机制"]
    query_terms = ["销量"]
    alias_forms = {"访问令牌"}
    before = (list(page_terms), list(query_terms), set(alias_forms))
    select_nav_terms(page_terms, query_terms, "销量怎么统计", alias_forms)
    assert (page_terms, query_terms, alias_forms) == before


def test_selected_terms_are_the_single_source_for_both_paths():
    """ILIKE 与 FTS 复用同一次选词结果，不出现「ILIKE 扩了、FTS 没扩」的口径分裂。"""
    terms, fts = select_nav_terms(["访问控制列表"], [], "系统架构", set())
    assert terms == ["访问控制列表"]
    # 每个顶层词元的索引词元都在结果里，且去重后保序
    assert fts == ["访问", "问控", "控制", "制列", "列表"]
    assert len(fts) == len(set(fts))
