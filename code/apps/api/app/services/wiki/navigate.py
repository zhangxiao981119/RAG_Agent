"""wiki 导航查询扩展 —— 查页（IO）/ 选词（纯函数）两层（第 4 步）。

★ 本模块只在 `WIKI_ENABLED=true` 时被 **延迟 import**（见 `retrieve/__init__.py`
  的 ① 接入点）：关闭态连 `WikiNavPage` 这个 dataclass 都不实例化，
  「零 SQL」才不会退化成「零 SQL 但已 import」。

★ 为什么要拆两层（易被写错的顺序依赖）：`select_nav_terms` 的入参 `alias_forms`
  来自 `load_alias_map()`，而后者在 `retrieve()` 中位于导航查询之后。把选词塞进
  查页函数就得把词表查询提前（扩大改动面）或放弃「与词表别名去重」。拆层后依赖
  方向是单向的：查页 → 取别名 → 选词 → 造 pattern / tsquery，且**只有查页碰数据库**
  （一次查询两路复用，ILIKE 与 FTS 共用同一份选词结果）。

★ 红线 A：wiki 只产出**词元**进 `build_ilike_pattern` / `build_fts_query`；
  `summary` / `sections` 一个字都不进上下文（SQL 只 SELECT document_id, key_terms）。
  术语错的最坏后果是「多召回几片无关原文」，由既有 L1 阈值与 RRF 排序自然淘汰，
  不会变成「模型被告知了一个假事实」。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.config import decisions
from app.services import keyword_text


@dataclass(frozen=True)
class WikiNavPage:
    """**一次导航 SQL** 的产物。所有字段带默认值 → `WikiNavPage()` 即「不扩展」哨兵。

    不把选词结果放进来：选词需要 `alias_forms`（词表别名），而 `alias_map` 在
    `retrieve()` 里晚于导航查询才取得。拆成「查页（IO）/ 选词（纯函数）」两层，
    SQL 仍只有一条，两路共用同一份选词结果。

    ★ `document_id` 是稳定键，**不用 page_id**：第 3 步测试报告 P2-2 实证
      「页重建 = DELETE + INSERT 换 id」，`p.id` 不稳定。
    """

    document_id: uuid.UUID | None = None
    key_terms: tuple[str, ...] = ()       # 命中页的 LLM 原文术语，原样带回
    degraded_reason: str = ""             # "" | "timeout" | "error" | "no_match"
    elapsed_ms: float = 0.0


def _acceptable(term: str) -> bool:
    """确定性过滤（设计 §2.2.3 规则表）；不含任何模型判断、无 IO。

    长度口径与 `build_ilike_terms` 同源（中文 ≥2 / ASCII ≥4 或含 `_`），
    停用词复用 `keyword_text.ILIKE_STOPWORDS`，**不新造第二套规则表**。
    """
    if not term or len(term) > decisions.WIKI_NAV_MAX_TERM_CHARS:
        return False
    if term in keyword_text.ILIKE_STOPWORDS or not term.strip("_- 0123456789"):
        return False
    is_cjk = bool(keyword_text.CJK_RE.fullmatch(term))
    minimum = (
        decisions.WIKI_NAV_MIN_CJK_CHARS
        if is_cjk
        else decisions.WIKI_NAV_MIN_ASCII_CHARS
    )
    return len(term) >= minimum or (not is_cjk and "_" in term)


def select_nav_terms(
    page_terms: list[str],
    query_terms: list[str],
    query_norm: str,
    alias_forms: set[str],
) -> tuple[list[str], list[str]]:
    """纯函数：→ (顶层词元, FTS 索引词元)。全序、限量、**不修改任何入参**。

    ★ 原 `query_terms`（= `build_ilike_terms(query)`，未扩展）**一个字节不改**：
      wiki 词元通过 `extra_terms` / `extra_index_tokens` 注入，不追加后重排截断
      （设计 §2.2.1 的致命方案 / §6-C7）。
    ★ 全序：长度降序（长词更具体，与 `build_ilike_terms` 同源）、字典序破平局。
      页级 `key_terms` 虽已 `sorted`（字典序），但与「区分度」无关，故这里重排。
    ★ 二级 FTS 预算按**整个顶层词元**申请：中文词元经 `to_fts_text` 会 1→N
      展开成 2-gram，装不下就整词跳过（两路一起跳过），绝不截半截索引词元 ——
      半截词元在 FTS 里是静默的伪匹配。
    """
    out: list[str] = []
    fts: list[str] = []
    used = 0
    for raw in sorted(
        (keyword_text.normalize_key_term(t) for t in page_terms),
        key=lambda t: (-len(t), t),
    ):
        term = raw
        if not _acceptable(term):
            continue
        if term in out or term in alias_forms:
            continue
        # 与 query 词元去重（双向包含）：页里的「访问令牌」与 query 里的「令牌」
        # 是同一主题，扩了等于没扩；反向包含同理。
        if any(term in qt or qt in term for qt in query_terms):
            continue
        if term in query_norm:
            continue
        tokens = keyword_text.to_fts_text(term).split()
        if used + len(tokens) > decisions.WIKI_NAV_MAX_FTS_TOKENS:
            continue
        out.append(term)
        fts.extend(tokens)
        used += len(tokens)
        if len(out) >= decisions.WIKI_NAV_MAX_TERMS:
            break
    return out, list(dict.fromkeys(fts))
