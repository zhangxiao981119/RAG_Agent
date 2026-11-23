# 测试报告 · Step 6 / S6-4B（写路径 db_key 贯通）与 S6-4C（按 ID 寻址）【终版】

> 角色：独立测试工程师（未参与编码；不改业务代码、不 commit/push）
> 被测变更：`781492f`（主体）→ `86bee2d`（全库扫描/治理逐库位）→ `d637f3b`（backfill 夹具同步、结构化错误）→ `2c2a79d`（按 ID 改为控制面副本推导）→ `34720f0`（设计文档）→ **`c69a2a3`（控制副本优先 + 缺失场景探测）** → `d7c993d`（设计文档同步，HEAD）
> 本终版对应 HEAD=`d7c993d`；核心逻辑变更为 `c69a2a3`
> 验收依据：`02-design/第六阶段-Step6-前置裁决.md` §4.2、§4.6、§4.10（D-S6-3-2 现为「控制面副本优先 + 缺失时探测」，评分 91，原「顺序探测」作废）
> 环境：Windows PowerShell 5，Python 3.13.14（项目声明 `>=3.12,<3.13`），PostgreSQL 16.15（容器 `code-postgres-1`，宿主 5433，账号 `user`），控制/测试库 `kagent_test`，工作目录 `code/apps/api`
> 口令来源：`code/.env` 的 `POSTGRES_PASSWORD`，仅注入进程环境变量，**未写入本文件或任何仓库文件**
> 时间：2026-11-23

---

## 1. 最终结论：**定向复验通过（6/6），无阻断缺陷，上轮 D4 已关闭，可进入 S6-5**

`c69a2a3` 将按 ID 寻址改为「控制面副本优先、缺失时探测已注册场景库」：

- 控制库有副本 → 按其 `kb_id` 推导库位，**不探测**场景库（真库实测：注册一个指向不存在数据库的库位键，推导仍正常返回，证明未触碰探测分支）；
- 控制库无副本 → 顺序探测已注册场景库：0 命中 → None（端点 404）、1 命中 → 该键、≥2 命中 → `DB_SCOPE_AMBIGUOUS`（结构化 detail）；
- wiki 页：控制库有页副本且归属文档副本在 → 按 kb 推导；归属文档副本缺失 → 按 `document_id` 探测场景库；页副本缺失 → 按 `page_id` 探测（0→404）；
- 关闭态两个入口仍直接返回 `default` 且零 SQL，端点行为与 `c69a2a3^` 一致；
- 内容查询（文档行、chunk、`storage_key`、wiki 页与可见性 SQL）全部在 `open_data_session` 打开的库位会话执行，控制副本不进入响应。

上一轮唯一的高严重度项 **D4（切绑后新文档在控制库无副本 → 按 ID 404）已由本轮探测路径关闭**：真库实测场景库独有文档可正确路由。本轮新增 3 项低严重度问题（测试引擎未 dispose、探测期异常未结构化/并发口径、孤儿 wiki 页 404 口径），均不阻断 S6-5。

| 项 | 结果 |
|---|---|
| 阻断缺陷 | 0 |
| 新增缺陷 | 3 项低（D6、D7、D8） |
| 历史缺陷 | D1（86bee2d 关闭）、D2/D3（d637f3b 关闭）、**D4（c69a2a3 关闭）**；D5 保留（口径，SOP 约束） |
| 用例 | 定向 46 条通过；`test_step6_db_scope.py` 18 条通过（0 skip，含 2 条真库）；回归批次 642 条：1 failed（已知环境项）/ 0 errors / 12 skipped |
| 是否可进入 S6-5 | **可**，须按 §5 的 SOP 约束执行存量搬迁（回滚增量回拷、库位注册与数据位置同源、上线连通性检查等） |

---

## 2. 本轮六项复验清单

| # | 复验项 | 结论 | 关键证据 |
|---|---|---|---|
| 1 | 控制库有副本 → 按 kb 解析库位且**不探测**场景库 | PASS | 单测 `test_resolve_document_control_copy_wins_without_probing` / `test_resolve_wiki_page_control_copy_derives_from_owner_document` 断言 `probed == []`（对 `db_scope.data_session` 装记录替身）；独立真库探针：同时注册 `kb_ghost`（指向不存在的库）与 `kb_probe`，控制副本推导返回 `kb_probe` 且探测调用 `[]`，`kb_ghost` 未被连接 |
| 2 | 控制库无副本 → 探测已注册场景库：0→None/404、1→该键、≥2→`DB_SCOPE_AMBIGUOUS`；真库两库复跑；清理临时库与引擎 | PASS（清理有一项低缺陷，见 D6） | 真库（两个临时场景库 + 控制库 `kagent_test`）：`C1 文档仅场景A = kb_scene_a`、`C2 wiki 页仅场景A = kb_scene_a`、`C5 随机 id = None`、`C6 两场景同 id = DB_SCOPE_AMBIGUOUS {'model':'Document','db_keys':['kb_scene_a','kb_scene_b']}`；全过程探测顺序均 `['kb_scene_a','kb_scene_b']`；清理后临时库残留 `[]`、控制库残留文档 `0`；探针自身对各库位引擎 `await dispose()` 成功（`checkedout_before=0`）。**仓库真库用例只 DROP 库、未 dispose `register_from_settings` 创建的场景引擎**（D6） |
| 3 | wiki 新页面只在场景库 → 详情可路由到该场景；控制页存在但 document 缺失 → 以实际实现为准并判断合理性 | PASS（实现为 document 探测，合理） | `resolve_wiki_page_db_key`：页缺失 → 按 `page_id` 探测（真库 `C2`）；页在控制库、归属文档副本缺失 → 按 `document_id` 探测（真库 `C3 = kb_scene_a`）；文档处处不存在 → None（真库 `C4`，端点 404）。该分支正常库下不可达（`wiki_pages.document_id` 有 FK 到 `documents`，页副本存在即文档副本必在），探针用 `session_replication_role=replica` 构造损坏态才可验证；由 500 `DB_SCOPE_OWNER_UNRESOLVED` 改为 404 属口径选择（D8） |
| 4 | 关闭态零 SQL、行为与基线一致；T-5-7 白名单 3 处且定义唯一 | PASS | 真探针：`A 关闭态 doc/wiki=default 零SQL=True 新增探测=0`；关闭态在 `db_scope.py:241-242`、`:262-263` 提前返回，未触及新代码；`app/` 内 `data_session(` 共 3 处（`db_scope.py:181/201/216`），`def data_session` 仅 `db_router.py:258`；T-5-7 已同步为 3 处并实测通过 |
| 5 | 回归：scenario 真库全目录 + documents + wiki + chat；对比 `c69a2a3^` 无断言弱化 | PASS | `tests=642 failures=1 errors=0 skipped=12 time=78.582s`；唯一失败为已知环境项（`.env` `WIKI_ENABLED=true` → 子进程 docker 主机名 DNS）；测试 diff 为「旧推导用例 → 更细分用例」，无删除/放宽（§3.5） |
| 6 | 是否可进入 S6-5（存量搬迁脚本） | 可（附 §5 约束） | 无阻断缺陷；D6/D7/D8 均为低严重度，S6-5 需按 §5 纳入上线检查 |

---

## 3. 执行证据

### 3.1 代码审阅要点（`git show c69a2a3`）

1. `_probe_scene_db_key`（`db_scope.py:205-227`）：遍历 `registered_db_keys()` 并跳过 `default`；每个键用 `data_session(db_key)` 做一次 `session.get(model, pk)`；0 → None，1 → 该键，≥2 → `DbScopeError(DB_SCOPE_AMBIGUOUS, {model, pk, db_keys})`。不回落 default。
2. `resolve_document_db_key`（`db_scope.py:230-248`）：关闭态直接 `DEFAULT_DB_KEY`；控制副本命中 → `resolve_kb_db_key(tenant_id, [kb_id])`（不探测）；未命中 → `_probe_scene_db_key(Document, doc_id)`。
3. `resolve_wiki_page_db_key`（`db_scope.py:251-273`）：页副本缺失 → 按 `page_id` 探测，否则读 `page.document_id` 的控制副本：有 → 按 kb 推导；无 → 按 `document_id` 探测。
4. `error_detail`（`db_scope.py:276-290`）仍把 `DbScopeError` 规整为 `{"code": ...}`；`documents.py:147-162` 与 `wiki.py:187-212` 的 500/404 映射未改动，6 个文档端点在 `open_data_session` 内取内容。
5. 本轮 app 侧只改 `db_scope.py`（`git show --stat`：4 文件，其余 3 个为测试），端点代码零改动。
6. `DB_SCOPE_OWNER_UNRESOLVED` 常量与引用已全部移除（全仓 grep 0 命中，仅旧报告文本提及）。

### 3.2 独立真库探针（控制库 `kagent_test` + 两个临时场景库，alembic head）

```
A 关闭态: doc/wiki=default 零SQL=True 新增探测=0
B 控制副本: key=kb_probe 探测调用=[] created_engines=2
B dispose: ['kb_ghost:checkedout_before=0', 'kb_probe:checkedout_before=0']
C1 文档仅场景A: kb_scene_a 探测=['kb_scene_a', 'kb_scene_b']
C2 wiki页仅场景A: kb_scene_a 探测=['kb_scene_a', 'kb_scene_b']
C3 控制页+文档仅场景A: kb_scene_a 探测=['kb_scene_a', 'kb_scene_b']
C4 孤儿页(文档不存在): None 探测=['kb_scene_a', 'kb_scene_b']
C5 随机id: None 探测=['kb_scene_a', 'kb_scene_b']
C6 两场景同id: ('DB_SCOPE_AMBIGUOUS', {'model': 'Document', 'pk': '...', 'db_keys': ['kb_scene_a', 'kb_scene_b']})
C dispose: ['kb_scene_a:checkedout_before=0', 'kb_scene_b:checkedout_before=0']
清理: 控制库残留文档=0
清理: 临时库残留=[]
```

`B` 的 `kb_ghost` 指向不存在的数据库：若实现探测了它必然抛连接错误，实际未抛且探测记录为空，证明「控制副本命中不探测」。
端点级映射（None → 404）由既有单测 `test_document_by_id_not_found` 覆盖并通过。

### 3.3 探测期失败形态探针（D7 证据）

注册 `kb_ghost`（不存在的库）后按随机 id 推导：

```
未结构化异常 asyncpg.exceptions.InvalidCatalogNameError: database "kagent_not_exist_db" does not exist
```

即：已注册但不可达的场景库会让「控制库无副本」的按 ID 请求抛出非 `DbScopeError` 异常，端点表现为裸 500（非 `error_detail` 结构）。

### 3.4 回归统计

```
$env:SCENARIO_TEST_DB_URL = "postgresql+asyncpg://user:<pw>@localhost:5433/kagent_test"
python -m pytest tests/scenario/test_step6_job_routing.py tests/scenario/test_step5_db_router.py -q
→ tests=46 failures=0 errors=0 skipped=0
python -m pytest tests/scenario/test_step6_db_scope.py -q
→ tests=18  failures=0 errors=0 skipped=0   # 含 2 条真库用例，实跑未 skip
python -m pytest tests/scenario tests/documents tests/wiki tests/chat -q
→ tests=642 failures=1 errors=0 skipped=12 time=78.582s
  · 1 = 已知环境项：test_rebuild_wiki_script_fails_closed_when_disabled（.env WIKI_ENABLED=true → docker 主机名 DNS）
  · 12 skip = env-gated 真库用例（WIKI_ACL_DB_URL / WIKI_NAV_ACL_DB_URL 未提供）
```

### 3.5 测试未被弱化（对比 `c69a2a3^`）

- `test_step6_job_routing.py`：删除 3 条「控制库无行 → None / 同 id 不判歧义」用例，替换为 8 条（0/1/≥2 命中、控制副本优先不探测、未绑定 kb → default、wiki 页三条路径），断言更具体（`probed == []` / `probed == [...]`、`session.calls`）。
- `test_step6_db_scope.py`：错误码用例由 `DB_SCOPE_OWNER_UNRESOLVED` 改为 `DB_SCOPE_AMBIGUOUS`（对应设计修订）；新增真库用例 `test_real_db_scene_only_probe_and_ambiguous`（两个临时库、唯一命中、同 id 歧义）。
- `test_step5_db_router.py`：T-5-7 调用点白名单 2 → 3，并补注释说明第三个调用点来源；定义唯一断言不变。
- 未发现删除或放宽既有断言。

### 3.6 提交边界

`c69a2a3` 仅 4 个文件（`app/services/db_scope.py` + 3 个测试）；`d7c993d` 仅设计文档 1 行；**均未包含 `04-test/` 报告**。工作区仍只有两份报告未跟踪。

---

## 4. 缺陷清单

| 编号 | 严重度 | 描述 | 位置 | 复现 | 建议 |
|---|---|---|---|---|---|
| D6 | 低（测试卫生） | 新增真库用例用 `register_from_settings()` 创建了 `kb_scene_a/b` 引擎，`finally` 只重注册（`_REGISTRY.clear()`）并 `pg_terminate_backend` + DROP 库，**未 dispose 这两个引擎**；`_CREATED_ENGINES` 计数同进程内持续累加，池中连接被强制终止后由 DROP 掩盖 | `tests/scenario/test_step6_db_scope.py:530-602`；`db_router.register_from_settings()` 无 dispose 导出 | 代码审阅（对照同文件 `_run_control_copy_same_id` 仅 dispose 自己创建的 base 引擎） | 在重注册前对每个非 default 键 `await db_router.engine_for(k).dispose()`（我的独立探针已按此写法验证可行） |
| D7 | 低（可用性耦合/口径） | 探测路径在控制会话事务未收尾时开新会话：单请求最多 2 条在途连接，与 `open_data_session` 文档「两段顺序不重叠，同一时刻在途连接 1 条」的表述不符；且任一已注册场景库不可达时抛裸异常（非 `DbScopeError`）→ 端点 500 无结构化 detail | `db_scope.py:205-227`、`:230-248`；对照 `db_scope.py:165-183` | §3.3 实测 `InvalidCatalogNameError` | 二选一：探测前先 `commit()` 收尾控制事务；把探测期连接/目录错误包成 `DbScopeError`（如 `DB_SCOPE_PROBE_UNAVAILABLE`）并在 detail 标注库位键；同步修正「1 条在途连接」的口径或明确探测为例外 |
| D8 | 低（口径） | `DB_SCOPE_OWNER_UNRESOLVED` 移除后，wiki 孤儿页（控制页在、归属文档处处不存在）由结构化 500 变为 404，损坏态被静默；该状态正常库下不可达（FK 保证） | `db_scope.py:251-273`；`wiki.py:187-212` | 真库 `C4`（需 `session_replication_role=replica` 构造） | 可接受；若希望暴露损坏态，可保留一个结构化 500 分支并在运维巡检中对孤儿页单独告警 |
| D5 | 低（口径，保留） | 控制副本 `kb_id` 与场景行不一致（迁移后变更文档归属）会推导到错误库位，无一致性校验 | `db_scope.py:243-247` | 代码审阅 | 由 SOP 冻结切绑后 kb 归属（§5.5） |
| R1 | 低（覆盖） | 真库覆盖到「推导层」（文档/wiki/歧义/清理）；**端点级真库**（HTTP + 数据面可见性 SQL）仍未覆盖 | `tests/scenario/test_step6_db_scope.py` | 代码审阅 | S6-5 迁移后补一条端到端用例（wiki 详情 + 文档详情，场景库造 chunk） |
| R2 | 低（环境） | 已知 1 条环境失败与 12 条 env-gated skip，与本次提交无关 | `tests/wiki/test_worker_and_script.py` | §3.4 | 记录在案 |

已关闭：上轮 D4（切绑后新文档按 ID 404）—— `c69a2a3` 探测路径真库实测可路由；上轮 D1/D2/D3 见前版。

---

## 5. 对 S6-5 迁移 SOP 的具体约束

1. **写暂停窗口覆盖 COPY 与切绑**：§4.3 第 10 步冻结写入必须在第 12 步 COPY 之前生效，直到绑定切换完成、差集/清单校验通过后才放开。
2. **库位注册必须与数据位置同源**：探测只认「本进程已注册的场景库键」，未注册/改名场景库里的新文档会被判 404。存量搬迁脚本必须在切换 DSN 注册的同一步完成数据落位，禁止「数据先搬、注册后补」。
3. **回滚前先把场景增量回拷控制库**：切绑期间写入场景库的行只存在于场景库；执行 §4.5 R3/R5 回滚前必须 COPY 回控制库，并把 R5 的行数校验口径改为「控制库 + 场景增量」。
4. **绑定变更只走发布流程**：禁止直接 UPDATE `scenarios.db_binding`，避免推导键与数据实际位置错位（TOCTOU 仍存在：推导与读取之间切换绑定会让数据面查错库）。
5. **切绑后冻结文档 kb 归属**：迁移完成后不允许跨库移动文档，否则控制副本推导失效（D5）。
6. **迁移产出「未回填清单」**：本轮起「场景 ⊆ 控制副本」不再是硬前提（新文档由探测兜底），但搬迁脚本必须输出「无控制副本的文档/wiki 页」清单，用于回滚回拷与后续补副本作业。
7. **上线连通性检查**：所有注册场景库必须可达，否则「控制库无副本」的按 ID 请求将裸 500（D7）。
8. **延迟预算**：控制库无副本时每次按 ID 请求增加 N 次主键 get（N=已注册场景库数），需计入 S6-5 压测；建议迁移完成后回填控制副本以消除探测。

---

## 6. 未覆盖项与残余风险

- 未跑端点级真库全链路（HTTP + 场景库 chunk 可见性）；端点映射与内容来源由单测 + 代码审阅 + 推导层真库探针覆盖（R1）。
- 探测的 N+1 查询、失败重试与超时策略未测；不可达库的表现仅验证到异常类型（D7）。
- 连接预算未重算：探测路径单请求最多 2 条在途连接，per-bucket 假设仍成立，但 §4.6 的「1 条在途」表述需随实现更新。
- 孤儿 wiki 页的兜底口径（404）未经真实数据触发，仅损坏态构造验证（D8）。

---

## 7. 环境信息

| 项 | 值 |
|---|---|
| OS / Shell | Windows（PowerShell 5） |
| Python | 3.13.14（项目声明上界 `<3.13`；未出现 3.13 特有问题） |
| PostgreSQL | 16.15，容器 `code-postgres-1`，宿主 `localhost:5433`，账号 `user` |
| 数据库 | 控制/测试 `kagent_test`（alembic head 0030）；两个临时场景库已 DROP |
| 工作目录 | `code/apps/api` |
| 口令 | 仅进程环境变量注入，未落盘 |

---

## 8. 备注

- 全部结论基于 `git show`（`c69a2a3`、`d7c993d`）代码审阅 + 仓库用例 + 独立真库探针；**未修改任何业务代码，未 git add/commit/push**。
- 探针脚本置于系统临时目录并已删除；临时库已 DROP（`pg_database` 无残留）；`kagent_test` 探针行残留 0。
- 本机 `pytest -q` 不打印末尾汇总行，计数一律取 `--junitxml` 的 `tests/failures/errors/skipped` 属性。
