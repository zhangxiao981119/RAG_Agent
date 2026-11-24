# 第七阶段 Step7 遗留修复3 —— P3 独立对抗性验证报告

- 验证对象：方案 A「为分级转换增加专用搬库登记端点」（P2 已完成，未 commit）
- 验证环境：Windows / PowerShell 5 / Python 3.13.14 / `code\apps\api`；真库 `localhost:5433` → 容器 `code-postgres-1`
- 真库 DSN：`postgresql+asyncpg://user:***@localhost:5433/kagent_test`（口令已替换为 `***`）
- 验证角色：测试工程师（P3），独立于编码者；**未修改任何业务代码**，仅按授权在 `tests/scenario/test_step7_api.py` 追加 3 条用例并参数化 1 条（净增 4 个测试项）
- 结论：**PASS**（无阻断、无缺陷；0 建议需立即处理，4 条未覆盖/存疑；全量失败集合与基线完全一致，skip 数不变）

---

## 一、改动清单（`git diff --numstat` 实查）

| 文件 | 增 / 删 | 内容 |
| --- | --- | --- |
| `02-design/第七阶段-Step7-tasks.md` | +14 / -5 | 通用 PUT 允许字段去掉 `tier`；新增 `/tier` 端点契约、错误码、审计形状 |
| `02-design/第七阶段-Step7-前置裁决.md` | +8 / -0 | 3.9 后补「写入路径收窄」说明（同句 UPDATE、只登记不搬数据、审计形状） |
| `code/apps/api/app/schemas/tenant_governance.py` | +18 / -4 | `GovernanceUpdate` 移除 `tier`；新增 `ScenarioTierIn{tier, db_binding}`（均 required，`extra=forbid`） |
| `code/apps/api/app/api/tenant_governance.py` | +35 / -0 | 新增 `PUT /scenario-governance/{scenario_id}/tier`；写后复用 `get_scenario_governance` + `_detail_out` |
| `code/apps/api/app/services/tenant/governance.py` | +146 / -33 | 新增 2 个错误码与 HTTP/消息映射；`_FIELD_COLUMNS` 移除 tier；通用 PUT 删除 tier 分支；新增 `_SCENARIO_TIER_UPDATE_SQL`、`registered_db_keys` 延迟代理、`update_scenario_tier` |
| `code/apps/api/tests/scenario/test_step7_api.py` | +365 / -45 | tier 段整体重写为专用端点用例；通用 PUT 收窄用例；失败路径「行与审计不变」用例；本次补测净增 4 项 |
| `code/apps/api/tests/scenario/test_step7_audit.py` | +10 / -4 | ③ 分级变化改走新端点，断言 before/after 由字符串升级为 `{tier, db_binding}` |

`git status` 另有 6 个 M 文件（`app/api/kbs.py`、`app/api/sync.py`、`app/services/sync_service.py`、`scripts/rebuild_wiki.py`、`tests/chat/test_ask_with_image.py`、`tests/wiki/test_worker_and_script.py`）：`git diff --numstat -- <这 6 个文件>` **输出为空**（仅 stat 变化），与本次变更无关，**未计入改动清单，也不应加入提交**。

静态审查：
- `python -m ruff check`（5 个被测文件）：`All checks passed!`
- 无超范围改动：diff 只落在治理面（schema / api / service / 两份 doc / 两个测试文件）。
- 无 fallback：新函数所有拒绝分支均 `raise`，没有「猜库位 / 自动选 default / 静默忽略」分支。
- 注释全中文、UTF-8；`★` 为仓库既有注释符号（U+2605），非 emoji。

---

## 二、8 条设计承诺逐条对账

### 承诺 1：原子性——tier 与 db_binding 同一条 UPDATE

- 代码证据：`governance.py:291-297` 常量 `_SCENARIO_TIER_UPDATE_SQL` 为 `UPDATE scenarios SET tier = :tier, db_binding = :db_binding WHERE id = :scenario_id AND tenant_id = :tenant_id`，两列同一句写入；列名写死在常量，不由请求键拼装。
- 代码证据：`update_scenario_tier`（`governance.py:714-827`）全部校验（开关 / 404 / hot / 一致性 / 格式 / 注册 / 白名单）都在该 UPDATE 之前，失败即 `raise GovernanceError`，不执行 UPDATE、不 commit。
- 真库证据：
  - `test_tier_failed_paths_leave_row_and_audit_untouched`：连续 5 条失败请求（hot / cold+非空 / warm+空 / `default` / 未注册），全部 400；独立连接回读仍为 `{"tier": "cold", "db_binding": None}`，`scenario.tier.change` 命中 0 条。
  - 各拒绝用例（`test_tier_hot_is_rejected`、`test_tier_binding_mismatch_rejected`、`test_tier_rejects_invalid_db_key`、`test_tier_rejects_unregistered_db_key`、`test_tier_rejects_db_key_outside_tenant_whitelist`）均自带 `_tier_row(...)` 行不变断言。
  - 成功路径 `test_tier_cold_to_warm_updates_row_and_invalidates_cache`：请求 200 后独立连接读到 `{"tier": "warm", "db_binding": "kb_finance"}`，证明单句同时生效（若拆两条，第二条前就已被 `ck_scenarios_tier_binding` 拒绝）。

### 承诺 2：拒绝矩阵与错误码

| 场景 | 期望 | 用例（真实执行通过） |
| --- | --- | --- |
| `tier=hot`（binding 有/无） | 400 `TIER_HOT_UNSUPPORTED` | `test_tier_hot_is_rejected[with-binding/without-binding]`（**本次补**参数化，证明 hot 判据先于一致性判据） |
| `cold` + 非空 binding | 400 `TIER_BINDING_MISMATCH`，context 精确 | `test_tier_binding_mismatch_rejected[cold-with-binding]` |
| `warm` + null binding | 400 `TIER_BINDING_MISMATCH`，context 精确 | `test_tier_binding_mismatch_rejected[warm-without-binding]` |
| 格式非法：`default` / `kb_a` / `KB_FINANCE` / `finance` | 400 `DB_SCOPE_INVALID`，`field=db_binding` | `test_tier_rejects_invalid_db_key`（4 参数） |
| 格式合法但未注册 | 400 `DB_BINDING_UNREGISTERED`，context=`{db_binding, registered}` | `test_tier_rejects_unregistered_db_key` |
| 已注册但不在租户白名单 | 400 `DB_BINDING_NOT_ALLOWED`，context=`{allowed, denied}` | `test_tier_rejects_db_key_outside_tenant_whitelist`（正向对照 `test_tier_accepted_when_whitelist_contains_key`） |
| 不存在 / 跨租户 | 404 `SCENARIO_NOT_FOUND` | `test_tier_scenario_not_found[missing/cross-tenant]` |
| 治理关闭 | 400 `GOVERNANCE_DISABLED` | `test_tier_rejected_when_governance_disabled` |

- 代码证据：`_GOVERNANCE_HTTP` 新增两码均 400（`governance.py:202-203`）；`_GOVERNANCE_MSG` 补齐中文消息（`:214-215`）。断言全部写字面量码值，不引用枚举。

### 承诺 3：审计只写一条 `scenario.tier.change`，detail 含 before/after 双字段；同值重复仍写；不写 `governance.update`

- 代码证据：`update_scenario_tier` 内唯一一次 `audit.record_with_session(... "scenario.tier.change" ...)`（`governance.py:808-819`），detail=`{"before": {tier, db_binding}, "after": {tier, db_binding}}`；函数内无 `scenario.governance.update`。
- 真库证据：
  - `test_tier_repeated_put_is_idempotent_and_audited`：两次同值 PUT 均 200；`scenario.tier.change` **精确 2 条**；第 1 条 before=`cold/None`、after=`warm/kb_finance`；第 2 条 `before == after == warm/kb_finance`；`scenario.governance.update` 为 **0 条**。
  - `test_tier_warm_to_warm_changes_binding_only`（**本次补**）：tier 不变、仅换库位，审计 1 条且 before/after 只差 `db_binding`。
  - `tests/scenario/test_step7_audit.py::test_governance_actions_are_audited` ③：改走新端点后 `len(tier_details) == 1`，before/after 均为 `{tier, db_binding}` 对象。

### 承诺 4：缓存失效在 commit 之后；失败路径不失效

- 代码证据：`governance.py:821-826` 顺序为 `await session.commit()` → `from app.services.scenario import resolver` → `resolver.invalidate(tenant_id, scenario_id)`（通用 PUT 同样在 `:705-710`）。所有拒绝分支都在 commit 之前 `raise`，不会走到 invalidate。
- 真库证据：
  - `test_tier_cold_to_warm_updates_row_and_invalidates_cache`：猴补丁记录调用，`invalidated == [(api.tenant_id, scenario_id)]` 精确一次、参数为本场景；写后 GET 与写响应 `==` 逐字段一致。
  - `test_tier_rejected_when_governance_disabled`：`invalidated == []`。
  - `test_tier_failed_paths_do_not_invalidate_cache`（**本次补**）：hot / 格式非法 / 未注册三条失败路径后 `invalidated == []`。

### 承诺 5：通用 PUT 不再接受 tier（422），策略字段仍可用；OpenAPI 中 tier 只在新端点

- 真库证据：
  - `test_governance_update_rejects_tier_field`：`PUT /scenario-governance/{id}` 传 `{"tier":"warm"}` → **422**，响应含 `tier`，DB 行不变。
  - `test_governance_update_still_accepts_policy_fields`：`{"recall_top_k_max": 8}` → 200、`caps.recall_top_k_max == 8`、tier/binding 不变。
- OpenAPI 证据（见「四、E」）：通用 PUT 请求体 `GovernanceUpdate` 属性为 `['pinned_version','recall_top_k_max','rerank_allowed','threshold_rerank_min','threshold_vector_min']`，**无 tier**；新端点请求体 `ScenarioTierIn` 属性 `['db_binding','tier']`、required `['tier','db_binding']`、`additionalProperties=false`。

### 承诺 6：路由守卫集合精确包含新路由

- `test_governance_router_is_mounted` 使用**集合精确相等**（漏挂红、多挂也红），expected 已含 `"/api/admin/scenario-governance/{scenario_id}/tier": {"put"}`。该用例通过。

### 承诺 7：文档描述与代码一致

- `第七阶段-Step7-tasks.md`：通用 PUT 允许字段已删 `tier`，并显式写明「传了即 422」；新端点条目逐条对应实现（同句 UPDATE、只登记不搬数据、hot/一致性/格式/未注册/白名单/404、审计一条且 detail 为 before/after 对象、提交后失效）。
- `第七阶段-Step7-前置裁决.md`：3.9 末尾新增段落，说明收窄原因（CHECK 行级即时校验，拆两条必违约）与「只登记事实、不搬数据」；与代码 docstring 一致。
- 未发现「文档说搬数据 / 代码不搬」之类的偏差。

### 承诺 8：`registered_db_keys` 代理不在 import 期构建 `app.database`

- 实验（干净进程，`code\apps\api` 下）：
  ```
  app.database imported: False
  app.infra.db_router imported: False
  registered_db_keys is function: True
  ```
  即 `import app.services.tenant.governance` 不拉入 `app.database`（后者 import 期会 `create_async_engine`）。
- 用例侧：`_register_db_keys` 通过 monkeypatch 替换 `governance_mod.registered_db_keys` 生效，证明服务内部解析的是模块级别名（可注入、可测）。

---

## 三、必做验证执行结果（真实命令与输出）

环境注入（口令替换为 `***`）：
```powershell
$pw = ((Get-Content "code\.env" | Where-Object { $_ -match '^POSTGRES_PASSWORD=' }) -replace '^POSTGRES_PASSWORD=', '').Trim();
$env:SCENARIO_TEST_DB_URL = "postgresql+asyncpg://user:$pw@localhost:5433/kagent_test"
```

1) 定向四文件：
```powershell
python -m pytest tests/scenario/test_step7_api.py tests/scenario/test_step7_governance.py tests/scenario/test_step7_audit.py tests/scenario/test_step4_guards.py
```
```
72 passed in 17.35s
```

2) scenario 全目录：
```powershell
python -m pytest tests/scenario
```
```
601 passed in 114.38s (0:01:54)
```

3) 全量：
```powershell
python -m pytest
```
```
2 failed, 1642 passed, 15 skipped, 1 warning in 154.08s (0:02:34)
```

全量失败集合（仅两条，与基线**完全一致**）：
```
FAILED tests/test_health.py::test_openapi_contains_health - socket.gaierror: [Errno 11001] getaddrinfo failed
FAILED tests/wiki/test_worker_and_script.py::test_rebuild_wiki_script_fails_closed_when_disabled - assert 'WIKI_ENABLED=false' in 'Traceback ... socket.gaierror: [Errno 11001] getaddrinfo failed'
```

与基线比对：

| 指标 | 基线 | 本次 | 差异 |
| --- | --- | --- | --- |
| failed nodeid 集合 | 上述 2 条 | 上述 2 条 | **无新增** |
| skipped | 15 | 15 | **无新增 skip** |
| passed | 1621 | 1642 | +21 = P2 净增 17 + 本次补测 4 |
| collected | 1638 | 1659 | +21 |

> 过程说明（非缺陷）：首轮验证时我在后台全量仍在运行时又并发跑了一次 `tests/scenario`，两个进程同时重建测试 schema，触发 `DROP INDEX idx_integration_app_scenario` 的锁等待（`1 passed, 32 errors`）。这是我的并发操作失误；已串行重跑三组命令，本报告所有数字均来自**无并发**的最终运行。

---

## 四、补测与静态检查

### D. 补测缺口（本次新增，未改弱任何既有断言）

| 缺口 | 补测用例 | 结果 |
| --- | --- | --- |
| `db_binding` **省略**（而非 null） | `test_tier_omitting_db_binding_is_422`：`{"tier":"warm"}` → 422，含 `db_binding`，行不变、审计 0 条 | 通过 |
| 只有 binding 变化（tier 不变）是否同一端点覆盖 | `test_tier_warm_to_warm_changes_binding_only`：warm/kb_finance → warm/kb_hr，行与审计 before/after 正确 | 通过 |
| 失败路径不失效缓存 | `test_tier_failed_paths_do_not_invalidate_cache`：hot / 格式非法 / 未注册后 `invalidated == []` | 通过 |
| hot 判据优先级（binding 有无都应是 `TIER_HOT_UNSUPPORTED`） | `test_tier_hot_is_rejected` 参数化 `[with-binding, without-binding]` | 通过 |
| 失败后 DB 行与审计不变 | 既有 `test_tier_failed_paths_leave_row_and_audit_untouched`（5 条失败体） | 通过 |
| 同值重复 PUT 审计精确 2 条 | 既有 `test_tier_repeated_put_is_idempotent_and_audited`（`len(rows) == 2`） | 通过 |
| 只有 tier 变化 | **不可构造**：`ck_scenarios_tier_binding` 下 tier 变必然伴随 binding 变（cold↔warm）；cold→warm 用例同时体现两列变更 | 说明项 |

### E. OpenAPI 静态检查（`app.openapi()`）

```
generic model: GovernanceUpdate
generic props: ['pinned_version', 'recall_top_k_max', 'rerank_allowed', 'threshold_rerank_min', 'threshold_vector_min']
generic required: None
tier model: ScenarioTierIn
tier props: ['db_binding', 'tier']
tier required: ['tier', 'db_binding']
tier additionalProperties: False
CHECK generic_has_no_tier: True
CHECK new_has_both: True
CHECK new_both_required: True
```

---

## 五、B 项：测试是否被削弱 / 覆盖是否迁移而非删除

- 原 `test_tier_hot_rejected_and_warm_requires_binding`（1 条）拆分并**增强**为：hot（2 参数）+ 一致性（2 参数）+ 格式（4 参数）+ 未注册 + 白名单拒绝 + 正向对照，覆盖从「2 个错误分支」扩到「全部 7 类拒绝」。
- 原 `test_repeated_put_is_idempotent_and_audited`（经通用 PUT 改 tier）迁移为 `test_tier_repeated_put_is_idempotent_and_audited`（经新端点），并升级断言：审计条数由「仅变化时 1 条」变为「每次都写，2 条」——与新契约一致，**不是放宽**。
- 原「通用 PUT 改 tier 成功」的路径没有丢失：由 `test_tier_cold_to_warm_updates_row_and_invalidates_cache` 通过新端点覆盖；同时新增 `test_governance_update_rejects_tier_field` 锁定 422（比原路径更严格）。
- 库读一律走独立连接（`committed_rows` / `_exec_committed`），未出现「读响应体自证」。
- 全量 skip 数 15 与基线一致，**没有新增 skip 换绿**。

---

## 六、发现的问题

**无阻断、无缺陷。**

（说明：`update_scenario_governance` 的 commit + invalidate 顺序在本次 diff 中按行看像是新增，实为原文件尾部代码保留、新函数在文件末尾追加；已用 `git show HEAD:...` 对账，不存在重复 commit / 重复 return。）

---

## 七、未覆盖 / 存疑项

1. **空白名单 `{}`（全部拒绝）未在新端点用例中覆盖**：代码路径 `allowed is not None and db_binding not in allowed` 对空集合 fail-closed，语义正确；现有 CASE 由既有的 `db-scope` 测试覆盖，但未用新端点串起来。建议后续补 1 条（非阻断）。
2. **「失效发生在 commit 之后」为代码行序证据**：`_capture_invalidate` 只能记录调用与参数，同步函数内无法 await 独立连接回读库来运行时证明「已提交」。行序 `commit → invalidate`（`:821-826`、`:705-710`）+ 失败路径零调用已足够判定，如需运行时强证明需改注入方式。
3. **`cold → cold`（tier=cold, binding=null）同值重放**未单测：与 `warm` 同值重放共用同一条路径（跳过格式/注册/白名单校验后直接 UPDATE + 审计），风险低。
4. **本机并发跑测试会因 schema fixture 重建互相阻塞**（`DROP INDEX ... AccessShareLock`）：属验证环境特性，串行执行即可，不影响代码结论。

---

## 八、最终结论

**PASS**

- 8 条设计承诺全部有代码 + 真库用例双重证据；补测 4 项后无红灯。
- 三组命令：`72 passed` / `601 passed` / `2 failed + 1642 passed + 15 skipped`。
- 全量失败 nodeid 集合与基线完全相同，skip 数不变，无新增失败、无新增 skip。
- 未修改业务代码；新增测试全部为补强，未改弱既有断言。
