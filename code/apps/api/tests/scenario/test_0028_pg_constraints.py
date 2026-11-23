"""F.1（Real-DB 层）：CHECK 约束 / 部分唯一索引 / FK RESTRICT / 迁移往返 / 种子行落库值。

**仅在 `SCENARIO_TEST_DB_URL` 存在时运行**（`postgresql+asyncpg://...`，独立测试库）。
缺失时整体 skip —— 与 `tests/graph/test_graph_recall_selective_integration.py` 同款约定：
真实 PG 层不阻塞 CI，但一旦提供 URL 就必须全绿。

★ 为什么这些断言必须连真库：CHECK 的**求值语义**（含 NULL 判定）、部分唯一索引的
  `WHERE` 生效方式、FK RESTRICT 的删除拦截，都是 PG 行为而不是 SQL 文本行为。
  离线渲染（test_0028_migration.py）只能证明语句被生成了，不能证明它拦得住坏数据。

★ 每条负向用例都必须满足三件事，缺一条就是**假绿**（曾经踩过）：
  1. INSERT **给全列**，让目标约束是唯一可能违反的那一条 ——
     只给部分列会让 `server_default` 触发别的约束（如 binding_mode 默认 explicit
     配空 kb_ids 必违反 `ck_scenario_versions_binding_consistent`），
     于是「断言任意 CheckViolation」永远成立，把真正的约束删掉也照样绿。
  2. 断言**具体约束名**（`exc.constraint_name`），不是「任意 CHECK 违规」。
  3. 同一用例内给一条**正向对照**：同样的 INSERT 换个合法值必须成功。
     否则分不清「约束拒绝了坏值」和「表根本插不进去」。
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone

import asyncpg
import pytest

# ★ 夹具与构造工具全部来自 `pg_helpers`（唯一真库口径，见其模块 docstring）。
#   本文件的用例体**逐字未动** —— `as` 别名只是为了不让 18 条断言跟着改名。
from tests.scenario.pg_helpers import (  # noqa: F401  (夹具经 conftest 注入)
    APPLICATION_COLUMNS_0027 as _APPLICATION_COLUMNS_0027,
    SCENARIO_TEST_DB_URL,
    SEED_TENANT_CODE as _SEED_TENANT_CODE,
    SEED_TENANT_ID as _SEED_TENANT_ID,
    alembic_config as _alembic_config,
    dsn as _dsn,
    ensure_seed_tenant as _ensure_seed_tenant,
    expect_violation as _expect_violation,
    fresh as _fresh,
    insert_version as _version,
    migrate as _migrate,
    new_scenario as _scenario,
    new_tenant as _tenant,
    requires_db,
    seed_counts as _seed_counts,
)




# ══════════════════════════════════════════════════════════════════════
# CHECK 约束组（§A.3.1）—— 每条都带正向对照
# ══════════════════════════════════════════════════════════════════════


@requires_db
@pytest.mark.asyncio
async def test_policy_keyset_rejects_unwired_key(conn):
    """C-A2：未接线的键写入必须被拒（否则是静默 no-op 的根因）。"""
    tenant_id, scenario_id = await _fresh(conn)
    await _expect_violation(
        conn,
        asyncpg.exceptions.CheckViolationError,
        "ck_scenario_versions_policy_keyset",
        lambda: _version(conn, tenant_id, scenario_id, policy={"final_top_n": 3}),
    )
    # 正向对照：闭集内的键用**同样的 INSERT 形状**必须成功
    async with conn.transaction():
        await _version(conn, tenant_id, scenario_id, policy={"recall_top_k": 30})


@requires_db
@pytest.mark.asyncio
async def test_policy_wired_key_accepted_at_publish(conn):
    """闭集内的键 + published 必须成功（正向对照，防「全拒」式的假绿）。"""
    tenant_id, scenario_id = await _fresh(conn)
    async with conn.transaction():
        await _version(
            conn,
            tenant_id,
            scenario_id,
            state="published",
            published_at=datetime.now(timezone.utc),
            policy={"recall_top_k": 30, "rerank_enabled": False,
                    "relevance_threshold_vector": 0.62},
        )


@requires_db
@pytest.mark.asyncio
async def test_policy_range_rejected(conn):
    """C-A3：recall_top_k=0 越界（键在闭集内，唯一可能违反的就是范围）。"""
    tenant_id, scenario_id = await _fresh(conn)
    await _expect_violation(
        conn,
        asyncpg.exceptions.CheckViolationError,
        "ck_scenario_versions_policy_recall_top_k",
        lambda: _version(conn, tenant_id, scenario_id, policy={"recall_top_k": 0}),
    )
    async with conn.transaction():
        await _version(conn, tenant_id, scenario_id, policy={"recall_top_k": 1})
        await _version(conn, tenant_id, scenario_id, version=2, policy={"recall_top_k": 1000})


@requires_db
@pytest.mark.asyncio
async def test_policy_threshold_range_rejected(conn):
    """C-A5：阈值 5.0 越界（模型输出的分数不是 0..1 时才该拒）。"""
    tenant_id, scenario_id = await _fresh(conn)
    await _expect_violation(
        conn,
        asyncpg.exceptions.CheckViolationError,
        "ck_scenario_versions_policy_threshold_vector",
        lambda: _version(
            conn, tenant_id, scenario_id, policy={"relevance_threshold_vector": 5.0}
        ),
    )
    async with conn.transaction():
        await _version(
            conn, tenant_id, scenario_id, policy={"relevance_threshold_vector": 0.62}
        )


@requires_db
@pytest.mark.asyncio
async def test_policy_threshold_rerank_range_rejected(conn):
    """C-A5 的第二个键：rerank 阈值同样受范围约束（两键分开建，必须各自被守护）。"""
    tenant_id, scenario_id = await _fresh(conn)
    await _expect_violation(
        conn,
        asyncpg.exceptions.CheckViolationError,
        "ck_scenario_versions_policy_threshold_rerank",
        lambda: _version(
            conn, tenant_id, scenario_id, policy={"relevance_threshold_rerank": -0.1}
        ),
    )
    async with conn.transaction():
        await _version(
            conn, tenant_id, scenario_id, policy={"relevance_threshold_rerank": 0.71}
        )


@requires_db
@pytest.mark.asyncio
async def test_policy_rerank_must_be_boolean(conn):
    """C-A4：`rerank_enabled` 存字符串 `"true"` 必须被拒。

    ★ 这条用例曾经是假绿：INSERT 只给 6 列 → `binding_mode` 落 server_default
      `'explicit'`、`kb_ids` 落 `'[]'` → 先违反 `ck_scenario_versions_binding_consistent`。
      而断言只接「任意 CheckViolation」，所以把 `ck_scenario_versions_policy_rerank_bool`
      整条删掉它照样绿。现在补全列 + 钉约束名 + 正向对照。
    """
    tenant_id, scenario_id = await _fresh(conn)
    await _expect_violation(
        conn,
        asyncpg.exceptions.CheckViolationError,
        "ck_scenario_versions_policy_rerank_bool",
        lambda: _version(conn, tenant_id, scenario_id, policy={"rerank_enabled": "true"}),
    )
    # 正向对照：真正的 JSON 布尔必须通过（同一条 INSERT，只改值）
    async with conn.transaction():
        await _version(conn, tenant_id, scenario_id, policy={"rerank_enabled": True})
        await _version(conn, tenant_id, scenario_id, version=2, policy={"rerank_enabled": False})


@requires_db
@pytest.mark.asyncio
async def test_binding_mode_consistency(conn):
    """C-B1：explicit + 空 kb_ids = 静默放开全部范围（fail-open），必须被拒。"""
    tenant_id, scenario_id = await _fresh(conn)
    await _expect_violation(
        conn,
        asyncpg.exceptions.CheckViolationError,
        "ck_scenario_versions_binding_consistent",
        lambda: _version(conn, tenant_id, scenario_id, binding_mode="explicit", kb_ids=[]),
    )
    # 正向对照：explicit + 非空 / unrestricted + 空 都合法
    async with conn.transaction():
        await _version(conn, tenant_id, scenario_id, binding_mode="explicit", kb_ids=["kb-1"])
        await _version(
            conn, tenant_id, scenario_id, version=2,
            binding_mode="unrestricted", kb_ids=[],
        )


@requires_db
@pytest.mark.asyncio
async def test_kb_ids_elements_must_be_strings(conn):
    """C-B2：kb_ids 存成对象数组必须被拒（binding 数量合法，唯一违规点是元素类型）。"""
    tenant_id, scenario_id = await _fresh(conn)
    await _expect_violation(
        conn,
        asyncpg.exceptions.CheckViolationError,
        "ck_scenario_versions_kb_ids_strings",
        lambda: _version(
            conn, tenant_id, scenario_id, binding_mode="explicit", kb_ids=[{"id": "x"}]
        ),
    )
    async with conn.transaction():
        await _version(
            conn, tenant_id, scenario_id, binding_mode="explicit", kb_ids=["kb-1", "kb-2"]
        )


@requires_db
@pytest.mark.asyncio
async def test_state_timestamp_consistency(conn):
    """C-V1：state='published' 但 published_at IS NULL 必须被拒。"""
    tenant_id, scenario_id = await _fresh(conn)
    await _expect_violation(
        conn,
        asyncpg.exceptions.CheckViolationError,
        "ck_scenario_versions_state_timestamps",
        lambda: _version(conn, tenant_id, scenario_id, state="published", published_at=None),
    )
    async with conn.transaction():
        await _version(
            conn, tenant_id, scenario_id, state="published",
            published_at=datetime.now(timezone.utc),
        )


@requires_db
@pytest.mark.asyncio
async def test_checksum_format_rejected(conn):
    """`checksum` 必须 16 位小写 hex（大写 / 长度不对都被拒）。"""
    tenant_id, scenario_id = await _fresh(conn)
    await _expect_violation(
        conn,
        asyncpg.exceptions.CheckViolationError,
        "ck_scenario_versions_checksum_format",
        lambda: _version(conn, tenant_id, scenario_id, checksum="NOT-A-CHECKSUM"),
    )
    async with conn.transaction():
        await _version(conn, tenant_id, scenario_id, checksum="0123456789abcdef")


# ══════════════════════════════════════════════════════════════════════
# 部分唯一索引 / 唯一约束 / FK RESTRICT
# ══════════════════════════════════════════════════════════════════════


@requires_db
@pytest.mark.asyncio
async def test_one_builtin_per_tenant(conn):
    """每租户至多一个内置场景（部分唯一索引 WHERE is_builtin）。

    正向对照内建：**同一个租户**先插一个非内置场景可以成功 ——
    证明拒绝来自「第二个内置」而不是「这个租户不能再插场景」。
    """
    tenant_id = await _tenant(conn)
    await _scenario(conn, tenant_id, is_builtin=False)   # 正向：非内置可多条
    await _scenario(conn, tenant_id, is_builtin=True)    # 正向：第一条内置
    await _expect_violation(
        conn,
        asyncpg.exceptions.UniqueViolationError,
        "uq_scenarios_one_builtin",
        lambda: _scenario(conn, tenant_id, is_builtin=True),
    )


@requires_db
@pytest.mark.asyncio
async def test_duplicate_scenario_key_rejected(conn):
    """同租户同 key 被拒；不同 key 成功（正向对照即第一次 `_scenario`）。"""
    tenant_id = await _tenant(conn)
    first = await _scenario(conn, tenant_id)
    await _scenario(conn, tenant_id)  # 正向：同租户另一个 key 可以
    key = await conn.fetchval("SELECT scenario_key FROM scenarios WHERE id = $1", first)
    await _expect_violation(
        conn,
        asyncpg.exceptions.UniqueViolationError,
        "uq_scenarios_tenant_key",
        lambda: conn.execute(
            "INSERT INTO scenarios (tenant_id, scenario_key, name) VALUES ($1,$2,$3)",
            tenant_id,
            key,
            "重名场景",
        ),
    )


@requires_db
@pytest.mark.asyncio
async def test_one_published_per_scenario(conn):
    """同一场景至多一个 published（部分唯一索引 WHERE state='published'）。

    正向对照内建：同场景的 **draft** 版本可以有多条（证明拒绝来自 published 而非版本号）。
    """
    tenant_id, scenario_id = await _fresh(conn)
    async with conn.transaction():
        await _version(conn, tenant_id, scenario_id, state="published",
                       published_at=datetime.now(timezone.utc))
        await _version(conn, tenant_id, scenario_id, version=2, state="draft")
    await _expect_violation(
        conn,
        asyncpg.exceptions.UniqueViolationError,
        "uq_scenario_versions_one_published",
        lambda: _version(conn, tenant_id, scenario_id, version=3, state="published",
                         published_at=datetime.now(timezone.utc)),
    )


@requires_db
@pytest.mark.asyncio
async def test_scenario_id_fk_restrict(conn):
    """有应用引用时删场景必须被 RESTRICT 挡住（CASCADE 会让一批应用悄悄退回默认）。"""
    tenant_id, scenario_id = await _fresh(conn)
    user_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO users (id, tenant_id, username, password_hash, display_name) "
        "VALUES ($1,$2,$3,$4,$5)",
        user_id, tenant_id, f"u-{str(user_id)[:8]}", "x", "测试用户",
    )
    # 正向对照：没有应用引用时删得掉（本用例里的多余场景）
    spare = await _scenario(conn, tenant_id)
    await conn.execute("DELETE FROM scenarios WHERE id = $1", spare)

    await conn.execute(
        "INSERT INTO integration_applications "
        "(id, tenant_id, name, key_hash, kb_ids, rate_limit_per_minute, "
        " daily_token_limit, daily_message_limit, created_by, scenario_id) "
        "VALUES ($1,$2,$3,$4,'[]'::jsonb,60,1000,100,$5,$6)",
        uuid.uuid4(), tenant_id, f"app-{str(scenario_id)[:8]}", f"kh-{scenario_id}",
        user_id, scenario_id,
    )
    await _expect_violation(
        conn,
        asyncpg.exceptions.ForeignKeyViolationError,
        "fk_integration_app_scenario",
        lambda: conn.execute("DELETE FROM scenarios WHERE id = $1", scenario_id),
    )


# ══════════════════════════════════════════════════════════════════════
# 迁移往返（F.1 的升级/降级用例，同步执行 —— env.py 用 asyncio.run）
# ══════════════════════════════════════════════════════════════════════

# `_APPLICATION_COLUMNS_0027` 与 `_seed_counts` 已随夹具一起搬到 `pg_helpers`
# （上面 import 段的别名），此处不再重复定义 —— 两份定义会让「降级后列集合」
# 与「种子计数口径」各有一次漂移机会。


@requires_db
def test_upgrade_downgrade_upgrade_idempotent(schema):
    """upgrade → downgrade → upgrade：对象只出现一次，种子行数 == 租户数。

    「种子行数 == 租户数」同时证明 `ON CONFLICT DO NOTHING` 与
    「每租户恰好一条内置场景」两件事。

    ★ `assert tenants >= 1` 是**前置门禁**：没有它，空库上这个断言退化成
      `0 == 0 == 0`，用例绿得毫无信息（曾经如此）。
    """
    _migrate("0028")
    first = asyncio.run(_seed_counts())
    assert first[1] >= 1, "测试库没有租户 —— 种子行数断言会退化成 0==0，无信息量"
    assert first[0] == first[1], "内置场景行数必须等于租户数"
    assert first[2] == first[1], "每个内置场景恰好一条已发布版本"
    assert first[3] == 0, "出现重复 scenario_key"

    _migrate("0027", downgrade=True)
    _migrate("0028")
    second = asyncio.run(_seed_counts())
    assert second == first, f"往返后状态不一致：{first} → {second}"


@requires_db
def test_downgrade_removes_only_0028_objects(schema):
    """降级后：两张新表消失、`scenario_id` 列消失、既有业务表行数与校验和不变（Z-1）。"""
    tables = ("chunks", "documents", "graph_entity_mentions", "graph_relations", "wiki_pages")

    async def _business_stats():
        connection = await asyncpg.connect(_dsn(SCENARIO_TEST_DB_URL))
        try:
            stats = {}
            for table in tables:
                row = await connection.fetchrow(
                    f"SELECT count(*) AS c, "
                    f"COALESCE(md5(string_agg(t::text, '|' ORDER BY t::text)),'') AS h "
                    f"FROM {table} t"
                )
                stats[table] = (row["c"], row["h"])
            return stats
        finally:
            await connection.close()

    async def _structure():
        connection = await asyncpg.connect(_dsn(SCENARIO_TEST_DB_URL))
        try:
            scenario = await connection.fetchval("SELECT to_regclass('public.scenarios')")
            versions = await connection.fetchval("SELECT to_regclass('public.scenario_versions')")
            columns = {
                row["column_name"]
                for row in await connection.fetch(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'integration_applications'"
                )
            }
            return scenario, versions, columns
        finally:
            await connection.close()

    _migrate("0028")
    before = asyncio.run(_business_stats())

    _migrate("0027", downgrade=True)
    scenario, versions, columns = asyncio.run(_structure())
    assert scenario is None, "降级后 scenarios 仍存在"
    assert versions is None, "降级后 scenario_versions 仍存在"
    assert columns == _APPLICATION_COLUMNS_0027, f"列集合与 0027 不一致：{columns}"
    after = asyncio.run(_business_stats())
    assert after == before, "降级改动了既有业务表的数据（违反 Z-1）"

    # 为后续用例恢复到 0028（种子行会为每个租户重新插入）
    _migrate("0028")


@requires_db
@pytest.mark.asyncio
async def test_seed_row_values_match_design(conn, schema):
    """读回种子行：policy 空、unrestricted、checksum 与运行时一致、dna 逐字段相等。

    ★ 这里曾经因为空库 `tenants=0` 直接 `pytest.skip`，导致「种子行的落库值」
      至今无人验证。现在 `schema` 夹具保证至少一个租户在 0028 之前存在，
      只有真的一个租户都没有（建库脚本没插）才 skip，且 skip 文案写明未验证什么。
    """
    from app.services.scenario.snapshot import compute_checksum

    tenant_count = int(await conn.fetchval("SELECT count(*) FROM tenants"))
    if tenant_count == 0:
        pytest.skip("建库脚本未插 tenant（tenants=0），本次未验证种子行的落库值")

    # 每租户恰好一条内置场景行（从库里读回，不信任迁移的意图）
    per_tenant = await conn.fetchrow(
        "SELECT count(*) AS builtin, count(DISTINCT tenant_id) AS owners "
        "FROM scenarios WHERE is_builtin"
    )
    assert per_tenant["builtin"] == per_tenant["owners"] == tenant_count, (
        "内置场景行数与租户数不一致："
        f"builtin={per_tenant['builtin']} owners={per_tenant['owners']} "
        f"tenants={tenant_count}"
    )

    row = await conn.fetchrow(
        "SELECT s.tenant_id, s.scenario_key, s.is_builtin, s.db_binding, s.status, "
        "       v.version, v.state, v.policy, v.kb_ids, v.binding_mode, "
        "       v.dna_declared, v.checksum, v.change_note "
        "FROM scenarios s JOIN scenario_versions v ON v.scenario_id = s.id "
        "WHERE s.is_builtin AND s.tenant_id = $1 AND v.state = 'published'",
        _SEED_TENANT_ID,
    )
    assert row is not None, "种子租户没有内置场景的已发布版本 —— 迁移的种子 INSERT 没生效"

    assert row["scenario_key"] == "default"
    assert row["is_builtin"] is True
    assert row["db_binding"] is None
    assert row["status"] == "active"
    assert row["version"] == 1
    assert row["state"] == "published"
    assert json.loads(row["policy"]) == {}
    assert json.loads(row["kb_ids"]) == []
    assert row["binding_mode"] == "unrestricted"
    assert "空覆盖集" in row["change_note"]

    dna = json.loads(row["dna_declared"])
    assert row["checksum"] == compute_checksum(
        policy=json.loads(row["policy"]),
        kb_ids=json.loads(row["kb_ids"]),
        binding_mode=row["binding_mode"],
        dna_declared=dna,
    )
    # 4 个代码常量：字面量断言（迁移里写的就是这些）
    assert dna["embedding_text_version"] == "etext-v1"
    assert dna["chunker_version"] == "c400-800-60"
    assert dna["graph_prompt_version"] == "gprompt-v1"
    assert dna["wiki_prompt_version"] == "wprompt-v1"
    # 4 个环境绑定项：只声明字段名，不落值
    assert dna["env_bound"] == [
        "embedding_model", "embedding_dim", "graph_extractor_model", "wiki_generator_model",
    ]
    for name in dna["env_bound"]:
        assert name not in dna, f"{name} 是环境绑定项，MUST NOT 在种子里落值"


@requires_db
@pytest.mark.asyncio
async def test_seed_row_is_resolvable_as_platform_dna(conn, schema):
    """把**真实落库的种子行**喂给 resolver：必须得到与 `current_index_profile()` 相等的口径。

    这是 §F.2 硬指标的真实数据版 —— 之前只在假行上验证过，
    假行的 `dna_declared` 是测试自己造的，证明不了迁移写对了。
    """
    from app.services.scenario.resolver import clear_cache, resolve_scenario
    from app.services.scenario.snapshot import ScenarioRow
    from app.config.index_profile import current_index_profile
    from app.models import IntegrationApplication

    row = await conn.fetchrow(
        "SELECT s.id, s.tenant_id, s.scenario_key, s.status, s.is_builtin, s.db_binding, "
        "       v.version, v.policy, v.kb_ids, v.binding_mode, v.dna_declared, v.checksum "
        "FROM scenarios s JOIN scenario_versions v ON v.scenario_id = s.id "
        "WHERE s.is_builtin AND v.state = 'published' AND s.tenant_id = $1",
        _SEED_TENANT_ID,
    )
    assert row is not None, "种子租户没有内置场景的已发布版本"

    scenario_row = ScenarioRow(
        tenant_id=row["tenant_id"],
        scenario_id=row["id"],
        scenario_key=row["scenario_key"],
        status=row["status"],
        is_builtin=row["is_builtin"],
        db_binding=row["db_binding"],
        version=row["version"],
        policy=json.loads(row["policy"]),
        kb_ids=tuple(json.loads(row["kb_ids"])),
        binding_mode=row["binding_mode"],
        dna_declared=json.loads(row["dna_declared"]),
        checksum=row["checksum"],
    )

    class _Session:
        async def execute(self, stmt):
            class _R:
                def all(self_inner):
                    return [type("Row", (), {"_mapping": {
                        "scenario_id": scenario_row.scenario_id,
                        "tenant_id": scenario_row.tenant_id,
                        "scenario_key": scenario_row.scenario_key,
                        "status": scenario_row.status,
                        "is_builtin": scenario_row.is_builtin,
                        "db_binding": scenario_row.db_binding,
                        "version": scenario_row.version,
                        "policy": dict(scenario_row.policy),
                        "kb_ids": list(scenario_row.kb_ids),
                        "binding_mode": scenario_row.binding_mode,
                        "dna_declared": dict(scenario_row.dna_declared),
                        "checksum": scenario_row.checksum,
                    }})()]

            return _R()

    clear_cache()
    application = IntegrationApplication(
        id=uuid.uuid4(), tenant_id=_SEED_TENANT_ID, name="种子行解析",
        key_hash="0" * 64, kb_ids=[], scenario_id=None,
        rate_limit_per_minute=60, daily_token_limit=1000, daily_message_limit=100,
        created_by=uuid.uuid4(),
    )
    snapshot = await resolve_scenario(
        _Session(), tenant_id=_SEED_TENANT_ID, application=application,
        requested_kb_ids=[uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")],
    )
    clear_cache()

    assert snapshot.source == "builtin_row"
    assert snapshot.binding_mode == "unrestricted"
    assert snapshot.policy.declared() == {}
    assert snapshot.index == current_index_profile()
    assert snapshot.index.fingerprint == current_index_profile().fingerprint
