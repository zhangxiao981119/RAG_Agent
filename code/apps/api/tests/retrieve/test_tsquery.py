"""关键词切分与查询构造单元测试。

覆盖点：中文重叠 2-gram（入库/查询两侧共用同一规则）、3 字窗口 OR 连接口径、
ASCII 整词、单字处理、操作符安全、预算截断、全角归一化、ILIKE 词元筛选。
背景：PostgreSQL 'simple' 配置不切中文，未预切分时 65.5% 的纯中文问题完全不命中
全文索引；而「全 AND」口径等于整句短语匹配，实测 206 条评测用例 0 命中。
"""
from __future__ import annotations

from app.config import decisions
from app.services.keyword_text import build_fts_query, build_ilike_terms, to_fts_text


# ── 入库侧切分 ────────────────────────────────────────


def test_to_fts_text_cjk_bigrams():
    assert to_fts_text("并发编程") == "并发 发编 编程"


def test_to_fts_text_punctuation_breaks_bigrams():
    """跨标点不生成 bigram —— 否则会拼出跨越词/句边界的碎片。"""
    assert to_fts_text("报销标准，1000元") == "报销 销标 标准 元 1000"


def test_to_fts_text_single_char_run_kept():
    """单字段原样保留，否则单字查询无法命中。"""
    assert to_fts_text("元") == "元"


def test_to_fts_text_ascii_kept_whole():
    """ASCII 不切 bigram：标识符与英文单词要保持整词语义。"""
    assert to_fts_text("get_retrieval_service") == "get_retrieval_service"


def test_to_fts_text_includes_heading():
    assert to_fts_text("正文内容", "标题") == "正文 文内 内容 标题"


def test_to_fts_text_nfkc_normalizes_fullwidth():
    """全角字母数字先归一化，否则索引里的 token 与查询侧对不上。"""
    assert to_fts_text("ＡＢＣ １２３") == "abc 123"


def test_to_fts_text_empty_returns_empty_string():
    assert to_fts_text(None, "") == ""


# ── 查询侧：3 字窗口 OR ────────────────────────────────


def test_pure_chinese_produces_window_query():
    """改造前纯中文返回 None（完全不命中索引），现在是 3 字窗口查询。"""
    assert build_fts_query("并发编程") == "((并发 & 发编) | (发编 & 编程))"


def test_windows_joined_with_or_not_and():
    """窗口之间 MUST 是 OR —— 全 AND 等于整句短语匹配，自然问句 0 命中。"""
    q = build_fts_query("公司福利补贴包含哪些项目")
    assert q is not None and " | " in q


def test_ascii_tokens_joined_with_or():
    """ASCII 组内保持 OR：召回路径优先保召回率。"""
    assert build_fts_query("Java Spring Boot") == "(java | spring | boot)"


def test_mixed_query_and_groups():
    assert (
        build_fts_query("Java 并发编程")
        == "((并发 & 发编) | (发编 & 编程)) & (java)"
    )


def test_groups_are_parenthesized():
    """tsquery 的 & 与 | 同优先级且左结合，不加括号会让 OR 泄漏成整句条件。"""
    q = build_fts_query("Java 并发编程")
    assert q is not None and q.startswith("(") and ") & (" in q


def test_short_run_kept_as_single_term():
    """2 字词元没有窗口可折叠，按单个 bigram 匹配。"""
    assert build_fts_query("报销") == "(报销)"


def test_single_char_run_dropped_when_other_tokens_exist():
    """「的」这类单字段在问句里只带来噪声，有其他词元时丢弃。"""
    assert build_fts_query("的 并发编程") == "((并发 & 发编) | (发编 & 编程))"


def test_single_char_query_kept():
    """整条查询只剩单字时保留，否则单字查询无法命中。"""
    assert build_fts_query("税") == "(税)"


def test_duplicate_windows_deduped():
    """重复词元去重：否则 tsquery 被撑大、且放大 ts_rank 权重。"""
    assert build_fts_query("java java JAVA") == "(java)"
    # 重复的 8 字段只产出 4 个互不相同的窗口，而非 6 个
    q = build_fts_query("并发编程并发编程")
    assert q is not None and q.count("&") == 4
    assert q.count("(并发 & 发编)") == 1


def test_tsquery_operators_in_input_are_neutralized():
    """用户输入里的 tsquery 操作符 MUST NOT 进入查询串。

    只提取 CJK 连续段与 [A-Za-z0-9_] 词元，因此 & | ! ( ) : 等操作符天然被过滤 ——
    这比事后转义更可靠（转义漏一个字符就是注入）。
    """
    assert (
        build_fts_query("Java & Spring | (Boot: 9999) ! 奖励")
        == "(奖励) & (java | spring | boot | 9999)"
    )


def test_term_budget_is_capped():
    """词元过多会拖慢查询，必须截断。"""
    q = build_fts_query(" ".join(f"token{i}" for i in range(40)))
    assert q is not None
    assert q.count("|") == decisions.FTS_QUERY_MAX_TERMS - 1


def test_window_count_is_capped():
    """长中文段能生出上百个窗口，超出预算要截断。"""
    # 用互不相同的汉字，否则窗口会被去重折叠成一个
    q = build_fts_query("".join(chr(0x4E00 + i) for i in range(200)))
    assert q is not None
    assert q.count("|") == decisions.FTS_QUERY_MAX_TERMS - 1


def test_long_input_is_truncated_before_regex():
    q = build_fts_query("Java " + "x" * 10000 + " Spring")
    assert q is not None
    assert q.startswith("(java")


def test_empty_query_returns_none():
    assert build_fts_query("") is None
    assert build_fts_query("，。！？") is None


# ── 查询侧：ILIKE 子串词元 ────────────────────────────


def test_ilike_terms_keep_long_chinese_run():
    """中文段按子串匹配，超过 8 字截断（尾部多为语气/修饰）。"""
    assert build_ilike_terms("差旅费报销需要提供哪些凭证") == ["差旅费报销需要提"]


def test_ilike_terms_drop_short_chinese_run():
    """2 字段不进 ILIKE：这类词会命中全库，交给 3 字窗口的 FTS 路。"""
    assert build_ilike_terms("报销 标准") == []


def test_ilike_terms_keep_identifier_and_long_ascii():
    """标识符按子串语义保留（`get_retrieval` 命中 `get_retrieval_service`）。"""
    assert build_ilike_terms("调用 get_retrieval 和 kafka") == [
        "get_retrieval",
        "kafka",
    ]


def test_ilike_terms_sorted_by_length_and_capped():
    terms = build_ilike_terms("一 甲乙丙丁戊己 甲乙丙丁 甲乙丙 甲乙 甲")
    assert terms == sorted(terms, key=len, reverse=True)
    assert len(terms) <= decisions.ILIKE_MAX_TERMS


def test_ilike_terms_escape_specials_by_regex_charset():
    """词元只来自中文段与 [A-Za-z0-9_]，正则元字符不会混进来。"""
    assert build_ilike_terms(".*+?[](){}|^$ 报销流程") == ["报销流程"]


def test_ilike_terms_empty_when_no_eligible_token():
    assert build_ilike_terms("") == []
    assert build_ilike_terms("什么 如何") == []
