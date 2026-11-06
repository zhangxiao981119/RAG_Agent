"""第 4 步 / N6 的 Golden 快照回归 —— **改动前后逐字节等价**。

快照 `golden_wiki_nav_baseline.json` 是在**改代码之前**用生产 `keyword_text.py`
对 206 条核心样本 + 20 条代表问句冻结的输出（生成脚本已按纪律删除，快照是
历史证据、改后不可重新生成，否则是循环论证）。

★ 覆盖的等价性：
  · `build_ilike_terms` —— 一个字节都没改，但必须证明；
  · `build_ilike_pattern` —— 新增 `extra_terms` keyword-only 默认参，空序列时
    遍历序列与改动前完全一致；
  · `build_fts_query` —— 新增 `extra_index_tokens` 与 N6 的别名独立预算，
    **alias_tokens 为空**的用例 MUST 100% 逐字节相同（C4 §3.6 前置条件 ①）。
    带别名的用例不在快照内：N6 修复**会**改变「达上限 / 剩余容量不足」那批的
    tsquery，这正是修复目的，不能拿它当不变式。

★ 本机无 PostgreSQL，217 条精确清单只存在于真库 `eval_cases` 表（C4 §2.1 / U1）。
  本测试覆盖的是仓库内可得样本，不代表精确 217 条；真库可达时须按 §2.2 脚本补跑。
"""
from __future__ import annotations

import json
from pathlib import Path

from app.services.keyword_text import (
    build_fts_query,
    build_ilike_pattern,
    build_ilike_terms,
)

_GOLDEN = Path(__file__).with_name("golden_wiki_nav_baseline.json")


def _snapshot() -> dict:
    return json.loads(_GOLDEN.read_text(encoding="utf-8"))


def test_golden_is_prechange_evidence_not_regenerable():
    """快照本身必须是「改动前」的证据，且样本量诚实标注。"""
    payload = _snapshot()
    meta = payload["meta"]
    assert "改动前" in meta["purpose"]
    assert meta["source"]["deduped_total"] == len(payload["cases"])
    # 217 条精确清单不可得 → 覆盖 206 条直系前身 + 20 条代表问句（去重后 220）
    assert meta["source"]["deduped_total"] >= 206
    assert "eval_cases" in meta["limitation"]
    assert meta["functions"] == [
        "build_ilike_terms", "build_ilike_pattern", "build_fts_query",
    ]


def test_all_golden_cases_are_byte_identical():
    """逐条比对三个纯函数的输出；任何一条不同都打印 q 与旧/新值。"""
    payload = _snapshot()
    mismatches: list[str] = []
    for case in payload["cases"]:
        q = case["q"]
        terms = build_ilike_terms(q)
        pattern = build_ilike_pattern(terms)
        tsquery = build_fts_query(q)
        if terms != case["ilike_terms"]:
            mismatches.append(f"{q!r} ilike_terms: {case['ilike_terms']!r} -> {terms!r}")
        if pattern != case["ilike_pattern"]:
            mismatches.append(
                f"{q!r} ilike_pattern: {case['ilike_pattern']!r} -> {pattern!r}"
            )
        if tsquery != case["fts_query"]:
            mismatches.append(f"{q!r} fts_query: {case['fts_query']!r} -> {tsquery!r}")
    assert not mismatches, (
        f"Golden 快照出现 {len(mismatches)} 处漂移（前 5 条）：\n"
        + "\n".join(mismatches[:5])
    )


def test_explicit_empty_extra_params_are_identical_to_omitting_them():
    """显式传空 `extra_*` 与不传 MUST 逐字节相同（回滚等价第 2 层证据）。"""
    for case in _snapshot()["cases"]:
        q = case["q"]
        terms = build_ilike_terms(q)
        assert build_ilike_pattern(terms, extra_terms=()) == build_ilike_pattern(terms)
        assert build_fts_query(q, extra_index_tokens=()) == build_fts_query(q)
        assert build_fts_query(q, None, extra_index_tokens=()) == build_fts_query(q, None)


def test_extra_terms_append_or_branches_without_touching_original_terms():
    """wiki 词元走附加 OR 分支；原 `terms` 列表与最长词元（needle）不被改写。"""
    terms = ["访问控制", "凭证"]
    frozen = list(terms)
    plain = build_ilike_pattern(terms)
    extended = build_ilike_pattern(terms, extra_terms=("缓存机制",))
    assert terms == frozen, "原词元集合 MUST 冻结（禁止 append 后重截）"
    assert plain == "访问控制|凭证"
    assert extended == "访问控制|凭证|缓存机制"
    # needle 仍是最长的**原** query 词元，不受扩展影响
    assert terms[0].lower() == "访问控制"
    assert extended.startswith(plain + "|")


def test_extra_index_tokens_keep_original_windows():
    """wiki 索引词元并入对应 OR 组，原 query 窗口一个不少。"""
    base = build_fts_query("销量")
    extended = build_fts_query("销量", extra_index_tokens=["访问", "问令"])
    assert base == "(销量)"
    assert extended == "(销量 | 访问 | 问令)"
    for window in base.strip("()").split(" | "):
        assert window in extended
