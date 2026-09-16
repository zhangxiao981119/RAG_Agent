"""手册 §7.2 的十条权限用例 —— 可运行实现。

    pytest tests/acl -v

★ 这十条不是"边界情况"，是**最常见的错法**。
  用例 3b / 5 / 9 / 10 属于"口径对、实现错、测试全绿"型的泄漏面，MUST NOT 删。
"""
from __future__ import annotations

import pytest

from app.config import decisions as D
from app.services.acl import (
    PUSHDOWN_WHERE_SQL,
    AclTagError,
    compute_doc_acl_tags,
    is_visible,
    pushdown_params,
    validate_acl_tags,
)

BACKEND = "/总部/技术中心/后端组/"
FRONTEND = "/总部/技术中心/前端组/"
TECH_CENTER = "/总部/技术中心/"
FINANCE = "/总部/财务部/"
HR = "/总部/人力资源部/"
PAYROLL = "/总部/人力资源部/薪酬组/"


# ══════════════════════════════════════════════════════════
# 用例 1 · 部门内正常命中
# ══════════════════════════════════════════════════════════
def test_01_dept_internal_hit_visible(principal_of, chunk_factory):
    alice = principal_of("alice")
    chunk = chunk_factory("c1", "kb_tech", acl_tags={f"dept:{BACKEND}"})
    assert is_visible(chunk, alice) is True


# ══════════════════════════════════════════════════════════
# 用例 2 · 未授权知识库（同部门也不行）—— G2 是必要条件
# ══════════════════════════════════════════════════════════
def test_02_unauthorized_kb_invisible(principal_of, chunk_factory):
    alice = principal_of("alice")
    chunk = chunk_factory(
        "c2",
        "kb_finance",  # alice 不在这个库的成员里
        acl_tags={f"dept:{BACKEND}"},  # 标签完全匹配，仍应不可见
        level_rank=D.LEVEL_RANK_PUBLIC,
    )
    assert is_visible(chunk, alice) is False


# ══════════════════════════════════════════════════════════
# 用例 3 · 密级不足 —— G1
# ══════════════════════════════════════════════════════════
def test_03_insufficient_clearance_invisible(principal_of, chunk_factory):
    alice = principal_of("alice")  # clearance = 20
    chunk = chunk_factory(
        "c3", "kb_tech", acl_tags={f"dept:{BACKEND}"}, level_rank=D.LEVEL_RANK_SECRET
    )
    assert is_visible(chunk, alice) is False

    ok = chunk_factory(
        "c3b",
        "kb_tech",
        acl_tags={f"dept:{BACKEND}"},
        level_rank=D.LEVEL_RANK_INTERNAL,  # 20 <= 20，边界应通过
    )
    assert is_visible(ok, alice) is True


# ══════════════════════════════════════════════════════════
# 用例 3b · ★ 公开库 + 部门标签 —— 破"进公开库就全可见"
# ══════════════════════════════════════════════════════════
def test_3b_public_kb_mixed_tag_invisible(principal_of, chunk_factory):
    alice = principal_of("alice")  # 技术中心
    carol = principal_of("carol")  # 财务部
    chunk = chunk_factory(
        "c3b",
        "kb_public",  # 库对所有人开放（G2 通过）
        acl_tags={f"dept:{FINANCE}"},  # 但文档本身挂的是财务部标签
        level_rank=D.LEVEL_RANK_PUBLIC,
    )
    assert is_visible(chunk, alice) is False  # G4 拦住
    assert is_visible(chunk, carol) is True  # 财务部正常可见


# ══════════════════════════════════════════════════════════
# 用例 3b-写入 · ★ 混标 MUST 被拒 —— H2
# ══════════════════════════════════════════════════════════
def test_3b_write_mixed_public_tag_rejected():
    with pytest.raises(AclTagError) as exc:
        compute_doc_acl_tags(extra_tags={"public", f"dept:{FINANCE}"})
    assert "public MUST 单独存在" in str(exc.value)

    # 单独 public 合法
    assert compute_doc_acl_tags(extra_tags={"public"}) == frozenset({"public"})


# ══════════════════════════════════════════════════════════
# 用例 4 · deny 优先 —— G3 最高优先级
# ══════════════════════════════════════════════════════════
def test_04_deny_overrides_allow(principal_of, chunk_factory):
    alice = principal_of("alice")
    before = chunk_factory("c4", "kb_tech", acl_tags={f"dept:{BACKEND}"})
    assert is_visible(before, alice) is True  # 命中 allow

    denied = chunk_factory(
        "c4d",
        "kb_tech",
        acl_tags={f"dept:{BACKEND}"},
        deny={"user:alice"},  # 同时命中 deny
    )
    assert is_visible(denied, alice) is False  # deny 赢


# ══════════════════════════════════════════════════════════
# 用例 5 · ★ level: 混进 acl_tags MUST 被拒 —— H3
# ══════════════════════════════════════════════════════════
def test_05_level_tag_rejected():
    with pytest.raises(AclTagError) as exc:
        compute_doc_acl_tags(extra_tags={"level:public"})
    assert "MUST NOT 含 level:" in str(exc.value)

    with pytest.raises(AclTagError):
        validate_acl_tags({"dept:/总部/技术中心/", "level:secret"})


# ══════════════════════════════════════════════════════════
# 用例 6 · 新账号只得公开库（Q9）
# ══════════════════════════════════════════════════════════
def test_06_new_account_public_only(principal_of, chunk_factory):
    dave = principal_of("dave")
    assert dave.subjects == frozenset({"user:dave", "public"})
    assert dave.authorized_kb_ids == frozenset({"kb_public"})

    public_doc = chunk_factory(
        "c6a", "kb_public", acl_tags={"public"}, level_rank=D.LEVEL_RANK_PUBLIC
    )
    dept_doc = chunk_factory(
        "c6b", "kb_public", acl_tags={f"dept:{FINANCE}"}, level_rank=D.LEVEL_RANK_PUBLIC
    )
    private_kb_doc = chunk_factory(
        "c6c", "kb_tech", acl_tags={"public"}, level_rank=D.LEVEL_RANK_PUBLIC
    )

    assert is_visible(public_doc, dave) is True
    assert is_visible(dept_doc, dave) is False
    assert is_visible(private_kb_doc, dave) is False  # G2：不在公开库里


# ══════════════════════════════════════════════════════════
# 用例 7 · 兼岗并集（Q4）
# ══════════════════════════════════════════════════════════
def test_07_concurrent_post_union(principal_of, chunk_factory):
    erin = principal_of("erin")
    assert f"dept:{BACKEND}" in erin.subjects
    assert f"dept:{FRONTEND}" in erin.subjects

    backend_doc = chunk_factory("c7a", "kb_tech", acl_tags={f"dept:{BACKEND}"})
    frontend_doc = chunk_factory("c7b", "kb_tech", acl_tags={f"dept:{FRONTEND}"})
    assert is_visible(backend_doc, erin) is True
    assert is_visible(frontend_doc, erin) is True


# ══════════════════════════════════════════════════════════
# 用例 8 · ★ 上级看下级（A1 祖先展开，Q3）
# ══════════════════════════════════════════════════════════
def test_08_ancestor_sees_descendant(principal_of, chunk_factory):
    alice = principal_of("alice")
    tech_doc = chunk_factory("c8", "kb_tech", acl_tags={f"dept:{TECH_CENTER}"})
    assert is_visible(tech_doc, alice) is True


# ══════════════════════════════════════════════════════════
# 用例 9 · ★ 同级不可见（兄弟隔离）
# ══════════════════════════════════════════════════════════
def test_09_sibling_invisible(principal_of, chunk_factory):
    alice = principal_of("alice")  # 后端组
    frontend_doc = chunk_factory("c9", "kb_tech", acl_tags={f"dept:{FRONTEND}"})
    assert f"dept:{FRONTEND}" not in alice.subjects  # 用户的子孙展开不跨兄弟
    assert is_visible(frontend_doc, alice) is False


# ══════════════════════════════════════════════════════════
# 用例 10 · ★★ 防全公司互通回归 —— 全篇最危险的一条
# ══════════════════════════════════════════════════════════
def test_10_no_company_wide_leak(principal_of, chunk_factory):
    alice = principal_of("alice")  # 后端组
    bob = principal_of("bob")  # 前端组

    backend_doc = chunk_factory("c10a", "kb_tech", acl_tags={f"dept:{BACKEND}"})
    frontend_doc = chunk_factory("c10b", "kb_tech", acl_tags={f"dept:{FRONTEND}"})

    # ① 双向隔离：各自只见自己的
    assert is_visible(backend_doc, alice) is True
    assert is_visible(frontend_doc, alice) is False
    assert is_visible(frontend_doc, bob) is True
    assert is_visible(backend_doc, bob) is False

    # ② 结构断言：文档标签 MUST NOT 含任何祖先路径
    tags = compute_doc_acl_tags(dept_path=BACKEND)
    assert tags == frozenset({f"dept:{BACKEND}"})
    assert f"dept:{TECH_CENTER}" not in tags, "H1 违规：文档标签展开了祖先"
    assert "dept:/总部/" not in tags, "H1 违规：文档标签展开了祖先"


# ══════════════════════════════════════════════════════════
# 用例 10b · ★★ 反面证明：祖先展开一旦打开，泄漏**真的会发生**
#   —— 这条断言的存在，是为了让任何人都不敢把
#      DOC_TAG_ANCESTOR_EXPANSION 改成 True。
# ══════════════════════════════════════════════════════════
def test_10b_ancestor_expansion_would_leak(
    principal_of, chunk_factory, monkeypatch
):
    alice = principal_of("alice")  # 后端组

    # 现状：前端组文档对他不可见
    normal = chunk_factory(
        "c10c", "kb_tech", acl_tags=compute_doc_acl_tags(dept_path=FRONTEND)
    )
    assert is_visible(normal, alice) is False

    # 一旦按"两侧都展开祖先"实现（错误做法）
    monkeypatch.setattr(D, "DOC_TAG_ANCESTOR_EXPANSION", True)
    leaked_tags = compute_doc_acl_tags(dept_path=FRONTEND)
    assert "dept:/总部/" in leaked_tags  # 已含根节点
    leaked = chunk_factory("c10d", "kb_tech", acl_tags=leaked_tags)
    assert is_visible(leaked, alice) is True, (
        "若这里不是 True，说明防泄漏机制并未依赖 DOC_TAG_ANCESTOR_EXPANSION，"
        "请检查实现是否绕开了口径常量。"
    )


# ══════════════════════════════════════════════════════════
# 补充用例 11 · 空标签规范化为 {public}
# ══════════════════════════════════════════════════════════
def test_11_empty_acl_tags_normalized_to_public():
    assert compute_doc_acl_tags() == frozenset({"public"})
    assert compute_doc_acl_tags(extra_tags=()) == frozenset({"public"})
    with pytest.raises(AclTagError):
        validate_acl_tags([])  # 空数组本身不合法


# ══════════════════════════════════════════════════════════
# 补充用例 12 · 租户隔离
# ══════════════════════════════════════════════════════════
def test_12_tenant_isolation(principal_of, chunk_factory):
    alice = principal_of("alice")
    other_tenant = chunk_factory(
        "c12",
        "kb_public",
        acl_tags={"public"},
        level_rank=D.LEVEL_RANK_PUBLIC,
        tenant_id="t2",
    )
    assert is_visible(other_tenant, alice) is False


# ══════════════════════════════════════════════════════════
# 补充用例 13 · 软删 / 历史版本
# ══════════════════════════════════════════════════════════
def test_13_soft_deleted_and_stale_invisible(principal_of, chunk_factory, ):
    from tests.acl.conftest import STALE_TIME

    alice = principal_of("alice")
    base = {"kb_id": "kb_tech", "acl_tags": {f"dept:{BACKEND}"}}

    assert is_visible(chunk_factory("c13a", deleted_at=STALE_TIME, **base), alice) is False
    assert is_visible(chunk_factory("c13b", is_latest=False, **base), alice) is False
    assert is_visible(chunk_factory("c13c", **base), alice) is True


# ══════════════════════════════════════════════════════════
# 补充用例 14 · §8 D-12 敏感部门对祖先不可见
# ══════════════════════════════════════════════════════════
def test_14_visible_to_parent_false_excludes_subtree(principal_of, chunk_factory):
    frank = principal_of("frank")  # 人力资源部（父）
    kate = principal_of("kate")  # 薪酬组（visible_to_parent=False）

    payroll_doc = chunk_factory("c14", "kb_public", acl_tags={f"dept:{PAYROLL}"})
    assert f"dept:{PAYROLL}" not in frank.subjects, "D-12 违规：祖先拿到了敏感子部门标签"
    assert is_visible(payroll_doc, frank) is False
    assert is_visible(payroll_doc, kate) is True  # 本部门成员照常可见

    # 下级仍能看上级（A1 祖先展开不受 visible_to_parent 影响）
    hr_doc = chunk_factory("c14b", "kb_public", acl_tags={f"dept:{HR}"})
    assert is_visible(hr_doc, kate) is True


# ══════════════════════════════════════════════════════════
# 补充用例 15 · 启动自检
# ══════════════════════════════════════════════════════════
def test_15_self_check_passes():
    D.self_check()
    assert D.CONTRACT_VERSION == "v1.1"


# ══════════════════════════════════════════════════════════
# 补充用例 16 · 下推 SQL MUST 含全部四个闸门（H7）
# ══════════════════════════════════════════════════════════
def test_16_pushdown_sql_contains_all_gates(principal_of):
    sql = PUSHDOWN_WHERE_SQL
    for token in ("tenant_id", "deleted_at", "is_latest", "level_rank", "ANY(:authorized_kb_ids)", "deny_subjects", "acl_tags"):
        assert token in sql, f"下推 SQL 缺少 {token}"
    assert sql.count("-- G") == 4

    params = pushdown_params(principal_of("alice"))
    assert params["tenant_id"] == "t1"
    assert params["clearance"] == D.LEVEL_RANK_INTERNAL
    assert "kb_tech" in params["authorized_kb_ids"]
