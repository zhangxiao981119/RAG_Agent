"""第 4 步 · wiki 导航的「选择性路由」集成探针 + 合成 fixture（设计 §2.3.2 ① / §2.6 ①）。

★ 本文件一次交付两件东西：
  1. **真库集成 fixture（N4 纪律）**：合成一个独立租户 + 独立 kb 下的若干 wiki 页
     （每页 1 条匹配 chunks 行，含 0011 NOT-NULL 三列与合法 ACL），让 PostgreSQL 自己
     执行四闸门，断言 `load_wiki_nav_page()` 在授权 / 越权主体下真的返回 / 不返回。
     —— 这部分**仅在**显式提供 `WIKI_NAV_ACL_DB_URL` 时运行；否则逐用例 skip，
     不做任何 DB 连接（与 `test_nav_acl.py` 同纪律）。
  2. **无需 DB 的用例**（模块级 pytestmark 仅挂在 DB fixture 上，不挂整文件）：
     · `select_nav_terms` 纯函数（用合成页同款 key_terms + 构造 query）；
     · 选择性路由的 `rag.wiki.nav.skipped_by_router` 打点（复用
       `test_wiki_nav_off_state.py` 的 retrieve 层替身骨架，不连库）。

★ N4 fixture 纪律：
  · **含合成页**：所有 tenants / kbs / documents / chunks / wiki_pages 均为本文件造，
    报告与日志 MUST 标注「含合成页」。
  · 独立租户 + 独立 kb，随机 uuid 避免撞主键。
  · `finally` 逐表清理 + SQL 复核（`_teardown` 逐表 count=0），不允许合成数据残留。

★ 环境纪律：缺 `WIKI_NAV_ACL_DB_URL` / 连不上 / 未跑到 0026 → fixture 内显式 skip；
  不依赖真库的那两条钉子（纯函数 + 路由打点）**任何环境都能跑**。
"""
from __future__ import annotations

import json
import os
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config.wiki_profile import WikiProfile
from app.services import retrieve as retrieve_mod
from app.services.retrieve import RetrievalService
from app.services.wiki.navigate import WikiNavPage, load_wiki_nav_page, select_nav_terms

# ── 真库 env 开关（仅 DB 用例使用；缺省 → fixture 内 skip，不做连接）─────────
WIKI_NAV_ACL_DB_URL = os.environ.get("WIKI_NAV_ACL_DB_URL")

TENANT = uuid.uuid4()           # 独立租户
KB = uuid.uuid4()               # 授权库
KB_OTHER = uuid.uuid4()         # 越权库（仅用于 chunk 侧制造 G2 失败）

WIKI_PROFILE = WikiProfile(generator_model="selective-probe-model", prompt_version="wprompt-v1")
OK_SUBJECT = "dept:it"
DENY_SUBJECT = "dept:finance"   # 与 OK_SUBJECT 无交集 → 制造 G4 失败

# 0011 迁移回填常量（与 test_nav_acl.py 同口径）：chunks 三列被置 NOT NULL 且无
# server_default，真库 INSERT 必须显式给值，否则 NotNullViolationError（P1-1 实测）。
_SEED_EMBEDDING_MODEL = "bge-m3"
_SEED_EMBEDDING_TEXT_VERSION = "etext-v1"
_SEED_CHUNKER_VERSION = "c400-800-60"

# 1024 维 pgvector 字符串（'[...]' 形态，配合 `:embedding::vector` 内联转型）。
_EMBEDDING_1024 = "[" + ",".join("0.01" for _ in range(1024)) + "]"

# (用例名, key_terms, chunk_kb, level_rank, acl_tags, deny_subjects, status)
# ★ 三页的 **wiki_pages.kb 都是 KB（授权库）**，所以页级过滤器（tenant/kb/scope/
#   status/gm/pv）全部通过；差异只在「链接的 chunk」一侧，从而把失败精准归因到
#   EXISTS 四闸门（而非页级过滤器）—— 这正是本探针要证明的。
_PAGES = (
    # 授权可见页（基线，证明 seed 真的能命中）
    ("access", ["访问令牌", "认证配置", "单点登录"], KB, 10, [OK_SUBJECT], [], "ready"),
    # G4：chunk 的 acl_tags 与 user_subjects 无交集 → EXISTS 失败
    ("audit", ["审计日志", "操作留痕"], KB, 10, [DENY_SUBJECT], [], "ready"),
    # G2：chunk 的 kb 不在 authorized_kb_ids（KB_OTHER）→ EXISTS 失败
    ("rbac", ["权限模型", "角色继承"], KB_OTHER, 10, [OK_SUBJECT], [], "ready"),
)
_PAGE_IDS = {name: (uuid.uuid4(), uuid.uuid4()) for name, *_ in _PAGES}  # (doc, page)


@pytest.fixture
def db_url() -> str:
    """缺 `WIKI_NAV_ACL_DB_URL` / 连不上 → 显式 skip（不报错、不连库）。"""
    if not WIKI_NAV_ACL_DB_URL:
        pytest.skip(
            "需显式提供 WIKI_NAV_ACL_DB_URL（容器内真库，已 alembic upgrade head）；"
            "未显式提供该变量或不在 compose 网络内时跳过"
        )
    return WIKI_NAV_ACL_DB_URL


def _pushdown(*, clearance: int = 60, kb_ids: list[uuid.UUID] | None = None,
              subjects: list[str] | None = None) -> dict:
    """与 `acl.visibility.pushdown_params()` 同形状：四闸门绑定值原样传，不重算。"""
    return {
        "tenant_id": TENANT,
        "clearance": clearance,
        "authorized_kb_ids": sorted(kb_ids if kb_ids is not None else [KB]),
        "user_subjects": sorted(subjects if subjects is not None else [OK_SUBJECT]),
    }


async def _seed(engine, tenant_id: uuid.UUID = TENANT, kb_id: uuid.UUID = KB) -> None:
    """合成数据：独立租户 + 两个 kb + 三个文档/页（**含合成页**）。

    每页 dict 对应 `wiki_pages` 的必填列 + 一条匹配 `chunks` 行：
      · 1024 维合法 embedding（`:embedding::vector`）；
      · 0011 NOT-NULL 三列（embedding_model / embedding_text_version / chunker_version）；
      · 合法 ACL 值（level_rank / acl_tags / deny_subjects / is_latest）。
    """
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO tenants (id, name, code, status)"
                 " VALUES (:tid, 'nav-sel-probe', :code, 'active')"),
            {"tid": tenant_id, "code": f"navsel-{tenant_id.hex[:12]}"},
        )
        for kb in (kb_id, KB_OTHER):
            await conn.execute(
                text("INSERT INTO knowledge_bases (id, tenant_id, name)"
                     " VALUES (:kb, :tid, :name)"),
                {"kb": kb, "tid": tenant_id, "name": f"nav-sel-kb-{kb.hex[:8]}"},
            )
        for name, key_terms, chunk_kb, level, acl_tags, deny, status in _PAGES:
            doc_id, page_id = _PAGE_IDS[name]
            await conn.execute(
                text(
                    "INSERT INTO documents"
                    " (id, tenant_id, kb_id, doc_group_id, filename, ext, size_bytes,"
                    "  storage_key, level_rank, acl_tags, deny_subjects)"
                    " VALUES (:doc, :tid, :kb, :grp, :filename, 'md', 1,"
                    "  :storage_key, :level, CAST(:acl AS text[]), CAST(:deny AS text[]))"
                ),
                {
                    "doc": doc_id, "tid": tenant_id, "kb": kb_id,
                    "grp": uuid.uuid4(), "filename": f"nav-sel-{name}.md",
                    "storage_key": f"nav-sel/{doc_id.hex}",
                    "level": level, "acl": acl_tags, "deny": deny,
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO chunks"
                    " (id, tenant_id, kb_id, document_id, chunk_index, content, token_count,"
                    "  level_rank, acl_tags, deny_subjects, is_latest,"
                    "  embedding, embedding_model, embedding_text_version, chunker_version)"
                    " VALUES (:cid, :tid, :kb, :doc, 0, 'nav-sel-probe-chunk', 1,"
                    "  :level, CAST(:acl AS text[]), CAST(:deny AS text[]), true,"
                    "  :embedding::vector, :embedding_model, :embedding_text_version,"
                    "  :chunker_version)"
                ),
                {
                    "cid": uuid.uuid4(), "tid": tenant_id, "kb": chunk_kb, "doc": doc_id,
                    "level": level, "acl": acl_tags, "deny": deny,
                    "embedding": _EMBEDDING_1024,
                    "embedding_model": _SEED_EMBEDDING_MODEL,
                    "embedding_text_version": _SEED_EMBEDDING_TEXT_VERSION,
                    "chunker_version": _SEED_CHUNKER_VERSION,
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
                    "page": page_id, "tid": tenant_id, "kb": kb_id, "doc": doc_id,
                    "title": f"合成页-{name}",
                    "terms": json.dumps(key_terms, ensure_ascii=False),
                    "gm": WIKI_PROFILE.generator_model, "pv": WIKI_PROFILE.prompt_version,
                    "hash": f"navsel{name[:8]:0>8}", "status": status,
                },
            )


async def _teardown(engine, tenant_id: uuid.UUID = TENANT) -> None:
    """按外键依赖顺序删干净 + SQL 复核（合成数据 MUST 零残留）。"""
    async with engine.begin() as conn:
        for table in ("wiki_pages", "chunks", "documents", "knowledge_bases"):
            await conn.execute(
                text(f"DELETE FROM {table} WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await conn.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id})
    async with engine.connect() as conn:
        for table in ("wiki_pages", "chunks", "documents", "knowledge_bases"):
            left = (
                await conn.execute(
                    text(f"SELECT count(*) FROM {table} WHERE tenant_id = :tid"),
                    {"tid": tenant_id},
                )
            ).scalar_one()
            assert left == 0, f"合成数据残留：{table} 仍有 {left} 行"
        tenants = (
            await conn.execute(
                text("SELECT count(*) FROM tenants WHERE id = :tid"), {"tid": tenant_id}
            )
        ).scalar_one()
        assert tenants == 0, "合成租户未清理"


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
        await _teardown(eng)
        await eng.dispose()


async def _nav(sessions, needles: list[str], **pushdown_kw):
    async with sessions() as session:
        return await load_wiki_nav_page(
            session,
            needles=needles,
            pushdown=_pushdown(**pushdown_kw),
            profile=WIKI_PROFILE,
            timeout_ms=200,
        )


# ════════════════════════════════════════════════════════════════════════
# DB 集成用例（仅在有 WIKI_NAV_ACL_DB_URL 时运行；否则 db_url fixture skip）
# ════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_load_wiki_nav_page_returns_seeded_page(db_url):
    """授权主体：needles 命中合成页 key_terms → 返回该页 document_id。"""
    async with _real_db(db_url) as engine:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        out = await _nav(sessions, ["访问令牌"])
        assert out.document_id == _PAGE_IDS["access"][0], (
            f"应命中 access 页，实际 document_id={out.document_id}"
        )
        assert out.degraded_reason == ""
        assert "访问令牌" in out.key_terms


@pytest.mark.asyncio
async def test_acl_four_gate_excludes_unauthorized_pages(db_url):
    """ACL 四闸门经 EXISTS 生效：页级过滤器全过、仅 chunk 侧越权 → 页不返回。

    · access 页（chunk 全过四闸门）→ 命中；
    · audit 页（chunk acl_tags 与主体无交集，G4 失败）→ 不返回；
    · rbac  页（chunk kb 不在 authorized，G2 失败）→ 不返回。
    """
    async with _real_db(db_url) as engine:
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        hit = await _nav(sessions, ["访问令牌"])
        assert hit.document_id == _PAGE_IDS["access"][0], "基线：授权页必须命中"

        g4 = await _nav(sessions, ["审计日志"])
        assert g4.document_id is None and g4.degraded_reason == "no_match", (
            "G4（acl_tags 不匹配）应让页被 EXISTS 排除"
        )

        g2 = await _nav(sessions, ["权限模型"])
        assert g2.document_id is None and g2.degraded_reason == "no_match", (
            "G2（chunk kb 越权）应让页被 EXISTS 排除"
        )


# ════════════════════════════════════════════════════════════════════════
# 无需 DB 的用例（任何环境都跑，不依赖 WIKI_NAV_ACL_DB_URL）
# ════════════════════════════════════════════════════════════════════════
def test_select_nav_terms_for_seeded_page_and_query():
    """纯函数层：用合成页同款 key_terms（access 页）+ 构造 query 选词。

    不依赖真库；只证明选词去重 / 排序 / FTS 词元派生正确。期望写死
    （不调 to_fts_text 反算），钉住「选词逻辑」本身。
    """
    page_terms = ["访问控制列表", "登录"]   # access 页 key_terms 的同款形态
    terms, fts = select_nav_terms(page_terms, [], "", set())
    # 长度降序：5 字 > 2 字；均非停用词、不在 query 内 → 全保留
    assert terms == ["访问控制列表", "登录"]
    # 每个顶层词元的重叠 2-gram（与 keyword_text.to_fts_text 同口径，写死）
    assert fts == ["访问", "问控", "控制", "制列", "列表", "登录"]


# ── 选择性路由的「跳过」打点（复用 test_wiki_nav_off_state.py 替身骨架）─────
# 注意：这些用例**不**挂在 db_url fixture 上，故无 PG 也能跑。

M_TENANT = uuid.UUID(int=9)
M_KB = uuid.UUID(int=1)
M_DOC = uuid.UUID(int=2)
M_QUERY = "访问令牌怎么配置"
M_PROFILE = WikiProfile(generator_model="qwen-test", prompt_version="wprompt-v1")


class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def fetchall(self) -> list:
        return list(self._rows)


class _FakeSession:
    """按 SQL 特征分发；任何 wiki / 图 / set_config SQL 都记下来并断言失败。"""

    def __init__(self, rows: list) -> None:
        self._rows = rows
        self.wiki_sql_seen: list[str] = []

    async def execute(self, stmt, params=None):  # noqa: ANN001, ANN201
        sql = stmt.text if hasattr(stmt, "text") else str(stmt)
        self.wiki_sql_seen.append(sql)
        if "<=>" in sql:
            return _FakeResult(self._rows)
        for marker in ("~*", "ts_rank", "filename", "FROM kb_terms",
                       "table_group_id = :group_id", "v(document_id, heading_path)"):
            if marker in sql:
                return _FakeResult([])
        raise AssertionError(f"未识别的 SQL: {sql[:90]}")

    async def rollback(self) -> None:
        return None


class _FakeEmbedding:
    async def embed_query(self, query: str) -> list[float]:  # noqa: ARG002
        return [0.01] * 8


class _FakeRerank:
    async def rerank(self, query, documents, top_k):  # noqa: ARG001, ARG002
        return []


@pytest.fixture(autouse=False)
def _common_stubs(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """替身基础设施 + 捕获 metrics 打点（不污染进程级 prometheus 计数器）。"""
    monkeypatch.setattr(retrieve_mod, "get_embedding_service", lambda: _FakeEmbedding())
    monkeypatch.setattr(retrieve_mod, "get_rerank_service", lambda: _FakeRerank())
    from app.config import decisions as _decisions  # noqa: PLC0415
    monkeypatch.setattr(_decisions, "KEYWORD_TSQUERY_ENABLED", False)
    monkeypatch.setattr(
        "app.config.wiki_profile.current_wiki_profile", lambda: M_PROFILE
    )
    calls: list[tuple] = []

    def _incr(name: str, value: int = 1, **tags: str) -> None:
        calls.append(("incr", name, value, tags))

    def _observe(name: str, milliseconds: float, **tags: str) -> None:
        calls.append(("observe", name, milliseconds, tags))

    monkeypatch.setattr(retrieve_mod.metrics, "incr", _incr)
    monkeypatch.setattr(retrieve_mod.metrics, "observe_ms", _observe)
    return calls


def _settings(monkeypatch: pytest.MonkeyPatch, *,
              wiki_enabled: bool, wiki_nav_selective: bool = False) -> None:
    monkeypatch.setattr(
        retrieve_mod,
        "get_settings",
        lambda: SimpleNamespace(
            rerank_enabled=False,
            graph_enabled=False,
            graph_retrieve_timeout_ms=800,
            wiki_enabled=wiki_enabled,
            wiki_nav_selective=wiki_nav_selective,
            wiki_nav_timeout_ms=50,
        ),
    )


def _stub_nav(monkeypatch: pytest.MonkeyPatch, page) -> dict:
    """打桩查页函数（① 是函数内 import，monkeypatch 模块属性即生效）。"""
    calls: dict = {}

    async def _fake(session, **kwargs):  # noqa: ANN001, ARG001
        calls.update(kwargs)
        return page

    import app.services.wiki.navigate as nav_mod  # noqa: PLC0415
    monkeypatch.setattr(nav_mod, "load_wiki_nav_page", _fake)
    return calls


async def _retrieve() -> tuple:
    session = _FakeSession([_row(uuid.uuid4())])
    result = await RetrievalService().retrieve(
        session, M_QUERY, M_TENANT, [M_KB], 40, ["dept:it"]
    )
    return result, session


def _row(cid: uuid.UUID):  # noqa: ANN001
    mapping = {
        "id": cid,
        "document_id": M_DOC,
        "kb_id": M_KB,
        "content": "访问令牌的配置方法",
        "heading_path": "第一章",
        "page_no": 1,
        "level_rank": 20,
        "acl_tags": [],
        "table_group_id": None,
        "governance_weight": 0,
        "vector_score": 0.80,
    }
    row = SimpleNamespace(**mapping)
    row._mapping = mapping  # noqa: SLF001
    return row


@pytest.mark.asyncio
async def test_selective_skip_metric_increments_when_terms_ge_min(
    monkeypatch, _common_stubs
) -> None:
    """WIKI_NAV_SELECTIVE 开 + ilike_terms>=3 → 跳过查页且打点 skipped_by_router。"""
    _settings(monkeypatch, wiki_enabled=True, wiki_nav_selective=True)
    nav_calls = _stub_nav(monkeypatch, WikiNavPage(degraded_reason="no_match"))
    # 强制 ilike_terms 长度 >= WIKI_NAV_SELECTIVE_MIN_TERMS(3)
    monkeypatch.setattr(
        retrieve_mod, "build_ilike_terms",
        lambda q: ["访问令牌", "缓存机制", "认证配置"],
    )
    await _retrieve()

    assert nav_calls == {}, "selective 路由开启且词元充足时不应查页"
    skipped = [c for c in _common_stubs if c[1] == "rag.wiki.nav.skipped_by_router"]
    assert len(skipped) == 1, f"应恰好打点一次 skipped_by_router，实际 {len(skipped)} 次"


@pytest.mark.asyncio
async def test_selective_off_still_calls_nav(monkeypatch, _common_stubs) -> None:
    """WIKI_NAV_SELECTIVE 关 → 即便词元充足也照常查页，且不打 skipped_by_router。"""
    _settings(monkeypatch, wiki_enabled=True, wiki_nav_selective=False)
    nav_calls = _stub_nav(
        monkeypatch, WikiNavPage(document_id=M_DOC, key_terms=("审计日志",))
    )
    monkeypatch.setattr(
        retrieve_mod, "build_ilike_terms",
        lambda q: ["访问令牌", "缓存机制", "认证配置"],
    )
    await _retrieve()

    assert nav_calls != {}, "selective 关闭时即便词元充足也应查页"
    skipped = [c for c in _common_stubs if c[1] == "rag.wiki.nav.skipped_by_router"]
    assert skipped == [], "selective 关闭时不应打点 skipped_by_router"


@pytest.mark.asyncio
async def test_selective_on_but_few_terms_calls_nav(monkeypatch, _common_stubs) -> None:
    """WIKI_NAV_SELECTIVE 开但 ilike_terms<3 → 仍查页（路由不触发），不打点。"""
    _settings(monkeypatch, wiki_enabled=True, wiki_nav_selective=True)
    nav_calls = _stub_nav(
        monkeypatch, WikiNavPage(document_id=M_DOC, key_terms=("审计日志",))
    )
    monkeypatch.setattr(
        retrieve_mod, "build_ilike_terms", lambda q: ["访问令牌"]  # len 1 < 3
    )
    await _retrieve()

    assert nav_calls != {}, "selective 开启但词元不足时应仍查页"
    skipped = [c for c in _common_stubs if c[1] == "rag.wiki.nav.skipped_by_router"]
    assert skipped == [], "词元不足时不应打点 skipped_by_router"
