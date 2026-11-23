# 测试报告：第七阶段 Step 7 租户治理、分级与容量看板

- 被测提交链：`c895649`（S7-1）→ `6b5cdc4`（S7-2）→ `6588577`（S7-3）→ `0c4ac7b`（S7-4）→ `0f0442b`（S7-5）→ `8310cfb`（S7-6）→ `55e0fe6`（S7-7）
- 仓库：`c:\Users\86186\Desktop\AI Agent学习\file_rag_agent`
- 测试时间：2026-11-23（Asia/Shanghai）
- 设计依据：`02-design/第七阶段-Step7-前置裁决.md`（D1–D9）、`02-design/第七阶段-Step7-tasks.md`（S7-1–S7-7）

---

## 一、结论

**Step 7 的 S7-1 至 S7-7 验收通过，失败集合与基线完全一致，可以关闭本阶段（推送仍等用户明确同意）。**

- 真库全量：**1602 tests / 2 failed / 0 errors / 15 skipped**；两条 failed 与基线逐条相同，均为本机无网导致的环境失败，未修、未跳过。
- 关闭态等价性成立：治理开关默认 `False`，关闭态走原 SQL；S7-4 的逐字段与 fingerprint 断言锁死了「关闭 == Step 6」。
- 治理面写操作全部带审计，且只在成功分支写；失败请求不产生成功审计（实测钉定 draft 返回 400 后审计 0 行）。
- 容量看板只读，所有常量都返回 `calibrated: false`；`n_hot_declared` 与 `n_hot_effective` 分开返回，`hot_unsupported: true`。
- 未用新增 skip 换绿：接真库时 skipped 仍为 15，与基线相同。

---

## 二、环境

| 项 | 值 |
|---|---|
| OS | Windows（PowerShell 5） |
| Python | 3.13.14（系统 `python`，仓库无 `.venv`） |
| 数据库 | PostgreSQL 16 + pgvector，`localhost:5433` |
| 测试库 | `kagent_test`（口令取自 `code/.env` 的 `POSTGRES_PASSWORD`，报告中不出现） |
| 迁移头 | `0031_step7_tenant_governance` |
| ruff | 0.16.1 |

测试命令模板（每次真实执行，未并发起第二个 pytest 进程）：

```powershell
$pw = ((Get-Content "code\.env" | Where-Object { $_ -match '^POSTGRES_PASSWORD=' }) -replace '^POSTGRES_PASSWORD=', '').Trim()
$env:SCENARIO_TEST_DB_URL = "postgresql+asyncpg://user:$pw@localhost:5433/kagent_test"
cd code\apps\api
python -m pytest tests/ -q
```

---

## 三、新增与修改的用例

### 3.1 Step 7 新增用例（收集数 101）

| 文件 | 收集数 | 覆盖点 |
|---|---:|---|
| `tests/scenario/test_0031_migration.py` | 18 | 7 个新列、CHECK、索引、回填、downgrade、守卫块 |
| `tests/scenario/test_step7_governance.py` | 22 | 开关关闭零查询、钉定解析、caps 进快照、白名单三态 |
| `tests/scenario/test_step7_pin_pg.py` | 4 | 真库：钉定同时影响版本与库位；版本损坏不回落 |
| `tests/scenario/test_step7_db_ceiling.py` | 12 | `NULL` / `{}` / 白名单三态；越界 400；关闭态零查询 |
| `tests/scenario/test_step7_effective_provenance.py` | 16 | 三段折叠顺序、`sources`、空 caps 等价 |
| `tests/scenario/test_step7_api.py` | 13 | 5 个管理端点、403、开关、pin、tier、预览、幂等、失败不写审计 |
| `tests/scenario/test_step7_capacity.py` | 10 | `M_A` 复算、`shared_buffers` 解析、库观测、hot 分离、阈值结构 |
| `tests/scenario/test_step7_quota_aggregate.py` | 3 | 跨应用/跨场景的租户配额键口径、应用级键独立 |
| `tests/scenario/test_step7_audit.py` | 3 | 版本三动作审计、治理三动作审计、失败不写审计 |

### 3.2 对既有用例的最小修改（非放宽断言）

| 文件 | 修改 | 原因 |
|---|---|---|
| `tests/scenario/test_step4_guards.py` | 路由守卫过滤范围从「路径含 scenario」收窄为 `/api/scenarios*` 与集成绑定接口 | 新路由 `/api/admin/scenario-governance*` 会被旧过滤误判为场景写契约的多余路由 |
| `tests/scenario/test_step7_api.py` | 新增 `test_governance_router_is_mounted`，集合精确相等 | 补回被收窄过滤让出的守卫强度：少挂/多挂都会红 |
| `tests/scenario/test_step4_archive_impact.py` | 检索桩补 `policy_caps=None` | S7-4 已给 `retrieve_chain` 增加关键字参数，旧桩签名不匹配 |
| `tests/chat/*`、`tests/retrieve/test_chain.py`、`tests/scenario/test_policy_wiring.py`、`test_scenario_wiring.py`、`tests/test_integrations.py` | 替身签名补 `policy_caps=None`（部分新增 caps 断言） | 同上，S7-4 的签名变化 |
| `tests/scenario/test_step5_db_router_pg.py`、`test_step6_db_scope.py` | 直写 SQL 补 `tier='warm'` | 0031 的 `ck_scenarios_tier_binding` 要求 warm 必须有 `db_binding` |

---

## 四、实测记录

### 4.1 真库定向

```
cd code\apps\api
python -m pytest tests/scenario -q
```
`555 passed`（Step 7 全部新增用例包含在内，0 failed / 0 skipped）。

### 4.2 全量对照

| 环境 | tests | failed | errors | skipped | 失败 nodeid |
|---|---:|---:|---:|---:|---|
| 基线（交接文档 §3.4，接真库） | 1343 | 2 | 0 | 15 | 见下 |
| 本次（接真库） | 1602 | 2 | 0 | 15 | 见下 |
| 本次（不接真库） | 1602 | 2 | 0 | 154 | 见下 |

两条失败与基线逐条相同：

- `tests/test_health.py::test_openapi_contains_health`（`socket.gaierror [Errno 11001]`）
- `tests/wiki/test_worker_and_script.py::test_rebuild_wiki_script_fails_closed_when_disabled`（同上）

判据按设计文档 §7.3：`failed_after − failed_before = ∅`，按 nodeid 集合做差，不比数量。本次差集为空；接真库 skipped 仍为 15，未用新增 skip 换绿。

**tests 从 1343 涨到 1602（+259）的说明**：1343 是 Step 5 之前的交接基线，Step 5、6、7 的累计新增用例都在这个差额里；其中 Step 7 新增 94 个测试函数（收集 101 条，含参数化）。这不是「总数变多掩盖失败」：失败集合逐条比对过。

不接真库时 skipped 从 107 涨到 154（+47），全部是 Step 7 新增的 PG 依赖用例在无库环境下按既有夹具规则跳过；接真库后这 47 条真实执行。

### 4.3 ruff

对 Step 7 改动与新增的全部 Python 文件执行 `python -m ruff check`，`All checks passed`。

---

## 五、实测中发现并修正的问题（如实登记）

1. **0031 回填在场景库被写拦截触发器拒绝**：裸 `UPDATE scenarios` 触发 `FOR EACH STATEMENT` 写拦截。改为 `DO $mig$ IF EXISTS (SELECT 1 FROM scenarios) THEN UPDATE ... END`，并新增守卫用例。
2. **0031 回填与 NOT NULL 顺序**：回填必须先于 `tier NOT NULL`；迁移测试改为按 `SET tier = CASE` 定位并断言顺序。
3. **`version_source` 在关闭态误报 pinned**：FakeSession 返回了治理列；改为 `_row_from(..., governed=...)`，关闭态显式忽略治理列。
4. **`ScenarioRow` 扩字段导致 self_check 旧调用 TypeError**：self_check 显式传 `governed=True`。
5. **S7-4 关键字参数导致多个替身 TypeError**：全部替身签名补 `policy_caps=None`，不放宽业务断言。
6. **tier CHECK 与 Step 5/6 的直写 SQL 冲突**：测试 SQL 同步写 `tier='warm'`，不削弱约束。

以上均已随对应步骤提交，不是遗留问题。

---

## 六、已知边界与偏差（需读者知情）

1. **`compute_m_a` 增加可选关键字 `control_pg_mb`**：运行期要用 `current_setting('shared_buffers')` 覆盖常量 128，又不想把算术抄第二份，因此保留默认值、增加关键字参数。`compute_m_a(0)` / `compute_m_a(2)` 与任务书一致。
2. **编排口径与任务书描述不一致**：`docker-compose.yml`（dev）的 `mem_limit: 4g` 设在 reranker 与 ocr，`docker-compose.prod.yml` 设在 embedding 与 reranker。常量 8192（两处 4g）不变；`UNBOUNDED_SERVICES` 按 prod 口径列名；两处注释都写明了 dev/prod 差异，读数时需知道自己部署的是哪套。
3. **`database_name_for('default')` 读 `settings.database_url`**：测试机的 `.env` 指向 `postgres`（本机不可解析），而测试库是 `SCENARIO_TEST_DB_URL`；容量用例仅在该用例内把 `settings.database_url` 临时指向测试库，端点口径未变。生产环境两者一致。
4. **`TenantNotFoundError` 仍按 500 暴露**：租户行缺失属于数据损坏，不是「资源不存在」；与既有 `get_allowed_db_keys` 的处置一致，未自行发明 404 码。
5. **`scenario.tier.change` 只在值真变化时写**：同值重复 PUT 仍写 `scenario.governance.update`（before == after），不重复写 tier.change；与任务书「tier 变化另写」一致。
6. **关闭态仍可读管理端**：`GET /admin/tenant/db-scope` 返回库中已存值并带 `governance_enabled: false`，`GET /admin/scenario-governance/{id}` 的预览按关闭态计算（忽略 pin 与 caps），避免「配了不生效却看起来生效」。
7. **Step 6 遗留边界（本期未改）**：`db_scope._probe_scene_db_key` 的探测路径不经过 `resolve_kb_db_key`，因此不受库白名单约束。本期任务未要求改，登记为已知边界。
8. **容量看板的 P95 是全进程 HTTP 延迟**：取自 `http.request.duration`，`scope=process-wide`，不是按库或按检索链路；基线未标定，因此该阈值 `evaluable: false`，不给假判断。

---

## 七、提交链

| 提交 | 内容 |
|---|---|
| `c895649` | `feat(step7): 0031 租户治理与分级列` |
| `6b5cdc4` | `feat(step7): 租户治理读取与快照落地` |
| `6588577` | `feat(step7): 钉定生效与库可见性上限` |
| `0c4ac7b` | `feat(step7): 三段折叠与取值来源` |
| `0f0442b` | `feat(step7): 租户治理面接口与审计` |
| `8310cfb` | `feat(step7): 容量看板与分级观测` |
| `55e0fe6` | `test(step7): 跨场景配额与治理审计回归` |

所有提交均为本地提交，**未推送**。
