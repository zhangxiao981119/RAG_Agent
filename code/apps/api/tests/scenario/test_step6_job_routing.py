"""Step 6 D-S6-3-1 / D-S6-3-2：写路径 db_key 贯通与按 ID 寻址库位推导。

覆盖（不连真库，全部猴补丁）：
  1. `open_job_session`：db_key 原样透传给 `data_session`；缺失/空串/非字符串
     → `JOB_DB_KEY_MISSING`（禁止回落默认库）；
  2. 6 个 worker 入口缺 db_key → `JOB_DB_KEY_MISSING`；
  3. 链式入队负载透传 db_key（parse 段链 / graph / wiki）；
  4. 按 ID 寻址的库位推导（控制面副本优先，缺失时探测场景库）：关闭态零 SQL；
     控制库有副本 → 按 kb 推导且不探测；控制库无副本 → 0/1/≥2 命中
     （≥2 → `DB_SCOPE_AMBIGUOUS`）；wiki 页覆盖「页只在场景库」与
     「页在控制库、归属文档缺失」两条探测路径；
  5. 按 ID 寻址端点：数据面会话按推导键打开、404、结构化 500。
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import documents as documents_api
from app.api.deps import CurrentUser, get_current_user, get_db
from app.config import decisions
from app.models import Document, WikiPage
from app.services import db_scope
from app.services import governance as governance_worker
from app.services.db_scope import DbScopeError
from app.workers import graph_job, parse_job
from tests.fake_settings import fake_settings

_TENANT_ID = uuid.uuid4()


# ══════════════════════════════════════════════════════════════════════
# 1. open_job_session
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_open_job_session_forwards_db_key(monkeypatch):
    seen: list[str] = []

    @asynccontextmanager
    async def _fake_data_session(db_key: str):
        seen.append(db_key)
        yield object()

    monkeypatch.setattr(db_scope, "data_session", _fake_data_session)

    async with db_scope.open_job_session("kb_finance") as session:
        assert session is not None

    assert seen == ["kb_finance"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_key", [None, "", 123])
async def test_open_job_session_rejects_missing_key(monkeypatch, bad_key):
    opened: list[str] = []

    @asynccontextmanager
    async def _fake_data_session(db_key: str):
        opened.append(db_key)
        yield object()

    monkeypatch.setattr(db_scope, "data_session", _fake_data_session)

    with pytest.raises(DbScopeError) as excinfo:
        async with db_scope.open_job_session(bad_key):
            pass

    assert excinfo.value.code == "JOB_DB_KEY_MISSING"
    assert opened == []


# ══════════════════════════════════════════════════════════════════════
# 2. 6 个 worker 入口缺 db_key
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entry, missing_args",
    [
        (parse_job.run_parse_segment, (str(uuid.uuid4()),)),
        (parse_job.run_parse_finalize, (str(uuid.uuid4()),)),
        (graph_job.run_graph_extract, (str(uuid.uuid4()),)),
        (graph_job.run_wiki_build, (str(uuid.uuid4()),)),
        (governance_worker.execute_governance_job, (str(uuid.uuid4()),)),
        (parse_job.run_sync_source, (str(uuid.uuid4()),)),
    ],
)
async def test_worker_entry_requires_db_key(monkeypatch, entry, missing_args):
    # graph / wiki 有「关闭能力即直接返回」的前置闸门，必须开启能力才能走到校验
    monkeypatch.setattr(
        graph_job, "get_settings", lambda: fake_settings(graph_enabled=True, wiki_enabled=True)
    )
    monkeypatch.setattr(
        parse_job, "get_settings", lambda: fake_settings(graph_enabled=True, wiki_enabled=True)
    )
    monkeypatch.setattr(
        governance_worker,
        "get_settings",
        lambda: fake_settings(graph_enabled=True, wiki_enabled=True),
    )

    with pytest.raises(DbScopeError) as excinfo:
        await entry({}, *missing_args)

    assert excinfo.value.code == "JOB_DB_KEY_MISSING"


@pytest.mark.asyncio
async def test_parse_worker_forwards_key_to_job_session(monkeypatch):
    seen: list[str] = []

    class _Session:
        async def get(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return None

    @asynccontextmanager
    async def _open(db_key: str):
        seen.append(db_key)
        yield _Session()

    monkeypatch.setattr(parse_job, "open_job_session", _open)

    result = await parse_job.run_parse_segment({}, str(uuid.uuid4()), 0, "trace", "kb_a")

    assert result == "dead"
    assert seen == ["kb_a"]


def test_worker_startup_registers_db_router():
    """worker 是独立进程：不自己注册库位，队列里的 db_key 在 worker 侧取不到绑定。"""
    import inspect

    from app.workers.parse_job import WorkerSettings

    source = inspect.getsource(WorkerSettings.on_startup)
    assert "register_from_settings()" in source
    assert "check_connection_budget()" in source


# ══════════════════════════════════════════════════════════════════════
# 3. 链式入队透传 db_key
# ══════════════════════════════════════════════════════════════════════


class _FakePool:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    async def enqueue_job(self, *args, **kwargs):  # noqa: ANN002, ANN003
        self.calls.append((args, kwargs))


@pytest.mark.asyncio
async def test_parse_segment_chain_enqueues_next_with_db_key(monkeypatch):
    pool = _FakePool()

    async def _get_pool():
        return pool

    monkeypatch.setattr(parse_job, "get_arq_pool", _get_pool)
    monkeypatch.setattr(parse_job, "get_settings", lambda: fake_settings())

    job_id = uuid.uuid4()
    span = decisions.OCR_MAX_SCAN_PAGES
    await parse_job._advance_segment_chain(job_id, 0, span * 3, "trace", "kb_finance")

    args, _ = pool.calls[0]
    assert args == ("run_parse_segment", str(job_id), 1, "trace", "kb_finance")


@pytest.mark.asyncio
async def test_parse_segment_chain_enqueues_finalize_with_db_key(monkeypatch):
    pool = _FakePool()

    async def _get_pool():
        return pool

    monkeypatch.setattr(parse_job, "get_arq_pool", _get_pool)
    monkeypatch.setattr(parse_job, "get_settings", lambda: fake_settings())

    job_id = uuid.uuid4()
    span = decisions.OCR_MAX_SCAN_PAGES
    await parse_job._advance_segment_chain(job_id, 1, span * 2, "trace", "kb_finance")

    args, _ = pool.calls[0]
    assert args == ("run_parse_finalize", str(job_id), "trace", "kb_finance")


@pytest.mark.asyncio
async def test_graph_and_wiki_enqueue_carry_db_key(monkeypatch):
    pool = _FakePool()

    async def _get_pool():
        return pool

    monkeypatch.setattr(graph_job, "get_arq_pool", _get_pool)
    monkeypatch.setattr(
        graph_job, "get_settings", lambda: fake_settings(graph_enabled=True, wiki_enabled=True)
    )

    assert await graph_job.enqueue_graph_extract("doc-1", "kb_finance", "trace") is True
    assert await graph_job.enqueue_wiki_build("doc-1", "kb_finance", "trace") is True

    graph_args, _ = pool.calls[0]
    wiki_args, _ = pool.calls[1]
    assert graph_args == ("run_graph_extract", "doc-1", "trace", "kb_finance")
    assert wiki_args == ("run_wiki_build", "doc-1", "trace", "kb_finance")


# ══════════════════════════════════════════════════════════════════════
# 4. 按 ID 寻址的库位推导（控制面副本，D-S6-3-2 修订）
# ══════════════════════════════════════════════════════════════════════


class _Rows:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return list(self._rows)


class _ControlSession:
    """控制面会话替身：get 按 (model, pk) 返对象；execute 按脚本出栈。"""

    def __init__(self, objects: dict, rows: list | None = None) -> None:
        self._objects = dict(objects)
        self._rows = list(rows or [])
        self.calls: list[tuple] = []

    async def get(self, model, pk):  # noqa: ANN001
        self.calls.append(("get", model, pk))
        return self._objects.get((model, pk))

    async def execute(self, stmt, params=None):  # noqa: ANN001
        self.calls.append(("execute", str(stmt), dict(params or {})))
        return _Rows(self._rows.pop(0))


class _NoSqlSession:
    """关闭态一旦发 SQL 即失败。"""

    async def get(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("关闭态不得执行任何 SQL")

    async def execute(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("关闭态不得执行任何 SQL")


def _document_model(kb_id: uuid.UUID) -> Document:
    return Document(
        id=uuid.uuid4(),
        tenant_id=_TENANT_ID,
        kb_id=kb_id,
        filename="制度.pdf",
        ext="pdf",
        size_bytes=10,
        status="indexed",
        version=1,
        level_rank=20,
        is_latest=True,
    )


def _wiki_page(document_id: uuid.UUID) -> WikiPage:
    return WikiPage(id=uuid.uuid4(), document_id=document_id)


@pytest.mark.asyncio
async def test_resolve_document_closed_state_returns_default_without_sql(monkeypatch):
    monkeypatch.setattr(
        db_scope, "get_settings", lambda: fake_settings(scene_db_split_enabled=False)
    )
    assert await db_scope.resolve_document_db_key(_NoSqlSession(), uuid.uuid4()) == "default"


@pytest.mark.asyncio
async def test_resolve_wiki_page_closed_state_returns_default_without_sql(monkeypatch):
    monkeypatch.setattr(
        db_scope, "get_settings", lambda: fake_settings(scene_db_split_enabled=False)
    )
    assert await db_scope.resolve_wiki_page_db_key(_NoSqlSession(), uuid.uuid4()) == "default"


class _ProbeSession:
    """场景库会话替身：命中与否由安装器按库位键指定。"""

    def __init__(self, hit: bool) -> None:
        self.hit = hit

    async def get(self, model, pk):  # noqa: ANN001
        return object() if self.hit else None


def _install_scene_probe(
    monkeypatch, hits: dict[str, bool]
) -> list[str]:
    """安装场景库探测替身（registered_db_keys + data_session），返回探测顺序。"""
    probed: list[str] = []

    @asynccontextmanager
    async def _fake_data_session(db_key: str):
        probed.append(db_key)
        yield _ProbeSession(hit=hits.get(db_key, False))

    monkeypatch.setattr(
        db_scope, "registered_db_keys", lambda: ("default", *tuple(hits))
    )
    monkeypatch.setattr(db_scope, "data_session", _fake_data_session)
    return probed


def _enable(monkeypatch) -> None:
    monkeypatch.setattr(
        db_scope, "get_settings", lambda: fake_settings(scene_db_split_enabled=True)
    )


@pytest.mark.asyncio
async def test_resolve_document_control_miss_probes_scene_and_returns_key(monkeypatch):
    """控制库无副本、场景库唯一命中 → 返回该场景键（新文档场景）。"""
    _enable(monkeypatch)
    doc_id = uuid.uuid4()
    session = _ControlSession({})
    probed = _install_scene_probe(monkeypatch, {"kb_a": False, "kb_b": True})

    assert await db_scope.resolve_document_db_key(session, doc_id) == "kb_b"
    assert session.calls == [("get", Document, doc_id)]
    assert probed == ["kb_a", "kb_b"]


@pytest.mark.asyncio
async def test_resolve_document_control_miss_zero_hit_returns_none(monkeypatch):
    """控制库无副本、探测场景库也 0 命中 → None（调用方 404）。"""
    _enable(monkeypatch)
    session = _ControlSession({})
    probed = _install_scene_probe(monkeypatch, {"kb_a": False, "kb_b": False})

    assert await db_scope.resolve_document_db_key(session, uuid.uuid4()) is None
    assert probed == ["kb_a", "kb_b"]


@pytest.mark.asyncio
async def test_resolve_document_control_miss_two_scene_hits_is_ambiguous(monkeypatch):
    """两个场景库同 id → 歧义，不替调用方挑一个。"""
    _enable(monkeypatch)
    doc_id = uuid.uuid4()
    session = _ControlSession({})
    _install_scene_probe(monkeypatch, {"kb_a": True, "kb_b": True})

    with pytest.raises(DbScopeError) as excinfo:
        await db_scope.resolve_document_db_key(session, doc_id)

    assert excinfo.value.code == "DB_SCOPE_AMBIGUOUS"
    assert excinfo.value.detail == {
        "model": "Document",
        "pk": str(doc_id),
        "db_keys": ["kb_a", "kb_b"],
    }


@pytest.mark.asyncio
async def test_resolve_document_control_copy_wins_without_probing(monkeypatch):
    """控制库有副本（场景库同 id 也有）→ 走控制派生，返回 kb 的库位且不探测。"""
    _enable(monkeypatch)
    kb_id = uuid.uuid4()
    doc = _document_model(kb_id)
    session = _ControlSession(
        {(Document, doc.id): doc}, [[(str(kb_id), "kb_probe")]]
    )
    probed = _install_scene_probe(monkeypatch, {"kb_probe": True})

    assert await db_scope.resolve_document_db_key(session, doc.id) == "kb_probe"
    assert probed == []


@pytest.mark.asyncio
async def test_resolve_document_control_unbound_kb_returns_default(monkeypatch):
    _enable(monkeypatch)
    doc = _document_model(uuid.uuid4())
    session = _ControlSession({(Document, doc.id): doc}, [[]])
    probed = _install_scene_probe(monkeypatch, {"kb_a": True})

    assert await db_scope.resolve_document_db_key(session, doc.id) == "default"
    assert probed == []


@pytest.mark.asyncio
async def test_resolve_wiki_page_control_miss_probes_scene(monkeypatch):
    """页只在场景库 → 按 page_id 探测场景库，返回该键。"""
    _enable(monkeypatch)
    page_id = uuid.uuid4()
    session = _ControlSession({})
    probed = _install_scene_probe(monkeypatch, {"kb_a": True})
    assert await db_scope.resolve_wiki_page_db_key(session, page_id) == "kb_a"
    assert session.calls == [("get", WikiPage, page_id)]
    assert probed == ["kb_a"]


@pytest.mark.asyncio
async def test_resolve_wiki_page_control_miss_zero_hit_returns_none(monkeypatch):
    _enable(monkeypatch)
    session = _ControlSession({})
    probed = _install_scene_probe(monkeypatch, {"kb_a": False})
    assert await db_scope.resolve_wiki_page_db_key(session, uuid.uuid4()) is None
    assert probed == ["kb_a"]


@pytest.mark.asyncio
async def test_resolve_wiki_page_control_miss_two_hits_is_ambiguous(monkeypatch):
    _enable(monkeypatch)
    page_id = uuid.uuid4()
    session = _ControlSession({})
    _install_scene_probe(monkeypatch, {"kb_a": True, "kb_b": True})

    with pytest.raises(DbScopeError) as excinfo:
        await db_scope.resolve_wiki_page_db_key(session, page_id)

    assert excinfo.value.code == "DB_SCOPE_AMBIGUOUS"
    assert excinfo.value.detail["model"] == "WikiPage"
    assert excinfo.value.detail["pk"] == str(page_id)


@pytest.mark.asyncio
async def test_resolve_wiki_page_control_copy_derives_from_owner_document(monkeypatch):
    """控制库有页副本与归属文档 → 按文档 kb 解析，不探测。"""
    _enable(monkeypatch)
    kb_id = uuid.uuid4()
    doc = _document_model(kb_id)
    page = _wiki_page(doc.id)
    session = _ControlSession(
        {(WikiPage, page.id): page, (Document, doc.id): doc},
        [[(str(kb_id), "kb_probe")]],
    )
    probed = _install_scene_probe(monkeypatch, {"kb_probe": True})

    assert await db_scope.resolve_wiki_page_db_key(session, page.id) == "kb_probe"
    assert probed == []


@pytest.mark.asyncio
async def test_resolve_wiki_page_owner_missing_probes_scene_by_document(monkeypatch):
    """页副本在控制库但归属文档副本缺失 → 按 document_id 探测场景库。"""
    _enable(monkeypatch)
    page = _wiki_page(uuid.uuid4())
    session = _ControlSession({(WikiPage, page.id): page})
    probed = _install_scene_probe(monkeypatch, {"kb_a": True})

    assert await db_scope.resolve_wiki_page_db_key(session, page.id) == "kb_a"
    assert session.calls == [
        ("get", WikiPage, page.id),
        ("get", Document, page.document_id),
    ]
    assert probed == ["kb_a"]


# ══════════════════════════════════════════════════════════════════════
# 5. 按 ID 寻址端点（documents）
# ══════════════════════════════════════════════════════════════════════


def _user() -> CurrentUser:
    return CurrentUser(
        user_id=uuid.uuid4(),
        tenant_id=_TENANT_ID,
        username="tester",
        display_name="tester",
        clearance=40,
        dept_path="研发中心",
        subjects=[],
        authorized_kb_ids=[],
        acl_epoch=1,
    )


class _Result:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self):
        return self

    def all(self) -> list:
        return list(self._rows)


class _FakeDocSession:
    def __init__(self, doc: Document) -> None:
        self._doc = doc
        self.executed = 0

    async def get(self, model, pk):  # noqa: ANN001
        return self._doc if model is Document else None

    async def execute(self, stmt):  # noqa: ANN001
        self.executed += 1
        return _Result([])


def _doc(doc_id: uuid.UUID) -> Document:
    return Document(
        id=doc_id,
        tenant_id=_TENANT_ID,
        kb_id=uuid.uuid4(),
        filename="制度.pdf",
        ext="pdf",
        size_bytes=10,
        status="indexed",
        version=1,
        level_rank=20,
        is_latest=True,
    )


def _client(data_session: _FakeDocSession) -> TestClient:
    test_app = FastAPI()
    test_app.include_router(documents_api.router, prefix="/api")
    test_app.dependency_overrides[get_current_user] = lambda: _user()

    async def _override_get_db():
        yield _FakeDocSession(_doc(uuid.uuid4()))

    test_app.dependency_overrides[get_db] = _override_get_db
    return TestClient(test_app, raise_server_exceptions=False)


@pytest.fixture
def endpoint_harness(monkeypatch):
    """返回 (client, opened_keys, data_session)；定位与数据面会话均可控。"""
    opened: list[str] = []
    doc_id = uuid.uuid4()
    doc_session = _FakeDocSession(_doc(doc_id))

    @asynccontextmanager
    async def _fake_open(control_session, db_key):  # noqa: ANN001
        opened.append(db_key)
        yield doc_session

    monkeypatch.setattr(documents_api, "open_data_session", _fake_open)
    return _client(doc_session), opened, doc_session, doc_id


def test_document_by_id_uses_control_copy_db_key(endpoint_harness, monkeypatch):
    client, opened, doc_session, doc_id = endpoint_harness

    async def _resolve(session, pk):  # noqa: ANN001
        return "kb_finance"

    monkeypatch.setattr(documents_api, "resolve_document_db_key", _resolve)

    resp = client.get(f"/api/documents/{doc_id}/parse-preview")

    assert resp.status_code == 200
    assert opened == ["kb_finance"]
    assert doc_session.executed == 1


def test_document_by_id_not_found(endpoint_harness, monkeypatch):
    client, opened, _, doc_id = endpoint_harness

    async def _resolve(session, pk):  # noqa: ANN001
        return None

    monkeypatch.setattr(documents_api, "resolve_document_db_key", _resolve)

    resp = client.get(f"/api/documents/{doc_id}/parse-preview")

    assert resp.status_code == 404
    assert opened == []


def test_document_by_id_scope_error_returns_structured_500(endpoint_harness, monkeypatch):
    client, opened, _, doc_id = endpoint_harness

    async def _resolve(session, pk):  # noqa: ANN001
        raise DbScopeError(
            "DB_SCOPE_MULTI_KEY",
            {"kb_ids": [str(uuid.uuid4())], "db_keys": ["default", "kb_finance"]},
        )

    monkeypatch.setattr(documents_api, "resolve_document_db_key", _resolve)

    resp = client.get(f"/api/documents/{doc_id}/parse-preview")

    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert detail["code"] == "DB_SCOPE_MULTI_KEY"
    assert detail["db_keys"] == ["default", "kb_finance"]
    assert "kb_ids" in detail
    assert opened == []
