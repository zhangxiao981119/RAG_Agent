"""关键词召回的文本切分与查询构造 —— 入库侧与查询侧的唯一真源。

背景：PostgreSQL 的 'simple' 配置不做中文分词，中文整句会变成**单个** token，
`@@` 永远匹配不上较短的查询 —— 评测集里 65.5% 的问题因此完全绕过全文索引。
做法：入库时把中文预切成重叠 2-gram、ASCII 保留整词（`to_fts_text`），查询侧用
同一套规则生成 tsquery（`build_fts_query`）。

★ 两侧 MUST 共用本模块：入库与查询各写一份 bigram 逻辑，只要有一个字符的处理
  不一致（全角/半角、断点位置），索引里存的 token 与查询生成的 token 就对不上，
  表现为**静默 0 召回**（不报错、不告警）。
"""
from __future__ import annotations

import re
import unicodedata

from app.config import decisions

# 中文连续段：汉字扩展 A + 基本汉字 + 日文假名 + 韩文音节。
# 只在这类字符的连续区间内生成 bigram —— 跨标点/空白不生成，避免拼出跨词的无意义片段。
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+")

# ASCII 词元：字母/数字/下划线，长度 >= 2（单字符噪声太大）。
# 与 tsquery 的操作符字符集天然不相交，故不存在注入面。
_ASCII_RE = re.compile(r"[A-Za-z0-9_]{2,}")


def _normalize(text: str) -> str:
    """NFKC 归一化 + 小写：统一全角字母数字与大小写，保证两侧切分结果一致。"""
    return unicodedata.normalize("NFKC", text).lower()


def _bigrams(run: str) -> list[str]:
    """把中文连续段切成重叠 2-gram；单字段原样保留（否则单字查询无法命中）。"""
    if len(run) == 1:
        return [run]
    return [run[i : i + 2] for i in range(len(run) - 1)]


def to_fts_text(*parts: str | None) -> str:
    """把 content / heading_path 切成以空格分隔的索引词元。

    输出直接喂给 `to_tsvector('simple', ...)`：'simple' 只按非字母数字切词并小写，
    正好接受这份预切分结果。空输入返回空串（调用方仍会写入），不返回 None ——
    列上有 NULL 与空串两种"无内容"会让回填脚本的 --check 口径变复杂。
    """
    tokens: list[str] = []
    for part in parts:
        if not part:
            continue
        text = _normalize(part)
        for run in _CJK_RE.findall(text):
            tokens.extend(_bigrams(run))
        tokens.extend(_ASCII_RE.findall(text))
    return " ".join(tokens)


def _dedupe(tokens: list[str]) -> list[str]:
    """去重并保持出现顺序，避免重复词元把 tsquery 撑大。"""
    seen: dict[str, None] = {}
    for token in tokens:
        seen.setdefault(token, None)
    return list(seen)


def build_fts_query(query: str) -> str | None:
    """把查询构造成 tsquery 文本；无可索引词元时返回 None（调用方跳过 FTS 路径）。

    连接口径：**3 字窗口 OR**
      中文连续段先切成重叠 2-gram，再把相邻两个 bigram 用 `&` 拼成一个「3 字窗口」，
      窗口之间用 `|` 连接 —— 语义 = 「文档里出现问句中的任意一个 3 字连续片段」。
      ASCII 词元组内仍用 `|`（优先保召回率），两组之间用 `&`。

    ★ 为什么不是「bigram 全 AND」：那等价于整句短语精确匹配。自然问句整段原样出现
      在 chunk 里几乎不可能，实测 206 条评测用例 0 命中 —— 关键词路形同失效。
    ★ 为什么不是「bigram 全 OR」：中文问句里的「的/是/什么」会命中全库（实测平均
      命中 78 行），关键词路退化成噪声源。3 字窗口把命中压到平均 11.5 行。
    ★ 两组各自加括号：tsquery 里 & 与 | 优先级相同且左结合，不加括号 OR 会泄漏。
    """
    text = _normalize(query[:400])
    runs = sorted(_CJK_RE.findall(text), key=len, reverse=True)[
        : decisions.FTS_QUERY_MAX_CJK_RUNS
    ]
    # 单字连续段（问句里的「的」「是」）在有其他词元时只会引入噪声；整条查询只剩
    # 单字时才保留（否则「税」这类单字查询无法命中）。
    if any(len(run) > 1 for run in runs):
        runs = [run for run in runs if len(run) > 1]

    windows: list[str] = []
    for run in runs:
        grams = _bigrams(run)
        if len(grams) == 1:
            windows.append(grams[0])
        else:
            windows.extend(
                f"({grams[i]} & {grams[i + 1]})" for i in range(len(grams) - 1)
            )
    windows = _dedupe(windows)[: decisions.FTS_QUERY_MAX_TERMS]
    ascii_tokens = _dedupe(_ASCII_RE.findall(text))[: decisions.FTS_QUERY_MAX_TERMS]

    groups: list[str] = []
    if windows:
        groups.append("(" + " | ".join(windows) + ")")
    if ascii_tokens:
        groups.append("(" + " | ".join(ascii_tokens) + ")")
    if not groups:
        return None
    return " & ".join(groups)


def build_ilike_terms(query: str) -> list[str]:
    """构造 ILIKE（子串）路的词元，按长度降序；空列表表示跳过该路。

    ★ 此前是「整句子串」：自然问句整句几乎不可能原样出现在 chunk 里，实测 206 条
      评测用例 0 命中、纯耗 11ms。改为按词元做子串匹配，才真正覆盖两类 FTS 抓不到
      的情形：3 字以下的短词元，以及 ASCII 标识符的子串（`get_retrieval` 命中
      `get_retrieval_service`）。
    ★ 只取够长的词元（中文 >= 3 字、ASCII 标识符/>= 4 字符），否则「什么」「如何」
      这类词会把全库拉进来。
    """
    text = _normalize(query[:400])
    cjk_terms = [run[:8] for run in _CJK_RE.findall(text) if len(run) >= 3]
    ascii_terms = [
        token for token in _ASCII_RE.findall(text) if "_" in token or len(token) >= 4
    ]
    terms = _dedupe(cjk_terms + ascii_terms)
    return sorted(terms, key=len, reverse=True)[: decisions.ILIKE_MAX_TERMS]
