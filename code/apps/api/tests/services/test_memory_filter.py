"""三级记忆层单元测试。

覆盖点：写入过滤器（合规底线）、读取白名单（双保险）、来源反查清除、
永久记忆口径快照、以及 decisions 的记忆口径自检。

背景：此前 `extract_facts` **没有任何写入过滤** ——
对话中的文档片段、引用痕迹、拒答文案都会被沉淀进用户画像，
构成权限副本（权限收回后仍能读到），绕过四层守卫的实时判定。
"""
from __future__ import annotations

import pytest

from app.config import decisions
from app.services.memory import (
    MemoryItem,
    blocked_reason,
    build_caliber_snapshot,
    build_memory_prompt,
    filter_facts,
    normalize_facts,
    purge_facts_by_trace_id,
)


# ── 写入过滤器 ──────────────────────────────────────────


def test_short_text_is_rejected():
    assert blocked_reason("嗯") == "too_short"
    assert blocked_reason("   ") == "too_short"


def test_normal_preference_passes():
    assert blocked_reason("常用技术中心知识库") is None
    assert blocked_reason("关注前端性能优化相关文档") is None


def test_long_text_is_suspected_fragment():
    """★ 长度是区分「偏好」与「文档片段」的代理指标。"""
    long_text = "本文档规定了报销标准，具体包括差旅费、交通费、住宿费等各项支出的限额与审批流程。" * 3
    assert blocked_reason(long_text) == "too_long_suspected_fragment"


def test_citation_marker_is_rejected():
    """出现引用编号说明是在抄答案，不是提炼偏好。"""
    assert blocked_reason("报销标准是 500 元 [1]") == "blocked_pattern"
    assert blocked_reason("见文档 [12]") == "blocked_pattern"


def test_refusal_text_is_rejected():
    """拒答意味着「不该回答」，更不该记住。"""
    assert blocked_reason("知识库中未找到相关内容") == "blocked_pattern"
    assert blocked_reason("结果：NO_RELEVANT_CONTENT") == "blocked_pattern"


def test_classified_marker_is_rejected():
    assert blocked_reason("机密文档摘要") == "blocked_pattern"
    assert blocked_reason("CONFIDENTIAL 级别") == "blocked_pattern"


def test_internal_acl_field_is_rejected():
    """内部权限字段名不应出现在长期记忆里。"""
    assert blocked_reason("grant_kb 字段值") == "blocked_pattern"
    assert blocked_reason("acl_tags 配置") == "blocked_pattern"


def test_filter_facts_splits_pass_and_reject():
    candidates = [
        "常用技术中心知识库",      # 通过
        "报销标准是 500 元 [1]",   # 引用痕迹
        "嗯",                      # 过短
        "关注前端性能优化",         # 通过
    ]
    passed, rejected = filter_facts(candidates, trace_id="t-1")
    assert [p.content for p in passed] == ["常用技术中心知识库", "关注前端性能优化"]
    assert len(rejected) == 2
    assert {r[1] for r in rejected} == {"blocked_pattern", "too_short"}


def test_filter_attaches_trace_id_and_confidence():
    """★ 每条通过的记忆项 MUST 带 source_trace_id —— 否则无法反查清除。"""
    passed, _ = filter_facts(["关注财务制度"], trace_id="trace-abc", confidence=0.9)
    assert passed[0].source_trace_id == "trace-abc"
    assert passed[0].confidence == 0.9
    assert passed[0].kind == "preference"


# ── 兼容旧格式 ──────────────────────────────────────────


def test_normalize_legacy_string_facts():
    """旧数据 facts 是 list[str]，升级后必须还能读出来。"""
    items = normalize_facts(["常用知识库", "关注前端"])
    assert len(items) == 2
    assert items[0].content == "常用知识库"
    assert items[0].kind == "preference"
    assert items[0].source_trace_id == ""


def test_normalize_new_dict_facts():
    items = normalize_facts(
        [{"content": "关注财务制度", "kind": "preference",
          "confidence": 0.8, "source_trace_id": "t-9"}]
    )
    assert items[0].source_trace_id == "t-9"
    assert items[0].confidence == 0.8


def test_normalize_skips_malformed():
    items = normalize_facts(["", "  ", {"content": ""}, None, 123, "有效项"])
    assert [i.content for i in items] == ["有效项"]


# ── 读取白名单 ──────────────────────────────────────────


def test_memory_prompt_only_returns_preference_kind():
    """★ 双保险的另一侧：即便写入放行了 fact 类，读取也不召回。"""
    memory = {
        "facts": [
            {"content": "关注财务制度", "kind": "preference",
             "confidence": 0.9, "source_trace_id": "t1"},
            {"content": "某文档正文片段", "kind": "fact",
             "confidence": 0.9, "source_trace_id": "t2"},
        ]
    }
    prompt = build_memory_prompt(memory)
    assert "关注财务制度" in prompt
    assert "某文档正文片段" not in prompt


def test_memory_prompt_excludes_low_confidence():
    memory = {
        "facts": [
            {"content": "高置信偏好", "kind": "preference",
             "confidence": 0.9, "source_trace_id": "t1"},
            {"content": "低置信猜测", "kind": "preference",
             "confidence": 0.3, "source_trace_id": "t2"},
        ]
    }
    prompt = build_memory_prompt(memory)
    assert "高置信偏好" in prompt
    assert "低置信猜测" not in prompt


def test_memory_prompt_rebuilds_from_facts_not_profile():
    """★ 不从 profile 字段取文本 —— 那是写入时的快照，无法按 kind 过滤。"""
    memory = {
        "profile": "用户背景：\n- 某文档正文片段\n- 关注财务制度",
        "facts": [
            {"content": "关注财务制度", "kind": "preference",
             "confidence": 0.9, "source_trace_id": "t1"},
        ],
    }
    prompt = build_memory_prompt(memory)
    assert "关注财务制度" in prompt
    assert "某文档正文片段" not in prompt


def test_memory_prompt_empty_when_no_usable_items():
    assert build_memory_prompt(None) == ""
    assert build_memory_prompt({}) == ""
    assert build_memory_prompt({"facts": []}) == ""
    assert build_memory_prompt({"facts": [{"content": "x", "kind": "fact"}]}) == ""


def test_memory_prompt_reads_legacy_facts():
    """旧格式（纯字符串）应被当作 preference 正常召回。"""
    prompt = build_memory_prompt({"facts": ["常用技术中心知识库"]})
    assert "常用技术中心知识库" in prompt


# ── 来源反查清除 ────────────────────────────────────────


def test_purge_removes_matching_trace():
    memory = {
        "facts": [
            {"content": "偏好A", "kind": "preference",
             "confidence": 1.0, "source_trace_id": "t-1"},
            {"content": "偏好B", "kind": "preference",
             "confidence": 1.0, "source_trace_id": "t-2"},
        ]
    }
    new_memory, removed = purge_facts_by_trace_id(memory, {"t-1"})
    assert removed == 1
    contents = [f["content"] for f in new_memory["facts"]]
    assert contents == ["偏好B"]


def test_purge_keeps_items_from_other_traces():
    memory = {
        "facts": [
            {"content": "偏好A", "source_trace_id": "t-1"},
            {"content": "偏好B", "source_trace_id": "t-2"},
        ]
    }
    _, removed = purge_facts_by_trace_id(memory, {"t-99"})
    assert removed == 0


def test_purge_cannot_remove_legacy_items():
    """★ 旧格式无 source_trace_id → 无法匹配，清不掉。

    这正是 MUST 要求新写入项带 source_trace_id 的原因 ——
    历史遗留项是已知盲区，需要一次性迁移清洗。
    """
    memory = {"facts": ["旧格式偏好（无来源）"]}
    _, removed = purge_facts_by_trace_id(memory, {"t-1"})
    assert removed == 0


def test_purge_empty_trace_set_is_noop():
    memory = {"facts": [{"content": "偏好A", "source_trace_id": "t-1"}]}
    _, removed = purge_facts_by_trace_id(memory, set())
    assert removed == 0


def test_purge_clears_profile_when_all_removed():
    memory = {
        "profile": "用户背景：\n- 偏好A",
        "facts": [{"content": "偏好A", "source_trace_id": "t-1"}],
    }
    new_memory, removed = purge_facts_by_trace_id(memory, {"t-1"})
    assert removed == 1
    assert new_memory["profile"] == ""
    assert new_memory["facts"] == []


# ── 永久记忆：口径快照 ──────────────────────────────────


def test_caliber_snapshot_contains_key_calibers():
    snap = build_caliber_snapshot()
    assert snap["contract_version"] == decisions.CONTRACT_VERSION
    assert snap["relevance_threshold_vector"] == decisions.RELEVANCE_THRESHOLD_VECTOR
    assert snap["rrf_k"] == decisions.RRF_K


def test_caliber_snapshot_has_no_user_content():
    """★ 永久记忆 MUST NOT 含任何用户内容。"""
    snap = build_caliber_snapshot()
    assert "facts" not in snap
    assert "profile" not in snap
    assert "user_id" not in snap


# ── 口径自检 ────────────────────────────────────────────


def test_self_check_passes():
    decisions.self_check()


def test_self_check_rejects_fact_in_long_term_kinds(monkeypatch):
    """长期记忆放 fact 类 = 把文档内容沉淀成权限副本，必须拒绝启动。"""
    monkeypatch.setattr(decisions, "MEMORY_LONG_TERM_KINDS", frozenset({"preference", "fact"}))
    with pytest.raises(RuntimeError, match="MEMORY_LONG_TERM_KINDS"):
        decisions.self_check()


def test_self_check_rejects_empty_whitelist(monkeypatch):
    monkeypatch.setattr(decisions, "MEMORY_LONG_TERM_KINDS", frozenset())
    with pytest.raises(RuntimeError, match="MEMORY_LONG_TERM_KINDS"):
        decisions.self_check()


def test_self_check_rejects_empty_blocklist(monkeypatch):
    monkeypatch.setattr(decisions, "BLOCKED_MEMORY_PATTERNS", ())
    with pytest.raises(RuntimeError, match="BLOCKED_MEMORY_PATTERNS"):
        decisions.self_check()


def test_self_check_rejects_disabled_trace_requirement(monkeypatch):
    monkeypatch.setattr(decisions, "MEMORY_SOURCE_TRACE_REQUIRED", False)
    with pytest.raises(RuntimeError, match="MEMORY_SOURCE_TRACE_REQUIRED"):
        decisions.self_check()


def test_self_check_rejects_bad_confidence_threshold(monkeypatch):
    monkeypatch.setattr(decisions, "MEMORY_WRITE_CONFIDENCE_THRESHOLD", 0.0)
    with pytest.raises(RuntimeError, match="MEMORY_WRITE_CONFIDENCE_THRESHOLD"):
        decisions.self_check()
