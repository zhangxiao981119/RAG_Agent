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
from typing import Final

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
