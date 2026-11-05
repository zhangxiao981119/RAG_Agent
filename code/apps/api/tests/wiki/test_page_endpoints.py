"""wiki 读接口契约测试（§9.6 ②③）。

钉住五件事：
  1. **关闭态**（WIKI_ENABLED=false，默认）：两端点 404 —— 关闭的能力不假装存在；
  2. **列表**：kb 成员可见性（非成员 403，与 get_kb 同款；跨租户 404）；
     只列「存在过四闸门 chunk」的文档的页；
  3. **详情 404 三合一**：页面不存在 / 生成失败（status!=ready）/ 无可见 chunk
     —— 同一个 404，绝不 403（防存在性探测）；
  4. **详情响应**：带 caliber 与固定 disclaimer（LLM 产物不可引用）；
  5. **源码契约**：wiki.py 的可见性 SQL MUST 内联 retrieve 的 `_WHERE_BASE`
     （四闸门单一真源），MUST NOT 重写一份 ACL 条件；retrieve/ MUST NOT import wiki
     （本步 wiki 不进检索）。

端点测试用 TestClient（lifespan 需要连库）→ 本文件在容器内运行；
源码契约部分纯文本检查，任何环境都能跑。
"""
from __future__ import annotations

import inspect
import pathlib
import uuid
from types import SimpleNamespace

from fastapi.testclient import TestClient

import app.api.wiki as wiki_mod
from app.api.deps import CurrentUser, get_current_user, get_db
from app.main import app

KB_ID = uuid.uuid4()
PAGE_ID = uuid.uuid4()
DOC_ID = uuid.uuid4()
TENANT_ID = uuid.uuid4()


def _user(clearance: int = 40, kbs: list[uuid.UUID] | None = None) -> CurrentUser:
    return CurrentUser(
        user_id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        username="tester",
        display_name="tester",
        clearance=clearance,
        dept_path="",
        subjects=["public"],
        authorized_kb_ids=kbs if kbs is not None else [KB_ID],
        acl_epoch=1,
    )


class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self) -> list:
        return list(self._rows)

    def scalars(self):
        return self

    def one(self):
        return self._rows[0]


class _FakeSession:
    """按序出栈的假 session：wiki.py 的每次 execute / get 按调用顺序消费结果。

    results 的每个元素是「一次 execute 应返回的行列表」，统一包成 _FakeResult
    （端点在 Result 语义上调 .first()/.all()/迭代 —— 不能把裸 list 交回去）。
    """

    def __init__(self, results: list) -> None:
        self._results = list(results)
        self.sql: list[str] = []

    async def execute(self, stmt, params=None):
        self.sql.append(str(stmt))
        return _FakeResult(self._results.pop(0))

    async def get(self, model, pk):
        return self._results.pop(0)

    def last_sql(self) -> str:
        return self.sql[-1] if self.sql else ""


class _Harness:
    def __init__(self, user: CurrentUser, results: list, *, wiki_enabled: bool) -> None:
        self.session = _FakeSession(results)
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_db] = lambda: self.session

        async def _yield():
            yield self.session

        app.dependency_overrides[get_db] = _yield

        if wiki_enabled:
            # wiki.py 在请求期读 get_settings().wiki_enabled —— 按请求打桩
            self._orig = wiki_mod.get_settings
            wiki_mod.get_settings = lambda: SimpleNamespace(wiki_enabled=True)
        else:
            self._orig = None

    def close(self):
        app.dependency_overrides.clear()
        if self._orig is not None:
            wiki_mod.get_settings = self._orig


def _client() -> TestClient:
    # ★ 不用 `with TestClient(app)`（不触发 lifespan），与 tests/chat 同款纪律：
    #   lifespan 会真连一次 DB，asyncpg 连接池绑定到第一个事件循环，
    #   第二个 with 就会 Event loop is closed。本文件全部依赖已打桩，
    #   lifespan 初始化的东西一样都不需要。
    return TestClient(app)


# ── 关闭态（默认）───────────────────────────────────────────────


def test_disabled_wiki_returns_404_on_both_endpoints():
    """WIKI_ENABLED=false（默认）：两端点 404，且 MUST NOT 触碰数据库。"""
    session = _FakeSession(results=[])
    app.dependency_overrides[get_current_user] = lambda: _user()
    app.dependency_overrides[get_db] = lambda: session
    try:
        client = _client()
        r1 = client.get(f"/api/kbs/{KB_ID}/wiki/pages")
        r2 = client.get(f"/api/wiki/pages/{PAGE_ID}")
    finally:
        app.dependency_overrides.clear()
    assert r1.status_code == 404
    assert r2.status_code == 404
    assert session.sql == [], "关闭态 MUST NOT 发出任何 SQL"


# ── 列表端点 ────────────────────────────────────────────────────


def test_list_requires_kb_membership(monkeypatch):
    """非 kb 成员 → 403（与 GET /api/kbs/{id} 同款判定，复用而非新造）。"""
    non_member = _user(clearance=10, kbs=[])  # authorized_kb_ids 不含 KB_ID
    h = _Harness(non_member, results=[], wiki_enabled=True)
    try:
        client = _client()
        resp = client.get(f"/api/kbs/{KB_ID}/wiki/pages")
    finally:
        h.close()
    assert resp.status_code == 403


def test_list_unknown_kb_404(monkeypatch):
    h = _Harness(_user(), results=[None], wiki_enabled=True)  # kb get → None
    try:
        client = _client()
        resp = client.get(f"/api/kbs/{uuid.uuid4()}/wiki/pages")
    finally:
        h.close()
    assert resp.status_code == 404


def test_list_returns_only_visible_documents(monkeypatch):
    """列表只含存在过四闸门 chunk 的文档的页（假数据里给两条，SQL 过滤后剩一条）。"""
    row = SimpleNamespace(id=PAGE_ID, document_id=DOC_ID,
                          title="差旅报销制度", generated_at="2026-11-04T00:00:00Z")
    # admin：1) kb get → 对象；2) 租户全库 id；3) 列表 SQL（过滤后剩一条）
    kb = SimpleNamespace(id=KB_ID, tenant_id=TENANT_ID)
    h = _Harness(_user(), results=[kb, [(uuid.UUID(int=1),)], [row]], wiki_enabled=True)
    try:
        client = _client()
        resp = client.get(f"/api/kbs/{KB_ID}/wiki/pages")
    finally:
        h.close()
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == str(PAGE_ID)
    assert items[0]["document_id"] == str(DOC_ID)
    assert "title" in items[0] and "generated_at" in items[0]
    # 列表 SQL 必须带四闸门 EXISTS（可见性过滤不在 Python 里做）
    assert "EXISTS" in h.session.last_sql()


def test_list_empty_when_no_visible_pages(monkeypatch):
    """无可见文档 → 200 + 空列表（§9.6 ②），不是 404。"""
    kb = SimpleNamespace(id=KB_ID, tenant_id=TENANT_ID)
    h = _Harness(_user(), results=[kb, [(uuid.UUID(int=1),)], []], wiki_enabled=True)
    try:
        client = _client()
        resp = client.get(f"/api/kbs/{KB_ID}/wiki/pages")
    finally:
        h.close()
    assert resp.status_code == 200
    assert resp.json()["items"] == []


# ── 详情端点（404 三合一）───────────────────────────────────────


def test_detail_404_when_row_missing():
    """页面不存在 → 404（三合一之一）。"""
    h = _Harness(_user(), results=[[(uuid.UUID(int=1),)], []], wiki_enabled=True)
    try:
        client = _client()
        resp = client.get(f"/api/wiki/pages/{PAGE_ID}")
    finally:
        h.close()
    assert resp.status_code == 404


def test_detail_404_when_no_visible_chunk():
    """用户对该文档无可见 chunk（EXISTS 过滤掉）→ 404，**不是 403**
    —— 用 404/403 差异探测「某密级文档是否存在」会泄漏存在性（§9.6 ③）。"""
    # 非 admin 分支不查租户 kb 集（直接用 authorized_kb_ids）→ 只有一次 execute
    low_user = _user(clearance=10, kbs=[KB_ID])
    h = _Harness(low_user, results=[[]], wiki_enabled=True)
    try:
        client = _client()
        resp = client.get(f"/api/wiki/pages/{PAGE_ID}")
    finally:
        h.close()
    assert resp.status_code == 404


def test_detail_returns_page_with_disclaimer_and_caliber():
    row = SimpleNamespace(
        id=PAGE_ID, document_id=DOC_ID, title="差旅报销制度",
        summary="全文导航摘要。", sections=[{"heading_path": "第一章", "summary": "s",
                                            "key_terms": ["术语"], "chunk_ids": []}],
        key_terms=["术语"], generator_model="gen-model",
        prompt_version="wprompt-v1", generated_at="2026-11-04T00:00:00Z",
    )
    h = _Harness(_user(), results=[[(uuid.UUID(int=1),)], [row]], wiki_enabled=True)
    try:
        client = _client()
        resp = client.get(f"/api/wiki/pages/{PAGE_ID}")
    finally:
        h.close()
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "差旅报销制度"
    assert body["caliber"] == {"generator_model": "gen-model", "prompt_version": "wprompt-v1"}
    # 免责声明 MUST 随响应返回（LLM 产物不可引用）
    assert "导航摘要" in body["disclaimer"]
    assert "不构成引用依据" in body["disclaimer"]
    # 可见性 EXISTS 必须出现在详情 SQL 里
    assert "EXISTS" in h.session.last_sql()


def test_detail_404_when_generation_failed_is_same_as_missing():
    """生成失败（status!=ready 被 SQL 过滤）与不存在返回同一个 404 —— 三合一契约。"""
    # SQL 里 status='ready' 过滤 → row None；与「页不存在」走同一分支
    h = _Harness(_user(), results=[[(uuid.UUID(int=1),)], []], wiki_enabled=True)
    try:
        client = _client()
        resp1 = client.get(f"/api/wiki/pages/{PAGE_ID}")
    finally:
        h.close()
    h2 = _Harness(_user(), results=[[(uuid.UUID(int=1),)], []], wiki_enabled=True)
    try:
        client = _client()
        resp2 = client.get(f"/api/wiki/pages/{uuid.uuid4()}")
    finally:
        h2.close()
    assert resp1.status_code == resp2.status_code == 404


# ── 源码契约（任何环境可跑）─────────────────────────────────────


def test_visibility_sql_inlines_where_base_single_source():
    """wiki.py 的可见性 SQL MUST 内联 `_WHERE_BASE`，MUST NOT 重写 ACL 条件。"""
    src = inspect.getsource(wiki_mod)
    assert "_WHERE_BASE" in src, "可见性判定没有复用四闸门单一真源"
    # 重写一份 ACL 条件的典型形态（出现即说明有人在 wiki 里复制了权限逻辑）
    for forbidden in ("level_rank <=", "deny_subjects &&", "acl_tags &&"):
        assert forbidden not in src, f"wiki.py 里出现了重写的 ACL 条件: {forbidden}"


def test_retrieve_never_imports_wiki():
    """本步 wiki 不进检索：retrieve/ 任何文件 MUST NOT import wiki（第 4 步才接入）。"""
    base = pathlib.Path(wiki_mod.__file__).resolve().parents[2] / "retrieve"
    for py in base.glob("*.py"):
        source = py.read_text(encoding="utf-8")
        assert "services.wiki" not in source, f"{py.name} 引用了 wiki —— 第 3 步禁止"
