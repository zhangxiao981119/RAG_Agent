"""数据层路由（Step 5）：库位键 → 引擎/会话的唯一入口。

三件事必须清楚：

1. **关闭态不建任何引擎**：`SCENE_DB_SPLIT_ENABLED=false`（默认）时，
   `register_from_settings()` 只登记默认绑定，绝不调用 `_build_engine()`；
   `engine_for("default")` 返回的仍是 `app.database.engine` 这个**同一个对象**
   （`is` 恒等），因此关闭态与未引入路由层之前完全等价。

2. **不进请求路径（除唯一收口处）**：本模块只提供接线件，`data_session()` 在 app/ 内
   只有一个调用点 —— `app/services/db_scope.py`（请求层数据面入口）。
   由 tests/scenario/test_step5_db_router.py 的 T-5-7 断言"调用点白名单 + 只定义一次"，
   防止第二个模块绕过 db_scope 直接开会话。

3. **解析与可用性分离**：`resolve_db_key()` 不校验注册表、不读 settings，
   它只负责把 `db_binding` 归一成库位键；键是否存在、DSN 是否可用由
   `_binding_for()` 在真正取引擎时判定（未注册 → fail-closed，**不回落默认库**）。
   这样"某场景绑定了 kb_x 但本进程没配 kb_x"会在取引擎处炸，而不是静默查错库。
"""
from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app import database as app_database
from app.config import placeholders
from app.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

DEFAULT_DB_KEY: Final[str] = "default"

# 库位键口径（与 settings.scene_db_dsn 的 field_validator、ck_scenarios_db_binding_format
# 同口径）。本模块**不用它做校验**：解析（resolve_db_key）与可用性（_binding_for）
# 分离，键格式由配置校验与 DB 约束负责，路由层只认"注册没注册"。
_KEY_PATTERN = re.compile(r"^kb_[a-z0-9_]{2,40}$")

DB_ROUTE_KEY_UNKNOWN: Final[str] = "DB_ROUTE_KEY_UNKNOWN"
DB_ROUTE_BUDGET_EXCEEDED: Final[str] = "DB_ROUTE_BUDGET_EXCEEDED"
DB_ROUTE_DSN_INVALID: Final[str] = "DB_ROUTE_DSN_INVALID"

__all__ = [
    "DEFAULT_DB_KEY",
    "DB_ROUTE_KEY_UNKNOWN",
    "DB_ROUTE_BUDGET_EXCEEDED",
    "DB_ROUTE_DSN_INVALID",
    "DbRouteError",
    "RouterReport",
    "resolve_db_key",
    "register_from_settings",
    "registered_db_keys",
    "engine_for",
    "data_session",
    "check_connection_budget",
    "log_db_router_banner",
]


class DbRouteError(Exception):
    """路由层错误：带机器可读的 code 与结构化 detail（便于日志/指标断言）。"""

    def __init__(self, code: str, detail: dict[str, object] | None = None) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def resolve_db_key(db_binding: str | None) -> str:
    """把场景的 `db_binding` 归一成库位键（纯函数、零 IO）。

    - `None` → `"default"`
    - 其它值**原样返回**：不做 strip / 小写 / 格式校验。
      校验由配置侧与 DB 约束负责；这里若偷偷规范化，会把
      "配置写了 KB_Finance 但注册表里没有" 变成"悄悄落默认库"。
    """
    if db_binding is None:
        return DEFAULT_DB_KEY
    return db_binding


@dataclass(frozen=True, slots=True)
class _EngineBinding:
    """一个库位的绑定：引擎 + 会话工厂 + 来源说明（来源只写名字，绝不带 DSN）。"""

    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    source: str


# 默认绑定直接复用 app.database 的模块级对象：关闭态下"路由层存在"与"不存在"等价。
_DEFAULT_BINDING = _EngineBinding(
    engine=app_database.engine,
    sessionmaker=app_database.SessionLocal,
    source="default",
)

_REGISTRY: dict[str, _EngineBinding] = {}
# 成功 create_async_engine 的次数（报告的唯一证据：关闭态必须为 0）。
_CREATED_ENGINES: int = 0


def _build_engine(dsn: str) -> AsyncEngine:
    """按 app/database.py 完全同源的参数建异步引擎。

    失败包成 DbRouteError(DSN_INVALID)；成功则 _CREATED_ENGINES +1
    （**每一个** create_async_engine 调用点都必须在这里，计数才是可信的）。
    """
    global _CREATED_ENGINES
    s = get_settings()
    try:
        engine = create_async_engine(
            dsn,
            pool_pre_ping=True,
            pool_size=s.db_pool_size,
            max_overflow=s.db_max_overflow,
            pool_recycle=1800,  # 与 app/database.py 同源：30 分钟主动回收
            pool_timeout=s.db_pool_timeout_s,
            connect_args={"server_settings": {"plan_cache_mode": "force_custom_plan"}},
        )
    except Exception as exc:
        # detail 只放异常类型与消息，不重复 DSN 明文
        raise DbRouteError(
            DB_ROUTE_DSN_INVALID,
            {"error": type(exc).__name__, "message": str(exc)},
        ) from exc
    _CREATED_ENGINES += 1
    return engine


def _arithmetic(s: Settings) -> tuple[int, int, int, int]:
    """连接预算的**唯一算术来源**（闸门与报告都调它，禁止各写一份）。

    返回 `(per_bucket, k, needed, budget)`：

    - `per_bucket = db_pool_size + db_max_overflow`：一个应用副本在一个库位上
      可能占满的连接数；
    - `k = 1 + len(scene_db_dsn)`（分流关闭时恒为 1）：库位数量；
    - `needed = per_bucket × db_replicas_expected × k + db_worker_conn_total`；
    - `budget = db_max_connections_for_budget - db_conn_reserved`。

    纯算术、零 IO —— 单测可以直接传构造好的 Settings 断言数字。
    """
    per_bucket = s.db_pool_size + s.db_max_overflow
    k = 1 + len(s.scene_db_dsn) if s.scene_db_split_enabled else 1
    needed = per_bucket * s.db_replicas_expected * k + s.db_worker_conn_total
    budget = s.db_max_connections_for_budget - s.db_conn_reserved
    return per_bucket, k, needed, budget


def _binding_for(db_key: str) -> _EngineBinding:
    """按库位键取绑定；未注册 → fail-closed（**禁止回落默认引擎**）。

    回落默认引擎会让"绑定了 kb_x 但没配 kb_x"变成"悄悄查默认库、返回一半结果"，
    这比启动/请求期直接报错难发现得多。
    """
    binding = _REGISTRY.get(db_key)
    if binding is not None:
        return binding
    if db_key == DEFAULT_DB_KEY:
        return _DEFAULT_BINDING
    raise DbRouteError(
        DB_ROUTE_KEY_UNKNOWN,
        {"db_key": db_key, "registered": sorted(_REGISTRY)},
    )


@dataclass(frozen=True, slots=True)
class RouterReport:
    """一次注册后的路由快照（供启动横幅与单测断言，不含任何 DSN）。"""

    bucket_count: int
    per_bucket_connections: int
    total_connections: int
    needed_connections: int
    budget: int
    keys: tuple[str, ...]
    created_engines: int
    notes: tuple[str, ...]


def register_from_settings() -> RouterReport:
    """按当前配置重注册全部库位。

    关闭态：只登记默认绑定，**绝不建引擎**（关闭态创建引擎会白占连接，
    也会让"关闭态绝不触达其它库"的承诺失效）。
    开启态：按 sorted(scene_db_dsn) 逐个建引擎并登记，再登记默认绑定。
    """
    s = get_settings()
    _REGISTRY.clear()

    if not s.scene_db_split_enabled:
        _REGISTRY[DEFAULT_DB_KEY] = _DEFAULT_BINDING
        return _report((DEFAULT_DB_KEY,))

    for key, dsn in sorted(s.scene_db_dsn.items()):
        engine = _build_engine(dsn)
        _REGISTRY[key] = _EngineBinding(
            engine=engine,
            sessionmaker=async_sessionmaker(engine, expire_on_commit=False),
            source=f"env:SCENE_DB_DSN[{key}]",
        )
    _REGISTRY[DEFAULT_DB_KEY] = _DEFAULT_BINDING
    return _report((DEFAULT_DB_KEY, *sorted(s.scene_db_dsn)))


def _report(keys: tuple[str, ...]) -> RouterReport:
    """按当前配置与注册表生成报告（算术全部来自 _arithmetic）。"""
    s = get_settings()
    per_bucket, k, needed, budget = _arithmetic(s)
    total = per_bucket * k
    notes: list[str] = []
    if not s.scene_db_split_enabled and s.scene_db_dsn:
        notes.append("分流关闭：SCENE_DB_DSN 已配置但不生效，全部落默认库")
    return RouterReport(
        bucket_count=k,
        per_bucket_connections=per_bucket,
        total_connections=total,
        needed_connections=needed,
        budget=budget,
        keys=keys,
        created_engines=_CREATED_ENGINES,
        notes=tuple(notes),
    )


def registered_db_keys() -> tuple[str, ...]:
    """当前进程已注册的库位键（含 default，排序）。

    ★ 纯读注册表：不读开关、不建引擎、不查库。
      Step 5 的关闭态只有 default 一个键，因此返回值恒为 `("default",)`；
      顺序探测（D-S6-3-2）未来只需遍历本函数，不必反推配置。
    """
    return tuple(sorted({DEFAULT_DB_KEY, *_REGISTRY}))


def engine_for(db_key: str) -> AsyncEngine:
    """取某库位的引擎。

    ★ 函数体内不读 settings、不读开关、不建引擎：库位选择在注册期完成，
      请求期只做一次 dict 查找 —— 否则请求路径会跟着配置漂移。
    """
    return _binding_for(db_key).engine


@asynccontextmanager
async def data_session(db_key: str) -> AsyncIterator[AsyncSession]:
    """按库位开一个会话（异步上下文）。

    ★ 函数体内同样不读 settings / 开关：路由结果只取决于注册表，
      保证"同一进程内库位解析"是确定的。
    """
    binding = _binding_for(db_key)
    async with binding.sessionmaker() as session:
        yield session


def check_connection_budget() -> None:
    """启动期连接预算闸门：超预算拒绝启动（纯算术、零 IO）。

    边界：`needed == budget` **允许**通过（用 > 而不是 >=）——
    预算本身就是"恰好用完"的上限。
    """
    s = get_settings()
    per_bucket, k, needed, budget = _arithmetic(s)
    if needed > budget:
        raise RuntimeError(
            "数据层连接预算超限，拒绝启动："
            f"{per_bucket}×{s.db_replicas_expected}(副本)×{k}(库位)"
            f"+{s.db_worker_conn_total}(worker) = {needed} > {budget}；"
            f"max_connections 预算 = {s.db_max_connections_for_budget} - "
            f"预留 {s.db_conn_reserved} = {budget}。"
            "处置：① 降 DB_POOL_SIZE/DB_MAX_OVERFLOW；② 减库位；③ 减副本。"
            "★ 不要靠调大 max_connections 解决"
        )


async def log_db_router_banner(session_factory) -> None:
    """启动横幅：DB 路由状态 / 桶数与预算 / 实测 max_connections / 站位值。

    - 实测失败只 WARN，**绝不 raise**：横幅是观测，不是启动前置条件；
    - 关闭态也必须打一行：这是"开关当时是开还是关"的唯一留痕；
    - 库位行只打 key 与 source，**永不打印 DSN 值**。
    """
    s = get_settings()
    report = _report(tuple(_REGISTRY) or (DEFAULT_DB_KEY,))

    actual = s.db_max_connections_for_budget
    try:
        async with session_factory() as session:
            raw = await session.scalar(text("SHOW max_connections"))
        actual = int(raw)
    except Exception:
        logger.warning(
            "DB 路由：读取 PG max_connections 失败，退回配置值 %s",
            s.db_max_connections_for_budget,
            exc_info=True,
        )

    if actual != s.db_max_connections_for_budget:
        logger.warning(
            "PG max_connections=%s 与配置 %s 不一致，闸门按较小者计算",
            actual,
            s.db_max_connections_for_budget,
        )

    budget = min(s.db_max_connections_for_budget, actual) - s.db_conn_reserved
    state = "开启" if s.scene_db_split_enabled else "关闭（全部落默认库）"
    logger.info(
        "DB 路由 : %s · 桶=%s · 每桶 %s · 总计 %s · 预算 %s/%s · max_connections 实测=%s",
        state,
        report.bucket_count,
        report.per_bucket_connections,
        report.total_connections,
        report.needed_connections,
        budget,
        actual,
    )

    line = placeholders.render_line("db_router")
    if line:
        logger.info(line)

    if s.scene_db_split_enabled:
        for key in sorted(_REGISTRY):
            if key == DEFAULT_DB_KEY:
                continue
            logger.info("DB 路由库位: %s ← %s", key, _REGISTRY[key].source)


# ── 站位值一致性断言（import 期，fail-fast；与 resolver.py 同款）────────
# 用 Settings.model_fields 的 default 而**不实例化 Settings**：实例化会读 .env，
# 断言就变成"依赖本机配置"，而不是"镜像值必须等于代码默认值"。
_DB_ROUTER_PLACEHOLDERS: Final[dict[str, str]] = {
    "DB_CONN_RESERVED": "db_conn_reserved",
    "DB_WORKER_CONN_TOTAL": "db_worker_conn_total",
    "DB_REPLICAS_EXPECTED": "db_replicas_expected",
}

_registered_names = placeholders.names_for("db_router")
for _placeholder_name, _field_name in _DB_ROUTER_PLACEHOLDERS.items():
    if _placeholder_name not in _registered_names:
        raise RuntimeError(
            f"{_placeholder_name} 是站位值，但未登记在 app/config/placeholders.py —— "
            "未登记的站位值不会出现在启动横幅"
        )
    _mirror = placeholders.value_of(_placeholder_name)
    _default = Settings.model_fields[_field_name].default
    if _mirror != _default:
        raise RuntimeError(
            f"app/config/placeholders.py 里 {_placeholder_name} 的镜像值 "
            f"{_mirror!r} 与 Settings.{_field_name} 的默认值 {_default!r} 不一致 —— "
            "启动横幅会显示一个不存在的值"
        )
