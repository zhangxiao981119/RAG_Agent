"""领域词表（kb_terms）单元测试 —— 抽取与查询扩展。

覆盖点：型号词条与写法变体、缩写-全称双向配对、出现次数聚合、
命中 term / 命中 alias 两个方向、单键别名上限、词表扩展后的词元形态。
"""
from __future__ import annotations

import pytest

from app.config import decisions
from app.services.lexicon import alias_index_tokens, collect_terms, load_alias_map


# ── 离线抽取（scripts/build_kb_terms.py 的核心）──────────


def test_collect_model_term_with_variants():
    """型号归一为连字符形式，另两种写法进 aliases。"""
    found = collect_terms(["XYZ-1000 智能终端"])
    assert found["xyz-1000"] == ("model", ["xyz1000", "xyz 1000"], 1)


def test_collect_acronym_cjk_first_both_directions():
    """`访问令牌（AccessToken）` 要生成两条互指的词条。"""
    found = collect_terms(["访问令牌（AccessToken）的有效期"])
    assert found["访问令牌"] == ("acronym", ["accesstoken"], 1)
    assert found["accesstoken"] == ("acronym", ["访问令牌"], 1)


def test_collect_acronym_ascii_first():
    found = collect_terms(["RefreshToken（刷新令牌）"])
    assert found["refreshtoken"] == ("acronym", ["刷新令牌"], 1)
    assert found["刷新令牌"] == ("acronym", ["refreshtoken"], 1)


def test_collect_freq_aggregated_across_chunks():
    """同一词条跨分片出现要累加出现次数，低频过滤才有依据。"""
    found = collect_terms(["XYZ-1000 规格", "XYZ-1000 价格"])
    assert found["xyz-1000"][2] == 2
    assert found["xyz-1000"][1] == ["xyz1000", "xyz 1000"]


def test_collect_skips_empty_text():
    assert not collect_terms(["", None])  # type: ignore[list-item]


# ── 查询扩展 ──────────────────────────────────────────


class _Row:
    def __init__(self, term: str, aliases: list[str]) -> None:
        self.term = term
        self.aliases = aliases


class _Result:
    def __init__(self, rows: list[_Row]) -> None:
        self._rows = rows

    def fetchall(self) -> list[_Row]:
        return self._rows


class _Session:
    def __init__(self, rows: list[_Row]) -> None:
        self._rows = rows
        self.calls: list[dict] = []

    async def execute(self, stmt, params=None):  # noqa: ANN001
        self.calls.append(dict(params or {}))
        return _Result(self._rows)


@pytest.mark.asyncio
async def test_alias_map_hit_by_term():
    session = _Session([_Row("accesstoken", ["访问令牌"])])
    result = await load_alias_map(session, ["kb"], ["accesstoken"])
    assert result == {"accesstoken": ["访问令牌"]}
    assert session.calls[0]["keys"] == ["accesstoken"]


@pytest.mark.asyncio
async def test_alias_map_hit_by_alias():
    """反方向也要能扩：用户问中文全称，补英文缩写。"""
    session = _Session([_Row("accesstoken", ["访问令牌"])])
    result = await load_alias_map(session, ["kb"], ["访问令牌"])
    assert result == {"访问令牌": ["accesstoken"]}


@pytest.mark.asyncio
async def test_alias_map_caps_aliases_per_key():
    aliases = ["写法一", "写法二", "写法三"]
    session = _Session([_Row("xyz-1000", aliases)])
    result = await load_alias_map(session, ["kb"], ["xyz-1000"])
    assert len(result["xyz-1000"]) == decisions.LEXICON_MAX_ALIASES_PER_KEY


@pytest.mark.asyncio
async def test_alias_map_empty_terms_skips_query():
    session = _Session([])
    assert await load_alias_map(session, ["kb"], []) == {}
    assert session.calls == []


def test_alias_index_tokens_uses_index_form():
    """中文别名 MUST 切成 bigram，否则与索引里的词元对不上。"""
    assert alias_index_tokens({"accesstoken": ["访问令牌"]}) == ["访问", "问令", "令牌"]


def test_alias_index_tokens_deduped_across_keys():
    tokens = alias_index_tokens({"a": ["访问令牌"], "b": ["访问令牌"]})
    assert tokens == ["访问", "问令", "令牌"]
