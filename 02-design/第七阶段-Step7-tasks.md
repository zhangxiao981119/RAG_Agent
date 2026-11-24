# 第七阶段 Step 7 任务清单

> 状态：**已拍板（2026-11-23）**；按 S7-1 到 S7-8 顺序执行。
> 执行纪律：每项新增测试；每项完成即本地 commit，**不推送**；推送等用户明确同意。
> 基线：`交接文档-2026-11-22.md` §3.4 的 1343 tests / 2 failed（环境失败）/ 0 errors / 15 skipped；Step 7 完成后失败集合必须仍是同样 2 条。
> 真库：`SCENARIO_TEST_DB_URL=postgresql+asyncpg://user:<口令>@localhost:5433/kagent_test`（口令在 `code/.env` 的 `POSTGRES_PASSWORD`）。

---

## [x] S7-1 迁移 0031 + 模型列

**文件**
- 新增 `code/apps/api/alembic/versions/0031_step7_tenant_governance.py`
  - `upgrade()`：
    - `tenants` 加 `allowed_db_keys text[] NULL`
    - `scenarios` 加 `pinned_version integer NULL`
    - `scenarios` 加 `recall_top_k_max integer NULL`
    - `scenarios` 加 `threshold_vector_min double precision NULL`
    - `scenarios` 加 `threshold_rerank_min double precision NULL`
    - `scenarios` 加 `rerank_allowed boolean NULL`
    - `scenarios` 加 `tier text NOT NULL DEFAULT 'cold'`
    - 回填：`UPDATE scenarios SET tier = CASE WHEN db_binding IS NULL THEN 'cold' ELSE 'warm' END`
    - 约束：
      - `ck_scenarios_tier`：`tier IN ('hot','warm','cold')`
      - `ck_scenarios_tier_binding`：`(tier='cold' AND db_binding IS NULL) OR (tier IN ('warm','hot') AND db_binding IS NOT NULL)`
      - `ck_scenarios_pinned_version`：`pinned_version IS NULL OR pinned_version >= 1`
      - `ck_scenarios_recall_cap`：`recall_top_k_max IS NULL OR recall_top_k_max > 0`
      - `ck_scenarios_threshold_vector_min`：`threshold_vector_min IS NULL OR threshold_vector_min BETWEEN 0 AND 1`
      - `ck_scenarios_threshold_rerank_min`：`threshold_rerank_min IS NULL OR threshold_rerank_min BETWEEN 0 AND 1`
    - 索引：`idx_scenarios_tenant_tier (tenant_id, tier)`
  - `downgrade()`：逆序删索引、约束、列。
  - 迁移注释写明：不建 `pinned_version → scenario_versions` 外键，理由见拍板文件 D2。
- 修改 `code/apps/api/app/models/entities.py`
  - `Tenant`：`allowed_db_keys: Mapped[list[str] | None] = mapped_column(ARRAY(Text))`
  - `Scenario`：上述 6 个字段与对应 `CheckConstraint`
  - 字段注释写中文，说明语义与“关闭开关即忽略”。

**测试** `code/apps/api/tests/scenario/test_0031_migration.py`
- 升级到 head 后 `information_schema.columns` 含 7 个新列。
- `tier='cold'` + `db_binding` 非空 → 插入/更新违反 `ck_scenarios_tier_binding`。
- `tier='hot'` + `db_binding` 非空 → DB 允许（应用层拒绝在 S7-5 测）。
- 既有行回填：`db_binding IS NULL → cold`，非空 → `warm`。
- `downgrade 0030` 后新列消失，Step 6 的 `db_binding` 与 `0030` 视图仍在。

**完成判据**：迁移可逆；约束在真库生效；模型与迁移逐列一致。

**commit**：`feat(step7): 0031 租户治理与分级列`

---

## [x] S7-2 治理读取 + 快照 + 开关

**文件**
- `code/apps/api/app/config/settings.py`：新增 `tenant_governance_enabled: bool = False`，注释写“关闭态不读 tenants、不读钉定列”。
- `code/.env.example`（若存在）：追加 `TENANT_GOVERNANCE_ENABLED=false`。
- 新增 `code/apps/api/app/services/tenant/__init__.py`。
- 新增 `code/apps/api/app/services/tenant/governance.py`
  - `@dataclass(frozen=True, slots=True) class TenantPolicyCaps`：`recall_top_k_max / threshold_vector_min / threshold_rerank_min / rerank_allowed`，提供 `is_empty()`。
  - `EMPTY_CAPS = TenantPolicyCaps()`。
  - `validate_db_key(value: str) -> bool`：`default` 或 `^kb_[a-z0-9_]{2,40}$`。
  - `async def get_allowed_db_keys(session, tenant_id) -> frozenset[str] | None`：开关关直接返回 `None` 且不发查询；NULL → `None`；`{}` → 空 `frozenset`；数组 → 集合。进程内 TTL 60s。
  - `def invalidate_allowed_db_keys(tenant_id: uuid.UUID | None = None) -> None`。
- 修改 `code/apps/api/app/services/scenario/resolver.py`
  - `ScenarioRow` 增加：`pinned_version: int | None = None`、`recall_top_k_max: int | None = None`、`threshold_vector_min: float | None = None`、`threshold_rerank_min: float | None = None`、`rerank_allowed: bool | None = None`、`version_state: str = "published"`。
  - 保留 `LOOKUP_SQL` 原样；新增 `LOOKUP_GOVERNED_SQL`：
    - 选 `s.pinned_version` 与四个 cap 列；
    - join 条件：`(s.pinned_version IS NULL AND v.state='published') OR (s.pinned_version IS NOT NULL AND v.version=s.pinned_version AND v.state IN ('published','archived'))`；
    - 选 `v.state AS version_state`。
  - 查询选择：`settings.tenant_governance_enabled` 为真走 governed，否则走原 SQL。
  - 缓存键保持 `(tenant_id, scenario_id)`；治理写入后调用现有 `clear_cache` 或按场景失效。
- 修改 `code/apps/api/app/services/scenario/snapshot.py`
  - `ScenarioSnapshot` 增加 `caps: TenantPolicyCaps = EMPTY_CAPS`、`version_source: Literal["published","pinned"] = "published"`。
  - `build_snapshot` 从 `ScenarioRow` 构造 caps；`version_source = "pinned" if row.pinned_version is not None else "published"`。
  - `fingerprint`：仅当 `caps` 非空时追加 `canonical(caps)`；空 caps 输出与 Step 6 相同。

**测试** `code/apps/api/tests/scenario/test_step7_governance.py`
- 关闭开关：钉 v1、发布 v2 → resolver 返回 v2；`version_source='published'`；不读 `tenants`（用 SQL 计数或 spy 断言）。
- 开启开关：同一数据 → 返回 v1；`version_source='pinned'`。
- 开启开关但无钉定 → 返回 v2，与关闭态逐字段相等。
- caps 四列进入快照；空 caps 时 `fingerprint` 与固定基线字符串相等。
- `get_allowed_db_keys`：NULL / `{}` / 数组三种语义；开关关零查询。

**完成判据**：关闭态与 Step 6 行为逐字段一致；开启态钉定与 caps 可见。

**commit**：`feat(step7): 租户治理读取与快照落地`

---

## [x] S7-3 钉定解析 + 库映射 + 白名单

**文件**
- 修改 `code/apps/api/app/services/db_scope.py`
  - 保留 `_MAP_SQL` 原样；新增 `_MAP_SQL_GOVERNED`，选版本条件与 `LOOKUP_GOVERNED_SQL` 一致。
  - 查询选择：`settings.tenant_governance_enabled` 为真走 governed，否则走原 SQL；`scene_db_split_enabled` 关闭时仍不发查询。
  - 新增 `DB_SCOPE_NOT_ALLOWED` 错误码。
  - 在 `resolve_kb_db_key` 得到库位集合后调用 `ensure_db_keys_allowed(session, tenant_id, keys)`：
    - 开关关：直接返回；
    - `allowed is None`：返回；
    - `keys - allowed` 非空：抛 `DbScopeError(DB_SCOPE_NOT_ALLOWED, {"denied": [...]})`。
- 修改 `code/apps/api/app/api/chat.py`、`code/apps/api/app/api/integrations.py`
  - 确认 `DbScopeError` 到 HTTP 的映射；`DB_SCOPE_NOT_ALLOWED` 必须是 400（不是 500、不是 403、不是空结果）。
  - 若现有统一 handler 已覆盖，新增用例即可，不改实现。

**测试**
- `code/apps/api/tests/scenario/test_step7_pin_pg.py`（真库）
  - 场景 v1 kb={A}、v2 kb={A,B}，钉 v1 后请求 B → 被 resolver R4 拒绝；请求 A → 库位按 v1 映射。
  - 钉 v1、发布 v2 后，钉定客户端 `checksum` 仍为 v1。
  - 版本被手工删除（模拟损坏）→ `SCENARIO_UNRESOLVABLE` 500，不回落 published。
- `code/apps/api/tests/scenario/test_step7_db_ceiling.py`
  - `allowed_db_keys=NULL`：任意库位通过。
  - `allowed=['kb_finance']`：请求映射到 `kb_other` → 400 `DB_SCOPE_NOT_ALLOWED`。
  - `allowed=[]`：全部拒绝。
  - 开关关：同一请求通过，且不发 `tenants` 查询。

**完成判据**：钉定同时影响版本与库位；越界 400 且错误码稳定。

**commit**：`feat(step7): 钉定感知库映射与库可见性上限`

---

## [x] S7-4 三段折叠 + provenance

**文件**
- 修改 `code/apps/api/app/services/scenario/effective.py`
  - 签名：`effective_retrieval(policy, *, platform_rerank_enabled, caps: TenantPolicyCaps | None = None)`。
  - 返回对象增加 `sources: tuple[tuple[str, str], ...]`，键覆盖 `recall_top_k / rerank_enabled / threshold_vector / threshold_rerank`。
  - 折叠顺序：L0 平台硬上限 → L2 场景值 → L1 租户 cap；只有 L1 真正改变值才记 `tenant`。
  - 阈值方向：`max(当前值, threshold_*_min)`；rerank：`当前值 and rerank_allowed`（`rerank_allowed` 为 None 视为不限制）。
  - `clamped` 语义保持“平台收窄”；租户收紧单独体现在 `sources`。
- 修改 `code/apps/api/app/services/retrieve/__init__.py`
  - `retrieve(...)` 增加可选 `policy_caps: TenantPolicyCaps | None = None`；传给 `effective_retrieval`。
  - 现有“场景策略生效”info 日志追加 `sources`；关闭态且无 caps 时不产生新日志。
  - 不改任何语句块、不改召回/融合/阈值/引用校验顺序。
- 更新所有调用点（用 `grep "policy=snapshot.policy"` 与 `grep "effective_retrieval("` 全量核对）：
  - `app/api/chat.py`
  - `app/api/integrations.py:374,406`
  - `app/services/eval_service.py`
  - 其他 `retrieve(` 调用方若来自场景快照，传 `snapshot.caps`；非场景调用保持默认 `None`。

**测试** `code/apps/api/tests/scenario/test_step7_effective_provenance.py`
- 场景 `recall_top_k=10`，tenant cap 8 → effective 8，source `tenant`。
- cap 20（不收紧）→ effective 10，source `scenario`。
- 场景 300、平台上限 200、cap 180 → effective 180，source `tenant`；cap 250 → effective 200，source `platform`。
- 阈值：场景 0.5、cap 0.55 → 0.55 / tenant；cap 0.45 → 0.5 / scenario。
- rerank：平台开、场景开、cap false → false / tenant；cap true → 按原值 / scenario。
- 空 caps：输出与 Step 6 逐字段相等（含 `sources` 全为 `platform/scenario`）。

**完成判据**：provenance 可回答每个键的来源；平台收窄优先；拓扑零改动（`retrieve` 语句块 diff 仅签名与日志字段）。

**commit**：`feat(step7): 策略三段折叠与 provenance`

---

## [x] S7-5 治理面 API + 审计

**文件**
- 新增 `code/apps/api/app/api/tenant_governance.py`
  - `router = APIRouter(prefix="/admin", tags=["admin"])`，全部端点 `require_admin`。
  - `GET /tenant/db-scope` → `{allowed_db_keys: list[str] | null}`。
  - `PUT /tenant/db-scope` body `{allowed_db_keys: list[str] | null}`：
    - 关闭开关 → 400 `GOVERNANCE_DISABLED`；
    - 元素不匹配 `validate_db_key` → 400；
    - 审计 `tenant.db_scope.update`（before/after）；
    - 失效 `get_allowed_db_keys` 缓存。
  - `GET /scenario-governance` → 本租户场景清单：`id / scenario_key / name / status / db_binding / tier / pinned_version / caps`。
  - `GET /scenario-governance/{scenario_id}` → 详情 + `effective` 预览：
    - 每个策略键返回 `scenario / cap / effective / source`；
    - `source` 复用 `effective_retrieval` 的 `sources`。
  - `PUT /scenario-governance/{scenario_id}` body 允许字段：`pinned_version / recall_top_k_max / threshold_vector_min / threshold_rerank_min / rerank_allowed`；用 `model_fields_set` 区分“未传”和“显式 null”。
    - 关闭开关 → 400 `GOVERNANCE_DISABLED`；
    - 场景不属本租户 → 404；
    - pin 版本不存在或 state != published → 400 `PIN_VERSION_NOT_PUBLISHED`；
    - caps 区间：`recall_top_k_max ∈ [1, policy_spec.RECALL_TOP_K_MAX]`；两个阈值 ∈ `[0,1]` 且复用 `policy_spec` 的字段区间（不手抄第二份）；
    - 审计 `scenario.governance.update`；
    - 失效 resolver 缓存。
    - `tier` 与 `db_binding` **不在**本接口（契约里没有这两个字段，传了即 422）。
  - `PUT /scenario-governance/{scenario_id}/tier` body `{tier, db_binding}`（搬库完成登记）：
    - `tier` 与 `db_binding` 必须同时提交，**同一句 UPDATE** 写两列（`ck_scenarios_tier_binding` 行级即时校验，拆两条必违约）；
    - 只登记“数据已搬完”的事实，不搬数据；搬库仍走 Step 6 SOP；
    - `tier='hot'` → 400 `TIER_HOT_UNSUPPORTED`；
    - `cold` 必须 `db_binding=null`、`warm` 必须非 null，否则 400 `TIER_BINDING_MISMATCH`；
    - 库位格式非法（含保留虚拟键 `default`）→ 400 `DB_SCOPE_INVALID`；
    - 库位未在当前进程注册 → 400 `DB_BINDING_UNREGISTERED`；不在租户白名单 → 400 `DB_BINDING_NOT_ALLOWED`；
    - 审计只写一条 `scenario.tier.change`（同值重复 PUT 也写），detail 为 `{before: {tier, db_binding}, after: {tier, db_binding}}`；
    - 提交后失效 resolver 缓存。
- 修改 `code/apps/api/app/main.py:459` 附近：`app.include_router(tenant_governance_router, prefix="/api")`。
- `code/apps/api/app/services/tenant/governance.py`：上述写操作实现，审计用 `audit.record_with_session` 同事务。

**测试** `code/apps/api/tests/scenario/test_step7_api.py`
- 非管理员 → 403。
- 关闭开关下任意写 → 400 `GOVERNANCE_DISABLED`。
- 库白名单非法元素 → 400；合法 → 200 且审计行存在。
- pin 到 draft → 400；pin 到 published → 200；发布新版后老客户端仍解析 pin。
- 通用 PUT 传 `tier` → 422；改用策略字段仍 200。
- 搬库登记：cold→warm / warm→cold 成功且 DB 行同步；hot → 400；cold+非空 / warm+空 → 400；格式非法 / 未注册 / 白名单拒绝 → 400；跨租户或不存在 → 404；同值重复 PUT 两次都留 `scenario.tier.change`。
- `effective` 预览的 source 与 S7-4 单测一致。
- 同一请求重复写相同值：幂等 200，审计记录 before==after（仍写审计，因为治理动作可追溯）。

**完成判据**：越界全部 400 且错误码稳定；治理变更可追溯；关闭态不产生“配了不生效”的假象。

**commit**：`feat(step7): 租户治理面接口与审计`

---

## [x] S7-6 容量看板

**文件**
- 新增 `code/apps/api/app/services/capacity.py`
  - 常量：`M_SHARED_DECLARED_MB = 8192`、`M_CONTROL_PG_LOWER_MB = 128`、`M_SCENE_PG_MB = 300`、`UNBOUNDED_SERVICES = (...)`。
  - `def compute_m_a(n_hot_effective: int, *, m_shared_mb=..., m_scene_pg_mb=..., control_pg_mb=...) -> int`：纯函数，默认入参即三个声明常量。
  - `async def capacity_snapshot(session) -> dict`：
    - `SELECT tier, count(*) FROM scenarios GROUP BY tier`；
    - `SELECT datname, pg_database_size(datname), blks_hit, blks_read FROM pg_stat_database`，按已注册库名过滤；
    - `current_setting('shared_buffers')` 解析为 MiB；
    - 进程级 P95 从 `app.infra.metrics` 读取：HTTP `http.request.duration`（`scope=process-wide`）与检索 `rag.retrieve.total`；
    - 常量与阈值按请求从 `CAPACITY_*`（settings）读取；
    - 阈值四条逐条 `{name, threshold, unit, calibrated, evaluable, over, reason}`，`calibrated` 仅在配置 `CAPACITY_CALIBRATION_REF` 时为 true；
    - 返回 `calibration_ref`、`n_hot_declared`、`n_hot_effective`（本期恒 0）、`hot_unsupported:true`。
- 修改 `code/apps/api/app/config/placeholders.py`：登记 `CAPACITY_M_SHARED_DECLARED_MB`、`CAPACITY_M_CONTROL_PG_LOWER_MB`、`CAPACITY_M_SCENE_PG_MB`，并加导入期断言（镜像值必须与本模块一致）。
- 修改 `code/apps/api/app/infra/db_router.py`：新增 `database_name_for(db_key: str) -> str`，从已注册 DSN 解析库名；未知键抛既有 `DbRouteError`。
- 修改 `code/apps/api/app/api/system.py`：新增 `GET /system/capacity`，`require_admin`，返回 `capacity_snapshot`。

**测试** `code/apps/api/tests/scenario/test_step7_capacity.py`
- `compute_m_a(0)` 与 `compute_m_a(2)` 按固定输入复算正确。
- 返回结构含公式、输入、`result.postgres_side_lower_mb`、`result.calibrated`（无 ref 时为 false）。
- `M_SCENE_PG_MB` 默认标注未标定；`n_hot_declared=1` 时 `n_hot_effective=0` 且 `hot_unsupported=true`。
- 真库：`pg_stat_database` 返回的库名集合包含 `current_database()`；命中率计算与原始值一致。
- 非管理员 403。
- 不出现“假基线”：未填 `CAPACITY_CALIBRATION_REF` 时三条数值阈值 `calibrated=false`；实例内存或检索基线未提供时对应阈值 `evaluable=false`、`over=None`。

**完成判据**：验收“容量看板按 M_A(N) 算内存”可复算；所有占位值都带来源与标定状态。

**commit**：`feat(step7): 容量看板与分级观测`

---

## [x] S7-7 配额与审计回归

**文件**
- 无生产代码；若 `test_step7_audit.py` 发现 `scenario.version.archive` 缺审计，在 `app/services/scenario/write_service.py` 补齐，并单独 commit。

**测试**
- `code/apps/api/tests/scenario/test_step7_quota_aggregate.py`
  - 同一租户、两个应用（分别绑定两个场景）各记一次用量：租户日 token 键只累加一次口径，总数等于两次之和。
  - 同一应用跨两个场景：不产生第二个租户键。
  - 应用级 `daily_*` 仍按应用各自计（锁定“按凭证”的既有语义）。
- `code/apps/api/tests/scenario/test_step7_audit.py`
  - `scenario.version.validate / publish / archive` 三个动作都有审计行。
  - `tenant.db_scope.update / scenario.governance.update / scenario.tier.change` 三个治理动作都有审计行，detail 含 before/after。
  - 失败请求（400）不写成功审计。

**完成判据**：跨场景配额不漏算；治理与版本动作可追溯。

**commit**：`test(step7): 跨场景配额与治理审计回归`

---

## [x] S7-8 全量回归 + 测试报告

**动作**
1. 真库跑场景子集：
   ```powershell
   cd "c:\Users\86186\Desktop\AI Agent学习\file_rag_agent\code\apps\api"
   $env:SCENARIO_TEST_DB_URL="postgresql+asyncpg://user:<口令>@localhost:5433/kagent_test"
   .venv\Scripts\python.exe -m pytest tests/scenario -q
   ```
2. 全量：
   ```powershell
   .venv\Scripts\python.exe -m pytest tests/ -q
   ```
3. 对比失败集合：必须仍只有交接文档记录的 2 条环境失败；新增用例全绿。
4. 写 `04-test/第七阶段-Step7-测试报告.md`：命令、环境、计数、失败清单、每条验收口径的证据（测试名）。
5. 本地 commit：`docs(step7): 测试报告`。

**完成判据**：失败集合不扩大；报告能逐条对上第 6 节验收表。

---

## 全局检查清单（每项提交前）

- [ ] 注释中文、UTF-8。
- [ ] 代码无 emoji。
- [ ] 没有 fallback、没有旧兼容分支、没有“假数据兜底”。
- [ ] 关闭开关时行为与 Step 6 等价（测试断言，不靠口头）。
- [ ] 新增占位值已登记 `placeholders.py`。
- [ ] 新增错误码只在请求层转 400，不在服务层吞掉。
- [ ] 没有把 `db_binding` 暴露成可编辑表单项。
- [ ] 没有用 `WHERE tenant_id` 当检索隔离。
- [ ] commit 未推送。
