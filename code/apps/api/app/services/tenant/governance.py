"""租户治理的读取侧（Step 7 S7-2）：库白名单 + 策略上限值对象。

三条纪律：

1. **关闭态零查询**：`TENANT_GOVERNANCE_ENABLED=false`（默认）时
   `get_allowed_db_keys` 直接返回 `None`，不发出任何 SQL —— 与 Step 6 等价。
2. **NULL 与空集合是两种语义**：`NULL` = 不限制；`{}` = 全部拒绝。
   两者一旦混淆，要么静默放开全部库位，要么把正常租户全拒。
3. **tenant 行缺失 = 数据损坏**：显式抛 `TenantNotFoundError`，
   绝不回落成「不限制」—— 那会把一次数据事故变成越权。
"""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import placeholders
from app.config.settings import get_settings

CACHE_TTL_SECONDS: float = 60.0
"""库白名单行缓存的 TTL（**站位值**，与场景行缓存同节奏）。"""

# ── 站位值登记断言（import 期，fail-fast；与 scenario/resolver.py 同款）────
_TTL_PLACEHOLDER = "TENANT_DB_SCOPE_CACHE_TTL_SECONDS"

if _TTL_PLACEHOLDER not in placeholders.names_for("tenant"):
    raise RuntimeError(
        f"{_TTL_PLACEHOLDER} 是站位值，但未登记在 app/config/placeholders.py —— "
        "未登记的站位值不会出现在启动横幅"
    )
if placeholders.value_of(_TTL_PLACEHOLDER) != CACHE_TTL_SECONDS:
    raise RuntimeError(
        f"app/config/placeholders.py 里 {_TTL_PLACEHOLDER} 的镜像值 "
        f"{placeholders.value_of(_TTL_PLACEHOLDER)!r} 与本模块的 "
        f"{CACHE_TTL_SECONDS!r} 不一致 —— 启动横幅会显示一个不存在的值"
    )

DB_KEY_RE: Final[re.Pattern[str]] = re.compile(r"^kb_[a-z0-9_]{2,40}$")
"""库位键口径（与 `settings.scene_db_dsn`、`ck_scenarios_db_binding_format` 一致）。"""

DEFAULT_DB_KEY: Final[str] = "default"
"""保留虚拟键：指向现有控制库，不是可配置的库位名。"""


@dataclass(frozen=True, slots=True)
class TenantPolicyCaps:
    """租户对场景策略的**收紧上限**（L1）。全部字段 `None` = 不限制。"""

    recall_top_k_max: int | None = None
    threshold_vector_min: float | None = None
    threshold_rerank_min: float | None = None
    rerank_allowed: bool | None = None

    def is_empty(self) -> bool:
        """是否四项都不限制（空 caps 不参与指纹，保证与 Step 6 指纹一致）。"""
        return (
            self.recall_top_k_max is None
            and self.threshold_vector_min is None
            and self.threshold_rerank_min is None
            and self.rerank_allowed is None
        )

    def declared(self) -> dict[str, object]:
        """只保留显式设置过的键（指纹的规范形态）。"""
        return {
            name: value
            for name, value in (
                ("recall_top_k_max", self.recall_top_k_max),
                ("threshold_vector_min", self.threshold_vector_min),
                ("threshold_rerank_min", self.threshold_rerank_min),
                ("rerank_allowed", self.rerank_allowed),
            )
            if value is not None
        }


EMPTY_CAPS: Final[TenantPolicyCaps] = TenantPolicyCaps()
"""空 caps 单例（不可变，可安全共享为默认值）。"""


class TenantNotFoundError(Exception):
    """`tenant_id` 在 `tenants` 表里查不到（认证上下文与库不一致的数据事故）。"""

    def __init__(self, tenant_id: uuid.UUID) -> None:
        self.tenant_id = tenant_id
        super().__init__(f"tenants 表缺少租户 {tenant_id} —— 认证上下文与库不一致")


_ALLOWED_SQL = text("SELECT allowed_db_keys FROM tenants WHERE id = :tenant_id")
"""读租户库白名单的唯一 SQL（只在开关开启时执行）。"""

# 键 = tenant_id；值 = (写入时刻 monotonic, 白名单或 None)。
# 只缓存命中行：tenant 行缺失必须每次都能被发现，不能被负缓存掩盖。
_ALLOWED_CACHE: dict[uuid.UUID, tuple[float, frozenset[str] | None]] = {}


def validate_db_key(value: str) -> bool:
    """库位键是否合法：`default`（保留虚拟键）或 `kb_<小写字母数字下划线>`。"""
    if not isinstance(value, str):
        return False
    return value == DEFAULT_DB_KEY or bool(DB_KEY_RE.match(value))


def invalidate_allowed_db_keys(tenant_id: uuid.UUID | None = None) -> None:
    """失效缓存：`None` 清全部；否则只清该租户。

    MUST 在治理写接口**事务提交后**调用（事务内调用会在回滚时留下
    「缓存空、库里旧」的错位）。
    """
    if tenant_id is None:
        _ALLOWED_CACHE.clear()
    else:
        _ALLOWED_CACHE.pop(tenant_id, None)


async def get_allowed_db_keys(
    session: AsyncSession, tenant_id: uuid.UUID
) -> frozenset[str] | None:
    """读租户允许使用的库位键集合。

    - 开关关闭：返回 `None` 且**不发查询**（调用方因此不会多开销）；
    - 开关开启：`NULL` → `None`（不限制）；`{}` → 空集合（全部拒绝）；
      数组 → 集合（白名单）。
    """
    if not get_settings().tenant_governance_enabled:
        return None

    cached = _ALLOWED_CACHE.get(tenant_id)
    if cached is not None and time.monotonic() - cached[0] <= CACHE_TTL_SECONDS:
        return cached[1]

    result = await session.execute(_ALLOWED_SQL, {"tenant_id": tenant_id})
    row = result.first()
    if row is None:
        raise TenantNotFoundError(tenant_id)

    raw = row._mapping["allowed_db_keys"]
    allowed = None if raw is None else frozenset(raw)
    _ALLOWED_CACHE[tenant_id] = (time.monotonic(), allowed)
    return allowed


# ══════════════════════════════════════════════════════════════════════
# Step 7 S7-5：治理写侧 / 读侧（管理端专用）
# ══════════════════════════════════════════════════════════════════════
#
# ★ 为什么写侧与读侧都放本模块而不是新开 service 文件：库白名单的读
#   （`get_allowed_db_keys`）与写（本段）必须共享同一个「合法键口径」
#   （`validate_db_key`）与同一个缓存失效函数。拆成两个文件就会出现
#   「读侧允许 default、写侧忘了允许」这种只在一个方向上暴露的错。
#
# ★ 为什么管理端读 `allowed_db_keys` **不走缓存**：缓存的存在意义是请求热路径；
#   管理端要看到「刚才那次 PUT 的结果」。走缓存会让「改完刷一下还是旧值」变成
#   一个 TTL 内的假失败，而排障者会去查写逻辑 —— 那是一条完全错误的线索。


class GovernanceCode(str, Enum):
    """治理面的失败码。值即响应体 `detail.code`（稳定机器键）。"""

    DISABLED = "GOVERNANCE_DISABLED"
    DB_SCOPE_INVALID = "DB_SCOPE_INVALID"
    INVALID = "GOVERNANCE_INVALID"
    NOT_FOUND = "SCENARIO_NOT_FOUND"
    PIN_NOT_PUBLISHED = "PIN_VERSION_NOT_PUBLISHED"
    TIER_HOT = "TIER_HOT_UNSUPPORTED"
    TIER_MISMATCH = "TIER_BINDING_MISMATCH"


_GOVERNANCE_HTTP: Final[dict[GovernanceCode, int]] = {
    GovernanceCode.DISABLED: 400,
    GovernanceCode.DB_SCOPE_INVALID: 400,
    GovernanceCode.INVALID: 400,
    # 与写侧 `SCENARIO_NOT_FOUND` 同为 404：不存在与跨租户共用同一个码，
    # 否则状态码差异本身就是存在性探针。
    GovernanceCode.NOT_FOUND: 404,
    GovernanceCode.PIN_NOT_PUBLISHED: 400,
    GovernanceCode.TIER_HOT: 400,
    GovernanceCode.TIER_MISMATCH: 400,
}

_GOVERNANCE_MSG: Final[dict[GovernanceCode, str]] = {
    GovernanceCode.DISABLED: "租户治理未启用",
    GovernanceCode.DB_SCOPE_INVALID: "库位键不合法",
    GovernanceCode.INVALID: "治理参数不合法",
    GovernanceCode.NOT_FOUND: "场景不存在",
    GovernanceCode.PIN_NOT_PUBLISHED: "只能钉定已发布的版本",
    GovernanceCode.TIER_HOT: "暂不支持 hot 分级",
    GovernanceCode.TIER_MISMATCH: "分级与库绑定状态不一致",
}


def governance_http_status(code: GovernanceCode) -> int:
    """治理码 → HTTP 状态（路由层的唯一映射来源）。"""
    return _GOVERNANCE_HTTP[code]


class GovernanceError(Exception):
    """治理面可预期失败。路由层把它转成 `{code, message, field, context}`。

    ★ 形态与 `scenario/write_errors.py::WriteError` 一致（含 `field` / `context`）：
      读者都是本租户管理员，需要这两项才能自助修配置。
    """

    code: GovernanceCode
    field: str | None
    context: dict[str, Any]

    def __init__(
        self,
        code: GovernanceCode,
        *,
        message_override: str | None = None,
        field: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.field = field
        self.context = dict(context or {})
        super().__init__(message_override or _GOVERNANCE_MSG[code])

    @property
    def message(self) -> str:
        return str(self)


# ── SQL（全部走绑定参数；租户条件写死在每一条语句里）────────────────────

_DB_SCOPE_LOCK_SQL = text(
    "SELECT allowed_db_keys FROM tenants WHERE id = :tenant_id FOR UPDATE"
)
"""写侧取 before 值：`FOR UPDATE` 串行化「两个管理员同时改白名单」。"""

_DB_SCOPE_UPDATE_SQL = text(
    "UPDATE tenants SET allowed_db_keys = CAST(:keys AS text[]) WHERE id = :tenant_id"
)
"""`CAST(:keys AS text[])` 显式声明参数类型：`text()` 的绑定参数没有列类型上下文，
不写 CAST 时 None 与空列表在 PG 侧无法确定数组元素类型。"""

_SCENARIO_COLUMNS = (
    "s.id, s.scenario_key, s.name, s.status, s.db_binding, s.tier, s.pinned_version, "
    "s.recall_top_k_max, s.threshold_vector_min, s.threshold_rerank_min, s.rerank_allowed"
)

_SCENARIO_ROW_SQL = text(
    f"SELECT {_SCENARIO_COLUMNS} FROM scenarios s "
    "WHERE s.id = :scenario_id AND s.tenant_id = :tenant_id"
)
"""单场景读（`get` / `update` 共用）。`tenant_id` 条件固定：跨租户读的唯一防线。"""

_SCENARIO_LIST_SQL = text(
    f"SELECT {_SCENARIO_COLUMNS} FROM scenarios s "
    "WHERE s.tenant_id = :tenant_id "
    "ORDER BY s.is_builtin DESC, s.scenario_key"
)
"""列表：内置场景排最前（运维最常看），其后按 key 稳定排序。"""

_SCENARIO_LOCK_SQL = text(
    f"SELECT {_SCENARIO_COLUMNS} FROM scenarios s "
    "WHERE s.id = :scenario_id AND s.tenant_id = :tenant_id FOR UPDATE"
)

_SCENARIO_VERSION_STATE_SQL = text(
    "SELECT state FROM scenario_versions "
    "WHERE tenant_id = :tenant_id AND scenario_id = :scenario_id AND version = :version"
)
"""钉定前置检查：只读 `state`，不回显整行（不需要 policy）。"""

_PINNED_VERSION_FILTER = (
    "(s.pinned_version IS NULL AND v.state = 'published') "
    "OR (s.pinned_version IS NOT NULL AND v.version = s.pinned_version "
    "AND v.state IN ('published','archived'))"
)
"""治理开启时的版本选择条件 —— 与 `resolver.LOOKUP_GOVERNED_SQL` **同口径**。

★ 复制这两行字符串而不是 import 拼装 resolver 的 SQL：resolver 的 SQL 还带内置分支
  与完整列集，直接复用会把「管理端预览」绑到解析热路径的列布局上；一旦解析 SQL
  为其自身性能裁剪列，预览就会静默缺数据。两处条件语义必须一致这件事由
  `test_step7_api.py` 的钉定用例（真库）来锁，而不是靠共享字符串。
"""

_PUBLISHED_VERSION_FILTER = "v.state = 'published'"

_DETAIL_SQL_TEMPLATE = f"""
SELECT {_SCENARIO_COLUMNS},
       v.version AS version,
       v.state   AS version_state,
       v.policy  AS version_policy
FROM scenarios s
LEFT JOIN scenario_versions v
  ON v.scenario_id = s.id
 AND v.tenant_id   = :tenant_id
 AND ({{version_filter}})
WHERE s.id = :scenario_id
  AND s.tenant_id = :tenant_id
"""
"""详情：场景行 + 版本行的 LEFT JOIN。

★ 用 LEFT JOIN 而不是 INNER JOIN：`version=None` 与「场景不存在」是两种结局
  （前者返回 `version_source='unresolvable'`，后者 404）。INNER JOIN 会把前者
  也变成「查不到行」，于是「场景存在但版本不可解析」被误报成 404。
"""

# 治理字段 ↔ 列名。显式映射（而不是直接拿请求键拼 SQL）是**注入防线**：
# UPDATE 语句的列名只能来自这张表，请求体的键最多只能命中已知项。
_FIELD_COLUMNS: Final[dict[str, str]] = {
    "pinned_version": "pinned_version",
    "recall_top_k_max": "recall_top_k_max",
    "threshold_vector_min": "threshold_vector_min",
    "threshold_rerank_min": "threshold_rerank_min",
    "rerank_allowed": "rerank_allowed",
    "tier": "tier",
}

_GOVERNANCE_FIELDS: Final[tuple[str, ...]] = tuple(_FIELD_COLUMNS)


def _caps_of(row) -> TenantPolicyCaps:
    """从查询行折叠出 caps（四个键都取，`None` = 不限制）。"""
    return TenantPolicyCaps(
        recall_top_k_max=row["recall_top_k_max"],
        threshold_vector_min=row["threshold_vector_min"],
        threshold_rerank_min=row["threshold_rerank_min"],
        rerank_allowed=row["rerank_allowed"],
    )


def _caps_payload(caps: TenantPolicyCaps) -> dict[str, object]:
    """caps 的响应形态：四个键**全部**返回（`None` 也返回）。

    ★ 不省 None 键：管理端要区分「上限不存在」与「上限字段没实现」。
      省键会让两者在 JSON 里长得一样。
    """
    return {
        "recall_top_k_max": caps.recall_top_k_max,
        "threshold_vector_min": caps.threshold_vector_min,
        "threshold_rerank_min": caps.threshold_rerank_min,
        "rerank_allowed": caps.rerank_allowed,
    }


def _scenario_payload(row) -> dict[str, object]:
    return {
        "id": row["id"],
        "scenario_key": row["scenario_key"],
        "name": row["name"],
        "status": row["status"],
        "db_binding": row["db_binding"],
        "tier": row["tier"],
        "pinned_version": row["pinned_version"],
        "caps": _caps_payload(_caps_of(row)),
    }


def _governance_before_after(row) -> dict[str, object]:
    return {name: row[name] for name in _GOVERNANCE_FIELDS}


# ── 库白名单：读（管理端）──────────────────────────────────────────────

async def get_db_scope(
    session: AsyncSession, tenant_id: uuid.UUID
) -> list[str] | None:
    """读租户库白名单的**最新**值（不走缓存，见本段开头说明）。

    返回原样数组（不排序）：管理端要看到库里真实存的形态；
    规范化只发生在写入时，读侧再排一次序会让「库里到底怎么存的」不可观测。
    """
    result = await session.execute(_ALLOWED_SQL, {"tenant_id": tenant_id})
    row = result.first()
    if row is None:
        raise TenantNotFoundError(tenant_id)
    raw = row._mapping["allowed_db_keys"]
    return None if raw is None else list(raw)


# ── 库白名单：写 ──────────────────────────────────────────────────────

async def update_db_scope(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    allowed_db_keys: list[str] | None,
    ip: str | None = None,
) -> list[str] | None:
    """整体替换租户库白名单。`None` = 不限制（与空数组「全部拒绝」不同）。

    ★ 开关关闭 → `GOVERNANCE_DISABLED`：关闭态禁止写入，否则会出现
      「治理关着但库里已经有白名单」的数据，开启那一刻行为突变且无法归因。
    ★ 提交后才失效缓存（`invalidate_allowed_db_keys`）：事务内失效会在回滚时
      留下「缓存空、库里旧」的错位。
    """
    if not get_settings().tenant_governance_enabled:
        raise GovernanceError(GovernanceCode.DISABLED)

    if allowed_db_keys is not None:
        for value in allowed_db_keys:
            if not validate_db_key(value):
                raise GovernanceError(
                    GovernanceCode.DB_SCOPE_INVALID,
                    field="allowed_db_keys",
                    context={"value": value},
                )
    # 规范化：去重 + 排序。排序让「同一集合不同提交顺序」产生完全相同的审计 detail，
    # 否则幂等重放会在审计里留下看似「值变了」的假差异。
    after: list[str] | None = (
        None if allowed_db_keys is None else sorted(set(allowed_db_keys))
    )

    lock = await session.execute(_DB_SCOPE_LOCK_SQL, {"tenant_id": tenant_id})
    lock_row = lock.first()
    if lock_row is None:
        raise TenantNotFoundError(tenant_id)
    raw_before = lock_row._mapping["allowed_db_keys"]
    before: list[str] | None = None if raw_before is None else list(raw_before)

    await session.execute(
        _DB_SCOPE_UPDATE_SQL, {"tenant_id": tenant_id, "keys": after}
    )

    # 本地 import：`services.audit` 会拉起 `app.models` / `app.database`，
    # 顶层 import 会让 governance 模块的导入路径变重（本模块被 snapshot 早引入）。
    from app.services import audit

    await audit.record_with_session(
        session,
        tenant_id,
        user_id,
        "tenant.db_scope.update",
        object_type="tenant",
        object_id=str(tenant_id),
        detail={"before": before, "after": after},
        ip=ip,
    )
    await session.commit()
    invalidate_allowed_db_keys(tenant_id)
    return after


# ── 场景治理：读 ──────────────────────────────────────────────────────

async def list_scenario_governance(
    session: AsyncSession, *, tenant_id: uuid.UUID
) -> list[dict[str, object]]:
    """列出本租户全部场景的治理字段（不含版本与有效值）。"""
    result = await session.execute(_SCENARIO_LIST_SQL, {"tenant_id": tenant_id})
    return [_scenario_payload(row) for row in result.mappings().all()]


async def get_scenario_governance(
    session: AsyncSession, *, tenant_id: uuid.UUID, scenario_id: uuid.UUID
) -> dict[str, object]:
    """场景治理详情：场景行 + 解析到的版本 + 生效参数预览。

    ★ 预览必须复用 `effective_retrieval` 与 `parse_policy`，不能另写一份折叠逻辑：
      管理端展示的上限与实际检索用的上限一旦分成两套算法，运维就会照着预览调参，
      而调完不生效（或生效到别的值）—— 且没有任何报错。
    """
    governed = bool(get_settings().tenant_governance_enabled)
    version_filter = (
        _PINNED_VERSION_FILTER if governed else _PUBLISHED_VERSION_FILTER
    )
    result = await session.execute(
        text(_DETAIL_SQL_TEMPLATE.format(version_filter=version_filter)),
        {"tenant_id": tenant_id, "scenario_id": scenario_id},
    )
    row = result.mappings().first()
    if row is None:
        raise GovernanceError(GovernanceCode.NOT_FOUND)

    caps = _caps_of(row) if governed else EMPTY_CAPS
    version = row["version"]
    if version is None:
        # 场景存在但按当前开关语义解析不到版本：不编造 effective。
        version_source = "unresolvable"
    elif governed and row["pinned_version"] is not None:
        version_source = "pinned"
    else:
        version_source = "published"

    effective: dict[str, dict[str, object]] | None = None
    if version is not None:
        # 本地 import：snapshot / effective 都反向上依赖本模块（`EMPTY_CAPS`），
        # 顶层 import 会形成循环。
        from app.services.scenario.effective import effective_retrieval
        from app.services.scenario.snapshot import parse_policy

        policy = parse_policy(row["version_policy"], scenario_id=scenario_id)
        flattened = effective_retrieval(
            policy,
            platform_rerank_enabled=bool(get_settings().rerank_enabled),
            caps=caps,
        )
        sources = dict(flattened.sources)
        effective = {
            "recall_top_k": {
                "scenario": policy.recall_top_k,
                "cap": caps.recall_top_k_max,
                "effective": flattened.recall_top_k,
                "source": sources["recall_top_k"],
            },
            "rerank_enabled": {
                "scenario": policy.rerank_enabled,
                "cap": caps.rerank_allowed,
                "effective": flattened.rerank_enabled,
                "source": sources["rerank_enabled"],
            },
            "threshold_vector": {
                "scenario": policy.relevance_threshold_vector,
                "cap": caps.threshold_vector_min,
                "effective": flattened.threshold_vector,
                "source": sources["threshold_vector"],
            },
            "threshold_rerank": {
                "scenario": policy.relevance_threshold_rerank,
                "cap": caps.threshold_rerank_min,
                "effective": flattened.threshold_rerank,
                "source": sources["threshold_rerank"],
            },
        }

    payload = _scenario_payload(row)
    payload.update(
        {
            "governance_enabled": governed,
            "version": version,
            "version_source": version_source,
            "effective": effective,
        }
    )
    return payload


# ── 场景治理：写 ──────────────────────────────────────────────────────

def _validate_cap(field: str, value: object) -> None:
    """cap 校验：类型与范围都走 `policy_spec` 的唯一规格表。

    ★ 不手写 `isinstance`：`bool` 是 `int` 子类这个坑必须由 `type_ok` 统一处理，
      每个字段各写一遍判断就是给这个坑开一扇新门。
    """
    from app.services.scenario import policy_spec

    if field == "recall_top_k_max":
        ok = (
            policy_spec.type_ok(value, "int")
            and 1 <= value <= policy_spec.RECALL_TOP_K_MAX  # type: ignore[operator]
        )
    elif field == "rerank_allowed":
        ok = policy_spec.type_ok(value, "bool")
    else:
        ok = (
            policy_spec.type_ok(value, "float")
            and 0.0 <= value <= 1.0  # type: ignore[operator]
        )
    if not ok:
        raise GovernanceError(
            GovernanceCode.INVALID, field=field, context={"value": value}
        )


async def update_scenario_governance(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    scenario_id: uuid.UUID,
    fields: dict[str, object],
    ip: str | None = None,
) -> dict[str, object]:
    """按 `fields` 中**显式传入**的键更新场景治理列（未传的不动）。

    `db_binding` 不在此接口范围内：它是部署事实（哪个库位承载这个场景），
    不是租户管理员能改的策略上限。改它必须走搬库流程（同事务改 `tier`）。

    ★ 幂等：同样值重复 PUT 仍 200，并照写审计（before == after）——
      审计的用途是「谁在什么时候对哪个场景提交过什么」，不是只记差异。
    """
    if not get_settings().tenant_governance_enabled:
        raise GovernanceError(GovernanceCode.DISABLED)

    unknown = set(fields) - set(_FIELD_COLUMNS)
    if unknown:
        # 路由层已用 `model_fields_set` 白名单过滤；到这里说明调用方绕过了路由。
        # fail-closed：静默忽略会让「写进去了但没生效」无法归因。
        raise GovernanceError(
            GovernanceCode.INVALID, field=sorted(unknown)[0]
        )

    lock = await session.execute(
        _SCENARIO_LOCK_SQL, {"tenant_id": tenant_id, "scenario_id": scenario_id}
    )
    row = lock.mappings().first()
    if row is None:
        raise GovernanceError(GovernanceCode.NOT_FOUND)

    # ── 钉定版本：只能钉 published ──────────────────────────────────
    if "pinned_version" in fields and fields["pinned_version"] is not None:
        pinned = fields["pinned_version"]
        state_row = (
            await session.execute(
                _SCENARIO_VERSION_STATE_SQL,
                {
                    "tenant_id": tenant_id,
                    "scenario_id": scenario_id,
                    "version": pinned,
                },
            )
        ).first()
        actual_state = None if state_row is None else state_row[0]
        if actual_state != "published":
            raise GovernanceError(
                GovernanceCode.PIN_NOT_PUBLISHED,
                field="pinned_version",
                context={"version": pinned, "state": actual_state},
            )

    # ── 分级：hot 未支持；tier 必须与 db_binding 一致（DB CHECK 同口径）──
    if "tier" in fields:
        tier_value = fields["tier"]
        if tier_value is None:
            raise GovernanceError(GovernanceCode.INVALID, field="tier")
        if tier_value == "hot":
            raise GovernanceError(GovernanceCode.TIER_HOT, field="tier")
    target_tier = fields.get("tier", row["tier"])
    db_binding = row["db_binding"]
    if (db_binding is None) != (target_tier == "cold"):
        # cold ⇔ db_binding IS NULL。两边的两种错都归到同一个码：
        # 管理员能做的处置完全相同（先搬库或先解除绑定），拆码只会让他多读一条文档。
        raise GovernanceError(
            GovernanceCode.TIER_MISMATCH,
            field="tier",
            context={"tier": target_tier, "db_binding": db_binding},
        )

    # ── caps：非 None 才校验（None = 清除上限）──────────────────────
    for name in (
        "recall_top_k_max",
        "threshold_vector_min",
        "threshold_rerank_min",
        "rerank_allowed",
    ):
        if name in fields and fields[name] is not None:
            _validate_cap(name, fields[name])

    before = _governance_before_after(row)

    updates = {name: value for name, value in fields.items() if name in _FIELD_COLUMNS}
    if updates:
        set_clause = ", ".join(
            f"{_FIELD_COLUMNS[name]} = :{name}" for name in updates
        )
        params: dict[str, object] = dict(updates)
        params["scenario_id"] = scenario_id
        params["tenant_id"] = tenant_id
        await session.execute(
            text(
                f"UPDATE scenarios SET {set_clause} "
                "WHERE id = :scenario_id AND tenant_id = :tenant_id"
            ),
            params,
        )

    after_row = (
        await session.execute(
            _SCENARIO_ROW_SQL, {"scenario_id": scenario_id, "tenant_id": tenant_id}
        )
    ).mappings().first()
    if after_row is None:
        # 同一事务里刚锁过的行不可能消失；走到这里说明并发删除绕过了锁语义，
        # 按数据事故处理，不返回编造的行。
        raise GovernanceError(GovernanceCode.NOT_FOUND)
    after = _governance_before_after(after_row)

    from app.services import audit

    await audit.record_with_session(
        session,
        tenant_id,
        user_id,
        "scenario.governance.update",
        object_type="scenario",
        object_id=str(scenario_id),
        detail={"before": before, "after": after},
        ip=ip,
    )
    if before["tier"] != after["tier"]:
        # 单独一条：分级变化会影响「这个场景的数据在哪台库上」，审计要能独立筛出来，
        # 而不是在 governance.update 的整份 before/after 里翻。
        await audit.record_with_session(
            session,
            tenant_id,
            user_id,
            "scenario.tier.change",
            object_type="scenario",
            object_id=str(scenario_id),
            detail={"before": before["tier"], "after": after["tier"]},
            ip=ip,
        )

    await session.commit()

    # 提交后失效解析缓存：事务内失效会在回滚时留下「缓存空、库里旧」的错位。
    from app.services.scenario import resolver

    resolver.invalidate(tenant_id, scenario_id)
    return _scenario_payload(after_row)
