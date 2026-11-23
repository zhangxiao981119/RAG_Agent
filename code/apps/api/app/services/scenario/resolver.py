"""场景解析器 —— 唯一的 impure 入口：一次 DB 读 + 一次纯函数委托。

依赖方向（严格单向）：
    api/integrations.py
          ↓
    services/scenario/resolver.py  →  models/entities.py, config/{index,graph,wiki}_profile.py
          ↓
    services/scenario/snapshot.py  →  policy_spec.py, errors.py, decisions.py, settings.py
          ↓
    （不依赖 services/retrieve / services/acl / mcp）

★ 为什么 resolver 不依赖 `services/retrieve`：它只产出「口径快照」，不调用检索。
  若它依赖检索，Step 3 接线时会出现「检索调 resolver、resolver 调检索」的循环。

★ 缓存只缓存**行**，绝不缓存**快照**：快照含请求态 `kb_ids`，缓存快照等于
  把 A 用户请求的 kb 集合发给 B 用户 —— 最严重的一类越权。
"""
from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import placeholders
from app.config.settings import get_settings
from app.infra import metrics
from app.models import IntegrationApplication
from app.services.scenario.errors import COMPONENT
from app.services.scenario.snapshot import (
    ScenarioRow,
    ScenarioSnapshot,
    build_snapshot,
)

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS: float = 60.0
"""行缓存的 TTL（**站位值**，设计文档 §C.7 / §G-7）。

理由：Step 4 起会有「发布新版本 → 生效」的期望时延，60s 内的最终一致可接受；
标定依据是发布操作的运维节奏，本项目尚无数据。多副本部署时必须换 Redis 频道
（当前单 uvicorn worker，跨进程靠 TTL 兜底 —— §G-2）。
"""

# ── 站位值登记断言（import 期，fail-fast；与 policy_spec.py 同款）────────
# 登记名带组件前缀（`SCENARIO_CACHE_TTL_SECONDS`），因为横幅里不带前缀会有歧义
# （这个 TTL 是「场景行缓存」的，不是 Redis 客户端或连接池的）。
_SCENARIO_CACHE_TTL_PLACEHOLDER = "SCENARIO_CACHE_TTL_SECONDS"

if _SCENARIO_CACHE_TTL_PLACEHOLDER not in placeholders.names_for("scenario"):
    raise RuntimeError(
        f"{_SCENARIO_CACHE_TTL_PLACEHOLDER} 是站位值，但未登记在 "
        "app/config/placeholders.py —— 未登记的站位值不会出现在启动横幅"
    )
if placeholders.value_of(_SCENARIO_CACHE_TTL_PLACEHOLDER) != CACHE_TTL_SECONDS:
    raise RuntimeError(
        f"app/config/placeholders.py 里 {_SCENARIO_CACHE_TTL_PLACEHOLDER} 的镜像值 "
        f"{placeholders.value_of(_SCENARIO_CACHE_TTL_PLACEHOLDER)!r} 与本模块的 "
        f"{CACHE_TTL_SECONDS!r} 不一致 —— 启动横幅会显示一个不存在的值"
    )

# 键 = (tenant_id, scenario_id 或 None)；用 None 代表「内置分支」
# （与设计文档的 "builtin" 字符串同一个键空间，用 None 是为了类型干净）。
# 值 = (写入时刻 monotonic, 行)。**只缓存命中行，不缓存 miss**：
# 负缓存会让「补种内置行」在 TTL 内不可见 —— 而漏补种正是要靠观测发现的问题。
_CACHE: dict[tuple[uuid.UUID, uuid.UUID | None], tuple[float, ScenarioRow]] = {}

LOOKUP_SQL = """
SELECT
    s.id            AS scenario_id,
    s.tenant_id     AS tenant_id,
    s.scenario_key  AS scenario_key,
    s.status        AS status,
    s.is_builtin    AS is_builtin,
    s.db_binding    AS db_binding,
    v.version       AS version,
    v.policy        AS policy,
    v.kb_ids        AS kb_ids,
    v.binding_mode  AS binding_mode,
    v.dna_declared  AS dna_declared,
    v.checksum      AS checksum
FROM scenarios s
JOIN scenario_versions v
  ON v.scenario_id = s.id
 AND v.state = 'published'
 AND v.tenant_id = :tenant_id
WHERE s.tenant_id = :tenant_id
  AND ((CAST(:scenario_id AS uuid) IS NULL AND s.is_builtin)
       OR s.id = CAST(:scenario_id AS uuid))
"""
"""解析热路径的唯一 SQL：两个分支（内置 / 指定场景）共用一次往返。

★ `s.tenant_id = :tenant_id` **必须永远存在** —— 这是跨租户读的唯一防线
  （设计文档 §A.6 障碍 6）。缺了它就是「拿别人的场景算出别人的口径」。
★ 内置分支的条件 `(:scenario_id IS NULL AND s.is_builtin)` **必须显式括号**：
  依赖 `AND` 高于 `OR` 的隐式优先级，任何人后续插一个 `AND` 都会改变语义
  （`test_lookup_sql_tenant_scoped` 断言这一点）。
★ `v.tenant_id = :tenant_id` 是**加严**：版本行的租户与本租户不一致时视为查不到
  （fail-closed），而不是把别人的版本当自己的用。
"""

LOOKUP_GOVERNED_SQL = """
SELECT
    s.id            AS scenario_id,
    s.tenant_id     AS tenant_id,
    s.scenario_key  AS scenario_key,
    s.status        AS status,
    s.is_builtin    AS is_builtin,
    s.db_binding    AS db_binding,
    s.pinned_version        AS pinned_version,
    s.recall_top_k_max      AS recall_top_k_max,
    s.threshold_vector_min  AS threshold_vector_min,
    s.threshold_rerank_min  AS threshold_rerank_min,
    s.rerank_allowed        AS rerank_allowed,
    v.version       AS version,
    v.state         AS version_state,
    v.policy        AS policy,
    v.kb_ids        AS kb_ids,
    v.binding_mode  AS binding_mode,
    v.dna_declared  AS dna_declared,
    v.checksum      AS checksum
FROM scenarios s
JOIN scenario_versions v
  ON v.scenario_id = s.id
 AND v.tenant_id   = :tenant_id
 AND (
      (s.pinned_version IS NULL AND v.state = 'published')
   OR (s.pinned_version IS NOT NULL
       AND v.version = s.pinned_version
       AND v.state IN ('published','archived'))
 )
WHERE s.tenant_id = :tenant_id
  AND ((CAST(:scenario_id AS uuid) IS NULL AND s.is_builtin)
       OR s.id = CAST(:scenario_id AS uuid))
"""
"""治理开启（`TENANT_GOVERNANCE_ENABLED=true`）时的解析 SQL。

与 `LOOKUP_SQL` 的唯一差别是版本选择：
  · 未钉定 → 已发布版本（与关闭态逐字节同一口径）；
  · 已钉定 → 只认该版本号，`published`/`archived` 都接受。
    archived 必须接受：钉定就是「即使后来发布了新版，也继续按旧版跑」，
    否则归档旧版会让所有钉定客户端立刻 500。

★ 版本条件写在 JOIN 内而不是 WHERE：写在 WHERE 会让「钉定版本被删」变成
  「查不到行」，而调用方无法区分「场景不存在」与「版本被删」—— 两者的诊断值不同。
  JOIN 内过滤后同样是查不到行（由 build_snapshot 报 R1），但 SQL 语义更直白。
★ `s.tenant_id = :tenant_id` 与双端 tenant_id 条件与 `LOOKUP_SQL` 完全一致，
  这条防线不因开启治理而放宽。
"""


def clear_cache() -> None:
    """清空行缓存（**仅供测试**，等价于让 TTL 立即到期）。"""
    _CACHE.clear()


def invalidate(tenant_id: uuid.UUID, scenario_id: uuid.UUID | None = None) -> None:
    """主动失效一行缓存。

    发布新版本的接口（Step 4）MUST 在事务**提交后**调用；事务内调用会在回滚时
    留下「缓存空、库里旧」的错位。`scenario_id=None` 表示清掉该租户的内置行缓存。
    """
    _CACHE.pop((tenant_id, scenario_id), None)


def _cache_get(key: tuple[uuid.UUID, uuid.UUID | None]) -> ScenarioRow | None:
    entry = _CACHE.get(key)
    if entry is None:
        return None
    stored_at, row = entry
    if time.monotonic() - stored_at > CACHE_TTL_SECONDS:
        _CACHE.pop(key, None)
        return None
    return row


def _cache_put(key: tuple[uuid.UUID, uuid.UUID | None], row: ScenarioRow) -> None:
    _CACHE[key] = (time.monotonic(), row)


def _row_from(mapping, *, governed: bool) -> ScenarioRow:
    """把 SQL 结果行折叠成不可变行对象（JSONB 侧保持驱动的原生形态）。

    `governed=False` 时**显式忽略**治理列：关闭态即便结果里带了这些键
    （测试替身 / 未来 SQL 调整）也不得让钉定生效 —— 开关语义由这里兜住，
    而不是依赖「原 SQL 恰好没选这几列」这个易碎前提。
    """
    return ScenarioRow(
        tenant_id=mapping["tenant_id"],
        scenario_id=mapping["scenario_id"],
        scenario_key=mapping["scenario_key"],
        status=mapping["status"],
        is_builtin=mapping["is_builtin"],
        db_binding=mapping["db_binding"],
        version=mapping["version"],
        policy=mapping["policy"] or {},
        kb_ids=tuple(mapping["kb_ids"] or ()),
        binding_mode=mapping["binding_mode"],
        dna_declared=mapping["dna_declared"] or {},
        checksum=mapping["checksum"],
        pinned_version=mapping.get("pinned_version") if governed else None,
        recall_top_k_max=mapping.get("recall_top_k_max") if governed else None,
        threshold_vector_min=mapping.get("threshold_vector_min") if governed else None,
        threshold_rerank_min=mapping.get("threshold_rerank_min") if governed else None,
        rerank_allowed=mapping.get("rerank_allowed") if governed else None,
        version_state=(mapping.get("version_state") if governed else None) or "published",
    )


async def _load_published(
    session: AsyncSession, *, tenant_id: uuid.UUID, scenario_id: uuid.UUID | None
) -> ScenarioRow | None:
    """读「场景身份 + 已发布版本」一行；`scenario_id is None` 时读本租户内置行。

    `None` 的语义严格分两种，由调用方（`build_snapshot`）按 `scenario_id` 区分：
      · `scenario_id is None` + 无行 → 内置行缺失（同值兜底，R5 表外分支）；
      · `scenario_id` 非 None + 无行 → 配置损坏（R1，500，不兜底）。
    """
    # 开关在**请求期**读取而不是 import 期：测试与运维都需要在不重启进程的情况下
    # 观察两种口径；关闭态走原 SQL，因此多一次布尔判断、不多一次查询。
    governed = get_settings().tenant_governance_enabled
    sql = LOOKUP_GOVERNED_SQL if governed else LOOKUP_SQL
    stmt = sa.text(sql).bindparams(
        tenant_id=tenant_id,
        scenario_id=scenario_id,
    )
    result = await session.execute(stmt)
    rows = result.all()
    if not rows:
        return None
    # 唯一索引（每场景至多一个 published / 每租户至多一个内置）保证至多一行；
    # 万一出现多行（索引被绕过），取首行而不是抛错：解析路径不因脏数据变成 500，
    # 该异常本身由巡检 SQL 发现（设计文档 §A.6 障碍 3 的同类做法）。
    return _row_from(rows[0]._mapping, governed=governed)


async def resolve_scenario(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    application: IntegrationApplication,
    requested_kb_ids: Sequence[uuid.UUID],
) -> ScenarioSnapshot:
    """由（凭证绑定的应用, 请求的 kb_ids）解析出本次请求的场景口径快照。

    失败一律 raise `ScenarioError`，**绝不返回默认快照兜底**（fail-closed）。
    """
    scenario_id = application.scenario_id
    key = (tenant_id, scenario_id)

    row = _cache_get(key)
    if row is None:
        row = await _load_published(session, tenant_id=tenant_id, scenario_id=scenario_id)
        if row is not None:
            _cache_put(key, row)

    # 真值表全在纯函数里（无 IO、无副作用），本函数只负责「读 + 打点」
    snapshot = build_snapshot(
        row=row,
        tenant_id=tenant_id,
        scenario_id=scenario_id,
        requested_kb_ids=requested_kb_ids,
    )

    if snapshot.source == "builtin_fallback":
        # 兜底不产生行为差异（同值有测试锁死），但「种子没补」这件事必须可观测：
        # 否则它只会在某次「你以为在用场景、其实在用默认」的排障里暴露。
        metrics.incr("rag.scenario.builtin_row_missing")
        logger.warning(
            "%s: 本租户缺少内置场景行，按同值内置快照兜底（不影响行为，请补种）",
            COMPONENT,
            extra={"tenant_id": str(tenant_id)},
        )
    return snapshot
