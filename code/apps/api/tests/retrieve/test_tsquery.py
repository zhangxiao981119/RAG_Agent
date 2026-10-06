"""tsquery 构造单元测试。

覆盖点：只提取 ASCII/数字词元、操作符安全、无词元时返回 None。
背景：此前关键词召回只走 ILIKE，表上已建的 idx_chunk_fts 全文索引未被使用。
"""
from __future__ import annotations

from app.services.retrieve import build_tsquery


def test_ascii_tokens_are_used():
    assert build_tsquery("Java 并发编程") == "java"


def test_multiple_tokens_joined_with_or():
    """OR 连接：关键词召回是召回路径，优先保召回率。"""
    assert build_tsquery("Java Spring Boot") == "java | spring | boot"


def test_numbers_are_included():
    assert build_tsquery("ISO 9001 标准") == "iso | 9001"


def test_pure_chinese_returns_none():
    """纯中文无 ASCII 词元 —— 'simple' 配置无法分词，交给 ILIKE 路径。"""
    assert build_tsquery("报销标准是什么") is None


def test_single_char_tokens_are_ignored():
    """单字符词元噪声太大（如 'a'），长度 >= 2 才纳入。"""
    assert build_tsquery("a b cd") == "cd"


def test_duplicate_tokens_deduped_preserving_order():
    assert build_tsquery("Java java JAVA Spring") == "java | spring"


def test_tsquery_operators_in_input_are_neutralized():
    """用户输入里的 tsquery 操作符 MUST NOT 进入查询串。

    只提取 [A-Za-z0-9_] 词元，因此 & | ! ( ) : 等操作符天然被过滤 ——
    这比事后转义更可靠（转义漏一个字符就是注入）。
    """
    q = build_tsquery("Java & Spring | (Boot: 9999)")
    assert "&" not in q
    assert "(" not in q and ")" not in q and ":" not in q
    assert q == "java | spring | boot | 9999"


def test_version_like_tokens_are_split_and_short_parts_dropped():
    """带点的版本号（3.2）会被拆成单字符而被丢弃。

    这是**有意的**：点在 to_tsquery 里需特殊处理，纳入会引入注入面；
    而 FTS 只是召回辅助路径，完整原串仍由 ILIKE 路径覆盖，
    因此不会因此漏召。
    """
    assert build_tsquery("Spring 3.2") == "spring"


def test_token_count_is_capped():
    """词元过多会拖慢查询，必须截断。"""
    q = build_tsquery(" ".join(f"token{i}" for i in range(30)), max_tokens=5)
    assert q.count("|") == 4  # 5 个词元


def test_very_long_input_is_truncated():
    """超长输入先截断再提取，避免正则在大文本上耗时。"""
    q = build_tsquery("Java " + "x" * 10000 + " Spring")
    assert q is not None
    assert q.startswith("java")
