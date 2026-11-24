"""请求层 / worker 数据面入口（Step 6 S6-3/S6-4）：控制面解析库位 → 数据面执行。

两条纪律，缺一不可：

1. **关闭态与未绑定库位零新增查询、零新增连接**：
   `SCENE_DB_SPLIT_ENABLED=false`（默认）时 `resolve_kb_db_key` / `map_kb_db_keys`
   直接返回 `"default"` 且不发出任何 SQL；`open_data_session("default")` 原样交出
   控制面会话。这与未引入路由层之前完全等价。
2. **库位只能由已认证上下文推出**（tenant_id + kb_ids，I-7）：
   不接受调用方指定库位；解析失败显式抛 `DbScopeError`，**禁止回落默认库**
   （回落会让"绑定了 kb_x 但本进程没配 kb_x"变成"悄悄查错库、返回一半结果"）。

库位判定口径：
  · 以 `scenario_versions.kb_ids` 为准（默认取**已发布版本**；治理开启且场景钉定后
    改取钉定版本，published/archived 均可），`jsonb_array_elements_text` 展开；
  · 治理开启时解析结果还要过 `tenants.allowed_db_keys` 白名单（NULL=不限，
    `{}`=全禁，数组=白名单），越界抛 `DB_SCOPE_NOT_ALLOWED`；
  · `scenarios.db_binding IS NULL` 即默认库 —— **default 也是一个库位**：
    一次请求里「默认库的 kb + 绑定库位的 kb」= 跨库，必须拒绝，否则只会查一半；
  · **不按 `scenarios.status` 过滤**：库位是物理事实，被禁用场景里的数据仍在那个库；
  · 同一 kb 被两个不同库位的已发布场景命中 = 数据冲突（与迁移 0030 的
    `fn_kb_db_binding_conflicts` 同口径），显式报错，不替调用方挑一个。
  · 按 ID 寻址（文档 / wiki 页）**控制面副本优先**：控制副本命中即按其 `kb_id`
    推导库位，不再探测（迁移 §4.3 第 12 步是 COPY，控制库与场景库同 id 属正常
    状态）；只有控制副本缺失时才按主键探测**白名单内**的已注册场景库 ——
    被拒绝的库位一次都不打开，全部被拒 → `None`（0→404、≥2→歧义）。
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.infra.db_router import DEFAULT_DB_KEY, data_session, registered_db_keys
from app.models import Document, WikiPage
from app.services.tenant.governance import get_allowed_db_keys

# 一次请求跨多个库位（本期不支持跨库检索/跨库写入）
DB_SCOPE_MULTI_KEY: Final[str] = "DB_SCOPE_MULTI_KEY"
# 控制库无副本时按主键探测场景库，同一主键命中多个库位（D-S6-3-2）
DB_SCOPE_AMBIGUOUS: Final[str] = "DB_SCOPE_AMBIGUOUS"
# worker 收到的队列负载缺少 db_key（D-S6-3-1）
JOB_DB_KEY_MISSING: Final[str] = "JOB_DB_KEY_MISSING"
# 租户库白名单拒绝（Step 7 S7-3）：解析出的库位不在 tenants.allowed_db_keys 内
DB_SCOPE_NOT_ALLOWED: Final[str] = "DB_SCOPE_NOT_ALLOWED"

__all__ = [
    "DB_SCOPE_MULTI_KEY",
    "DB_SCOPE_AMBIGUOUS",
    "DB_SCOPE_NOT_ALLOWED",
    "JOB_DB_KEY_MISSING",
    "DbScopeError",
    "ensure_db_keys_allowed",
    "resolve_kb_db_key",
    "map_kb_db_keys",
    "resolve_document_db_key",
    "resolve_wiki_page_db_key",
    "error_detail",
    "open_data_session",
    "open_job_session",
    "require_job_db_key",
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

# 治理开启时的映射 SQL：版本选择与 resolver 的 LOOKUP_GOVERNED_SQL **逐字同口径**。
# ★ 为什么映射必须 pin-aware：kb 集合会随版本变化，若映射仍取 published，
#   钉定旧版的请求会被引到新版库位 —— 版本与库位来自两个版本 = 读到别人的数据。
_MAP_SQL_GOVERNED = text("""
SELECT kb.value AS kb_id, s.db_binding AS db_key
FROM scenarios s
JOIN scenario_versions v
  ON v.scenario_id = s.id
 AND v.tenant_id   = s.tenant_id
 AND (
      (s.pinned_version IS NULL AND v.state = 'published')
   OR (s.pinned_version IS NOT NULL
       AND v.version = s.pinned_version
       AND v.state IN ('published','archived'))
 )
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
    - 治理开启时再按 `tenants.allowed_db_keys` 校验（**含 default**）：
      `NULL` 不限；`{}` 全禁；数组为白名单。越界 → `DB_SCOPE_NOT_ALLOWED`
      （HTTP 400）。先校验再判跨库：白名单拒绝比"挑不出库位"更根本。

    ★ 不校验该键是否已注册：注册与否由 `data_session` 在真正开会话时 fail-closed，
      这里若提前回落默认库，就会把配置缺口藏成"查了半个库"。
    """
    mapping = await map_kb_db_keys(control_session, tenant_id, kb_ids)
    keys = sorted(set(mapping.values())) or [DEFAULT_DB_KEY]
    await ensure_db_keys_allowed(control_session, tenant_id, keys)
    if len(keys) > 1:
        raise DbScopeError(
            DB_SCOPE_MULTI_KEY,
            {"kb_ids": [str(kb_id) for kb_id in kb_ids], "db_keys": keys},
        )
    return keys[0]


async def ensure_db_keys_allowed(
    control_session: AsyncSession,
    tenant_id: uuid.UUID,
    db_keys: Sequence[str],
) -> None:
    """租户库白名单校验（Step 7 S7-3）。

    关闭治理开关时 `get_allowed_db_keys` 直接返回 `None` 且不发 SQL，
    因此这里也不产生任何查询 —— 关闭态与 Step 6 完全一致。
    """
    allowed = await get_allowed_db_keys(control_session, tenant_id)
    if allowed is None:
        return
    denied = sorted({str(key) for key in db_keys} - allowed)
    if denied:
        raise DbScopeError(
            DB_SCOPE_NOT_ALLOWED,
            {"tenant_id": tenant_id, "denied": denied},
        )


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

    # 治理开启 → 映射跟钉定版本走（版本与库位必须同一口径）
    sql = _MAP_SQL_GOVERNED if get_settings().tenant_governance_enabled else _MAP_SQL
    rows = (
        await control_session.execute(
            sql,
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


def require_job_db_key(db_key: str | None) -> str:
    """worker 侧库位键校验：缺失/非字符串即显式失败（禁止回落默认库）。"""
    if not isinstance(db_key, str) or not db_key:
        raise DbScopeError(JOB_DB_KEY_MISSING, {"db_key": repr(db_key)})
    return db_key


@asynccontextmanager
async def open_job_session(db_key: str) -> AsyncIterator[AsyncSession]:
    """按队列负载携带的 db_key 打开数据面会话（worker 唯一的数据面入口）。

    - worker 没有控制面会话可复用，因此 `"default"` 也走 `data_session`；
    - db_key 缺失/非法直接抛 `DbScopeError(JOB_DB_KEY_MISSING)` —— 不回落默认库，
      否则会在错误库位写出"成功"的假象数据。
    """
    key = require_job_db_key(db_key)
    async with data_session(key) as session:
        yield session


async def _probe_scene_db_key(
    model: type,
    pk: uuid.UUID,
    *,
    allowed_db_keys: frozenset[str] | None,
) -> str | None:
    """控制库无副本时，按主键探测**白名单内已注册的场景库**（default 跳过）。

    只在控制库查不到副本时调用；控制库命中一律不探测（见两个 resolve 入口）。
    `allowed_db_keys is None`（治理关闭 / 白名单 NULL）表示不限制，遍历全部
    已注册场景库；否则只遍历集合内的键 —— **被白名单拒绝的库位一次都不打开**
    （连 `data_session` 都不调用）。
    0 → None（调用方按 404）；1 → 该键；≥2 → `DB_SCOPE_AMBIGUOUS`（歧义只在
    被允许的库位之间判定，不替调用方挑一个）。
    """
    hits: list[str] = []
    for db_key in registered_db_keys():
        if db_key == DEFAULT_DB_KEY:
            continue
        if allowed_db_keys is not None and db_key not in allowed_db_keys:
            continue
        async with data_session(db_key) as session:
            if await session.get(model, pk) is not None:
                hits.append(db_key)

    if not hits:
        return None
    if len(hits) > 1:
        raise DbScopeError(
            DB_SCOPE_AMBIGUOUS,
            {"model": model.__name__, "pk": str(pk), "db_keys": hits},
        )
    return hits[0]


async def resolve_document_db_key(
    control_session: AsyncSession,
    doc_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
) -> str | None:
    """按 ID 推导文档库位：控制面副本优先，缺失时探测白名单内已注册场景库。

    - 关闭态：直接返回 `"default"`，零 SQL（不读白名单）；
    - 开启态：`control_session.get(Document, doc_id)` 命中 → 用其 kb_id 经
      `resolve_kb_db_key` 推导（**不探测**：控制库+场景库同 id 是迁移 COPY 的
      正常状态；该函数自带白名单校验）；未命中 → 取本租户白名单后按主键探测
      场景库：被白名单拒绝的库位不探测；全部被拒 → `None`，调用方按 404 处理；
      1 → 该键；≥2 → 歧义。
    """
    if not get_settings().scene_db_split_enabled:
        return DEFAULT_DB_KEY
    document = await control_session.get(Document, doc_id)
    if document is not None:
        return await resolve_kb_db_key(
            control_session, document.tenant_id, [document.kb_id]
        )
    allowed = await get_allowed_db_keys(control_session, tenant_id)
    return await _probe_scene_db_key(Document, doc_id, allowed_db_keys=allowed)


async def resolve_wiki_page_db_key(
    control_session: AsyncSession,
    page_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
) -> str | None:
    """按 ID 推导 wiki 页库位：控制面副本优先，缺失时探测白名单内已注册场景库。

    - 关闭态：直接返回 `"default"`，零 SQL（不读白名单）；
    - 开启态：控制库有页副本 → 取其 document_id；控制库有该文档 → 按 kb 解析
      （自带白名单校验，不探测）；文档副本缺失 → 取白名单后按 document_id
      探测场景库；页副本缺失 → 取白名单后按 page_id 探测场景库。
      被白名单拒绝的库位不探测；全部被拒 → `None`，调用方按 404 处理；
      任何路径都不回落 default。
    """
    if not get_settings().scene_db_split_enabled:
        return DEFAULT_DB_KEY
    page = await control_session.get(WikiPage, page_id)
    if page is None:
        allowed = await get_allowed_db_keys(control_session, tenant_id)
        return await _probe_scene_db_key(WikiPage, page_id, allowed_db_keys=allowed)

    document = await control_session.get(Document, page.document_id)
    if document is not None:
        return await resolve_kb_db_key(
            control_session, document.tenant_id, [document.kb_id]
        )
    allowed = await get_allowed_db_keys(control_session, tenant_id)
    return await _probe_scene_db_key(Document, page.document_id, allowed_db_keys=allowed)


def error_detail(exc: DbScopeError) -> dict[str, object]:
    """把 `DbScopeError` 规整成可直接放 `HTTPException.detail` 的结构（JSON 可写）。

    值为模型类时取 `__name__`，UUID 转字符串 —— 其余（字符串/列表/字典）原样保留。
    """
    scope = exc.detail or {}
    detail: dict[str, object] = {"code": exc.code}
    for key, value in scope.items():
        if key == "model":
            detail[key] = getattr(value, "__name__", str(value))
        elif isinstance(value, uuid.UUID):
            detail[key] = str(value)
        else:
            detail[key] = value
    return detail
