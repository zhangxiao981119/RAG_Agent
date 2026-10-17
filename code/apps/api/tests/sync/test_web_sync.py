"""WU-M-14 sync_service web 分支测试 —— 三级指纹增量 / 版本链 / 软失效。

边界全替身：web_fetcher 四个函数、MinIO、arq 池、SessionLocal 全部
monkeypatch，零网络零 DB。钉住八件事：
  1. 首轮建文档：version=1、source_kind=sync_web、ext=html、filename=URL
  2. ETag 未变跳过：不建版本、不入库、不入队
  3. 内容变化版本链：同 doc_group version+1，旧版本 is_latest 同事务置 false
  4. 页面消失软失效：只置 is_latest=false（INV-M-09 无 DELETE），gone 落指纹
  5. 消失重现复活：内容未变复活最新版，不重复建版本
  6. WebAuthError：last_error 持久化、无入库（DoD 中断语义）
  7. 抓取失败不判消失：保留既有状态待下轮（失败 ≠ 消失）
  8. 发现截断跳过软失效：max_pages 截断轮不误杀未发现页
"""
from __future__ import annotations

import uuid

import pytest

from app.models import Document, SyncSource
from app.services import sync_service
from app.services.sync.web_fetcher import FetchResult, WebAuthError, WebFetchError

BASE = "https://wiki.internal"
URL_A = f"{BASE}/docs/a.html"
URL_B = f"{BASE}/docs/b.html"


def _web_source(**overrides) -> SyncSource:
    """构造内存态 web 源（不落库，sync 流程只读写 synced_files 等属性）。"""
    defaults: dict = {
        "tenant_id": uuid.uuid4(),
        "kb_id": uuid.uuid4(),
        "name": "内网 wiki",
        "source_type": "web",
        "path": BASE,
        "file_patterns": "*.html",
        "level_rank": 20,
        "status": "active",
        "synced_files": {},
        "auth_type": "none",
        "auth_payload": None,
        "entry_urls": [f"{BASE}/docs/"],
        "scope_rules": {},
    }
    defaults.update(overrides)
    return SyncSource(**defaults)


def _page(url: str, body: str, etag: str | None = None) -> FetchResult:
    return FetchResult(url=url, content=body.encode("utf-8"), etag=etag)


class FakeClient:
    async def aclose(self) -> None:
        return None


class FakeStorage:
    def __init__(self, sink: dict) -> None:
        self._sink = sink

    async def put_object(self, key: str, data: bytes, content_type: str) -> None:
        self._sink["objects"][key] = (data, content_type)


class FakePool:
    def __init__(self, sink: dict) -> None:
        self._sink = sink

    async def enqueue_job(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        self._sink["enqueued"].append(args)


class FakeSession:
    """极简 session 替身：记录 execute 的 update 语句参数与 add 的对象。

    flush 补 id（模拟 ORM Python-side default）；merge 原样返回同一对象 ——
    生产代码对 merge 返回值的 synced_files 赋值直接反映到测试持有的 source。
    """

    def __init__(self, sink: dict) -> None:
        self._sink = sink

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *exc) -> None:  # noqa: ANN002
        return None

    def add(self, obj) -> None:  # noqa: ANN001
        self._sink["added"].append(obj)

    async def flush(self) -> None:
        for obj in self._sink["added"]:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    async def execute(self, stmt) -> None:  # noqa: ANN001
        # update 语句只记录编译参数供断言（where 命中值 + values 新值）
        self._sink["updates"].append(dict(stmt.compile().params))

    async def merge(self, obj):  # noqa: ANN001
        return obj

    async def commit(self) -> None:
        self._sink["commits"] += 1


class Harness:
    """一次（或多次）同步运行的全部替身与记录。"""

    def __init__(self) -> None:
        self.sink: dict = {"objects": {}, "enqueued": [], "added": [], "updates": [], "commits": 0}
        self.discover_result: list[str] = []
        self.fetch_map: dict[str, FetchResult | Exception] = {}
        self.probe_error: Exception | None = None

    def docs(self) -> list[Document]:
        return [o for o in self.sink["added"] if isinstance(o, Document)]


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Harness:
    h = Harness()

    async def _open_session(source):  # noqa: ANN001
        return FakeClient()

    async def _probe(client, source):  # noqa: ANN001
        if h.probe_error:
            raise h.probe_error

    async def _discover(client, source):  # noqa: ANN001
        return list(h.discover_result)

    async def _fetch(client, url, allowed):  # noqa: ANN001
        item = h.fetch_map[url]
        if isinstance(item, Exception):
            raise item
        return item

    async def _get_storage():
        return FakeStorage(h.sink)

    async def _get_pool():
        return FakePool(h.sink)

    monkeypatch.setattr(sync_service, "open_web_session", _open_session)
    monkeypatch.setattr(sync_service, "probe_source", _probe)
    monkeypatch.setattr(sync_service, "discover_pages", _discover)
    monkeypatch.setattr(sync_service, "fetch_page", _fetch)
    monkeypatch.setattr(sync_service, "get_storage", _get_storage)
    monkeypatch.setattr(sync_service, "get_arq_pool", _get_pool)
    monkeypatch.setattr(sync_service, "SessionLocal", lambda: FakeSession(h.sink))
    return h


# ── 三级指纹纯逻辑 ────────────────────────────────────────────────


def test_fingerprint_any_level_match_means_unchanged():
    entry = {"etag": '"e"', "last_modified": "old", "content_hash": "h"}

    # 一级命中：ETag 相同
    r = FetchResult(url=URL_A, content=b"x", etag='"e"')
    assert sync_service._fingerprint_unchanged(entry, r, "h")
    # ETag 失配不立即判变更 → content_hash 兜底命中
    r2 = FetchResult(url=URL_A, content=b"x", etag='"other"')
    assert sync_service._fingerprint_unchanged(entry, r2, "h")
    # 全部失配 → 变更
    r3 = FetchResult(url=URL_A, content=b"y", etag='"other"')
    assert not sync_service._fingerprint_unchanged(entry, r3, "h2")
    # 无历史指纹 → 变更
    assert not sync_service._fingerprint_unchanged({}, r, "h")


# ── 主流程 ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_sync_indexes_pages_as_v1(harness: Harness):
    source = _web_source()
    harness.discover_result = [URL_A, URL_B]
    harness.fetch_map = {
        URL_A: _page(URL_A, "<p>A</p>", '"e-a"'),
        URL_B: _page(URL_B, "<p>B</p>", '"e-b"'),
    }

    count = await sync_service.sync_source(source)

    assert count == 2
    docs = harness.docs()
    assert len(docs) == 2
    for doc in docs:
        assert doc.version == 1 and doc.is_latest is True
        assert doc.ext == "html" and doc.source_kind == "sync_web"
        assert doc.filename.startswith(BASE)  # filename=URL
        assert doc.content_hash  # sha256 已落列
        assert doc.storage_key in harness.sink["objects"]
        stored_data, content_type = harness.sink["objects"][doc.storage_key]
        assert content_type == "text/html" and stored_data in (b"<p>A</p>", b"<p>B</p>")
    assert len(harness.sink["enqueued"]) == 2  # 每页一个 ParseJob 入队
    assert set(source.synced_files) == {URL_A, URL_B}
    entry = source.synced_files[URL_A]
    assert entry["version"] == 1 and entry["etag"] == '"e-a"' and entry["gone"] is False


@pytest.mark.asyncio
async def test_unchanged_etag_skips_reindex(harness: Harness):
    source = _web_source()
    harness.discover_result = [URL_A]
    harness.fetch_map = {URL_A: _page(URL_A, "<p>A</p>", '"e-a"')}
    await sync_service.sync_source(source)
    docs_after_first = len(harness.docs())
    enq_after_first = len(harness.sink["enqueued"])

    count = await sync_service.sync_source(source)  # 同 ETag 再同步

    assert count == 0
    assert len(harness.docs()) == docs_after_first
    assert len(harness.sink["enqueued"]) == enq_after_first


@pytest.mark.asyncio
async def test_content_change_builds_version_chain(harness: Harness):
    source = _web_source()
    harness.discover_result = [URL_A]
    harness.fetch_map = {URL_A: _page(URL_A, "<p>v1</p>", '"e1"')}
    await sync_service.sync_source(source)
    old_doc = harness.docs()[0]
    old_gid = old_doc.doc_group_id

    harness.fetch_map = {URL_A: _page(URL_A, "<p>v2</p>", '"e2"')}
    count = await sync_service.sync_source(source)

    assert count == 1
    docs = harness.docs()
    assert len(docs) == 2
    new_doc = docs[-1]
    assert new_doc.doc_group_id == old_gid and new_doc.version == 2
    # 旧版本软失效的 update 已落库（同事务）：doc_group 命中 + is_latest=False
    flips = [p for p in harness.sink["updates"] if old_gid in p.values() and p.get("is_latest") is False]
    assert flips, "旧版本必须被置 is_latest=false"
    # 新版本存储键带 v2
    assert any("/v2/" in k for k in harness.sink["objects"])


@pytest.mark.asyncio
async def test_disappeared_page_soft_invalidated(harness: Harness):
    source = _web_source()
    harness.discover_result = [URL_A]
    harness.fetch_map = {URL_A: _page(URL_A, "<p>A</p>", '"e-a"')}
    await sync_service.sync_source(source)
    doc = harness.docs()[0]

    harness.discover_result = []  # 页面从站点消失
    count = await sync_service.sync_source(source)

    assert count == 0
    # 无 DELETE —— 只有 update 软失效（INV-M-09）
    retire = [p for p in harness.sink["updates"] if doc.doc_group_id in p.values() and p.get("is_latest") is False]
    assert retire
    assert source.synced_files[URL_A]["gone"] is True


@pytest.mark.asyncio
async def test_reappeared_page_revives_without_new_version(harness: Harness):
    source = _web_source()
    harness.discover_result = [URL_A]
    harness.fetch_map = {URL_A: _page(URL_A, "<p>A</p>", '"e-a"')}
    await sync_service.sync_source(source)
    doc = harness.docs()[0]

    harness.discover_result = []
    await sync_service.sync_source(source)

    harness.discover_result = [URL_A]  # 页面重现，内容未变
    count = await sync_service.sync_source(source)

    assert count == 0  # 复活不建新版本
    revive = [p for p in harness.sink["updates"] if doc.id in p.values() and p.get("is_latest") is True]
    assert revive, "重现页必须复活最新版"
    assert source.synced_files[URL_A]["gone"] is False
    assert len(harness.docs()) == 1


@pytest.mark.asyncio
async def test_web_auth_error_records_last_error(harness: Harness):
    source = _web_source()
    harness.probe_error = WebAuthError("入口页鉴权失败（HTTP 401）")

    count = await sync_service.sync_source(source)

    assert count == 0
    assert "WebAuthError" in (source.last_error or "")
    assert harness.sink["added"] == []  # 无任何入库


@pytest.mark.asyncio
async def test_fetch_failure_not_treated_as_gone(harness: Harness):
    source = _web_source()
    harness.discover_result = [URL_A]
    harness.fetch_map = {URL_A: _page(URL_A, "<p>A</p>", '"e-a"')}
    await sync_service.sync_source(source)
    updates_before = len(harness.sink["updates"])

    harness.fetch_map = {URL_A: WebFetchError("页面抓取失败（HTTP 500）")}
    count = await sync_service.sync_source(source)

    assert count == 0
    assert len(harness.sink["updates"]) == updates_before  # 无任何翻转（不误判消失）
    assert source.synced_files[URL_A]["gone"] is False


@pytest.mark.asyncio
async def test_truncated_discovery_skips_retirement(harness: Harness, monkeypatch: pytest.MonkeyPatch):
    source = _web_source()
    harness.discover_result = [URL_A, URL_B]
    harness.fetch_map = {
        URL_A: _page(URL_A, "<p>A</p>", '"e-a"'),
        URL_B: _page(URL_B, "<p>B</p>", '"e-b"'),
    }
    await sync_service.sync_source(source)

    # 截断轮：max_pages=1，只有 A 被发现，B 其实还在站上
    settings = sync_service.get_settings()
    monkeypatch.setattr(settings, "web_max_pages_per_sync", 1)
    harness.discover_result = [URL_A]
    await sync_service.sync_source(source)

    assert source.synced_files[URL_B]["gone"] is False  # 截断轮不判消失
