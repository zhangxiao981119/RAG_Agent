"""Step 6 S6-3/S6-4：请求层两段式（app/services/db_scope.py）单测。

钉住四件事（全部不连库）：
  1. 关闭态零 SQL：resolve / map 直接返回 default，假会话一旦被 execute 就失败；
  2. 开启态判定（default 计为一个库位）：全 default → default；全同绑定 → 该键；
     默认+绑定混合 / 多绑定 → DbScopeError；map 按 kb 分桶，未命中的 kb 映射为 default；
  3. 默认场景（db_binding IS NULL）不产生绑定行，但会以 default 身份参与跨库判定 ——
     落实为 SQL 契约断言（WHERE 必须带 `db_binding IS NOT NULL`，且不得按
     scenarios.status 过滤）+ 「混合 default+bound 报错」的行为断言；
  4. open_data_session：default 原样交出控制会话且不 commit；非 default 先 commit
     再开会话（用记录调用顺序的替身断言顺序）。

★ 期望值一律字面量，不复用被测模块的常量，避免"实现错了断言跟着错"。
"""
from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
from alembic import command

from app.infra.db_router import registered_db_keys
from app.services import db_scope
from app.services.db_scope import (
    DB_SCOPE_MULTI_KEY,
    DbScopeError,
    map_kb_db_keys,
    open_data_session,
    resolve_kb_db_key,
)
from tests.fake_settings import fake_settings
from tests.scenario.pg_helpers import (
    SCENARIO_TEST_DB_URL,
    alembic_config,
    dsn,
    insert_version,
    new_scenario,
    requires_db,
)

_TENANT_ID = uuid.uuid4()
_KB_A = uuid.uuid4()
_KB_B = uuid.uuid4()
_KB_C = uuid.uuid4()


# ══════════════════════════════════════════════════════════════════════
# 替身
# ══════════════════════════════════════════════════════════════════════


class _Rows:
    """`execute()` 的返回值替身：只实现调用方用到的 `.all()`。"""

    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return list(self._rows)


class _ExplodingSession:
    """任何 execute / commit 都立即失败 —— 证明"零 SQL、零 commit"。"""

    async def execute(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("关闭态 MUST NOT 发出任何 SQL")

    async def commit(self) -> None:
        raise AssertionError("default 分支 MUST NOT commit")


class _ScriptedSession:
    """按序出栈的假会话：每次 execute 返回脚本里的下一条结果。"""

    def __init__(self, results: list) -> None:
        self._results = list(results)
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, stmt, params=None):  # noqa: ANN001
        self.calls.append((str(stmt), dict(params or {})))
        return _Rows(self._results.pop(0))


@pytest.fixture
def disabled(monkeypatch):
    """把开关钉在关闭态：用例自己声明配置，不读 .env。"""
    monkeypatch.setattr(
        db_scope, "get_settings", lambda: fake_settings(scene_db_split_enabled=False)
    )


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(
        db_scope, "get_settings", lambda: fake_settings(scene_db_split_enabled=True)
    )


# ══════════════════════════════════════════════════════════════════════
# 1. 关闭态零 SQL
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_disabled_resolve_returns_default_without_sql(disabled):
    session = _ExplodingSession()
    assert await resolve_kb_db_key(session, _TENANT_ID, [_KB_A, _KB_B]) == "default"


@pytest.mark.asyncio
async def test_disabled_map_returns_all_default_without_sql(disabled):
    session = _ExplodingSession()
    assert await map_kb_db_keys(session, _TENANT_ID, [_KB_A, _KB_B]) == {
        _KB_A: "default",
        _KB_B: "default",
    }


@pytest.mark.asyncio
async def test_empty_kb_ids_short_circuit_even_when_enabled(enabled):
    """开启态但空集合：没有任何 kb 可解析，必须零 SQL（不是回落，是无需求）。"""
    session = _ExplodingSession()
    assert await resolve_kb_db_key(session, _TENANT_ID, []) == "default"
    assert await map_kb_db_keys(session, _TENANT_ID, []) == {}


# ══════════════════════════════════════════════════════════════════════
# 2. 开启态：0 / 1 / 多库位
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_enabled_no_non_default_binding_returns_default(enabled):
    """查询返回空 = 全部 kb 的已发布场景都是默认库（或没有绑定）→ default。"""
    session = _ScriptedSession([[]])
    assert await resolve_kb_db_key(session, _TENANT_ID, [_KB_A]) == "default"


@pytest.mark.asyncio
async def test_enabled_single_binding_returns_that_key(enabled):
    """全部 kb 落在同一个绑定库位 → 该键（default 未被任何 kb 命中）。"""
    session = _ScriptedSession(
        [[(str(_KB_A), "kb_finance"), (str(_KB_B), "kb_finance")]]
    )
    assert await resolve_kb_db_key(session, _TENANT_ID, [_KB_A, _KB_B]) == "kb_finance"


@pytest.mark.asyncio
async def test_enabled_mixed_default_and_bound_raises_multi_key(enabled):
    """D1：默认库 kb + 绑定库 kb = 跨库，必须报 MULTI_KEY 而不是只返回绑定键。

    查询只返回非默认库位的行（kb_b → kb_finance），kb_a 未命中即 default；
    若实现只看"非默认库位数量"，这里会错误地返回 kb_finance。
    """
    session = _ScriptedSession([[(str(_KB_B), "kb_finance")]])
    with pytest.raises(DbScopeError) as excinfo:
        await resolve_kb_db_key(session, _TENANT_ID, [_KB_A, _KB_B])

    exc = excinfo.value
    assert exc.code == DB_SCOPE_MULTI_KEY
    assert exc.detail["db_keys"] == ["default", "kb_finance"]
    assert exc.detail["kb_ids"] == [str(_KB_A), str(_KB_B)]


@pytest.mark.asyncio
async def test_enabled_multi_binding_raises_multi_key(enabled):
    session = _ScriptedSession(
        [[(str(_KB_A), "kb_finance"), (str(_KB_B), "kb_hr")]]
    )
    with pytest.raises(DbScopeError) as excinfo:
        await resolve_kb_db_key(session, _TENANT_ID, [_KB_A, _KB_B])

    exc = excinfo.value
    assert exc.code == DB_SCOPE_MULTI_KEY
    assert exc.code == "DB_SCOPE_MULTI_KEY"
    assert exc.detail["db_keys"] == ["kb_finance", "kb_hr"]  # 排序后的库位键
    assert exc.detail["kb_ids"] == [str(_KB_A), str(_KB_B)]
    assert "DB_SCOPE_MULTI_KEY" in str(exc)


@pytest.mark.asyncio
async def test_enabled_resolve_passes_tenant_and_string_kb_ids(enabled):
    """参数契约：tenant 是 uuid，kb_ids 以字符串数组下发（与 JSONB text 比较）。"""
    session = _ScriptedSession([[]])
    await resolve_kb_db_key(session, _TENANT_ID, [_KB_A])
    sql, params = session.calls[0]
    assert params["tenant_id"] == _TENANT_ID
    assert params["kb_ids"] == [str(_KB_A)]
    # SQL 契约：以已发布版本的 kb_ids 为准，只返回非默认库位行，且不按场景状态过滤
    assert "scenario_versions" in sql
    assert "jsonb_array_elements_text" in sql
    assert "published" in sql
    assert "s.db_binding IS NOT NULL" in sql
    assert "s.status" not in sql


# ══════════════════════════════════════════════════════════════════════
# 3. map 分桶
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_map_buckets_by_db_key_and_defaults_unmatched_kbs(enabled):
    session = _ScriptedSession([[(str(_KB_A), "kb_finance"), (str(_KB_B), "kb_hr")]])
    mapping = await map_kb_db_keys(session, _TENANT_ID, [_KB_A, _KB_B, _KB_C])
    assert mapping == {
        _KB_A: "kb_finance",
        _KB_B: "kb_hr",
        _KB_C: "default",  # 未命中 = 默认库
    }


@pytest.mark.asyncio
async def test_map_does_not_consume_rows_when_disabled(disabled):
    """关闭态即便结果里"有绑定"也不该被查询 —— 用爆炸会话直接证明。"""
    session = _ExplodingSession()
    mapping = await map_kb_db_keys(session, _TENANT_ID, [_KB_A])
    assert mapping == {_KB_A: "default"}


@pytest.mark.asyncio
async def test_map_raises_when_one_kb_hits_two_buckets(enabled):
    """同一 kb 被两个不同库位的已发布场景命中 = 0030 定义的冲突态，必须报错。"""
    session = _ScriptedSession(
        [[(str(_KB_A), "kb_finance"), (str(_KB_A), "kb_hr")]]
    )
    with pytest.raises(DbScopeError) as excinfo:
        await map_kb_db_keys(session, _TENANT_ID, [_KB_A])

    assert excinfo.value.code == DB_SCOPE_MULTI_KEY
    assert excinfo.value.detail == {"conflicts": {str(_KB_A): ["kb_finance", "kb_hr"]}}


# ══════════════════════════════════════════════════════════════════════
# 4. open_data_session 两段式
# ══════════════════════════════════════════════════════════════════════


class _RecordingControl:
    def __init__(self, order: list) -> None:
        self._order = order

    async def commit(self) -> None:
        self._order.append("control.commit")


@pytest.mark.asyncio
async def test_open_data_session_default_yields_control_session_without_commit():
    order: list = []
    control = _RecordingControl(order)

    async with open_data_session(control, "default") as session:
        assert session is control
        order.append("use")

    assert order == ["use"], "default 分支 MUST NOT commit、MUST NOT 另开会话"


@pytest.mark.asyncio
async def test_open_data_session_bound_commits_then_opens_and_closes(monkeypatch):
    """顺序契约：control.commit → open(db_key) → yield → close。"""
    order: list = []
    control = _RecordingControl(order)
    data_plane_session = object()

    @asynccontextmanager
    async def _fake_data_session(db_key: str):
        order.append(f"open:{db_key}")
        try:
            yield data_plane_session
        finally:
            order.append(f"close:{db_key}")

    # 白名单允许：T-5-7 只约束 app/ 内的调用点，测试里替身走猴补丁
    monkeypatch.setattr(db_scope, "data_session", _fake_data_session)

    async with open_data_session(control, "kb_finance") as session:
        assert session is data_plane_session
        order.append("use")

    assert order == [
        "control.commit",
        "open:kb_finance",
        "use",
        "close:kb_finance",
    ]


# ══════════════════════════════════════════════════════════════════════
# 5. DbScopeError 契约
# ══════════════════════════════════════════════════════════════════════


def test_db_scope_error_carries_code_and_detail():
    err = DbScopeError(DB_SCOPE_MULTI_KEY, {"db_keys": ["kb_a", "kb_b"]})
    assert err.code == "DB_SCOPE_MULTI_KEY"
    assert err.detail == {"db_keys": ["kb_a", "kb_b"]}
    assert str(err) == "DB_SCOPE_MULTI_KEY: {'db_keys': ['kb_a', 'kb_b']}"


def test_owner_unresolved_code_is_machine_readable():
    """D-S6-3-2 修订：wiki 页归属文档副本缺失时的错误码（不再有跨库歧义码）。"""
    assert db_scope.DB_SCOPE_OWNER_UNRESOLVED == "DB_SCOPE_OWNER_UNRESOLVED"
    err = DbScopeError(
        db_scope.DB_SCOPE_OWNER_UNRESOLVED,
        {"page_id": "p", "document_id": "d"},
    )
    assert err.code == "DB_SCOPE_OWNER_UNRESOLVED"


def test_registered_db_keys_contains_default_sorted_and_unique():
    """registered_db_keys 是已注册库位的公开视图（治理扫描等使用）：含 default、去重、排序。"""
    keys = registered_db_keys()
    assert "default" in keys
    assert keys == tuple(sorted(set(keys)))


# ══════════════════════════════════════════════════════════════════════
# 真库层：控制库 + 场景库同 id 是迁移 COPY 的正常状态（不得判歧义）
# ══════════════════════════════════════════════════════════════════════

_DOC_INSERT_SQL = (
    "INSERT INTO documents "
    "(id, tenant_id, kb_id, doc_group_id, filename, ext, size_bytes, storage_key) "
    "VALUES ($1, $2, $3, $1, $4, $5, $6, $7)"
)


def _with_database(url: str, database: str) -> str:
    """替换 DSN 的库名（保留账号、口令、主机与端口）。"""
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{database}"))


async def _run_control_copy_same_id() -> None:
    base_url = SCENARIO_TEST_DB_URL or ""
    temp_name = f"scene_probe_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(_with_database(dsn(base_url), "postgres"))
    control = await asyncpg.connect(dsn(base_url))
    tenant_id, kb_id, doc_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    created = False
    try:
        try:
            await admin.execute(f'CREATE DATABASE "{temp_name}" TEMPLATE template0')
        except asyncpg.exceptions.InsufficientPrivilegeError as exc:
            pytest.skip(f"当前账号无 CREATE DATABASE 权限（{exc}）")
        created = True
        temp_url = _with_database(base_url, temp_name)
        await asyncio.to_thread(command.upgrade, alembic_config(temp_url), "head")

        # 控制库：租户 + 库 + 已发布场景（kb → kb_probe）+ 文档副本
        await control.execute(
            "INSERT INTO tenants (id, name, code) VALUES ($1, $2, $3)",
            tenant_id,
            "控制副本用例租户",
            f"cc-{tenant_id.hex[:8]}",
        )
        await control.execute(
            "INSERT INTO knowledge_bases (id, tenant_id, name, description) "
            "VALUES ($1, $2, $3, $4)",
            kb_id,
            tenant_id,
            f"kb-{str(kb_id)[:8]}",
            "控制副本用例库",
        )
        scenario_id = await new_scenario(control, tenant_id)
        await insert_version(
            control,
            tenant_id,
            scenario_id,
            version=1,
            state="published",
            kb_ids=[str(kb_id)],
            published_at=datetime.now(timezone.utc),
        )
        await control.execute(
            "UPDATE scenarios SET db_binding = $1 WHERE id = $2",
            "kb_probe",
            scenario_id,
        )
        await control.execute(
            _DOC_INSERT_SQL,
            doc_id,
            tenant_id,
            kb_id,
            "控制副本.pdf",
            "pdf",
            10,
            "control/doc.pdf",
        )

        # 场景库：同 id 文档行（迁移 COPY 后两个库都有这一行）
        scene = await asyncpg.connect(_with_database(dsn(base_url), temp_name))
        try:
            await scene.execute(
                "INSERT INTO tenants (id, name, code) VALUES ($1, $2, $3)",
                tenant_id,
                "控制副本用例租户",
                f"cc-{tenant_id.hex[:8]}",
            )
            await scene.execute(
                "INSERT INTO knowledge_bases (id, tenant_id, name, description) "
                "VALUES ($1, $2, $3, $4)",
                kb_id,
                tenant_id,
                f"kb-{str(kb_id)[:8]}",
                "控制副本用例库",
            )
            await scene.execute(
                _DOC_INSERT_SQL,
                doc_id,
                tenant_id,
                kb_id,
                "控制副本.pdf",
                "pdf",
                10,
                "control/doc.pdf",
            )
        finally:
            await scene.close()

        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        engine = create_async_engine(base_url)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                # 只读控制副本的 kb_id 推导：返回绑定库位，不因场景库同 id 报歧义
                assert await db_scope.resolve_document_db_key(session, doc_id) == "kb_probe"
        finally:
            await engine.dispose()
    finally:
        # 清理控制库造的数据（先子表后主表），再丢弃临时场景库
        await control.execute("DELETE FROM documents WHERE id = $1", doc_id)
        await control.execute(
            "DELETE FROM scenario_versions WHERE tenant_id = $1", tenant_id
        )
        await control.execute("DELETE FROM scenarios WHERE tenant_id = $1", tenant_id)
        await control.execute(
            "DELETE FROM knowledge_bases WHERE tenant_id = $1", tenant_id
        )
        await control.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
        await control.close()
        if created:
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid()",
                temp_name,
            )
            await admin.execute(f'DROP DATABASE IF EXISTS "{temp_name}"')
        await admin.close()


@requires_db
def test_real_db_control_copy_same_id_returns_scene_key(schema, monkeypatch):
    """控制库与场景库同 id：新入口返回控制副本 kb 的绑定库位，而非歧义。"""
    monkeypatch.setattr(
        db_scope, "get_settings", lambda: fake_settings(scene_db_split_enabled=True)
    )
    asyncio.run(_run_control_copy_same_id())
