"""真库测试的**唯一**夹具与构造工具（Step 1–2 的 18 条约束用例与 Step 4A 的写接口用例共用）。

★ 为什么必须提取到一个模块、而不是各文件复制一份（设计 §D.1.2）：
  这里的 8 个函数定义了本项目的**真库口径** —— 三条最容易被写歪的纪律就在它们里面：

  1. `insert_version` **永远给全列**，绝不依赖 `server_default`。少给列会让
     `binding_mode` 落默认 `'explicit'`、`kb_ids` 落 `'[]'`，于是先撞
     `ck_scenario_versions_binding_consistent` —— 目标约束删掉也照样红，
     负向用例变成「断言任意 CHECK 违规」的假绿（这条曾经真的踩过）。
  2. `expect_violation` **钉具体约束名**，不是「任意 CheckViolation」。
  3. 每个负向用例都自带**正向对照**（同样的构造换个合法值必须成功），
     否则分不清「约束拒绝了坏值」与「这条 INSERT 根本插不进去」。

  复制一份 = 两份口径；两份口径分叉的第一个后果就是新的那条约束的负向用例
  在新文件里又开始假绿，而且没有任何信号（它还是会绿）。

★ 本模块**不含任何断言**，只提供构造与读取；断言留在各 `test_*.py` 里 ——
  这样「口径」与「判据」分开，改口径时不会顺手改掉判据。

★ 仅在 `SCENARIO_TEST_DB_URL` 存在时才有库可连（`requires_db` 负责 skip）。
  但**函数一律无条件定义**：条件定义会让 `from ... import insert_version` 在无 URL 时
  直接 ImportError，把「跳过」变成「收集失败」—— 那会让 CI 在没有 PG 时整目录报错。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import datetime

import asyncpg
import pytest
from alembic import command
from alembic.config import Config

SCENARIO_TEST_DB_URL = os.environ.get("SCENARIO_TEST_DB_URL")
"""独立测试库的 DSN（`postgresql+asyncpg://...`）。缺失 ⇒ 全部真库用例 skip。"""

requires_db = pytest.mark.skipif(
    not SCENARIO_TEST_DB_URL, reason="needs SCENARIO_TEST_DB_URL (real PG with 0028 applied)"
)

_API_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 种子用例的固定租户（固定 id + 固定 code ⇒ 多次运行幂等，不累积行）
SEED_TENANT_ID = uuid.UUID("00000000-0000-4000-8000-0000000000aa")
SEED_TENANT_CODE = "scenario-seed-tenant"

APPLICATION_COLUMNS_0027 = {
    "id", "tenant_id", "name", "key_hash", "kb_ids", "status",
    "rate_limit_per_minute", "daily_token_limit", "daily_message_limit",
    "created_by", "created_at", "disabled_at",
}
"""`integration_applications` 在 0027 的列集合（用来断言降级把 `scenario_id` 删干净）。"""


# ══════════════════════════════════════════════════════════════════════
# 连接 / 迁移
# ══════════════════════════════════════════════════════════════════════


def dsn(url: str | None = None) -> str:
    """把 SQLAlchemy 的 `+asyncpg` DSN 转成 asyncpg 原生 DSN。"""
    raw = url or SCENARIO_TEST_DB_URL or ""
    return raw.replace("postgresql+asyncpg://", "postgresql://")


def alembic_config(url: str | None = None) -> Config:
    """构造只指向测试库的 alembic Config。

    ★ 生效通道是 `attributes`：`env.py` 读它并**优先于** `get_settings().database_url`。
      只设 `set_main_option` 会被 env.py 里的兜底赋值覆盖 —— 那样
      `migrate("0027", downgrade=True)` 打的是 `.env` 指向的库（那次未遂事故）。
      两个都设，是为了让「万一 attributes 通道被删掉」时至少还剩一条线索。
    """
    config = Config()
    config.set_main_option("script_location", os.path.join(_API_ROOT, "alembic"))
    target = url or SCENARIO_TEST_DB_URL or ""
    config.attributes["sqlalchemy_url"] = target
    config.set_main_option("sqlalchemy.url", target)
    return config


def migrate(revision: str, *, downgrade: bool = False) -> None:
    """同步执行迁移（`env.py` 用 `asyncio.run`，故不能在事件循环内调用）。"""
    if downgrade:
        command.downgrade(alembic_config(), revision)
    else:
        command.upgrade(alembic_config(), revision)


def current_revision() -> str | None:
    """读 `alembic_version` 的当前版本；空库 / 无该表 → `None`。"""

    async def _run() -> str | None:
        connection = await asyncpg.connect(dsn())
        try:
            if await connection.fetchval("SELECT to_regclass('public.alembic_version')") is None:
                return None
            return await connection.fetchval("SELECT version_num FROM alembic_version")
        finally:
            await connection.close()

    return asyncio.run(_run())


def ensure_seed_tenant() -> int:
    """在 0028 upgrade **之前**插一行 tenant，返回当前租户数。

    ★ 为什么必须这么做：全仓没有任何迁移插入 `tenants`
      （`grep "INSERT INTO tenants" alembic/versions/` 零命中），
      所以新起一个库跑完 0001→0028 后 `tenants=0` → 种子 INSERT 插 0 行。
      此时「种子行数 == 租户数」退化成 `0 == 0`，「读回种子行」直接 skip ——
      两个用例都变成**空转**，种子行的落库值无人验证。
    """

    async def _run() -> int:
        connection = await asyncpg.connect(dsn())
        try:
            exists = await connection.fetchval(
                "SELECT id FROM tenants WHERE code = $1", SEED_TENANT_CODE
            )
            if exists is None:
                await connection.execute(
                    "INSERT INTO tenants (id, name, code) VALUES ($1, $2, $3)",
                    SEED_TENANT_ID,
                    "种子用例租户",
                    SEED_TENANT_CODE,
                )
            return int(await connection.fetchval("SELECT count(*) FROM tenants"))
        finally:
            await connection.close()

    return asyncio.run(_run())


async def seed_counts() -> tuple[int, int, int, int]:
    """`(scenarios, tenants, scenario_versions, 重复 key 组数)`。

    ★ 这里是 **async** 而不是「同步壳 + `asyncio.run`」：调用方
      （`test_0028_pg_constraints.py` 的迁移往返用例）自己写的是
      `asyncio.run(_seed_counts())`，那是**同步用例**在跑异步逻辑的既有写法。
      如果这里也做成同步壳，`asyncio.run(<tuple>)` 会抛
      「a coroutine was expected」——一个只在重构后才出现、且报错信息完全不指向
      真正原因的坑（重构时踩过一次，留在这里作为警示）。
    """
    connection = await asyncpg.connect(dsn())
    try:
        scenarios = int(await connection.fetchval("SELECT count(*) FROM scenarios"))
        tenants = int(await connection.fetchval("SELECT count(*) FROM tenants"))
        versions = int(await connection.fetchval("SELECT count(*) FROM scenario_versions"))
        dup = int(
            await connection.fetchval(
                "SELECT count(*) FROM ("
                "  SELECT tenant_id, scenario_key FROM scenarios"
                "  GROUP BY 1,2 HAVING count(*) > 1) t"
            )
        )
        return scenarios, tenants, versions, dup
    finally:
        await connection.close()


# ══════════════════════════════════════════════════════════════════════
# 真库前置（**普通函数**，不是 fixture）
# ══════════════════════════════════════════════════════════════════════
#
# ★ 为什么这里不直接定义 fixture、交给 `conftest.py` import 进去：
#   `conftest` 里还有一个 `async def api(schema)` —— 参数名 `schema` 会与
#   import 进来的 `schema` **同名遮蔽**，`ruff` 的 F811（Redefinition of unused）
#   会直接报错，而为了消掉它去改 import 别名，等于让「夹具名」与「导入名」分叉。
#   所以分工是：**实现**在本模块（唯一口径），**fixture 壳**在 conftest（pytest 要的注册点）。
#   这样两边都不重复实现，也不产生命名冲突。


def prepare_schema() -> str:
    """把测试库推到 **head**（当前 0029），且**保证至少一个租户在 0028 之前就存在**。

    ★ 为什么基线是 `head` 而不是固定的 `"0028"`（Step 4B-1 改）：
      4B-1 让 `write_service.publish()` 在发布时算口径快照，而那份快照调的是
      0029 的 `fn_chunk_reindex_candidates`。基线若停在 0028，**4A 的每一条
      publish 用例都会以 `UndefinedFunctionError` 500** —— 那个红与它们要断言的
      状态机毫无关系，而且不会被任何人当成"基线选错了"，只会被当成"4B 改坏了 4A"。
      用 `head` 而不是把 `"0029"` 再写死一次：下次加迁移时同一个坑不会重演。
      ★ 这不是"放宽既有断言"：4A 的断言一字未改，只是它们脚下的库升到了 head。
      ★ 它同时暴露一个**部署顺序事实**（已登记到交付报告）：代码必须先迁移后上线，
        否则 publish 会 500。这是"W5 与脚本必须同源"（I-4B-2）的直接代价，不是缺陷。

    ★ 三步的顺序是有讲究的，每一步都对应一次踩过的坑：
      1. 库若已在 0028（上一次跑完留下的），先降回 0027 —— 否则后面的
         `upgrade(head)` 是部分 no-op，种子 INSERT **根本不会执行**，
         而「读回种子行」的用例会以「种子租户没有内置场景」的形式莫名失败，
         甚至更糟：拿上一次运行留下的旧行当成本次迁移的产物来断言。
      2. 插一行 tenant —— 必须在 0028 之前，否则种子 INSERT 无从 SELECT 出任何行。
      3. `assert tenant_count >= 1` 前置门禁：没有它，「种子行数 == 租户数」
         会退化成 `0 == 0`，用例绿得毫无信息。
    """
    current = current_revision()
    if current is not None and current >= "0028":
        migrate("0027", downgrade=True)
    migrate("0027")
    tenant_count = ensure_seed_tenant()
    assert tenant_count >= 1, "前置赛据失败：测试库里没有 tenants 行"
    migrate("head")
    return SCENARIO_TEST_DB_URL or ""


@contextlib.asynccontextmanager
async def rolled_back_conn(url: str) -> AsyncIterator[asyncpg.Connection]:
    """一条连接 + 一个最终回滚的事务（用例互不污染）。

    ★ 这是给「直接写 SQL 违反约束」那类用例用的：它们在事务内造数据然后回滚。
      **写接口用例 MUST NOT 用它** —— handler 内部有 `commit()`，外层事务一提交
      就再也回滚不掉，用例之间会互相污染（那才是这类测试最经典的假绿来源）。
    """
    connection = await asyncpg.connect(dsn(url))
    transaction = connection.transaction()
    await transaction.start()
    try:
        yield connection
    finally:
        await transaction.rollback()
        await connection.close()


# ══════════════════════════════════════════════════════════════════════
# 构造工具（conn 版：跑在调用方的事务里，随外层回滚消失）
# ══════════════════════════════════════════════════════════════════════


async def new_tenant(conn) -> uuid.UUID:
    tenant_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO tenants (id, name, code) VALUES ($1, $2, $3)",
        tenant_id,
        "测试租户",
        f"t-{tenant_id}",
    )
    return tenant_id


async def new_user(conn, tenant_id: uuid.UUID) -> uuid.UUID:
    user_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO users (id, tenant_id, username, password_hash, display_name) "
        "VALUES ($1,$2,$3,$4,$5)",
        user_id, tenant_id, f"u-{str(user_id)[:8]}", "x", "测试用户",
    )
    return user_id


async def new_scenario(
    conn, tenant_id: uuid.UUID, *, is_builtin: bool = False, key: str | None = None
) -> uuid.UUID:
    scenario_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO scenarios (id, tenant_id, scenario_key, name, is_builtin) "
        "VALUES ($1, $2, $3, $4, $5)",
        scenario_id,
        tenant_id,
        key or f"s_{str(scenario_id)[:8]}",  # 满足 ^[a-z][a-z0-9_]{1,63}$（连字符不在允许集内）
        "测试场景",
        is_builtin,
    )
    return scenario_id


async def insert_version(
    conn,
    tenant_id: uuid.UUID,
    scenario_id: uuid.UUID,
    *,
    version: int = 1,
    state: str = "draft",
    policy: dict | None = None,
    kb_ids: list | None = None,
    binding_mode: str = "restricted_placeholder",
    validated_at: datetime | None = None,
    published_at: datetime | None = None,
    dna: dict | None = None,
    checksum: str = "0123456789abcdef",
) -> None:
    """插入一条版本行 —— **永远给全列**，绝不依赖 server_default。

    ★ 依赖 server_default 会让目标约束之外的另一条约束先炸：
      `binding_mode` 默认 `'explicit'` + `kb_ids` 默认 `'[]'` 必然违反
      `ck_scenario_versions_binding_consistent`，于是「断言任意 CheckViolation」
      的用例永远成立、目标约束删掉也照样绿。

    `binding_mode="restricted_placeholder"` 是哨兵：按 `kb_ids` 是否为空自动推导，
    调用方只有在**故意**构造不一致时才显式传 `binding_mode=`。
    """
    if dna is None:
        from tests.scenario.conftest import load_migration

        dna = load_migration()._SEED_DNA
    if policy is None:
        policy = {}
    if kb_ids is None:
        kb_ids = []
    if binding_mode == "restricted_placeholder":
        binding_mode = "unrestricted" if not kb_ids else "explicit"
    await conn.execute(
        "INSERT INTO scenario_versions "
        "(tenant_id, scenario_id, version, state, policy, kb_ids, binding_mode, "
        " dna_declared, checksum, validated_at, published_at) "
        "VALUES ($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7,$8::jsonb,$9,$10,$11)",
        tenant_id,
        scenario_id,
        version,
        state,
        json.dumps(policy),
        json.dumps(kb_ids),
        binding_mode,
        json.dumps(dna),
        checksum,
        validated_at,
        published_at,
    )


async def new_application(
    conn,
    tenant_id: uuid.UUID,
    *,
    created_by: uuid.UUID,
    scenario_id: uuid.UUID | None = None,
    name: str | None = None,
) -> uuid.UUID:
    application_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO integration_applications "
        "(id, tenant_id, name, key_hash, kb_ids, rate_limit_per_minute, "
        " daily_token_limit, daily_message_limit, created_by, scenario_id) "
        "VALUES ($1,$2,$3,$4,'[]'::jsonb,60,1000,100,$5,$6)",
        application_id, tenant_id, name or f"app-{str(application_id)[:8]}",
        f"kh-{application_id}", created_by, scenario_id,
    )
    return application_id


async def expect_violation(conn, exc_type, constraint: str, action) -> None:
    """断言 `action()` **恰好**因 `constraint` 这条约束被拒（SQLSTATE 之外再钉名字）。"""
    with pytest.raises(exc_type) as exc_info:
        async with conn.transaction():  # 嵌套事务 = savepoint，外层不受污染
            await action()
    assert exc_info.value.constraint_name == constraint, (
        f"期望被 {constraint} 拒绝，实际是 "
        f"{exc_info.value.constraint_name!r}（sqlstate="
        f"{exc_info.value.sqlstate}）"
    )


async def fresh(conn):
    """新建 (租户, 场景)，供每个用例独立使用。"""
    tenant_id = await new_tenant(conn)
    scenario_id = await new_scenario(conn, tenant_id)
    return tenant_id, scenario_id


# ══════════════════════════════════════════════════════════════════════
# 构造工具（committed 版：立即提交、独立连接 —— 给「走 API 的用例」用）
# ══════════════════════════════════════════════════════════════════════
#
# ★ 为什么写接口用例必须用 committed 版：handler 内部会 `commit()`。
#   若外层包一个事务、期望最后 rollback，handler 的 commit 会把它一起提交，
#   用例之间开始互相污染 —— 而这**不会让用例变红**，只会让「同租户已有同名 key」
#   这类依赖变成了不可复现的偶发失败。代价是数据留在库里，所以容器用完即弃。


async def _exec_committed(sql: str, *args):
    connection = await asyncpg.connect(dsn())
    try:
        return await connection.fetch(sql, *args)
    finally:
        await connection.close()


async def committed_tenant() -> uuid.UUID:
    """新建并提交一个租户（**每个用例一个**，提交真实落库、不清理）。"""
    tenant_id = uuid.uuid4()
    await _exec_committed(
        "INSERT INTO tenants (id, name, code) VALUES ($1, $2, $3)",
        tenant_id, "写接口用例租户", f"w-{tenant_id}",
    )
    return tenant_id


async def committed_user(tenant_id: uuid.UUID) -> uuid.UUID:
    user_id = uuid.uuid4()
    await _exec_committed(
        "INSERT INTO users (id, tenant_id, username, password_hash, display_name) "
        "VALUES ($1,$2,$3,$4,$5)",
        user_id, tenant_id, f"u-{str(user_id)[:8]}", "x", "写接口用例用户",
    )
    return user_id


async def committed_kb(tenant_id: uuid.UUID, *, owner_id: uuid.UUID | None = None) -> uuid.UUID:
    """提交一个知识库行。

    ★ `is_public` 必须留 `false`：`uq_kb_single_public` 是**全库**唯一索引
      （`WHERE is_public`），第二个 public 库在任何租户下都插不进去 ——
      于是「每个用例一个独立租户」这个隔离策略会被一个全局唯一索引打破。
    """
    kb_id = uuid.uuid4()
    await _exec_committed(
        "INSERT INTO knowledge_bases (id, tenant_id, name, description, owner_id) "
        "VALUES ($1,$2,$3,$4,$5)",
        kb_id, tenant_id, f"kb-{str(kb_id)[:8]}", "写接口用例知识库", owner_id,
    )
    return kb_id


async def committed_scenario(
    tenant_id: uuid.UUID, *, is_builtin: bool = False, key: str | None = None
) -> uuid.UUID:
    """提交一个场景行（用于「内置场景」这类**写接口无法创建**的前置）。

    ★ 内置场景必须用 SQL 造：`is_builtin=true` 只有迁移种子会写，
      写接口永远产出 `is_builtin=False`（这正是 W1 的设计）。想测「内置不可改」
      只能在 DB 层先造一个 —— 而它也恰好模拟了「存量租户的种子行」这一真实形态。
    """
    scenario_id = uuid.uuid4()
    await _exec_committed(
        "INSERT INTO scenarios (id, tenant_id, scenario_key, name, is_builtin) "
        "VALUES ($1,$2,$3,$4,$5)",
        scenario_id, tenant_id, key or f"s_{str(scenario_id)[:8]}", "写接口用例场景", is_builtin,
    )
    return scenario_id


async def committed_builtin_scenario(tenant_id: uuid.UUID, *, version: int = 1) -> uuid.UUID:
    """提交一个**内置**场景 + 一条已发布版本，**逐字段复刻迁移 0028 的种子行**。

    ★ 为什么要复刻而不能随便插：`build_snapshot` 会对内置行做 S-9 漂移检测
      （`dna_declared` vs 平台 `current_dna()`），而 `checksum` 也被
      `ck_..._checksum_format` 约束。任何一处与种子不同，读路径就会走成
      「内置行不一致 → UNRESOLVABLE」，于是用例以 500 的形式"莫名失败"，
      而真实原因只是造数据的时候少抄了一个字段。
      `policy` 空 + `unrestricted` + `kb_ids=[]` 是种子的三条硬事实（D-8）。
    """
    from app.services.scenario.dna_declare import build_dna_declared
    from app.services.scenario.snapshot import compute_checksum

    dna = build_dna_declared()
    checksum = compute_checksum(
        policy={}, kb_ids=[], binding_mode="unrestricted", dna_declared=dna
    )
    scenario_id = await committed_scenario(tenant_id, is_builtin=True, key="default")
    await _exec_committed(
        "INSERT INTO scenario_versions "
        "(tenant_id, scenario_id, version, state, policy, kb_ids, binding_mode, "
        " dna_declared, checksum, change_note, published_at) "
        "VALUES ($1,$2,$3,'published','{}'::jsonb,'[]'::jsonb,'unrestricted',"
        "        $4::jsonb,$5,'空覆盖集（内置场景即平台默认）', now())",
        tenant_id, scenario_id, version, json.dumps(dna), checksum,
    )
    return scenario_id


async def committed_application(
    tenant_id: uuid.UUID,
    *,
    created_by: uuid.UUID,
    scenario_id: uuid.UUID | None = None,
    kb_ids: Sequence[uuid.UUID | str] = (),
) -> uuid.UUID:
    """提交一个集成应用行（W7 与归档引用检查的前置）。

    ★ `kb_ids` 必须显式给：`_chat_ask_impl` 在集成模式下取
      「应用白名单 ∩ 用户授权 ∩ 请求集合」的**三重交集**（`chat.py:311`），
      空白名单会让每一次 `/integrations/ask` 都 403 FORBIDDEN ——
      而 403 与「场景坏了」在用例里长得完全不一样，很容易被误读成实现 bug。
    """
    application_id = uuid.uuid4()
    await _exec_committed(
        "INSERT INTO integration_applications "
        "(id, tenant_id, name, key_hash, kb_ids, rate_limit_per_minute, "
        " daily_token_limit, daily_message_limit, created_by, scenario_id) "
        "VALUES ($1,$2,$3,$4,$5::jsonb,60,1000,100,$6,$7)",
        application_id, tenant_id, f"app-{str(application_id)[:8]}",
        f"kh-{application_id}", json.dumps([str(kb) for kb in kb_ids]),
        created_by, scenario_id,
    )
    return application_id


async def committed_rows(sql: str, *args) -> list[dict]:
    """用**独立连接**回读库（不经 ORM / 不经缓存）—— 断言「库里到底是什么」。"""
    rows = await _exec_committed(sql, *args)
    return [dict(row) for row in rows]


async def committed_scalar(sql: str, *args):
    rows = await _exec_committed(sql, *args)
    return rows[0][0] if rows else None
