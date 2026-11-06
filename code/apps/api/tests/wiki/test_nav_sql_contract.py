"""第 4 步 · 导航 SQL 的静态契约（段 B）。

★ 这些是**文本级**断言，只钉住「SQL 形状不许漂移」；四类越权的**语义级**
  证明在 `tests/wiki/test_nav_acl.py` 的真库探针里（第 3 步 P2-1 已实证：
  纯 SQL 文本断言会假红，看得见 `tenant_id` 不等于越权真的被挡住）。

钉住设计 §2.1.3 / §2.1.4 全部可写错的点：
  1. `_WHERE_BASE` **逐字内联**（取同一个字符串对象，不是副本）
  2. `p.tenant_id = :tenant_id` 显式存在（按 kb 集合扫描时不能省）
  3. `strpos(lower(kt.term), n.term) > 0` —— N1 裁决的读侧字面子串方案
  4. 口径双过滤 / scope / status / EXISTS 四闸门 / `c.document_id = p.document_id`
  5. 全序排序用 `document_id`，**不用 `p.id`**（页重建会换 id，P2-2）
  6. SELECT 只有 `document_id, key_terms` —— `summary` / `sections` 一个字都不读
"""
from __future__ import annotations

import pytest

from app.config import decisions
from app.services.retrieve import _WHERE_BASE
from app.services.wiki import navigate as nav_mod


@pytest.fixture(scope="module")
def sql() -> str:
    return nav_mod._WIKI_NAV_SQL.format(where_base=_WHERE_BASE)


def test_where_base_is_inlined_verbatim(sql: str) -> None:
    """四闸门 MUST 与主召回同一字符串；复制一份到本文件必然漂移且不报错。"""
    assert _WHERE_BASE in sql
    for gate in (
        "c.tenant_id = :tenant_id",
        "c.is_latest = true",
        "c.level_rank <= :clearance",
        "c.kb_id = ANY(:authorized_kb_ids)",
        "NOT (c.deny_subjects && :user_subjects)",
        "c.acl_tags && :user_subjects",
    ):
        assert gate in sql, f"导航 SQL 少了闸门：{gate}"


def test_explicit_tenant_filter_on_wiki_page(sql: str) -> None:
    """按 kb 集合扫描必须显式卡 `p.tenant_id`，不能只靠 EXISTS 里的 c.tenant_id。"""
    assert "p.tenant_id = :tenant_id" in sql
    assert "AND c.document_id = p.document_id" in sql


def test_n1_literal_substring_not_jsonb_operator(sql: str) -> None:
    """N1：读侧 `lower + strpos` 字面子串，零迁移零索引零新列。"""
    assert "strpos(lower(kt.term), n.term) > 0" in sql
    assert "jsonb_array_elements_text(p.key_terms)" in sql
    for rejected in ("?|", "?&", "@>", "key_terms_norm"):
        assert rejected not in sql, (
            f"不该出现 `{rejected}`（那是 N1 被否决的写侧归一 + GIN 方案）"
        )


def test_caliber_scope_status_and_array_binding(sql: str) -> None:
    for fragment in (
        "p.scope = 'doc'",
        "p.status = 'ready'",
        "p.generator_model = :gm",
        "p.prompt_version  = :pv",
        "unnest(CAST(:needles AS text[]))",
        "LIMIT :max_pages",
    ):
        assert fragment in sql, f"导航 SQL 缺片段：{fragment}"
    assert "string_to_array" not in sql, "词元可能含逗号/空格，禁止拼串再拆"


def test_total_order_uses_document_id_not_page_id(sql: str) -> None:
    assert "ORDER BY best_len DESC, hit_terms DESC, document_id ASC" in sql
    assert "p.id" not in sql, "页重建 = DELETE+INSERT 换 id，p.id 不是稳定键"


def test_selects_only_two_columns(sql: str) -> None:
    """红线 A：wiki 正文一个字都不进上下文（连读都不该被读）。"""
    tail = sql.split("FROM hit")[-1]
    assert "SELECT document_id, key_terms" in sql
    for forbidden in ("summary", "sections", "content", "heading_path"):
        assert forbidden not in sql, f"导航 SQL 不该 SELECT {forbidden}"
    assert "LIMIT" in tail


def test_max_pages_is_the_only_limit(sql: str) -> None:
    """`WIKI_NAV_CANDIDATE_PAGE_GUARD` 是观测触发阈值，绝不能变成 SQL 的 LIMIT
    （LIMIT 放在 ACL 之前会系统性漏掉命中的页）。"""
    assert sql.count("LIMIT") == 1
    assert decisions.WIKI_NAV_MAX_PAGES == 1
    assert decisions.WIKI_NAV_CANDIDATE_PAGE_GUARD == 5_000
