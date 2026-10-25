"""型号/标识符归一化的单元测试 —— P-B 领域词扩展。

覆盖点：
  1. 型号识别：`XYZ-1000` / `xyz1000` / `XYZ 1000` 归一到同一键 `xyz-1000`
  2. ILIKE 正则按「可选分隔符」展开，文档写成哪种形式都能命中
  3. 型号词元不占额外额度，且压掉它的纯数字碎片（`1000`）
  4. FTS 侧补 AND 词对（`xyz & 1000`），索引里字母与数字本就是两个 token
  5. 现有行为不变：无数字的查询不产生型号词元

背景：doc 里写 `XYZ-1000`、用户输入 `xyz1000` 时，索引侧 `_ASCII_RE` 存的是
`xyz` 与 `1000` 两个独立 token，查询侧整词 `xyz1000` 在索引里不存在 —— 静默 0 召回。
"""
from __future__ import annotations

from app.config import decisions
from app.services.keyword_text import (
    build_fts_query,
    build_ilike_pattern,
    build_ilike_terms,
)


# ── 型号识别与归一化 ──────────────────────────────────


def test_model_code_hyphen_form_kept_as_canonical():
    """带连字符的型号取归一形式（连字符是文档里最常见写法）。"""
    assert build_ilike_terms("XYZ-1000 的销量是多少")[0] == "xyz-1000"


def test_model_code_concatenated_normalized():
    """用户写 `xyz1000`，文档写 `XYZ-1000`，MUST 归一到同一键。"""
    assert build_ilike_terms("xyz1000 的处理器是什么配置")[0] == "xyz-1000"


def test_model_code_space_form_normalized():
    """空格分隔同样归一：`XYZ 1000` 与 `XYZ-1000` 是同一个型号。"""
    assert "xyz-1000" in build_ilike_terms("XYZ 1000 的库存状态")


def test_model_code_single_letter_prefix():
    """单字母前缀（`M-1002`）也识别，但只在 ILIKE 路生效（索引侧不存单字母）。"""
    assert build_ilike_terms("m1002 的库存")[0] == "m-1002"


def test_model_code_fullwidth_normalized():
    """全角型号（NFKC 后）与半角等价。"""
    assert build_ilike_terms("ＸＹＺ－１０００")[0] == "xyz-1000"


def test_model_code_drops_digit_fragment():
    """型号已覆盖整段，纯数字碎片（`1000`）MUST 丢弃 —— 它会命中全库。"""
    terms = build_ilike_terms("xyz-1000 的销量是多少")
    assert "1000" not in terms


def test_plain_words_produce_no_model_term():
    """无数字的查询不产生型号词元，行为与改造前一致。"""
    assert build_ilike_terms("调用 get_retrieval 和 kafka") == [
        "get_retrieval",
        "kafka",
        "调用",
    ]


def test_model_term_respects_ilike_max_terms():
    """型号词元也计入 ILIKE 上限，不能突破全局额度。"""
    terms = build_ilike_terms("xyz-1000 报销标准 差旅费 凭证 说明")
    assert len(terms) <= decisions.ILIKE_MAX_TERMS
    assert terms[0] == "xyz-1000"


# ── ILIKE 正则展开 ────────────────────────────────────


def test_pattern_expands_optional_separator():
    """型号词元展开成可选分隔符：`xyz[-_ ]?1000` 覆盖三种写法。"""
    assert build_ilike_pattern(["xyz-1000"]) == r"xyz[-_ ]?1000"


def test_pattern_keeps_plain_terms():
    assert build_ilike_pattern(["销量", "凭证"]) == "销量|凭证"


def test_pattern_escapes_regex_specials():
    """非型号词元只做 re.escape，不引入通配语义。"""
    assert build_ilike_pattern(["a.b"]) == r"a\.b"


def test_pattern_with_lexicon_alias():
    """词表别名作为同一逻辑词元的分支并列 OR。"""
    assert (
        build_ilike_pattern(["accesstoken"], {"accesstoken": ["访问令牌"]})
        == "accesstoken|访问令牌"
    )


def test_pattern_alias_with_space_form_escaped():
    """带空格的写法不是型号词元，按字面转义（不会变成正则通配）。"""
    assert (
        build_ilike_pattern(["xyz-1000"], {"xyz-1000": ["xyz 1000"]})
        == r"xyz[-_ ]?1000|xyz\ 1000"
    )


# ── FTS 侧 AND 词对 ───────────────────────────────────


def test_fts_query_concatenated_model_adds_pair():
    """索引里 `xyz1000` 不存在，但 `xyz` 与 `1000` 都在 —— 补一个 AND 词对。"""
    assert build_fts_query("xyz1000") == "(xyz1000 | (xyz & 1000))"


def test_fts_query_hyphen_model_adds_pair():
    assert build_fts_query("xyz-1000") == "(xyz | 1000 | (xyz & 1000))"


def test_fts_query_single_letter_prefix_skips_pair():
    """单字母前缀不补词对：索引侧 `_ASCII_RE` 长度 >= 2，`m` 根本不在索引里。"""
    assert build_fts_query("M-1002") == "(1002)"


def test_fts_query_no_pair_for_plain_words():
    assert build_fts_query("Java Spring") == "(java | spring)"


def test_fts_query_model_with_chinese_windows():
    """型号词对只进 ASCII 组，中文 3 字窗口组保持独立。"""
    q = build_fts_query("xyz1000 销量")
    assert q == "(销量) & (xyz1000 | (xyz & 1000))"


def test_fts_query_merges_cjk_alias_tokens():
    """中文别名词元并入窗口组做 OR，不新增 AND 条件。"""
    assert build_fts_query("销量", ["访问", "问令"]) == "(销量 | 访问 | 问令)"


def test_fts_query_merges_ascii_alias_tokens():
    assert build_fts_query("销量", ["accesstoken"]) == "(销量) & (accesstoken)"
