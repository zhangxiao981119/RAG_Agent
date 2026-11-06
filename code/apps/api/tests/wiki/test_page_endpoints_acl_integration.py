"""wiki 读接口 ACL 真库集成测试 —— 第 3 步 tester 行为级探针收编（P2-10）。

与 `test_page_endpoints.py` 的分工：
  · 那个文件是**替身 session 单元钉子**，只做 SQL 文本断言（`assert "EXISTS" in last_sql()`），
    任何环境可跑，但不评估 SQL 语义 —— 删掉 EXISTS 时部分行为级用例不会变红（见
    `04-test/测试报告-第3步-wiki.md` P2-1 与变异 M1）。原钉子全部保留，本文件是补强。
  · 本文件走**真实 FastAPI 路由 + 真实 PostgreSQL**：合成数据落真表，四闸门 EXISTS
    交给数据库执行，断言不同 ACL 主体下的状态码与**响应体逐字节**差异。M1 变异
    （删掉详情 SQL 的 EXISTS）会让「无权主体 404」变成越权 200，本用例随即变红。

★ 环境纪律（为什么这样搭，别照搬 TestClient 写法）：
  · 缺 DB / 非容器环境**显式 skip**：模块级 `WIKI_ACL_DB_URL` 未提供即整体跳过；
    提供了但连不上、或没跑到 0026 迁移也 skip —— 宿主机 pytest 不会因此报错。
    容器内用法：`WIKI_ACL_DB_URL=postgresql+asyncpg://user:pass@postgres:5432/kagent pytest ...`。
  · **不进 `with TestClient(app)`**（与 tests/chat 同款纪律）：lifespan 会真连一次全局库，
    asyncpg 连接池绑到第一个事件循环，后续请求报 `attached to a different loop`。
    改用 `httpx.ASGITransport`（不发送 lifespan scope）+ 自建 `NullPool` engine，
    整个用例在**单个事件循环**里跑完，连池都不跨循环复用。
  · 认证主体与能力开关仍走依赖覆盖 / 模块打桩（沿用既有测试做法）：被测的是
    **真实路由 + 真实 SQL + 四闸门语义**，不是 JWT 解析；session 是真库会话，非替身。
  · 合成数据用独立租户，`finally` 逐表清理，不碰真业务数据。
"""
from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.api.wiki as wiki_mod
from app.api.deps import CurrentUser, get_current_user, get_db
from app.main import app

_DB_URL = os.getenv("WIKI_ACL_DB_URL", "").strip()

pytestmark = [
    pytest.mark.skipif(
        not _DB_URL,
        reason="需显式提供 WIKI_ACL_DB_URL（容器内真库，已 alembic upgrade head）；宿主机默认跳过",
    ),
    pytest.mark.asyncio,
]

# 每次收集生成一批独立 id，配合独立租户，避免与真业务数据/历史残留撞主键。
TENANT_ID = uuid.uuid4()
KB_ID = uuid.uuid4()
DOC_DEPT = uuid.uuid4()
DOC_PUBLIC = uuid.uuid4()
DOC_FAILED = uuid.uuid4()
PAGE_DEPT = uuid.uuid4()
PAGE_PUBLIC = uuid.uuid4()
PAGE_FAILED = uuid.uuid4()

DEPT_SUBJECT = "dept:/总部/技术中心/后端组/"


def _user(*, clearance: int, subjects: list[str], kb_ids: list[uuid.UUID]) -> CurrentUser:
    return CurrentUser(
        user_id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        username="acl-probe",
        display_name="acl-probe",
        clearance=clearance,
        dept_path="",
        subjects=subjects,
        authorized_kb_ids=kb_ids,
        acl_epoch=1,
    )


async def _probe(engine) -> None:
    """连库 + 确认已跑到 0026；不满足就抛，由用例转成显式 skip。"""
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
    """合成三份文档：dept-only / public / failed 页，外加两条过闸门的 chunk。

    dept-only chunk 的 acl_tags 只含部门主体、level_rank 高于 public-only 主体的
    clearance —— 于是 public-only 主体对这份文档**零可见 chunk**，详情 MUST 404。
    """
    docs = (
        (DOC_DEPT, "dept-only.pdf", 50, [DEPT_SUBJECT]),
        (DOC_PUBLIC, "public.md", 10, ["public"]),
        (DOC_FAILED, "failed.md", 10, ["public"]),
    )
    pages = (
        (PAGE_DEPT, DOC_DEPT, "受限文档导航页", "ready"),
        (PAGE_PUBLIC, DOC_PUBLIC, "公开文档导航页", "ready"),
        (PAGE_FAILED, DOC_FAILED, "生成失败页", "failed"),
    )
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, code, status)"
                " VALUES (:tid, 'acl-probe-tenant', :code, 'active')"
            ),
            {"tid": TENANT_ID, "code": f"acl-probe-{TENANT_ID.hex[:12]}"},
        )
        await conn.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name)"
                " VALUES (:kb, :tid, 'acl-probe-kb')"
            ),
            {"kb": KB_ID, "tid": TENANT_ID},
        )
        for doc_id, filename, level_rank, acl_tags in docs:
            await conn.execute(
                text(
                    "INSERT INTO documents"
                    " (id, tenant_id, kb_id, doc_group_id, filename, ext, size_bytes,"
                    "  storage_key, level_rank, acl_tags, deny_subjects)"
                    " VALUES (:doc, :tid, :kb, :grp, :filename, 'md', 1,"
                    "  :storage_key, :level_rank, CAST(:acl_tags AS text[]),"
                    " CAST(:deny AS text[]))"
                ),
                {
                    "doc": doc_id,
                    "tid": TENANT_ID,
                    "kb": KB_ID,
                    "grp": uuid.uuid4(),
                    "filename": filename,
                    "storage_key": f"acl-probe/{doc_id.hex}",
                    "level_rank": level_rank,
                    "acl_tags": acl_tags,
                    "deny": [],
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO chunks"
                    " (id, tenant_id, kb_id, document_id, chunk_index, content,"
                    "  token_count, level_rank, acl_tags, deny_subjects, is_latest)"
                    " VALUES (:cid, :tid, :kb, :doc, 0, 'acl-probe-chunk', 1,"
                    "  :level_rank, CAST(:acl_tags AS text[]), CAST(:deny AS text[]), true)"
                ),
                {
                    "cid": uuid.uuid4(),
                    "tid": TENANT_ID,
                    "kb": KB_ID,
                    "doc": doc_id,
                    "level_rank": level_rank,
                    "acl_tags": acl_tags,
                    "deny": [],
                },
            )
        for page_id, doc_id, title, page_status in pages:
            await conn.execute(
                text(
                    "INSERT INTO wiki_pages"
                    " (id, tenant_id, kb_id, document_id, title, summary,"
                    "  generator_model, prompt_version, content_hash, status)"
                    " VALUES (:page, :tid, :kb, :doc, :title, '合成导航摘要',"
                    "  'acl-probe-model', 'wprompt-v1', 'aclprobe0000', :status)"
                ),
                {
                    "page": page_id,
                    "tid": TENANT_ID,
                    "kb": KB_ID,
                    "doc": doc_id,
                    "title": title,
                    "status": page_status,
                },
            )


async def _cleanup(engine) -> None:
    """按外键依赖顺序删干净合成数据（wiki_pages/chunks 有 document_id CASCADE，仍显式删）。"""
    async with engine.begin() as conn:
        for table in ("wiki_pages", "chunks", "documents", "knowledge_bases"):
            await conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :tid"), {"tid": TENANT_ID})
        await conn.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": TENANT_ID})


async def test_wiki_acl_is_enforced_by_real_postgres():
    """四闸门 EXISTS 是真实生效的访问控制：不同 ACL 主体下状态码与响应体 MUST 不同。

    ★ 变异可检性：把 `wiki.py::_PAGE_VISIBLE_SQL` 的 `AND EXISTS (...)` 整段删掉后，
      public-only 主体访问 dept-only 页会从 404 变成 200，下面 `assert ... == 404`
      立即变红 —— 这正是 P2-1 里假 session 钉子没守住的行为级防线。
    """
    # NullPool 下每次请求新建连接，asyncpg 的命名 prepared statement 缓存无收益；
    # 关掉它还能兼容 pgbouncer / 多连接复用型 PG 代理（避免同名语句冲突）。
    engine = create_async_engine(
        _DB_URL, poolclass=NullPool, connect_args={"statement_cache_size": 0}
    )
    try:
        await _probe(engine)
    except Exception as exc:  # 环境不具备（连不上 / 未迁移）→ 显式 skip，不让宿主机报错
        await engine.dispose()
        pytest.skip(f"真库不可用或未迁移到 0026：{exc}")

    orig_get_settings = wiki_mod.get_settings
    try:
        await _seed(engine)

        sessions = async_sessionmaker(engine, expire_on_commit=False)

        async def _override_get_db():
            async with sessions() as session:
                yield session

        # 主体用 holder 间接引用：同一事件循环里切换 ACL 身份，不重建依赖覆盖，
        # 避免「依赖覆盖全局性导致身份串用」（第 3 步探针调试踩过的坑）。
        holder = {
            "user": _user(
                clearance=60, subjects=["public", DEPT_SUBJECT], kb_ids=[KB_ID]
            )
        }
        app.dependency_overrides[get_db] = _override_get_db
        app.dependency_overrides[get_current_user] = lambda: holder["user"]
        wiki_mod.get_settings = lambda: SimpleNamespace(wiki_enabled=True)

        transport = httpx.ASGITransport(app=app)  # 不发送 lifespan scope
        async with httpx.AsyncClient(transport=transport, base_url="http://acl-probe") as client:
            detail_url = f"/api/wiki/pages/{PAGE_DEPT}"
            list_url = f"/api/kbs/{KB_ID}/wiki/pages"

            # ① 全权主体：dept-only 页 200，响应体带 caliber / 固定免责声明
            r_admin = await client.get(detail_url)
            assert r_admin.status_code == 200, r_admin.text
            body_admin = r_admin.json()
            assert body_admin["title"] == "受限文档导航页"
            assert body_admin["caliber"] == {
                "generator_model": "acl-probe-model",
                "prompt_version": "wprompt-v1",
            }
            assert "不构成引用依据" in body_admin["disclaimer"]

            r_list_admin = await client.get(list_url)
            assert r_list_admin.status_code == 200, r_list_admin.text
            ids_admin = {i["document_id"] for i in r_list_admin.json()["items"]}
            assert {str(DOC_DEPT), str(DOC_PUBLIC)} <= ids_admin

            # ② public-only 主体：对 dept-only 文档零可见 chunk → 详情 404
            #    （M1 变异下这里会变成 200，本断言即为变红点）
            holder["user"] = _user(clearance=20, subjects=["public"], kb_ids=[KB_ID])
            r_denied = await client.get(detail_url)
            assert r_denied.status_code == 404, r_denied.text

            # 不同 ACL 主体的状态码与响应体 MUST 有差异（不是同一份响应）
            assert r_denied.status_code != r_admin.status_code
            assert r_denied.content != r_admin.content

            # ③ 404 三合一逐字节一致：无可见 chunk / 页不存在 / 生成失败
            r_missing = await client.get(f"/api/wiki/pages/{uuid.uuid4()}")
            r_failed = await client.get(f"/api/wiki/pages/{PAGE_FAILED}")
            assert r_missing.status_code == r_failed.status_code == 404
            assert r_denied.content == r_missing.content == r_failed.content
            assert r_denied.headers["content-type"] == r_admin.headers["content-type"]

            # ④ 列表过滤：public-only 看得到 public 页，看不到 dept-only 页 / failed 页
            r_list_public = await client.get(list_url)
            assert r_list_public.status_code == 200, r_list_public.text
            ids_public = {i["document_id"] for i in r_list_public.json()["items"]}
            assert str(DOC_PUBLIC) in ids_public
            assert str(DOC_DEPT) not in ids_public
            assert str(DOC_FAILED) not in ids_public

            # ⑤ 非 kb 成员：列表 403（复用 get_kb 口径），详情仍是 404 不泄漏存在性
            holder["user"] = _user(clearance=20, subjects=["public"], kb_ids=[])
            r_list_nonmember = await client.get(list_url)
            assert r_list_nonmember.status_code == 403
            r_detail_nonmember = await client.get(detail_url)
            assert r_detail_nonmember.status_code == 404
    finally:
        app.dependency_overrides.clear()
        wiki_mod.get_settings = orig_get_settings
        try:
            await _cleanup(engine)
        finally:
            await engine.dispose()
