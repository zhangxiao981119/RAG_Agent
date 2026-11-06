"""第 4 步 · 导航查询的 ACL / 口径**真库探针**（设计 §2.1.4 / §2.6 ④）。

★ 为什么必须真库（第 3 步 P2-1 / 变异 M1 的教训）：`test_nav_sql_contract.py`
  的 SQL 文本断言只能证明「SQL 里写了 EXISTS」，删掉 EXISTS 时**部分**行为级用例
  不变红。这里合成数据落真表，让 PostgreSQL 自己执行四闸门，断言
  `load_wiki_nav_page()` 在不同主体下真的返回 / 不返回那一页。

★ N4 fixture 纪律（本文件就是 fixture 脚本，不另起独立脚本以免范围蔓延）：
  · **含合成页**：所有 tenants / kbs / documents / chunks / wiki_pages 都是本文件
    造的，报告与日志 MUST 标注「含合成页」，不得混算进真实页覆盖率。
  · 独立租户 + 独立 kb（比「单独 kb」更隔离），随机 uuid 避免撞主键。
  · `finally` 逐表清理 + **SQL 复核**（`_verify_cleanup` 逐表 count=0），
    不允许合成数据残留进生产库。
  · 优先路径仍是 `python -m scripts.rebuild_wiki --all` 补真实页；合成页只用于
    让 `hit_cases >= 20` 门槛可被满足，不用于产出「不劣化」的业务结论。

★ 环境纪律：缺 `WIKI_NAV_ACL_DB_URL` / 连不上 / 未跑到 0026 → **显式 skip**。
  容器内用法：
  WIKI_NAV_ACL_DB_URL=postgresql+asyncpg://user:pass@postgres:5432/kagent pytest ...
  本机（无 Docker daemon / 无 PostgreSQL 实例）**未跑**，见交付回报的未跑项清单。
  纯函数用例（`test_hit_page_whose_terms_are_all_in_query_expands_nothing`）
  不依赖真库，任何环境都跑；它证明的是选词规则，不替代 ACL 真库探针。
"""
from __future__ import annotations

import json
import os
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config.wiki_profile import WikiProfile
from app.services.wiki.navigate import load_wiki_nav_page, select_nav_terms

_DB_URL = os.getenv("WIKI_NAV_ACL_DB_URL", "").strip()

TENANT = uuid.uuid4()
KB_A = uuid.uuid4()       # 授权库
KB_B = uuid.uuid4()       # 越权库
PROFILE = WikiProfile(generator_model="acl-probe-model", prompt_version="wprompt-v1")
OK_SUBJECT = "dept:it"

# (用例名, needle, kb, level_rank, acl_tags, deny_subjects, status, gm)
CASES = (
    ("ok", "访问", KB_A, 10, [OK_SUBJECT], [], "ready", PROFILE.generator_model),
    ("cross_kb", "跨库", KB_B, 10, [OK_SUBJECT], [], "ready", PROFILE.generator_model),
    ("high_level", "密级", KB_A, 50, [OK_SUBJECT], [], "ready", PROFILE.generator_model),
    ("deny", "拒绝", KB_A, 10, [OK_SUBJECT], [OK_SUBJECT], "ready", PROFILE.generator_model),
    ("acl_mismatch", "标签", KB_A, 10, ["dept:finance"], [], "ready", PROFILE.generator_model),
    ("stale", "过期", KB_A, 10, [OK_SUBJECT], [], "stale", PROFILE.generator_model),
    ("failed", "失败", KB_A, 10, [OK_SUBJECT], [], "failed", PROFILE.generator_model),
    ("caliber", "口径", KB_A, 10, [OK_SUBJECT], [], "ready", "other-model"),
)
IDS = {name: (uuid.uuid4(), uuid.uuid4()) for name, *_ in CASES}


@pytest.fixture
def db_url() -> str:
    if not _DB_URL:
        pytest.skip(
            "需显式提供 WIKI_NAV_ACL_DB_URL（容器内真库，已 alembic upgrade head）；"
            "宿主机默认跳过（本机无 Docker daemon / 无 PostgreSQL 实例）"
        )
    return _DB_URL


def _pushdown(*, clearance: int = 60, kb_ids: list[uuid.UUID] | None = None,
              subjects: list[str] | None = None) -> dict:
    """与 `acl.visibility.pushdown_params()` 同形状：四闸门绑定值原样传，不重算。"""
    return {
        "tenant_id": TENANT,
        "clearance": clearance,
        "authorized_kb_ids": sorted(kb_ids if kb_ids is not None else [KB_A]),
        "user_subjects": sorted(subjects if subjects is not None else [OK_SUBJECT]),
    }


async def _probe(engine) -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
        ready = (
            await conn.execute(
                text(
                    "SELECT (to_regclass('public.wiki_pages') IS NOT NULL"
                    " AND to_regclass('public.chunks') IS NOT NULL"
                    " AND to_regclass('public.documents') IS NOT NULL"
                    " AND to_regclass('public.knowledge_bases') IS NOT NULL"
                    " AND to_regclass('public.tenants') IS NOT NULL) AS ok"
                )
            )
        ).scalar_one()
    if not ready:
        raise RuntimeError("库未跑到 0026（wiki_pages / chunks 等表缺失）")


async def _seed(engine) -> None:
    """合成数据：一个独立租户 + 两个 kb + 八个文档/页（**含合成页**）。"""
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO tenants (id, name, code, status)"
                 " VALUES (:tid, 'nav-acl-probe', :code, 'active')"),
            {"tid": TENANT, "code": f"navacl-{TENANT.hex[:12]}"},
        )
        for kb in (KB_A, KB_B):
            await conn.execute(
                text("INSERT INTO knowledge_bases (id, tenant_id, name)"
                     " VALUES (:kb, :tid, :name)"),
                {"kb": kb, "tid": TENANT, "name": f"nav-acl-kb-{kb.hex[:8]}"},
            )
        for name, needle, kb, level, acl_tags, deny, status, gm in CASES:
            doc_id, page_id = IDS[name]
            await conn.execute(
                text(
                    "INSERT INTO documents"
                    " (id, tenant_id, kb_id, doc_group_id, filename, ext, size_bytes,"
                    "  storage_key, level_rank, acl_tags, deny_subjects)"
                    " VALUES (:doc, :tid, :kb, :grp, :filename, 'md', 1,"
                    "  :storage_key, :level, CAST(:acl AS text[]), CAST(:deny AS text[]))"
                ),
                {
                    "doc": doc_id, "tid": TENANT, "kb": kb, "grp": uuid.uuid4(),
                    "filename": f"nav-acl-{name}.md",
                    "storage_key": f"nav-acl/{doc_id.hex}",
                    "level": level, "acl": acl_tags, "deny": deny,
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO chunks"
                    " (id, tenant_id, kb_id, document_id, chunk_index, content,"
                    "  token_count, level_rank, acl_tags, deny_subjects, is_latest)"
                    " VALUES (:cid, :tid, :kb, :doc, 0, 'nav-acl-probe-chunk', 1,"
                    "  :level, CAST(:acl AS text[]), CAST(:deny AS text[]), true)"
                ),
                {
                    "cid": uuid.uuid4(), "tid": TENANT, "kb": kb, "doc": doc_id,
                    "level": level, "acl": acl_tags, "deny": deny,
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO wiki_pages"
                    " (id, tenant_id, kb_id, document_id, title, summary, key_terms,"
                    "  generator_model, prompt_version, content_hash, status)"
                    " VALUES (:page, :tid, :kb, :doc, :title, '合成导航摘要',"
                    "  CAST(:terms AS jsonb), :gm, :pv, :hash, :status)"
                ),
                {
                    "page": page_id, "tid": TENANT, "kb": kb, "doc": doc_id,
                    "title": f"合成页-{name}",
                    "terms": json.dumps([f"{needle}令牌", "审计日志"], ensure_ascii=False),
                    "gm": gm, "pv": PROFILE.prompt_version,
                    "hash": f"navacl{name[:8]:0>8}", "status": status,
                },
            )


async def _cleanup(engine) -> None:
    """按外键依赖顺序删干净；wiki_pages/chunks 有 document_id CASCADE 仍显式删。"""
    async with engine.begin() as conn:
        for table in ("wiki_pages", "chunks", "documents", "knowledge_bases"):
            await conn.execute(
                text(f"DELETE FROM {table} WHERE tenant_id = :tid"), {"tid": TENANT}
            )
        await conn.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": TENANT})


async def _verify_cleanup(engine) -> None:
    """N4 的「SQL 复核」：清理后逐表 count MUST 全 0，否则合成页残留。"""
    async with engine.connect() as conn:
        for table in ("wiki_pages", "chunks", "documents", "knowledge_bases"):
            left = (
                await conn.execute(
                    text(f"SELECT count(*) FROM {table} WHERE tenant_id = :tid"),
                    {"tid": TENANT},
                )
            ).scalar_one()
            assert left == 0, f"合成数据残留：{table} 仍有 {left} 行"
        tenants = (
            await conn.execute(
                text("SELECT count(*) FROM tenants WHERE id = :tid"), {"tid": TENANT}
            )
        ).scalar_one()
        assert tenants == 0, "合成租户未清理"


@asynccontextmanager
async def _real_db(url: str):
    """seed → yield engine → **无论成功失败都清理 + SQL 复核**。"""
    eng = create_async_engine(
        url, poolclass=NullPool, connect_args={"statement_cache_size": 0}
    )
    try:
        await _probe(eng)
    except Exception as exc:  # 环境不具备 → 显式 skip，不让宿主机报错
        await eng.dispose()
        pytest.skip(f"真库不可用或未迁移到 0026：{exc}")
    try:
        await _seed(eng)
        yield eng
    finally:
        await _cleanup(eng)
        await _verify_cleanup(eng)
        await eng.dispose()


async def _nav(sessions, needles: list[str], **pushdown_kw):
    async with sessions() as session:
        return await load_wiki_nav_page(
            session,
            needles=needles,
            pushdown=_pushdown(**pushdown_kw),
            profile=PROFILE,
            timeout_ms=200,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "needle", "kwargs", "expect_hit"),
    [
        # 基线：先证明「有权限时确实命中」，否则后面全部「不命中」是假绿
        ("ok", "访问", {}, True),
        # 四类越权（§2.6 ④）
        ("cross_kb", "跨库", {"kb_ids": [KB_A]}, False),   # 页在 KB_B，授权只有 KB_A
        ("high_level", "密级", {"clearance": 10}, False),  # chunk level_rank=50 > 10
        ("deny", "拒绝", {}, False),                        # deny_subjects && user_subjects
        ("acl_mismatch", "标签", {}, False),                # acl_tags && user_subjects 不成立
        # 三态 + 口径不匹配
        ("stale", "过期", {}, False),
        ("failed", "失败", {}, False),
        ("caliber", "口径", {}, False),                     # gm 与 profile 不符 → fail-closed
    ],
)
async def test_nav_sql_enforces_acl_status_and_caliber(
    db_url, name, needle, kwargs, expect_hit
):
    """真库语义级断言：越权 / 非 ready / 口径不符页 MUST 一条都不返回。"""
    async with _real_db(db_url) as engine:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        out = await _nav(sessions, [needle], **kwargs)

        assert (out.document_id is not None) is expect_hit, (
            f"{name}: document_id={out.document_id}，期望 hit={expect_hit}"
        )
        if expect_hit:
            assert out.degraded_reason == ""
            assert out.key_terms and needle in out.key_terms[0]
        else:
            assert out.key_terms == (), "越权页不该带回任何可扩词元"
            assert out.degraded_reason == "no_match"


@pytest.mark.asyncio
async def test_success_path_restores_statement_timeout(db_url):
    """成功路径最后一条 SQL 还原到调用前值（§2.4 ④ 的真库复核）。"""
    async with _real_db(db_url) as engine:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            before = (
                await session.execute(text("SELECT current_setting('statement_timeout')"))
            ).scalar_one()
            out = await load_wiki_nav_page(
                session,
                needles=["访问"],
                pushdown=_pushdown(),
                profile=PROFILE,
                timeout_ms=200,
            )
            after = (
                await session.execute(text("SELECT current_setting('statement_timeout')"))
            ).scalar_one()
        assert out.document_id is not None
        assert after == before, "事务级超时预算泄漏到了后续 SQL"


def test_hit_page_whose_terms_are_all_in_query_expands_nothing():
    """§2.6 ④ 的纯函数层：命中页但 key_terms 全在 query 里 → terms=()。

    ★ 这条**不依赖真库**，任何环境都跑；它只证明选词去重规则，
      不替代上面的 ACL 真库探针（两者的失败形态完全不同）。
    """
    terms, fts = select_nav_terms(
        ["访问令牌", "审计日志"],
        ["访问令牌", "审计日志"],
        "访问令牌和审计日志",
        set(),
    )
    assert terms == [] and fts == []
