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
from collections.abc import Sequence

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

# 两个既有私有常量提升为**公开别名**（只改名字，不改值、不改正则）：
# 供 wiki 导航选词的 _acceptable 复用同一份中文判定与停用词表，避免新造第二套
# 规则 —— 两侧不同步的表现是「静默挑错词元」，不报错、只是扩展失效。
CJK_RE = _CJK_RE
ILIKE_STOPWORDS = _ILIKE_SPLIT_WORDS


def _normalize(text: str) -> str:
    """NFKC 归一化 + 小写：统一全角字母数字与大小写，保证两侧切分结果一致。"""
    return unicodedata.normalize("NFKC", text).lower()


def normalize_query_text(text: str) -> str:
    """查询侧归一（与 `_normalize` **同源**，禁止复制第二份归一逻辑）。

    ★ 截到 400 字符与 `build_fts_query` / `build_ilike_terms` 的 `query[:400]` 对齐：
      wiki 选词用本函数的结果判断「这个词元原问句里是否已有」，若与召回侧用的
      不是同一段文本，判定会和实际检索输入分叉。
    """
    return _normalize(text[:400])


def normalize_key_term(term: str) -> str:
    """页级 `key_terms` 的归一：NFKC + 小写 + 去首尾空白 + 折叠内部空白。

    与 `_normalize` 同源（不另写一份 unicodedata 调用）；额外折叠空白是因为
    第 3 步落库的是 LLM 原文术语，可能带换行 / 多空格，而查询词元不含这些。
    ★ 不做 NFKC 之外的语义改写：归一只是让「大小写 / 全角」差消失，
      术语本身仍是短语（`"App Router"`），与查询词元（`router`）靠子串匹配衔接。
    """
    return " ".join(_normalize(term).split())


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


def build_fts_query(
    query: str,
    alias_tokens: list[str] | None = None,
    *,
    extra_index_tokens: Sequence[str] = (),
    alias_max_terms: int = decisions.LEXICON_FTS_ALIAS_MAX_TERMS,
) -> str | None:
    """把查询构造成 tsquery 文本；无可索引词元时返回 None（调用方跳过 FTS 路径）。

    ★ 两个扩展来源都走**独立预算**，原 query 窗口（`FTS_QUERY_MAX_TERMS`）冻结：
      · `alias_tokens`（词表别名，lexicon）：按 `alias_max_terms` 限流后追加，
        不再把合并结果重截回 24（F6 / C4 的静默吞别名缺陷，N6 随本步修）；
      · `extra_index_tokens`（wiki 导航）：已由 `select_nav_terms` 按
        `WIKI_NAV_MAX_FTS_TOKENS` 整词限流，按总 cap 追加。
      两个参数都是 keyword-only 且带默认值 → **所有既有调用点一个实参都不改**；
      两者为空时返回值与改动前**逐字节相同**（Golden Test 钉住，回滚等价第 2 层证据）。

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

    # 词表扩词（lexicon 别名）：并入对应组做 OR，不新增 AND 组 —— 扩的是
    # 「同一语义的另一种写法」，不是额外的必要条件。
    # ★ 独立预算（N6 / C4-F6 修复）：此前把 `windows + 别名` 重截回 24，而
    #   `_dedupe` 保序让原窗口恒在前，被砍掉的**恒是别名** —— 达上限 / 剩余容量
    #   不足的 query 会静默丢掉全部或部分别名，既不报错也不打点（C4 报告 §1.5
    #   合成反例确凿）。现在别名侧按 `alias_max_terms` 单独限流后追加，
    #   `:128` 已截到 24 的原 query 窗口一个不少，也不再重截。
    if alias_tokens:
        extra = list(dict.fromkeys(alias_tokens))[:alias_max_terms]
        cjk_aliases = [tok for tok in extra if _CJK_RE.fullmatch(tok)]
        ascii_aliases = [tok for tok in extra if not _CJK_RE.fullmatch(tok)]
        windows = _dedupe(windows + cjk_aliases)
        ascii_items = _dedupe(ascii_items + ascii_aliases)

    # wiki 导航扩词：第二个独立扩展来源，同样不占原 query 窗口名额。
    # ★ 不得复用上面别名的追加行 —— 那是另一个来源、另一份预算，混用会让
    #   「wiki 扩词」与「词表扩词」互相挤占，评测时无法归因。
    # ★ cap 是 tsquery 总词元硬上限（原窗口 24 + wiki 二级预算 6）；
    #   `extra_index_tokens` 已在 select_nav_terms 里按整词限流，常规情形下
    #   这里的裁剪碰不到原窗口。两个扩展来源同时满额时 wiki 让位给先到的别名，
    #   是 cap 的正常行为（原窗口与别名都保留），不是「原 query 窗口被挤掉」。
    if extra_index_tokens:
        cap = decisions.FTS_QUERY_MAX_TERMS + decisions.WIKI_NAV_MAX_FTS_TOKENS
        cjk_extra = [t for t in extra_index_tokens if _CJK_RE.fullmatch(t)]
        ascii_extra = [t for t in extra_index_tokens if not _CJK_RE.fullmatch(t)]
        windows = _dedupe(windows + cjk_extra)[:cap]
        ascii_items = _dedupe(ascii_items + ascii_extra)[:cap]

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
    *,
    extra_terms: Sequence[str] = (),
) -> str:
    """把词元拼成正则分支；型号词元展开为「可选分隔符」以容忍三种写法。

    `xyz-1000` -> `xyz[-_ ]?1000`，一条分支同时命中 `XYZ-1000` / `xyz1000` /
    `XYZ 1000`。非型号词元只做 re.escape，不引入任何通配语义。

    aliases 是词表扩展结果（键为词元、值为其它写法）：同义词作为**同一逻辑词元**
    的分支并列 OR，不占用 ILIKE_MAX_TERMS 的名额 —— 否则「访问令牌」与
    「accesstoken」会互相挤掉本该保留的中文语义单元。

    ★ `extra_terms`（wiki 导航扩词）是 keyword-only 且默认空：作为**附加 OR 分支**
      追加在既有词元之后，不占 `ILIKE_MAX_TERMS` 名额，也**不修改 `terms` 本身**
      （原词元集合冻结 = 关闭态逐字节等价的前提，设计 §2.2.1 / §6-C7 的致命方案）。
      为空时返回值与改动前**逐字节相同**（Golden Test 钉住），所有既有调用点零改动。
    """
    aliases = aliases or {}
    parts: list[str] = []
    # 把 extra_terms 接进同一循环：型号展开 / re.escape 只保留一份逻辑，
    # 且 extra_terms=() 时 `(*terms, *())` 的遍历顺序与改动前完全一致。
    for term in (*terms, *extra_terms):
        for alt in (term, *aliases.get(term, [])):
            match = _MODEL_TERM_RE.match(alt)
            if match:
                parts.append(
                    f"{re.escape(match.group(1))}[-_ ]?{re.escape(match.group(2))}"
                )
            else:
                parts.append(re.escape(alt))
    return "|".join(parts)


_SNIPPET_LOOKBACK: int = 30
"""命中点向左回看的字符数：窗口起点 = max(0, 命中点 - 30)。

30 字约一到两个短句 —— 足够带上「主语 + 谓语」让片段可独立阅读，
又不会把回看区撑大到吞掉证据词。纯展示调优值，不参与任何检索判定。
"""


def _find_probe_hit(content: str, query: str) -> int:
    """找 query 在 content 里的首个命中点；无命中返回 -1（hit_snippet 专用）。

    两层探测，与检索路同源：
      1. 词元层：**逐词元** search（`build_ilike_terms` 已按长度降序 → 长词元
         优先），每个词元用 `build_ilike_pattern([term])` 的容错正则 + IGNORECASE，
         取 `m.start()`。★ 两处必须坚持的细节：
         · 必须用 pattern 而不是 `find(term)`：型号词元 `ax-3000` 在检索路被展开成
           `ax[-_ ]?3000`，能匹配 `AX3000` / `ax-3000` / `ax_3000` / `ax 3000`
           四种写法 —— 精确探测 `ax-3000` 会对 `AX3000` 永不命中，长 chunk 含型号
           就复现「snippet 停在片头」的用户投诉同款问题。pattern 全 re.escape，
           无注入面。
         · 必须逐词元循环，**不能**把全部词元 OR 成一条正则：`re.search` 对 OR
           分支取**最左**命中，多词元时短词元（如「报销」）在片头先出现，窗口会
           静默锚回片头 —— 设计意图是「长词元更具体、更可能是用户要的证据」，
           长词元优先序必须保留。
      2. CJK 2-gram 层：与 FTS 路同一套切分（见 hit_snippet docstring）。
    """
    for term in build_ilike_terms(query):
        match = re.search(build_ilike_pattern([term]), content, re.IGNORECASE)
        if match:
            return match.start()
    text = _normalize(query[:400])
    for gram in _dedupe(g for run in _CJK_RE.findall(text) for g in _bigrams(run)):
        pos = content.lower().find(gram)
        if pos >= 0:
            return pos
    return -1


def hit_snippet(content: str, query: str, width: int = 200) -> str:
    """按**命中位置**开窗的引用片段 —— citations.snippet 的构造函数。

    ★ 这是**纯展示字段**：只影响 citations 事件里的 snippet（用户引用面板看到的
      预览文本），不影响 [n] 编号、不影响喂给生成的 prompt、不参与任何检索判定
      —— 编号与上下文仍由 `assemble_context_chunks` 的顺序决定，两边互不知晓。

    背景：固定取 `content[:200]` 时，长 chunk 的前 200 字常常是开场白（如「报销
    流程概述」），真正的证据（「上班时间为 9:00-12:00」）在同一条 chunk 的中段，
    引用面板看起来像「没引用到」。把窗口挪到命中点附近，片段才与用户的问题对得上。

    语义（词元与检索的 ILIKE / FTS 路同源 —— 都来自本模块的切分规则，口径一致）：
      1. 词元层：**逐词元** search（长词元优先，`build_ilike_terms` 已按长度降序），
         每个词元走 `build_ilike_pattern([term])` 的容错正则（型号词元展开为
         `ax[-_ ]?3000`）+ `IGNORECASE`，取 `m.start()` 为命中点 —— 与检索路
         ILIKE **同源同语义**：检索能命中的 chunk（`AX3000` 能被 `ax[-_ ]?3000`
         匹配），开窗探测必须同样能命中，否则「检索明明召回了、snippet 却停在片头」。
         pattern 全 re.escape，无注入面；取 start 而不是整段 match：分隔符写法
         （`ax 3000`）匹配到的是含分隔符的整段，证据的开头是 start。
         ★ 逐词元而不是 OR 成一条正则：`re.search` 对 OR 分支取最左命中，
         多词元时短词元在片头先出现会把窗口静默锚回片头（见 _find_probe_hit）。
         长词元更具体、更可能是用户要的证据，优先序必须保留。
      2. CJK 2-gram 兜底（与 `to_fts_text` / FTS 路同一套切分）。★ 这一层不是
         可选优化：像「公司几点上班」这种不含疑问切分词的问句，整段会成一个词元、
         几乎不可能原样出现在文档里 —— 而 FTS 路恰恰是靠 2-gram 把这条 chunk
         召回的，检索能命中、开窗探不到等于白修。gram 命中点可能落在低区分度词上，
         但只影响展示起点，无正确性风险。
      3. 仍不命中 / query 无词元 / content ≤ width → 返回 `content[:width]`，
         与旧版 `content[:200]` **逐字节一致**（回退即现行为）；
      4. 命中 → 窗口起点 = max(0, 命中点 - 30)，并向左对齐到最近的换行或句号
         边界（让片段从一句完整的话开始）；找不到边界就用 max(0, 命中点 - 30)；
         窗口切片长度 ≤ width；起点 > 0 开头加「…」，未达片尾结尾加「…」，
         省略号占用的字符计入 width（返回串总长 ≤ width，与回退行为同界）。
         width < 4 时钳到 4（左右省略号 + 至少 2 字符，再小开窗没有意义）；
         width 非正直接走回退。

    ★ 已知退化（是特性边界，不是保证）：2-gram 兜底层的命中点可能落在**常见词**
      上（如「公司」在证据之前先出现），窗口会偏向那个位置而不是证据 —— 这是
      「纯展示起点」的已知退化。本函数不承诺「窗口一定框住证据」，只承诺
      「比固定取片头更可能对得上」。要真正的证据定位，得靠语义匹配，不是开窗。
    ★ 为什么用 `lower()` / IGNORECASE 而不是 `_normalize` 对齐：NFKC 可能改变
      字符串长度（如「ﬁ」→「fi」），归一化后按索引回切原文会错位。大小写折叠
      是长度不变的，对展示窗口足够。
    """
    if width <= 0:
        # width 非正没有可展示的窗口，直接走回退（content[:负数] 会变成
        # 「从尾部去字符」的错误切片，必须显式挡住）
        return ""
    if len(content) <= width:
        return content[:width]
    width = max(width, 4)  # 左右省略号 + 至少 2 字符：再小开窗没有意义

    pos = _find_probe_hit(content, query)
    if pos < 0:
        return content[:width]

    start = max(0, pos - _SNIPPET_LOOKBACK)
    lookback = content[start:pos]
    boundary = max(lookback.rfind("\n"), lookback.rfind("。"))
    if boundary != -1:
        start += boundary + 1

    prefix = "…" if start > 0 else ""
    end = min(len(content), start + width)
    suffix = "…" if end < len(content) else ""
    body = content[start:end][: width - len(prefix) - len(suffix)]
    return f"{prefix}{body}{suffix}"
