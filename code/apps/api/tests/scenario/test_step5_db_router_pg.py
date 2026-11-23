"""Step 5 真库用例：T-5-9 … T-5-12（库位只读口径 / 视图盘点 / 冲突检测 / 视图==resolver）。

★ 本文件**只**覆盖这 4 条。「T-5-13（迁移往返 / 对象存在性 / 单一头）」已由
  `tests/scenario/test_0030_migration.py` 的 Real-DB 层实现（`test_real_db_objects_exist_with_column_contract`
  / `test_real_db_downgrade_removes_then_upgrade_restores` / `test_migration_is_linear_single_head`），
  **本文件不重复实现**，也不改那份迁移。

★ 库位键必须满足 `ck_scenarios_db_binding_format = ^kb_[a-z0-9_]{2,40}$`：
  设计稿示例里的 `kb_x` 只有 1 个字符，写进库会被 CHECK 直接拒绝，故本文件统一用
  `'kb_ab'` / `'kb_cd'`。

★ 期望值一律字面量（不复用被测模块的常量）；负向用例只允许经 `expect_violation`
  抛错（项目纪律：跨源文件只允许那一处 `pytest.raises`）。

★ 造数据的两种方式，按「读的人是谁」选：
  · T-5-9 / T-5-10 / T-5-11：断言只用**同一条 `conn`**，用 `conn` 夹具在事务内造、
    结束回滚，用例互不污染。
  · T-5-12：resolver 必须走**另一条连接**（SQLAlchemy async session），而它看不到
    未提交的事务 —— 若用 `conn` 夹具造数据，resolver 侧一律 `row is None`，
    会以「场景不存在」的假象红。故这一条在**提交**的事务里造数据（与 Step 4A/4B
    走 handler 的用例必须用 committed 族是同一个原因：跨连接可见性）。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import asyncpg
import pytest

from app.infra import db_router
from app.models import IntegrationApplication
from app.services.scenario.resolver import clear_cache, resolve_scenario
from tests.scenario.pg_helpers import (
    SCENARIO_TEST_DB_URL,
    committed_rows,
    dsn,
    expect_violation,
    fresh,
    insert_version,
    new_scenario,
    new_tenant,
    requires_db,
)

pytestmark = [pytest.mark.asyncio, requires_db]


# ══════════════════════════════════════════════════════════════════════
# T-5-9 默认库位不可写（存储层 NULL，'default' 只存在于展示层）
# ══════════════════════════════════════════════════════════════════════


async def test_t5_9_default_binding_is_not_writable(conn):
    """把 `db_binding` 写成 `'default'` 必须被 `ck_scenarios_db_binding_format` 拒绝。

    这条守的是「默认库位只有一种表示法 = NULL」：一旦 `'default'` 能落库，
    `COALESCE(db_binding,'default')` 的展示口径就与存储层出现两个"默认"，
    而两者在盘点里长得一样、分不出来。
    """
    _tenant_id, scenario_id = await fresh(conn)

    await expect_violation(
        conn,
        asyncpg.exceptions.CheckViolationError,
        "ck_scenarios_db_binding_format",
        lambda: conn.execute(
            "UPDATE scenarios SET db_binding = 'default' WHERE id = $1", scenario_id
        ),
    )

    # 正向对照：同一个 UPDATE 换成合法库位键必须成功 ——
    # 否则分不清「约束拒绝了 default」与「这条 UPDATE 根本写不进去」。
    async with conn.transaction():
        await conn.execute(
            "UPDATE scenarios SET db_binding = 'kb_ab' WHERE id = $1", scenario_id
        )
        stored = await conn.fetchval(
            "SELECT db_binding FROM scenarios WHERE id = $1", scenario_id
        )
    assert stored == "kb_ab"


# ══════════════════════════════════════════════════════════════════════
# T-5-10 视图行数恒等 + 三个场景的库位口径
# ══════════════════════════════════════════════════════════════════════


async def test_t5_10_routing_view_row_count_and_keys(conn):
    """`count(v_scenario_db_routing) == count(scenarios)`（本租户内），且三类行口径正确。

    破坏方式（必须变红）：
      ① 把视图的 `LEFT JOIN` 改成 `JOIN` → 无 published 版本的 C 从盘点里消失，
         行数从 3 变 2 → 红；
      ② 把 `COALESCE(s.db_binding,'default')` 去掉 → A/C 的 db_key 变 NULL → 红；
      ③ 把 `db_key_source` 改成用 NULL 表达默认 → A/C 的 source 变 NULL → 红。
    """
    tenant_id = await new_tenant(conn)
    scenario_a = await new_scenario(conn, tenant_id)
    scenario_b = await new_scenario(conn, tenant_id)
    scenario_c = await new_scenario(conn, tenant_id)
    kb_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    # A：已发布 + 默认库位（db_binding 留 NULL）
    await insert_version(
        conn, tenant_id, scenario_a, state="published", published_at=now, kb_ids=[kb_id]
    )
    # B：已发布 + 绑定库位
    await conn.execute(
        "UPDATE scenarios SET db_binding = 'kb_ab' WHERE id = $1", scenario_b
    )
    await insert_version(
        conn, tenant_id, scenario_b, state="published", published_at=now, kb_ids=[kb_id]
    )
    # C：只有 draft（不得被当成版本口径）
    await insert_version(conn, tenant_id, scenario_c, state="draft", kb_ids=[kb_id])

    scenario_count = int(
        await conn.fetchval("SELECT count(*) FROM scenarios WHERE tenant_id = $1", tenant_id)
    )
    view_rows = await conn.fetch(
        "SELECT scenario_id, published_version, db_key, db_key_source "
        "FROM v_scenario_db_routing WHERE tenant_id = $1",
        tenant_id,
    )
    assert scenario_count == 3
    assert len(view_rows) == scenario_count, (
        f"视图行数 {len(view_rows)} != scenarios 行数 {scenario_count}"
        "（LEFT JOIN 被改成 JOIN，或唯一 published 索引失效导致行数被放大）"
    )

    rows = {row["scenario_id"]: row for row in view_rows}
    a = rows[scenario_a]
    assert a["published_version"] == 1
    assert a["db_key"] == "default"
    assert a["db_key_source"] == "default"

    b = rows[scenario_b]
    assert b["published_version"] == 1
    assert b["db_key"] == "kb_ab"
    assert b["db_key_source"] == "bound"

    c = rows[scenario_c]
    assert c["published_version"] is None
    assert c["db_key"] == "default"
    assert c["db_key_source"] == "default"


# ══════════════════════════════════════════════════════════════════════
# T-5-11 kb 级冲突检测（同一 kb 被两个场景绑到两个不同库位）
# ══════════════════════════════════════════════════════════════════════


async def test_t5_11_kb_conflict_detected_then_resolved(conn):
    """P(kb_ab) 与 Q(kb_cd) 的已发布版本同时含同一 kb → 1 行冲突；改 Q 后 0 行。

    破坏方式（必须变红）：
      ① 把 `HAVING count(DISTINCT db_key) > 1` 改成 `HAVING count(*) > 1` →
         改 Q 后两个场景仍在、仍会报 1 行 → 第二格红；
      ② 删掉 `WHERE s.db_binding IS NOT NULL` → 未绑定场景也参与判定 → T-5-11b 红。
    """
    tenant_id = await new_tenant(conn)
    scenario_p = await new_scenario(conn, tenant_id)
    scenario_q = await new_scenario(conn, tenant_id)
    kb_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    await conn.execute(
        "UPDATE scenarios SET db_binding = 'kb_ab' WHERE id = $1", scenario_p
    )
    await conn.execute(
        "UPDATE scenarios SET db_binding = 'kb_cd' WHERE id = $1", scenario_q
    )
    await insert_version(
        conn, tenant_id, scenario_p, state="published", published_at=now,
        kb_ids=[str(kb_id)],
    )
    await insert_version(
        conn, tenant_id, scenario_q, state="published", published_at=now,
        kb_ids=[str(kb_id)],
    )

    conflicts = await conn.fetch(
        "SELECT tenant_id, kb_id, db_keys FROM fn_kb_db_binding_conflicts() "
        "WHERE tenant_id = $1",
        tenant_id,
    )
    assert len(conflicts) == 1
    assert conflicts[0]["kb_id"] == str(kb_id)
    assert conflicts[0]["db_keys"] == ["kb_ab", "kb_cd"]

    # 把 Q 改成与 P 相同的库位：判据是「≥2 个不同库位」，不再成立
    await conn.execute(
        "UPDATE scenarios SET db_binding = 'kb_ab' WHERE id = $1", scenario_q
    )
    resolved = await conn.fetch(
        "SELECT tenant_id, kb_id, db_keys FROM fn_kb_db_binding_conflicts() "
        "WHERE tenant_id = $1",
        tenant_id,
    )
    assert resolved == []


async def test_t5_11b_default_binding_never_conflicts(conn):
    """两个都未绑定的场景共用同一 kb → 不得报冲突（默认库与任何库都不冲突）。

    ★ 注意本用例的**牙齿边界**：只删 `WHERE s.db_binding IS NOT NULL` 时它会照样绿
      —— `count(DISTINCT db_key)` 天然忽略 NULL，「全默认」两种实现都返回 0 行。
      真正钉住该守卫的是 T-5-11c（冲突组内混入未绑定场景）。
      本用例守的是另一件事：健康态（全部默认）不得被误报成冲突。
    """
    tenant_id = await new_tenant(conn)
    scenario_first = await new_scenario(conn, tenant_id)
    scenario_second = await new_scenario(conn, tenant_id)
    kb_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    await insert_version(
        conn, tenant_id, scenario_first, state="published", published_at=now,
        kb_ids=[kb_id],
    )
    await insert_version(
        conn, tenant_id, scenario_second, state="published", published_at=now,
        kb_ids=[kb_id],
    )

    conflicts = await conn.fetch(
        "SELECT tenant_id, kb_id, db_keys FROM fn_kb_db_binding_conflicts() "
        "WHERE tenant_id = $1",
        tenant_id,
    )
    assert conflicts == [], "两个未绑定场景共用同一 kb 被误判成冲突（默认库不冲突）"


async def test_t5_11c_unbound_scenario_does_not_enter_conflict_group(conn):
    """冲突组里混入一个未绑定场景：db_keys 不得含 NULL，scene_count 必须只数已绑定的 2 个。

    破坏方式（必须变红）：删掉 `WHERE s.db_binding IS NOT NULL` →
      array_agg 会把 NULL 带进来、scene_count 从 2 变 3 → 本条红。
      （T-5-11b 做不到这一点：全默认时 count(DISTINCT db_key) 本来就忽略 NULL。）
    """
    tenant_id = await new_tenant(conn)
    scenario_p = await new_scenario(conn, tenant_id)
    scenario_q = await new_scenario(conn, tenant_id)
    scenario_unbound = await new_scenario(conn, tenant_id)
    kb_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    await conn.execute(
        "UPDATE scenarios SET db_binding = 'kb_ab' WHERE id = $1", scenario_p
    )
    await conn.execute(
        "UPDATE scenarios SET db_binding = 'kb_cd' WHERE id = $1", scenario_q
    )
    for scenario_id in (scenario_p, scenario_q, scenario_unbound):
        await insert_version(
            conn, tenant_id, scenario_id, state="published", published_at=now,
            kb_ids=[kb_id],
        )

    conflicts = await conn.fetch(
        "SELECT db_keys, scene_count FROM fn_kb_db_binding_conflicts() "
        "WHERE tenant_id = $1",
        tenant_id,
    )
    assert len(conflicts) == 1
    assert conflicts[0]["db_keys"] == ["kb_ab", "kb_cd"]
    assert conflicts[0]["scene_count"] == 2, (
        "未绑定场景被算进了冲突组：WHERE s.db_binding IS NOT NULL 不起作用"
    )


# ══════════════════════════════════════════════════════════════════════
# T-5-12 视图口径 == resolver 口径
# ══════════════════════════════════════════════════════════════════════


async def _seed_published_pair() -> uuid.UUID:
    """在一个**提交**的事务里造「默认库位 + 绑定库位」两个已发布场景。

    ★ 必须提交（而不是用 `conn` 夹具）：resolver 走另一条连接，看不到未提交事务。
    """
    connection = await asyncpg.connect(dsn())
    try:
        async with connection.transaction():
            tenant_id = await new_tenant(connection)
            kb_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc)

            default_scenario = await new_scenario(connection, tenant_id)
            await insert_version(
                connection, tenant_id, default_scenario, state="published",
                published_at=now, kb_ids=[kb_id],
            )

            bound_scenario = await new_scenario(connection, tenant_id)
            await connection.execute(
                "UPDATE scenarios SET db_binding = 'kb_ab' WHERE id = $1", bound_scenario
            )
            await insert_version(
                connection, tenant_id, bound_scenario, state="published",
                published_at=now, kb_ids=[kb_id],
            )
    finally:
        await connection.close()
    return tenant_id


async def test_t5_12_view_caliber_equals_resolver(schema):
    """每个有 published 版本的场景：视图与 resolver 给出**同一组**版本号与库位键。

    破坏方式（必须变红）：
      ① 视图的 `v.tenant_id = s.tenant_id` 或 `state='published'` 与 `LOOKUP_SQL`
         不一致 → 某一行的 published_version 与 snapshot.version 对不上 → 红；
      ② `resolve_db_key` 改成对非 NULL 值做小写/规范化 → 库位键对不上 → 红。

    ★ `schema` 必须显式列入参数：本用例不依赖 `conn`，不列它就可能先跑在未升至
      head 的库上（视图不存在）。解析前/后各清一次行缓存：同一测试内多次解析，
      缓存键按 (tenant_id, scenario_id) 区分，但仍显式清理以对齐「TTL 立即到期」，
      避免将来改成共享键时出现静默串味。
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    tenant_id = await _seed_published_pair()

    view_rows = await committed_rows(
        "SELECT scenario_id, published_version, db_key "
        "FROM v_scenario_db_routing "
        "WHERE tenant_id = $1 AND published_version IS NOT NULL "
        "ORDER BY db_key",
        tenant_id,
    )
    assert len(view_rows) == 2
    assert [row["db_key"] for row in view_rows] == ["default", "kb_ab"]

    engine = create_async_engine(SCENARIO_TEST_DB_URL, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        for view_row in view_rows:
            application = IntegrationApplication(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                name="路由口径用例应用",
                key_hash="0" * 64,
                kb_ids=[],
                scenario_id=view_row["scenario_id"],
                rate_limit_per_minute=60,
                daily_token_limit=1000,
                daily_message_limit=100,
                created_by=uuid.uuid4(),
            )
            clear_cache()
            try:
                async with session_factory() as session:
                    snapshot = await resolve_scenario(
                        session,
                        tenant_id=tenant_id,
                        application=application,
                        requested_kb_ids=[],
                    )
            finally:
                clear_cache()

            assert snapshot.version == view_row["published_version"]
            assert db_router.resolve_db_key(snapshot.db_binding) == view_row["db_key"]
    finally:
        await engine.dispose()
