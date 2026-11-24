"""S6-3 开启态接口级用例（kbs）：删除门禁与批量上传的库位错误映射。

不连真库：用猴补丁替换 `resolve_kb_db_key`，验证库里已接线后的对外语义：
  1. delete_kb：kb 绑定非默认库位 → 409 KB_IN_BOUND_SCENE，且不进入删除逻辑；
  2. delete_kb：解析抛 DbScopeError（同一 kb 命中多个库）→ 409 KB_SCOPE_CONFLICT，
     detail 保留原始原因码（不得裸 500、不得丢 code）；
  3. 批量上传：单文件库位解析冲突 → 该文件进入 failed 且 detail 含 code，
     其余文件不受影响（不是整批 500）。
"""
from __future__ import annotations

import contextlib
import uuid
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.kbs import router
from app.api.deps import CurrentUser, get_current_user, get_db
from app.config.settings import get_settings
from app.main import app as main_app
from app.models import KnowledgeBase
from app.services.db_scope import DbScopeError
from app.services.tenant.governance import invalidate_allowed_db_keys

_TENANT_ID = uuid.uuid4()
_KB_ID = uuid.uuid4()


def _user() -> CurrentUser:
    return CurrentUser(
        user_id=uuid.uuid4(),
        tenant_id=_TENANT_ID,
        username="tester",
        display_name="tester",
        clearance=40,  # admin：库管理权
        dept_path="研发中心",
        subjects=[],
        authorized_kb_ids=[_KB_ID],
        acl_epoch=1,
    )


def _kb() -> KnowledgeBase:
    return KnowledgeBase(
        id=_KB_ID,
        tenant_id=_TENANT_ID,
        name="测试库",
        description="",
    )


class _FakeSession:
    def __init__(self, kb: KnowledgeBase | None) -> None:
        self._kb = kb

    def add(self, obj) -> None:  # noqa: ANN001
        pass

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        pass

    async def refresh(self, obj) -> None:  # noqa: ANN001
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        if getattr(obj, "created_at", None) is None:
            from datetime import datetime, timezone

            obj.created_at = datetime.now(timezone.utc)

    async def get(self, model, key):  # noqa: ANN001
        if model is KnowledgeBase:
            return self._kb
        return None

    async def delete(self, obj) -> None:  # noqa: ANN001
        raise AssertionError("库位判定失败时 MUST NOT 进入删除逻辑")


class _FakeStorage:
    def __init__(self) -> None:
        self.put_keys: list[str] = []

    async def put_object(self, key: str, data: bytes, content_type: str) -> None:
        self.put_keys.append(key)


class _FakeArqPool:
    async def enqueue_job(self, name: str, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        pass


@pytest.fixture
def client(monkeypatch):
    storage = _FakeStorage()

    async def _async_of(value):
        return value

    monkeypatch.setattr("app.api.kbs.get_storage", lambda: _async_of(storage))

    async def _fake_audit(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
        return None

    monkeypatch.setattr("app.api.kbs.audit.record", _fake_audit)

    test_app = FastAPI()
    test_app.include_router(router, prefix="/api")
    test_app.state.arq_pool = _FakeArqPool()
    test_app.state.test_storage = storage
    test_app.dependency_overrides[get_current_user] = lambda: _user()

    async def _override_get_db():
        yield _FakeSession(_kb())

    test_app.dependency_overrides[get_db] = _override_get_db
    return TestClient(test_app, raise_server_exceptions=False)


def test_delete_kb_bound_key_returns_409(client, monkeypatch):
    async def _resolve_bound(session, tenant_id, kb_ids):  # noqa: ANN001
        return "kb_finance"

    monkeypatch.setattr("app.api.kbs.resolve_kb_db_key", _resolve_bound)

    resp = client.delete(f"/api/kbs/{_KB_ID}")
    assert resp.status_code == 409
    assert "KB_IN_BOUND_SCENE" in resp.json()["detail"]


def test_delete_kb_scope_conflict_returns_409_with_code(client, monkeypatch):
    async def _resolve_conflict(session, tenant_id, kb_ids):  # noqa: ANN001
        raise DbScopeError(
            "DB_SCOPE_MULTI_KEY",
            {"conflicts": {str(_KB_ID): ["kb_finance", "kb_hr"]}},
        )

    monkeypatch.setattr("app.api.kbs.resolve_kb_db_key", _resolve_conflict)

    resp = client.delete(f"/api/kbs/{_KB_ID}")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "KB_SCOPE_CONFLICT" in detail
    assert "DB_SCOPE_MULTI_KEY" in detail


def test_batch_upload_scope_conflict_reports_per_file(client, monkeypatch):
    async def _resolve_conflict(session, tenant_id, kb_ids):  # noqa: ANN001
        raise DbScopeError(
            "DB_SCOPE_MULTI_KEY",
            {"conflicts": {str(_KB_ID): ["kb_finance", "kb_hr"]}},
        )

    monkeypatch.setattr("app.api.kbs.resolve_kb_db_key", _resolve_conflict)

    resp = client.post(
        f"/api/kbs/{_KB_ID}/documents/batch",
        files=[("files", ("制度.pdf", b"%PDF-1.4", "application/pdf"))],
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["succeeded"] == []
    assert len(body["failed"]) == 1
    assert body["failed"][0]["filename"] == "制度.pdf"
    assert "DB_SCOPE_MULTI_KEY" in body["failed"][0]["detail"]


def test_single_upload_scope_conflict_returns_409_with_code(client, monkeypatch):
    """单文件上传：库位解析冲突 → 409 KB_SCOPE_CONFLICT 且保留原因码，不落存储。"""
    storage = client.app.state.test_storage

    async def _resolve_conflict(session, tenant_id, kb_ids):  # noqa: ANN001
        raise DbScopeError(
            "DB_SCOPE_MULTI_KEY",
            {"conflicts": {str(_KB_ID): ["kb_finance", "kb_hr"]}},
        )

    monkeypatch.setattr("app.api.kbs.resolve_kb_db_key", _resolve_conflict)

    resp = client.post(
        f"/api/kbs/{_KB_ID}/documents",
        files={"file": ("制度.pdf", b"%PDF-1.4", "application/pdf")},
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "KB_SCOPE_CONFLICT" in detail
    assert "DB_SCOPE_MULTI_KEY" in detail
    assert storage.put_keys == []


# ══════════════════════════════════════════════════════════════════════
# Step 7 S7-3：列表统计的库白名单（GET /api/kbs）
# ══════════════════════════════════════════════════════════════════════
#
# 缺陷形态：`list_kbs` 先 `map_kb_db_keys` 分桶、再逐个库位 `open_data_session`
# 统计文档数；单库接口走 `resolve_kb_db_key`（内部含白名单校验），列表却漏了，
# 于是被 `allowed_db_keys` 拒绝的库位仍会被打开读统计。
#
# 本组用例只替换「控制面会话」与「数据面会话」两处边界，走真实的路由、真实的
# `map_kb_db_keys` / `ensure_db_keys_allowed` 与全局 DbScopeError → 400 处理器：
#   · 控制面替身按 SQL 文本分派（knowledge_bases / scenarios / allowed_db_keys）；
#   · 数据面替身记录「哪个库位被打开、发出了几次 count」并按桶返回计数；
#   · 断言计数是显式给出的非零值，不使用「碰巧为 0」的弱断言。

_DEFAULT_KEY = "default"
_DENIED_KEY = "kb_x"


class _Row:
    """治理读取用的行替身：`get_allowed_db_keys` 只取 `._mapping`。"""

    def __init__(self, mapping: dict) -> None:
        self._mapping = mapping


class _Result:
    """`execute()` 返回值替身：覆盖列表路径用到的 `scalars().all()` / `all()` / `first()`。"""

    def __init__(self, rows: list) -> None:
        self._rows = list(rows)

    def scalars(self) -> "_Result":
        return self

    def all(self) -> list:
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _DataPlane:
    """数据面会话替身：每次 count 都记录到 `count_calls`，并返回该桶的预置计数。"""

    def __init__(self, db_key: str, rows: list, count_calls: list) -> None:
        self.db_key = db_key
        self._rows = list(rows)
        self._count_calls = count_calls
        self.statements: list[str] = []

    async def execute(self, stmt, params=None):  # noqa: ANN001
        self.statements.append(str(stmt))
        self._count_calls.append(self.db_key)
        return _Result(self._rows)


class _ControlSession:
    """控制面会话替身：按 SQL 文本分派，记录语句顺序与 commit 次数。"""

    def __init__(self, *, kbs: list, map_rows: list, allowed) -> None:  # noqa: ANN001
        self._kbs = kbs
        self._map_rows = map_rows
        self._allowed = allowed
        self.statements: list[str] = []
        self.commits = 0

    async def execute(self, stmt, params=None):  # noqa: ANN001
        sql = str(stmt)
        self.statements.append(sql)
        if "knowledge_bases" in sql:
            return _Result(self._kbs)
        if "scenarios" in sql:
            return _Result(self._map_rows)
        if "allowed_db_keys" in sql:
            return _Result([_Row({"allowed_db_keys": self._allowed})])
        raise AssertionError(f"未预期的控制面 SQL：{sql}")

    async def commit(self) -> None:
        # 非 default 分支会先 commit 收尾控制面事务；被拒时绝不允许走到这里。
        self.commits += 1


def _make_kb(kb_id: uuid.UUID, tenant_id: uuid.UUID, name: str) -> KnowledgeBase:
    return KnowledgeBase(
        id=kb_id,
        tenant_id=tenant_id,
        name=name,
        description="",
        is_public=False,
        owner_id=None,
    )


def _make_user(
    tenant_id: uuid.UUID,
    *,
    clearance: int = 40,
    authorized: tuple[uuid.UUID, ...] = (),
) -> CurrentUser:
    return CurrentUser(
        user_id=uuid.uuid4(),
        tenant_id=tenant_id,
        username="tester",
        display_name="tester",
        clearance=clearance,
        dept_path="",
        subjects=[],
        authorized_kb_ids=list(authorized),
        acl_epoch=1,
    )


@contextlib.contextmanager
def _installed(
    monkeypatch,
    *,
    user: CurrentUser,
    kbs: list,
    map_rows: list,
    allowed,
    counts: dict[str, list],
    deny_open: tuple[str, ...] = (),
    governance: bool = True,
    split: bool = True,
):
    """装好替身边界：配置开关、依赖覆盖、数据面打开守卫。"""
    invalidate_allowed_db_keys()
    settings = get_settings()
    monkeypatch.setattr(settings, "tenant_governance_enabled", governance)
    monkeypatch.setattr(settings, "scene_db_split_enabled", split)

    control = _ControlSession(kbs=kbs, map_rows=map_rows, allowed=allowed)
    opened: list[str] = []
    count_calls: list[str] = []

    @asynccontextmanager
    async def _open(control_session, db_key):  # noqa: ANN001
        if db_key in deny_open:
            raise AssertionError(f"被拒库位 {db_key} MUST NOT 被打开（白名单未生效）")
        opened.append(db_key)
        yield _DataPlane(db_key, counts.get(db_key, []), count_calls)

    monkeypatch.setattr("app.api.kbs.open_data_session", _open)

    main_app.dependency_overrides[get_current_user] = lambda: user

    async def _override_get_db():
        yield control

    main_app.dependency_overrides[get_db] = _override_get_db
    try:
        yield control, opened, count_calls
    finally:
        main_app.dependency_overrides.clear()
        invalidate_allowed_db_keys()


def _map_rows(*pairs: tuple[uuid.UUID, str]) -> list[tuple[str, str]]:
    """`map_kb_db_keys` 的查询结果形态：`(kb_id 文本, 库位键)`。"""
    return [(str(kb_id), db_key) for kb_id, db_key in pairs]


def test_kbs_list_denied_location_returns_400_and_never_opens_it(monkeypatch):
    """白名单只有 default，某 kb 位于 kb_x → 400，且 kb_x 从未被打开、无 count。"""
    tenant_id = uuid.uuid4()
    kb_default, kb_denied = uuid.uuid4(), uuid.uuid4()
    user = _make_user(tenant_id)
    kbs = [
        _make_kb(kb_default, tenant_id, "默认库知识库"),
        _make_kb(kb_denied, tenant_id, "场景库知识库"),
    ]

    with _installed(
        monkeypatch,
        user=user,
        kbs=kbs,
        map_rows=_map_rows((kb_denied, _DENIED_KEY)),
        allowed=["default"],
        counts={},
        deny_open=(_DENIED_KEY,),
    ) as (control, opened, count_calls):
        resp = TestClient(main_app).get("/api/kbs")

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["code"] == "DB_SCOPE_NOT_ALLOWED"
    assert detail["denied"] == [_DENIED_KEY]
    assert detail["tenant_id"] == str(tenant_id)
    # 证据：被拒库位没有被打开，也没有任何 count 发出（连 default 都没有）
    assert opened == []
    assert count_calls == []


def test_kbs_list_allowed_both_buckets_returns_real_counts(monkeypatch):
    """白名单含 default 与 kb_x → 200，两个桶都被打开且各自计数正确。"""
    tenant_id = uuid.uuid4()
    kb_default, kb_scene = uuid.uuid4(), uuid.uuid4()
    user = _make_user(tenant_id)
    kbs = [
        _make_kb(kb_default, tenant_id, "默认库知识库"),
        _make_kb(kb_scene, tenant_id, "场景库知识库"),
    ]

    with _installed(
        monkeypatch,
        user=user,
        kbs=kbs,
        map_rows=_map_rows((kb_scene, _DENIED_KEY)),
        allowed=["default", _DENIED_KEY],
        counts={
            "default": [(kb_default, 2)],
            _DENIED_KEY: [(kb_scene, 3)],
        },
    ) as (control, opened, count_calls):
        resp = TestClient(main_app).get("/api/kbs")

    assert resp.status_code == 200
    counts = {row["id"]: row["doc_count"] for row in resp.json()}
    assert counts == {str(kb_default): 2, str(kb_scene): 3}
    # 两个桶都打开了（按库位排序），且每个桶恰好一次 count
    assert opened == ["default", _DENIED_KEY]
    assert count_calls == ["default", _DENIED_KEY]
    # 控制面：kb 列表 + 库位映射 + 白名单各一条
    assert len(control.statements) == 3


def test_kbs_list_empty_allowlist_denies_default_too(monkeypatch):
    """白名单为空数组 = 全禁：即使所有 kb 都在 default 也 400。"""
    tenant_id = uuid.uuid4()
    kb_default = uuid.uuid4()
    user = _make_user(tenant_id)

    with _installed(
        monkeypatch,
        user=user,
        kbs=[_make_kb(kb_default, tenant_id, "默认库知识库")],
        map_rows=[],
        allowed=[],
        counts={},
        deny_open=("default",),
    ) as (control, opened, count_calls):
        resp = TestClient(main_app).get("/api/kbs")

    assert resp.status_code == 400
    assert resp.json()["detail"]["denied"] == ["default"]
    assert opened == []
    assert count_calls == []


def test_kbs_list_null_allowlist_is_unrestricted(monkeypatch):
    """白名单为 NULL（不限）→ 200，跨库位统计照常。"""
    tenant_id = uuid.uuid4()
    kb_default, kb_scene = uuid.uuid4(), uuid.uuid4()
    user = _make_user(tenant_id)

    with _installed(
        monkeypatch,
        user=user,
        kbs=[
            _make_kb(kb_default, tenant_id, "默认库知识库"),
            _make_kb(kb_scene, tenant_id, "场景库知识库"),
        ],
        map_rows=_map_rows((kb_scene, _DENIED_KEY)),
        allowed=None,
        counts={
            "default": [(kb_default, 5)],
            _DENIED_KEY: [(kb_scene, 7)],
        },
    ) as (control, opened, count_calls):
        resp = TestClient(main_app).get("/api/kbs")

    assert resp.status_code == 200
    counts = {row["id"]: row["doc_count"] for row in resp.json()}
    assert counts == {str(kb_default): 5, str(kb_scene): 7}
    assert opened == ["default", _DENIED_KEY]


def test_kbs_list_governance_off_keeps_behavior_and_adds_no_sql(monkeypatch):
    """治理关闭 → 200 行为不变，且白名单校验不额外发任何 SQL。"""
    tenant_id = uuid.uuid4()
    kb_default, kb_scene = uuid.uuid4(), uuid.uuid4()
    user = _make_user(tenant_id)

    with _installed(
        monkeypatch,
        user=user,
        kbs=[
            _make_kb(kb_default, tenant_id, "默认库知识库"),
            _make_kb(kb_scene, tenant_id, "场景库知识库"),
        ],
        map_rows=_map_rows((kb_scene, _DENIED_KEY)),
        allowed=None,  # 关闭态不会读这一列，值不参与判定
        counts={
            "default": [(kb_default, 1)],
            _DENIED_KEY: [(kb_scene, 4)],
        },
        governance=False,
        split=True,
    ) as (control, opened, count_calls):
        resp = TestClient(main_app).get("/api/kbs")

    assert resp.status_code == 200
    counts = {row["id"]: row["doc_count"] for row in resp.json()}
    assert counts == {str(kb_default): 1, str(kb_scene): 4}
    assert opened == ["default", _DENIED_KEY]
    # 关闭态零 SQL：只有 kb 列表与库位映射两条，没有 tenants 读取
    assert len(control.statements) == 2
    assert not any("allowed_db_keys" in sql for sql in control.statements)


def test_kbs_list_partial_grant_user_still_denied_on_subset(monkeypatch):
    """普通用户只被授权一个 kb，但该 kb 位于被拒库位 → 仍 400，不因过滤后子集漏检。"""
    tenant_id = uuid.uuid4()
    kb_denied = uuid.uuid4()
    user = _make_user(tenant_id, clearance=20, authorized=(kb_denied,))

    with _installed(
        monkeypatch,
        user=user,
        kbs=[_make_kb(kb_denied, tenant_id, "场景库知识库")],
        map_rows=_map_rows((kb_denied, _DENIED_KEY)),
        allowed=["default"],
        counts={},
        deny_open=(_DENIED_KEY,),
    ) as (control, opened, count_calls):
        resp = TestClient(main_app).get("/api/kbs")

    assert resp.status_code == 400
    assert resp.json()["detail"]["denied"] == [_DENIED_KEY]
    assert opened == []
    assert count_calls == []


def test_kbs_list_empty_kb_set_with_empty_allowlist_returns_200(monkeypatch):
    """没有 kb 行时不存在数据面读取：白名单为 [] 也不误报 400，直接返回空列表。"""
    tenant_id = uuid.uuid4()
    user = _make_user(tenant_id)

    with _installed(
        monkeypatch,
        user=user,
        kbs=[],
        map_rows=[],
        allowed=[],
        counts={},
    ) as (control, opened, count_calls):
        resp = TestClient(main_app).get("/api/kbs")

    assert resp.status_code == 200
    assert resp.json() == []
    # 没有任何库位被打开，也没有 count 发出
    assert opened == []
    assert count_calls == []
    # 控制面只有 kb 列表与白名单两条；空 kb 集不发出库位映射查询
    assert len(control.statements) == 2
    assert not any("scenarios" in sql for sql in control.statements)


def test_kbs_list_whitelist_rejection_precedes_data_plane(monkeypatch):
    """顺序约束：白名单拒绝发生在任何 open_data_session / commit 之前，无未提交写。"""
    tenant_id = uuid.uuid4()
    kb_default, kb_denied = uuid.uuid4(), uuid.uuid4()
    user = _make_user(tenant_id)

    with _installed(
        monkeypatch,
        user=user,
        kbs=[
            _make_kb(kb_default, tenant_id, "默认库知识库"),
            _make_kb(kb_denied, tenant_id, "场景库知识库"),
        ],
        map_rows=_map_rows((kb_denied, _DENIED_KEY)),
        allowed=["default"],
        counts={},
        deny_open=(_DENIED_KEY,),
    ) as (control, opened, count_calls):
        resp = TestClient(main_app).get("/api/kbs")

    assert resp.status_code == 400
    # 顺序：kb 列表 → 库位映射 → 白名单；数据面从未进入
    assert "knowledge_bases" in control.statements[0]
    assert "scenarios" in control.statements[1]
    assert "allowed_db_keys" in control.statements[2]
    assert opened == []
    assert count_calls == []
    # 失败时控制面无未提交写：没有 commit，且全部语句都是只读 SELECT
    assert control.commits == 0
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in control.statements)
