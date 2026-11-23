"""请求层数据面入口（Step 6 S6-3/S6-4）：控制面解析库位 → 数据面执行。

两条纪律，缺一不可：

1. **关闭态与未绑定库位零新增查询、零新增连接**：
   `SCENE_DB_SPLIT_ENABLED=false`（默认）时 `resolve_kb_db_key` / `map_kb_db_keys`
   直接返回 `"default"` 且不发出任何 SQL；`open_data_session("default")` 原样交出
   控制面会话。这与未引入路由层之前完全等价。
2. **库位只能由已认证上下文推出**（tenant_id + kb_ids，I-7）：
   不接受调用方指定库位；解析失败显式抛 `DbScopeError`，**禁止回落默认库**
   （回落会让"绑定了 kb_x 但本进程没配 kb_x"变成"悄悄查错库、返回一半结果"）。

库位判定口径：
  · 以 `scenario_versions.kb_ids`（**已发布版本**）为准，`jsonb_array_elements_text` 展开；
  · `scenarios.db_binding IS NULL` 即默认库 —— **default 也是一个库位**：
    一次请求里「默认库的 kb + 绑定库位的 kb」= 跨库，必须拒绝，否则只会查一半；
  · **不按 `scenarios.status` 过滤**：库位是物理事实，被禁用场景里的数据仍在那个库；
  · 同一 kb 被两个不同库位的已发布场景命中 = 数据冲突（与迁移 0030 的
    `fn_kb_db_binding_conflicts` 同口径），显式报错，不替调用方挑一个。
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.infra.db_router import DEFAULT_DB_KEY, data_session

# 一次请求跨多个库位（本期不支持跨库检索/跨库写入）
DB_SCOPE_MULTI_KEY: Final[str] = "DB_SCOPE_MULTI_KEY"
# 按 ID 探测命中多个库（D-S6-3-2 顺序探测的预留码，本步骤不实现探测）
DB_SCOPE_AMBIGUOUS: Final[str] = "DB_SCOPE_AMBIGUOUS"

__all__ = [
    "DB_SCOPE_MULTI_KEY",
    "DB_SCOPE_AMBIGUOUS",
    "DbScopeError",
    "resolve_kb_db_key",
    "map_kb_db_keys",
    "open_data_session",
]


class DbScopeError(Exception):
    """库位解析错误：带机器可读的 code 与结构化 detail（便于日志/接口映射）。"""

    def __init__(self, code: str, detail: dict[str, object] | None = None) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


# kb → 库位映射：一次查询取回全部命中的 (kb_id, db_key)，是库位判定的唯一事实来源。
# · 只返回 `db_binding IS NOT NULL` 的行（非默认库位）；未命中的 kb 由调用方补 default。
# · `v.state = 'published'` + `v.tenant_id = s.tenant_id`：与 resolver 的 LOOKUP_SQL 同口径。
# · GROUP BY 让同一个 kb 在同一库位下只出一行；若同一 kb 出现在两个不同库位的
#   已发布场景里，会返回两行 —— 这是数据冲突（0030 的冲突函数正好报这种状态），
#   由 map_kb_db_keys 显式报错，不替调用方挑一个。
_MAP_SQL = text("""
SELECT kb.value AS kb_id, s.db_binding AS db_key
FROM scenarios s
JOIN scenario_versions v
  ON v.scenario_id = s.id
 AND v.tenant_id   = s.tenant_id
 AND v.state       = 'published'
CROSS JOIN LATERAL jsonb_array_elements_text(v.kb_ids) AS kb(value)
WHERE s.tenant_id = :tenant_id
  AND s.db_binding IS NOT NULL
  AND kb.value = ANY(:kb_ids)
GROUP BY kb.value, s.db_binding
""")


async def resolve_kb_db_key(
    control_session: AsyncSession,
    tenant_id: uuid.UUID,
    kb_ids: Sequence[uuid.UUID],
) -> str:
    """把本次请求涉及的 kb 集合解析成**唯一**库位键。

    - 关闭态：直接返回 `"default"`，零 SQL（与未接线前等价）。
    - 开启态：复用 `map_kb_db_keys`（单一事实来源）拿到每个 kb 的库位，
      把**未命中的 kb 计为 default** 后去重：0 个 → `"default"`；
      1 个 → 该键；≥2 个 → `DbScopeError(DB_SCOPE_MULTI_KEY)`。
      「默认库 + 绑定库位」的混合请求因此会报 MULTI_KEY，而不是只查绑定库。

    ★ 不校验该键是否已注册：注册与否由 `data_session` 在真正开会话时 fail-closed，
      这里若提前回落默认库，就会把配置缺口藏成"查了半个库"。
    """
    mapping = await map_kb_db_keys(control_session, tenant_id, kb_ids)
    if not mapping:
        return DEFAULT_DB_KEY
    keys = sorted(set(mapping.values()))
    if len(keys) > 1:
        raise DbScopeError(
            DB_SCOPE_MULTI_KEY,
            {"kb_ids": [str(kb_id) for kb_id in kb_ids], "db_keys": keys},
        )
    return keys[0]


async def map_kb_db_keys(
    control_session: AsyncSession,
    tenant_id: uuid.UUID,
    kb_ids: Sequence[uuid.UUID],
) -> dict[uuid.UUID, str]:
    """把一组 kb 分桶到库位键（一次查询；未命中的 kb 映射为 `"default"`）。

    `"default"` 本身就是一个库位：调用方按 mapping 的不同取值数判断是否跨库。
    关闭态：零 SQL，全部 `"default"`。
    开启态：同一 kb 若命中 ≥2 个不同库位（0030 定义的冲突态），
    抛 `DbScopeError(DB_SCOPE_MULTI_KEY)` —— 不替调用方挑一个库。
    """
    mapping: dict[uuid.UUID, str] = {kb_id: DEFAULT_DB_KEY for kb_id in kb_ids}
    if not kb_ids:
        return mapping
    if not get_settings().scene_db_split_enabled:
        return mapping

    rows = (
        await control_session.execute(
            _MAP_SQL,
            {"tenant_id": tenant_id, "kb_ids": [str(kb_id) for kb_id in kb_ids]},
        )
    ).all()

    hits: dict[str, set[str]] = {}
    for kb_id_text, db_key in rows:
        hits.setdefault(str(kb_id_text), set()).add(db_key)

    conflicts = {kb_id: sorted(keys) for kb_id, keys in hits.items() if len(keys) > 1}
    if conflicts:
        raise DbScopeError(
            DB_SCOPE_MULTI_KEY,
            {"conflicts": conflicts},
        )

    for kb_id_text, keys in hits.items():
        mapping[uuid.UUID(kb_id_text)] = next(iter(keys))
    return mapping


@asynccontextmanager
async def open_data_session(
    control_session: AsyncSession,
    db_key: str,
) -> AsyncIterator[AsyncSession]:
    """按库位打开数据面会话（请求层唯一的数据面入口）。

    调用前提（由调用方保证）：
      · 控制面阶段已结束（租户/权限/kb 归属已解析完）；
      · 控制面会话**没有未提交的写** —— 非 default 分支会先 commit 把事务收尾。

    - `db_key == "default"`：原样 yield 控制面会话，不 commit、不开新连接。
      关闭态与未绑定库位因此零新增连接。
    - 其它：先 `commit()` 结束控制面事务、把连接归还池，再打开数据面会话。
      两段**顺序不重叠**，同一时刻在途连接仍是 1 条。
    """
    if db_key == DEFAULT_DB_KEY:
        yield control_session
        return

    await control_session.commit()
    async with data_session(db_key) as session:
        yield session
