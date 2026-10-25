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

# 型号/料号：1~6 个字母 + 可选分隔符 + 2~6 位数字，例如 XYZ-1000 / xyz1000 / M-1002。
# ★ 前后都要卡边界：否则「java 1000」会被拆出「a 1000」、「xyz1000abc」会被当成型号。
# ★ 分隔符允许 - _ 空格三种 —— 同一型号在文档与提问里的写法各不相同，这是长尾
#   召回失败的主要来源之一（doc 写 XYZ-1000，用户输入 xyz1000）。
_MODEL_CODE_RE = re.compile(r"(?<![a-z0-9])([a-z]{1,6})[-_ ]?(\d{2,6})(?![a-z0-9])")

# 归一后的型号词元（字母-数字）。ILIKE 正则据此展开可选分隔符。
_MODEL_TERM_RE = re.compile(r"^([a-z]{1,6})-(\d{2,6})$")

# ILIKE 路的中文切段词：疑问词与虚词。
# 按「疑问/功能词」切开，才能把语义单元取出来 —— 整段截前 8 字得到的是
# 「差旅费报销需要提」这种任意前缀，要求文档里原样出现，实测 81.6% 的用例
# 词元都是这种前缀，几乎不可能命中。
# ★ 必须长词在前：正则分支同起点取第一个匹配项（「为什么」要先于「什么」）。
# ★ 只在这些词处切，「的/吗/呢」单字切分会把「目的」这类词切碎，但残段
#   长度不足 2 会被丢弃，不会污染词元。
_ILIKE_SPLIT_WORDS = (
    "为什么", "怎么样", "什么样", "是不是", "有没有", "什么", "怎么", "如何",
    "哪些", "多少", "是否", "需要", "请问", "可以", "的", "吗", "呢",
)
_ILIKE_SPLIT_RE = re.compile("|".join(_ILIKE_SPLIT_WORDS))


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


def _model_codes(text: str) -> list[tuple[str, str]]:
    """抽取型号并对 (字母, 数字) 去重，输入须已 `_normalize`。"""
    seen: dict[tuple[str, str], None] = {}
    for letters, digits in _MODEL_CODE_RE.findall(text):
        seen.setdefault((letters, digits), None)
    return list(seen)


def build_fts_query(query: str, alias_tokens: list[str] | None = None) -> str | None:
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
    # ASCII 组：整词 + 型号的 AND 词对。
    # ★ 型号词对解决「索引里只有两个碎片」的问题：doc 写 `XYZ-1000` 时入库侧
    #   `_ASCII_RE` 存的是 `xyz` 与 `1000` 两个独立 token，查询侧整词 `xyz1000`
    #   在索引里不存在 —— 不补词对就是静默 0 召回。
    # ★ 单字母前缀（`M-1002`）不补：入库侧长度 >= 2 已丢弃 `m`，词对永远匹配不上，
    #   这类型号交给 ILIKE 路（扫原文，分隔符写法都能命中）。
    ascii_items = _dedupe(
        _ASCII_RE.findall(text)
        + [f"({letters} & {digits})" for letters, digits in _model_codes(text) if len(letters) >= 2]
    )[: decisions.FTS_QUERY_MAX_TERMS]

    # 词表扩展的别名（已由 lexicon 转成索引词元）：并入对应组做 OR，
    # 不新增 AND 组 —— 扩的是「同一语义的另一种写法」，不是额外的必要条件。
    if alias_tokens:
        cjk_aliases = [tok for tok in alias_tokens if _CJK_RE.fullmatch(tok)]
        ascii_aliases = [tok for tok in alias_tokens if not _CJK_RE.fullmatch(tok)]
        windows = _dedupe(windows + cjk_aliases)[: decisions.FTS_QUERY_MAX_TERMS]
        ascii_items = _dedupe(ascii_items + ascii_aliases)[: decisions.FTS_QUERY_MAX_TERMS]

    groups: list[str] = []
    if windows:
        groups.append("(" + " | ".join(windows) + ")")
    if ascii_items:
        groups.append("(" + " | ".join(ascii_items) + ")")
    if not groups:
        return None
    return " & ".join(groups)


def build_ilike_terms(query: str) -> list[str]:
    """构造 ILIKE（子串）路的词元，按长度降序；空列表表示跳过该路。

    ★ 此前是「整句子串」：自然问句整句几乎不可能原样出现在 chunk 里，实测 206 条
      评测用例 0 命中、纯耗 11ms。改为按词元做子串匹配，才真正覆盖两类 FTS 抓不到
      的情形：短词元，以及 ASCII 标识符的子串（`get_retrieval` 命中
      `get_retrieval_service`）。
    ★ 中文段 MUST 先按疑问/功能词切成语义单元：直接截前 N 字得到的是任意前缀
      （「差旅费报销需要提供哪些凭证」→「差旅费报销需要提」），要求文档原样含有
      这串字，206 条用例里 81.6% 都落在这种前缀上、几乎不命中。切段后取
      「差旅费报销／提供／凭证」，期望文档覆盖从 52/102 升到 88/102。
    ★ 词元上限取 3：实测覆盖在此处饱和（3→4 只多 1 条，4→6 不再增长），
      而上限越高噪声行越多（精度 66%→54%→49%）。这是全局常数，与知识库无关 ——
      决定词元好坏的是「该词元在文档里是否有区分度」，不是它来自哪个库。
    """
    text = _normalize(query[:400])
    codes = _model_codes(text)
    # 型号覆盖整段：数字碎片（`1000`）与拆开的字母前缀（`xyz`）不再单独成词元 ——
    # 前者命中全库，后者在文档里几乎总与型号一起出现，占额度没有增量。
    covered = {
        part
        for letters, digits in codes
        for part in (letters, digits, letters + digits)
    }
    cjk_terms = [
        segment[:8]
        for run in _CJK_RE.findall(text)
        for segment in _ILIKE_SPLIT_RE.split(run)
        if len(segment) >= 2
    ]
    ascii_terms = [
        token
        for token in _ASCII_RE.findall(text)
        if token not in covered and ("_" in token or len(token) >= 4)
    ]
    model_terms = [f"{letters}-{digits}" for letters, digits in codes]
    terms = _dedupe(model_terms + cjk_terms + ascii_terms)
    return sorted(terms, key=len, reverse=True)[: decisions.ILIKE_MAX_TERMS]


def extract_model_terms(text: str) -> list[tuple[str, list[str]]]:
    """抽取型号词条：(归一形式, 三种写法)，供词表构建脚本使用。

    归一形式统一用连字符（文档里最常见），写法变体覆盖无分隔与空格两种。
    """
    return [
        (
            f"{letters}-{digits}",
            [f"{letters}{digits}", f"{letters} {digits}"],
        )
        for letters, digits in _model_codes(_normalize(text))
    ]


def build_ilike_pattern(
    terms: list[str],
    aliases: dict[str, list[str]] | None = None,
) -> str:
    """把词元拼成正则分支；型号词元展开为「可选分隔符」以容忍三种写法。

    `xyz-1000` -> `xyz[-_ ]?1000`，一条分支同时命中 `XYZ-1000` / `xyz1000` /
    `XYZ 1000`。非型号词元只做 re.escape，不引入任何通配语义。

    aliases 是词表扩展结果（键为词元、值为其它写法）：同义词作为**同一逻辑词元**
    的分支并列 OR，不占用 ILIKE_MAX_TERMS 的名额 —— 否则「访问令牌」与
    「accesstoken」会互相挤掉本该保留的中文语义单元。
    """
    aliases = aliases or {}
    parts: list[str] = []
    for term in terms:
        for alt in (term, *aliases.get(term, [])):
            match = _MODEL_TERM_RE.match(alt)
            if match:
                parts.append(
                    f"{re.escape(match.group(1))}[-_ ]?{re.escape(match.group(2))}"
                )
            else:
                parts.append(re.escape(alt))
    return "|".join(parts)
