"""场景化配置（Step 1–2）测试的公共习语。

两层结构，与仓库既有做法一致（见 tests/graph/test_graph_recall_selective_integration.py）：

  (a) **NO-DB 层（必跑）**：纯函数 / 静态断言 / 假 Session。解析真值表、DNA 来源、
      policy 校验、SQL 文本、MCP 契约全部在这一层可验。
  (b) **Real-DB 层（`SCENARIO_TEST_DB_URL` 缺失则整体 skip）**：迁移的 upgrade /
      downgrade、CHECK 约束、部分唯一索引、种子行落库值。

★ 本目录的测试**不调用任何云端 / 大模型接口**：凡是需要 embedding 或 LLM 的地方
  一律走假实现或纯函数（用户明令）。
"""
from __future__ import annotations

import importlib.util
import pathlib
import uuid
from collections.abc import Callable
from typing import Any

import pytest
import pytest_asyncio

from app.models import IntegrationApplication

# ★ 真库相关的一切只在 `pg_helpers.py` 实现一次，这里只做两件事：
#   ① 把写接口用例要用的 committed 构造器转出（`from ... import *` 会带上私有名，故逐个赋值）；
#   ② 定义 `schema` / `conn` 两个 **fixture 壳**（pytest 只在 conftest / 插件里发现夹具）。
#   为什么不在 `pg_helpers` 里直接写 fixture、再 import 进来：`api(schema)` 的参数名会
#   遮蔽 import 进来的 `schema`，ruff 的 F811 直接报错；改成别名则夹具名与导入名分叉。
from tests.scenario import pg_helpers

SCENARIO_TEST_DB_URL = pg_helpers.SCENARIO_TEST_DB_URL
committed_application = pg_helpers.committed_application
committed_builtin_scenario = pg_helpers.committed_builtin_scenario
committed_kb = pg_helpers.committed_kb
committed_scenario = pg_helpers.committed_scenario
committed_tenant = pg_helpers.committed_tenant
committed_user = pg_helpers.committed_user


# ★ 真库夹具的**壳**在这里、**实现**在 `pg_helpers`：
#   pytest 要求 fixture 定义在 conftest（或插件）里才可见，而 `pg_helpers` 不是 conftest。
#   为什么不用「import fixture 对象」的写法：`api(schema)` 的参数名会遮蔽 import 进来的
#   `schema`，ruff 的 F811 直接报错；改成别名则夹具名与导入名分叉，更难维护。
@pytest.fixture(scope="module")
def schema() -> str:
    """把测试库推到 head（实现与推导见 `pg_helpers.prepare_schema`）。

    ★ 为什么这里必须有 skip 守卫（2026-11 实证，别删）：
      本夹具**直接连库**，而 `SCENARIO_TEST_DB_URL` 缺失时
      `pg_helpers.dsn()` 回落空串 → `asyncpg.connect("")` 去连 localhost:5432，
      结果是 **ERROR（ConnectionRefusedError）而不是 skip**。
      那些用例（`test_0029_migration.py` 的三条 `test_real_db_*`）当时漏挂了
      `@requires_db`，于是「没有库」在报告里表现为「测试坏了」——
      与「缺环境应 skip」的纪律相反，也让任何人的本地全量永远带 3 条 error。
      ★ 修在夹具层而不是逐个补 `@requires_db`：漏挂装饰器是**必然会发生**的，
      而「依赖 `schema` 就必须有库」是**结构性事实**，把判据放在唯一知道这件事的地方，
      下一个新增真库用例的人不必记得补装饰器。与同文件 `api` 夹具（下方 `if not
      SCENARIO_TEST_DB_URL: pytest.skip(...)`）是同一形状。
    """
    if not SCENARIO_TEST_DB_URL:
        pytest.skip("needs SCENARIO_TEST_DB_URL (real PG with 0028 applied)")
    return pg_helpers.prepare_schema()


@pytest_asyncio.fixture()
async def conn(schema):
    """事务内造数据、结束回滚（实现见 `pg_helpers.rolled_back_conn`）。"""
    async with pg_helpers.rolled_back_conn(schema) as connection:
        yield connection

_MIGRATION_PATH = (
    pathlib.Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0028_add_scenarios.py"
)


def load_migration():
    """以文件路径导入迁移 0028（文件名以数字开头，不能按模块名 import）。"""
    spec = importlib.util.spec_from_file_location("scenario_migration_0028", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def migration():
    """迁移 0028 模块（会话级：模块级只有常量与纯函数，无 IO）。"""
    return load_migration()


@pytest.fixture(autouse=True)
def clear_scenario_cache():
    """每个用例前后清空 resolver 的行缓存。

    ★ 这不是「为了让测试好过」：行缓存是**进程内生产设施**（TTL 60s），
      用例之间共享它会让「删行后仍返回旧行」看起来像逻辑错误。
      等价于让 TTL 立即到期，是缓存本来的生命周期的一部分。
    """
    from app.services.scenario.resolver import clear_cache

    clear_cache()
    yield
    clear_cache()


# ══════════════════════════════════════════════════════════════════════
# 假 Session：只实现 resolver 用到的那一面（execute → all → _mapping）
# ══════════════════════════════════════════════════════════════════════


class FakeRow:
    """模拟 SQLAlchemy `Row`：解析只用到 `._mapping` 的按键取值。"""

    def __init__(self, mapping: dict[str, Any]) -> None:
        self._mapping = mapping


class FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)


class FakeSession:
    """按脚本返回「场景 + 已发布版本」行的假 Session。

    `rows` 为空 = 查不到（覆盖 R1 的「无 published 版本」与 R5 的「内置行缺失」）。
    """

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = list(rows or [])
        self.statements: list[Any] = []

    async def execute(self, stmt) -> FakeResult:
        self.statements.append(stmt)
        return FakeResult([FakeRow(row) for row in self.rows])


# ══════════════════════════════════════════════════════════════════════
# 行工厂
# ══════════════════════════════════════════════════════════════════════

TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
SCENARIO_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
KB_A = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
KB_B = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000002")


def build_row(
    *,
    tenant_id: uuid.UUID = TENANT_ID,
    scenario_id: uuid.UUID = SCENARIO_ID,
    scenario_key: str = "default",
    status: str = "active",
    is_builtin: bool = True,
    db_binding: str | None = None,
    version: int = 1,
    policy: dict[str, Any] | None = None,
    kb_ids: tuple[str, ...] = (),
    binding_mode: str = "unrestricted",
    dna_declared: dict[str, Any] | None = None,
    checksum: str = "0123456789abcdef",
) -> dict[str, Any]:
    """构造一行「场景 + 已发布版本」的查询结果（键名与 LOOKUP_SQL 的别名一致）。"""
    return {
        "scenario_id": scenario_id,
        "tenant_id": tenant_id,
        "scenario_key": scenario_key,
        "status": status,
        "is_builtin": is_builtin,
        "db_binding": db_binding,
        "version": version,
        "policy": dict(policy or {}),
        "kb_ids": list(kb_ids),
        "binding_mode": binding_mode,
        "dna_declared": dict(
            dna_declared
            if dna_declared is not None
            else {
                "embedding_text_version": "etext-v1",
                "chunker_version": "c400-800-60",
                "graph_prompt_version": "gprompt-v1",
                "wiki_prompt_version": "wprompt-v1",
                "env_bound": [
                    "embedding_model",
                    "embedding_dim",
                    "graph_extractor_model",
                    "wiki_generator_model",
                ],
            }
        ),
        "checksum": checksum,
    }


def make_application(
    *,
    scenario_id: uuid.UUID | None = None,
    application_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID = TENANT_ID,
    kb_ids: tuple[str, ...] = (),
) -> IntegrationApplication:
    """构造真实 ORM 对象（未 flush，未设置的列取 None —— 正是「存量应用」的形态）。"""
    return IntegrationApplication(
        id=application_id or uuid.UUID("44444444-4444-4444-4444-444444444444"),
        tenant_id=tenant_id,
        name="测试应用",
        key_hash="0" * 64,
        kb_ids=list(kb_ids),
        scenario_id=scenario_id,
        rate_limit_per_minute=60,
        daily_token_limit=200_000,
        daily_message_limit=1_000,
        created_by=uuid.UUID("55555555-5555-5555-5555-555555555555"),
    )


@pytest.fixture()
def application_factory() -> Callable[..., IntegrationApplication]:
    return make_application


# ══════════════════════════════════════════════════════════════════════
# Step 4A：写接口的 HTTP 级夹具（真库 + ASGI 直连，不启 lifespan）
# ══════════════════════════════════════════════════════════════════════


class Step4Env:
    """一次写接口用例的「世界」：一个租户 + 一个管理员 + 一个知识库。

    ★ 为什么每个用例独立租户（而不是外层事务回滚）：
      handler 内部会 `commit()`。外面包一个事务并指望最后 rollback，会被 handler
      的 commit 一起提交 —— 用例之间开始互相污染，而且**不会变红**，
      只会让「同租户已有同名 key」这类前置变成不可复现的偶发失败。
      代价是数据留在库里，所以容器用完即弃。
    ★ 为什么 `kb_ids` 是真实知识库行：`write_service._require_kb_ids` 会校验
      知识库存在且属于本租户 —— 用假 UUID 会让所有 W2/W3 退化成 400。
    """

    def __init__(self, client, session_factory, *, tenant_id, user_id, kb_ids):
        self.client = client
        self.session_factory = session_factory
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.kb_ids = list(kb_ids)

    def as_user(self, tenant_id: uuid.UUID, user_id: uuid.UUID) -> None:
        """切换「当前登录用户」（跨租户用例用：A 租户去动 B 租户的资源）。"""
        _CURRENT["user"] = _make_admin(tenant_id, user_id)

    @property
    def user(self):
        """当前登录用户（`CurrentUser` 实例）。

        ★ 集成侧用例需要它：`/integrations/ask` 的 principal 同时带
          「应用」与「真实用户」两样东西，而 override 掉的鉴权依赖必须返回
          **同一个** `CurrentUser`，否则 `tenant_id` 与 `write_service` 用的那个不一致，
          场景就查不到了（而且报的是 404 而不是「租户不匹配」，很容易误判成实现 bug）。
        """
        return _CURRENT["user"]


_CURRENT: dict[str, Any] = {}


def _make_admin(tenant_id: uuid.UUID, user_id: uuid.UUID):
    from app.api.deps import CurrentUser

    return CurrentUser(
        user_id=user_id,
        tenant_id=tenant_id,
        username="scenario-admin",
        display_name="场景管理员",
        clearance=100,  # require_admin 要求 >= 40
        dept_path="/",
        subjects=[],
        authorized_kb_ids=[],
        acl_epoch=1,
    )


@pytest_asyncio.fixture()
async def api(schema):
    """真库 + ASGI 直连的写接口夹具（`SCENARIO_TEST_DB_URL` 缺失则 skip）。

    ★ `ASGITransport` 不跑 lifespan —— 这是**故意**的，且是硬要求（设计 §D.1.2）：
      本机没有 Redis，`main.py` 的 lifespan 里有 arq 连接池、重排探活、
      `check_scenario_data` 扫描。跑了会让这批用例因为**与场景无关的原因**失败，
      于是下一个人「顺手」把它们标 skip，整批证据消失。
    ★ `poolclass=NullPool`：并发用例的两个请求各要一条连接，而 `FOR UPDATE`
      会让其中一个等待。连接池若只有 1~2 条，测的就不是锁而是池。
    """
    if not SCENARIO_TEST_DB_URL:
        pytest.skip("needs SCENARIO_TEST_DB_URL (real PG with 0028 applied)")

    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.api.deps import get_current_user, get_db
    from app.main import app

    engine = create_async_engine(SCENARIO_TEST_DB_URL, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _get_db():
        async with session_factory() as session:
            yield session

    async def _current_user():
        return _CURRENT["user"]

    tenant_id = await committed_tenant()
    user_id = await committed_user(tenant_id)
    kb_id = await committed_kb(tenant_id, owner_id=user_id)
    _CURRENT["user"] = _make_admin(tenant_id, user_id)

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user] = _current_user
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            yield Step4Env(
                client, session_factory,
                tenant_id=tenant_id, user_id=user_id, kb_ids=[kb_id],
            )
    finally:
        app.dependency_overrides.clear()
        _CURRENT.clear()
        await engine.dispose()


@pytest_asyncio.fixture()
async def other_kb(api) -> uuid.UUID:
    """同一租户下的第二个知识库（用于断言「kb_ids 整体替换」而不是追加）。"""
    return await committed_kb(api.tenant_id, owner_id=api.user_id)
