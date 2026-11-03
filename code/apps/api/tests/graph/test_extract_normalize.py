"""抽取输出的归一与过滤规则（纯函数，不连库、不调 LLM）。

这些规则决定「什么进图、什么被丢弃」，是图质量的实际开关：
  · 整句当实体名 → 丢弃（否则邻居扩展变成随机漫步）
  · 类型不在闭集 → 归一成 OTHER（不丢实体，但绝不落非法类型进 CHECK 约束）
  · 自环 / 未知 chunk_id / 指向未登记实体的提及 → 丢弃
  · 谓词超长 → 丢弃（截断会让两条不同的边撞键合并，那是静默错图）
"""
from __future__ import annotations

import uuid

from app.config import decisions
from app.services.graph.extract import (
    _collect,
    _extract_json,
    normalize_name_key,
    normalize_relation,
)


def test_normalize_name_key_nfkc_space_and_case():
    # 全角字母 + 连续空白 + 大小写 → 同一个键（这是实体合并的前提）
    assert normalize_name_key("  ＩＢＭ\t\n 中国 ") == "ibm 中国"
    assert normalize_name_key("IBM 中国") == normalize_name_key("ibm   中国")


def test_normalize_relation_strips_punctuation_and_caps_length():
    assert normalize_relation(" 负责。 ") == "负责"
    assert normalize_relation("，包含；") == "包含"
    # 超过上限 → 空串（丢弃），不做截断
    assert normalize_relation("关" * (decisions.GRAPH_RELATION_MAX_CHARS + 1)) == ""
    assert normalize_relation("") == ""


def test_extract_json_tolerates_fences_and_prefix():
    assert _extract_json('好的：```json\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json("没有 JSON") is None


def test_collect_drops_long_names_and_unknown_chunk_ids():
    chunk_a = uuid.uuid4()
    chunk_b = uuid.uuid4()
    other = uuid.uuid4()  # 不在本批 → 必须被丢弃
    parsed = {
        "entities": [
            {"name": "财务部", "type": "ORG"},
            {"name": "这是" + "很长的整句" * 20, "type": "ORG"},  # 超长 → 丢弃
            {"name": "", "type": "ORG"},  # 空名 → 丢弃
        ],
        "mentions": [
            {"name": "财务部", "chunk_id": str(chunk_a), "surface": "财务部"},
            {"name": "财务部", "chunk_id": str(other), "surface": "财务部"},  # 未知 id
            {"name": "不存在的实体", "chunk_id": str(chunk_b), "surface": "x"},
            {"name": "财务部", "chunk_id": "不是-uuid", "surface": "x"},
        ],
        "relations": [],
    }
    entities, mentions, relations = _collect(parsed, batch_ids={chunk_a, chunk_b})
    assert set(entities) == {"财务部"}
    assert len(mentions) == 1
    assert mentions[0].chunk_id == chunk_a
    assert relations == []


def test_collect_unknown_type_falls_back_to_other():
    chunk = uuid.uuid4()
    parsed = {
        "entities": [
            {"name": "预算", "type": "未知类型"},
            {"name": "采购流程", "type": "process"},  # 小写 → 归一为 PROCESS
        ],
        "mentions": [],
        "relations": [],
    }
    entities, _, _ = _collect(parsed, batch_ids={chunk})
    assert entities["预算"].entity_type == "OTHER"
    assert entities["采购流程"].entity_type == "PROCESS"
    assert entities["预算"].entity_type in decisions.GRAPH_ENTITY_TYPES


def test_collect_merges_duplicate_entities_and_aliases():
    parsed = {
        "entities": [
            {"name": "张三", "type": "PERSON", "aliases": ["张工"]},
            {"name": "张三", "type": "PERSON", "aliases": ["三哥", "张工"]},
        ],
        "mentions": [],
        "relations": [],
    }
    entities, _, _ = _collect(parsed, batch_ids=set())
    assert len(entities) == 1
    entity = entities[normalize_name_key("张三")]
    assert entity.aliases == ("三哥", "张工")  # 已排序去重


def test_collect_drops_self_loops_and_dangling_relations():
    parsed = {
        "entities": [
            {"name": "甲部门", "type": "ORG"},
            {"name": "乙部门", "type": "ORG"},
        ],
        "mentions": [],
        "relations": [
            {"src": "甲部门", "dst": "甲部门", "relation": "属于"},  # 自环
            {"src": "甲部门", "dst": "未登记实体", "relation": "属于"},
            {"src": "甲部门", "dst": "乙部门", "relation": "负责"},
            {"src": "甲部门", "dst": "乙部门", "relation": "负责"},  # 重复三元组
            {"src": "甲部门", "dst": "乙部门", "relation": "关" * 30},  # 超长谓词
        ],
    }
    _, _, relations = _collect(parsed, batch_ids=set())
    assert relations == [("甲部门", "乙部门", "负责")]


def test_collect_truncates_to_per_call_cap():
    cap = decisions.GRAPH_MAX_ENTITIES_PER_CHUNK * decisions.GRAPH_EXTRACT_BATCH_CHUNKS
    parsed = {
        "entities": [
            {"name": f"实体{i}", "type": "TERM"} for i in range(cap + 20)
        ],
        "mentions": [],
        "relations": [],
    }
    entities, _, _ = _collect(parsed, batch_ids=set())
    assert len(entities) == cap
