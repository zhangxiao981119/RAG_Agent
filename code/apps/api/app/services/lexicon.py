"""知识库领域词表：查询侧的别名扩展。

词表由 scripts/build_kb_terms.py 离线构建（kb_terms 表），本模块只负责两件事：
  1. 用查询词元去词表里**精确匹配** term / aliases
  2. 把「命中的写法」换成「另一侧写法」，供给 ILIKE 与 FTS 两路补词元

★ 只做精确键匹配，不做相似度/模糊匹配：词表解决的是「同义不同字」
  （访问令牌 / AccessToken），一旦放开模糊匹配，扩出来的词元与查询语义
  没有必然关系，等于自己往召回里灌噪声。
★ 不调大模型：本方案的立足点就是不依赖推理的离线词表扩展。
"""
from __future__ import annotations

import logging
import re
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import decisions
from app.services.keyword_text import extract_model_terms, to_fts_text

logger = logging.getLogger(__name__)

# 中文全称在前：`访问令牌（AccessToken）`
_CJK_FIRST = re.compile(
    r"([\u4e00-\u9fff]{2,12})\s*[（(]\s*([A-Za-z][A-Za-z0-9_\-]{1,30})\s*[)）]"
)
# 缩写在前：`AccessToken（访问令牌）`
_ASCII_FIRST = re.compile(
    r"([A-Za-z][A-Za-z0-9_\-]{1,30})\s*[（(]\s*([\u4e00-\u9fff]{2,12})\s*[)）]"
)

# term / aliases 双向匹配：term 命中查 aliases，aliases 命中查 term。
# jsonb 的 ?| 判断「数组里是否含有这些键中的任意一个」，一次查询覆盖两个方向。
_ALIAS_SQL = text("""
SELECT term, aliases
FROM kb_terms
WHERE kb_id = ANY(:kb_ids)
  AND (term = ANY(:keys) OR aliases ?| :keys)
""")


def collect_terms(texts: list[str]) -> dict[str, tuple[str, list[str], int]]:
    """从一批文本里抽词条：term -> (kind, aliases, freq)。

    纯规则抽取，离线跑（scripts/build_kb_terms.py）。词条做 NFKC + 小写归一，
    与查询侧词元的归一规则一致 —— 否则库里存 `XYZ-1000`、查询给 `xyz-1000`
    会精确匹配不上。
    """
    found: dict[str, tuple[str, list[str], int]] = {}

    def add(term: str, kind: str, aliases: list[str]) -> None:
        if not term:
            return
        old = found.get(term)
        if old is None:
            found[term] = (kind, aliases, 1)
            return
        merged = list(dict.fromkeys(old[1] + aliases))
        found[term] = (kind, merged, old[2] + 1)

    for raw in texts:
        if not raw:
            continue
        for term, variants in extract_model_terms(raw):
            add(term, "model", variants)
        for cjk, ascii_name in _CJK_FIRST.findall(raw):
            add(cjk, "acronym", [ascii_name.lower()])
            add(ascii_name.lower(), "acronym", [cjk])
        for ascii_name, cjk in _ASCII_FIRST.findall(raw):
            add(ascii_name.lower(), "acronym", [cjk])
            add(cjk, "acronym", [ascii_name.lower()])
    return found


async def load_alias_map(
    session: AsyncSession,
    kb_ids: list[uuid.UUID],
    terms: list[str],
) -> dict[str, list[str]]:
    """查询词元 → 需要补充的其它写法（按词元键返回，值为别名列表）。

    扩不出来返回空字典，调用方据此保持原有词元与正则不变。
    """
    keys = list(dict.fromkeys(terms))
    if not keys or not kb_ids:
        return {}

    rows = (
        await session.execute(_ALIAS_SQL, {"kb_ids": kb_ids, "keys": keys})
    ).fetchall()

    alias_map: dict[str, list[str]] = {}
    for row in rows:
        forms = [row.term, *(row.aliases or [])]
        for key in keys:
            if key not in forms:
                continue
            extras = [
                form for form in forms if form != key
            ][: decisions.LEXICON_MAX_ALIASES_PER_KEY]
            if extras:
                alias_map[key] = extras
    return alias_map


def alias_index_tokens(alias_map: dict[str, list[str]]) -> list[str]:
    """把别名转成 FTS 索引词元。

    MUST 走 to_fts_text：索引里存的是中文 2-gram + ASCII 整词，
    直接拿原始别名去查 tsquery，中文别名一个字都对不上（静默 0 召回）。
    """
    tokens: list[str] = []
    for extras in alias_map.values():
        for alias in extras:
            tokens.extend(to_fts_text(alias).split())
    return list(dict.fromkeys(tokens))
