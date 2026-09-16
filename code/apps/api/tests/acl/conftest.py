"""权限用例的公共种子数据（手册 §7.2 前置数据）。

组织树：3 层 —— /总部/技术中心/后端组/、/总部/技术中心/前端组/、/总部/财务部/
外加一条 D-12 用的敏感分支：/总部/人力资源部/薪酬组/（visible_to_parent=False）

知识库：1 个普通（按部门授权）+ 1 个公开（is_public）
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from app.config import decisions as D
from app.services.acl import (
    Chunk,
    DeptNode,
    KnowledgeBaseRecord,
    Principal,
    UserRecord,
    build_principal,
)

TENANT = "t1"

# ── 组织树 ────────────────────────────────────────────────
ORG: dict[str, DeptNode] = {
    "/总部/": DeptNode(path="/总部/"),
    "/总部/技术中心/": DeptNode(path="/总部/技术中心/"),
    "/总部/技术中心/后端组/": DeptNode(path="/总部/技术中心/后端组/"),
    "/总部/技术中心/前端组/": DeptNode(path="/总部/技术中心/前端组/"),
    "/总部/财务部/": DeptNode(path="/总部/财务部/"),
    "/总部/人力资源部/": DeptNode(path="/总部/人力资源部/"),
    # ★ §8 D-12：敏感部门，祖先看不见它（及其子树）
    "/总部/人力资源部/薪酬组/": DeptNode(
        path="/总部/人力资源部/薪酬组/", visible_to_parent=False
    ),
}

# ── 知识库 ────────────────────────────────────────────────
KBS: tuple[KnowledgeBaseRecord, ...] = (
    KnowledgeBaseRecord(
        id="kb_tech", tenant_id=TENANT, members=("dept:/总部/技术中心/",)
    ),
    KnowledgeBaseRecord(
        id="kb_finance", tenant_id=TENANT, members=("dept:/总部/财务部/",)
    ),
    KnowledgeBaseRecord(id="kb_public", tenant_id=TENANT, is_public=True),
)

# ── 用户 ──────────────────────────────────────────────────
USERS: dict[str, UserRecord] = {
    "alice": UserRecord(  # 后端组
        id="alice",
        tenant_id=TENANT,
        dept_paths=("/总部/技术中心/后端组/",),
        clearance=D.LEVEL_RANK_INTERNAL,
        role_names=("user",),
    ),
    "bob": UserRecord(  # 前端组（alice 的兄弟部门）
        id="bob",
        tenant_id=TENANT,
        dept_paths=("/总部/技术中心/前端组/",),
        clearance=D.LEVEL_RANK_INTERNAL,
        role_names=("user",),
    ),
    "carol": UserRecord(  # 财务部
        id="carol",
        tenant_id=TENANT,
        dept_paths=("/总部/财务部/",),
        clearance=D.LEVEL_RANK_CONFIDENTIAL,
        role_names=("user",),
    ),
    "dave": UserRecord(  # 新账号：无部门、无角色
        id="dave",
        tenant_id=TENANT,
        dept_paths=(),
        clearance=D.LEVEL_RANK_PUBLIC,
    ),
    "erin": UserRecord(  # 兼岗：后端组 + 前端组
        id="erin",
        tenant_id=TENANT,
        dept_paths=("/总部/技术中心/后端组/", "/总部/技术中心/前端组/"),
        clearance=D.LEVEL_RANK_INTERNAL,
        role_names=("user",),
    ),
    "frank": UserRecord(  # 人力资源部（薪酬组的上级部门）
        id="frank",
        tenant_id=TENANT,
        dept_paths=("/总部/人力资源部/",),
        clearance=D.LEVEL_RANK_CONFIDENTIAL,
        role_names=("user",),
    ),
    "kate": UserRecord(  # 薪酬组（visible_to_parent=False）
        id="kate",
        tenant_id=TENANT,
        dept_paths=("/总部/人力资源部/薪酬组/",),
        clearance=D.LEVEL_RANK_CONFIDENTIAL,
        role_names=("user",),
    ),
}

STALE_TIME = datetime(2020, 1, 1, tzinfo=UTC)


# ── fixtures ──────────────────────────────────────────────
@pytest.fixture
def depts() -> dict[str, DeptNode]:
    return ORG


@pytest.fixture
def kbs() -> tuple[KnowledgeBaseRecord, ...]:
    return KBS


@pytest.fixture
def principal_of(
    depts: dict[str, DeptNode], kbs: tuple[KnowledgeBaseRecord, ...]
) -> Callable[[str], Principal]:
    """`principal_of("alice")` → 解析好的 Principal。"""

    def _factory(user_id: str) -> Principal:
        return build_principal(USERS[user_id], depts, kbs)

    return _factory


@pytest.fixture
def chunk_factory() -> Callable[..., Chunk]:
    """`chunk_factory("c1", "kb_tech", acl_tags={"dept:/总部/技术中心/后端组/"})`。"""

    def _factory(
        chunk_id: str,
        kb_id: str,
        *,
        acl_tags: set[str] | frozenset[str] | None = None,
        level_rank: int = D.LEVEL_RANK_INTERNAL,
        deny: set[str] | frozenset[str] = frozenset(),
        tenant_id: str = TENANT,
        deleted_at: datetime | None = None,
        is_latest: bool = True,
    ) -> Chunk:
        return Chunk(
            id=chunk_id,
            tenant_id=tenant_id,
            kb_id=kb_id,
            level_rank=level_rank,
            acl_tags=frozenset({"public"} if acl_tags is None else acl_tags),
            deny_subjects=frozenset(deny),
            deleted_at=deleted_at,
            is_latest=is_latest,
        )

    return _factory
