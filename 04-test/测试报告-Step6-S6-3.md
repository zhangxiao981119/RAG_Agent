# 测试报告 · Step 6 / S6-3（终版 · 含第二轮复验）

> 角色：独立测试工程师（P3，未参与编码；不改业务代码、不 commit/push）
> 被测变更：
> - 初测对象 `0334d00`「请求层读路径两段式接线（S6-3/S6-4）」
> - 复验对象 `9b84104`「混合默认库位判定与库位冲突的错误映射」、`7b4829b`「单文件上传库位冲突映射为 409」
> - 配套文档 `2dd77f7`「明确 default 计为一个库位（混合即跨库 409）」
> 验收依据：`02-design/第六阶段-Step6-前置裁决.md` §2.3（连接预算）、§4.6（读写两段式）、§4.7（删除门禁）、§4.10（S6-3 实现补强与实施纪律）
> 对照实现：迁移 `0030` 的 `fn_kb_db_binding_conflicts()`
> 环境：Windows PowerShell 5，Python 3.13.14（项目声明 `>=3.12,<3.13`，本机高于上界），PostgreSQL 16.15（容器 `code-postgres-1`，宿主 5433，账号 `user`），测试库 `kagent_test`，工作目录 `code/apps/api`
> 口令来源：`code/.env` 的 `POSTGRES_PASSWORD`，仅注入进程环境变量，**未写入本文件或任何仓库文件**。
> 时间：2026-11-23

---

## 1. 最终结论：**通过**

两个修复 commit 真正消除了初测的阻断缺陷，且未引入回归：

| 初测缺陷 | 严重度 | 复验结论 | 关键证据 |
|---|---|---|---|
| D1 `default` 不计库位 → 混合请求静默只查一半 | 高（需裁决） | **已修复**，`default` 计为一个库位；混合即 409 | 真库探针 §3.5 第 3 条：`DB_SCOPE_MULTI_KEY {'db_keys': ['default','kb_finance']}`；`chat.py:330-341` 在任何控制面写之前 409（`resolve` 早于 `session.commit()`:475） |
| D2 `DbScopeError` 未捕获 → 500 丢 code | 中 | **已修复**：删除/批量/单文件三处均 409 或逐文件失败并保留 code | 路由级探针 §3.6 E1~E4；`kbs.py:186-195`（删除）、`kbs.py:602-620`（批量）、`kbs.py:555-562`（单文件） |
| D5 检索异常分支在数据面会话内写控制面 | 低 | **已修复**：refused 事件与 `_persist_assistant` 移出 `async with` | `chat.py:702-736`；用例 `tests/chat/test_chat_db_scope.py:267-291` 顺序断言 `["data.enter","data.exit","persist"]` |

用例统计（第二轮，最终代码）：

| 批次 | 结果 |
|---|---|
| 指定 6 个文件（step6/step5/chat 库位/kbs 接线/batch upload/wiki 页面） | **58 passed / 0 failed / 0 skipped**，2.688s |
| `tests/scenario` 全目录（真库 `kagent_test`，0 skip） | **397 passed / 0 failed / 0 skipped**，55.371s |
| 全量 `tests/` | **1438 条，2 failed（均为环境问题，见 §3.4）、15 skipped、0 errors**，95.778s |

判据：阻断级缺陷 0；剩余仅为「开关开启前置条件」与 3 条低危/覆盖项（§4），不构成本次有线路径判定的不通过。

---

## 2. 两轮缺陷状态一览

| 编号 | 描述 | 严重度 | 状态 |
|---|---|---|---|
| D1 | `default` 不计库位，混合请求只查一半 | 高 | **已修复**（`db_scope.py:79-104` 复用 `map`，`default` 补位后去重） |
| D2 | 删除/上传冲突态 500，丢 code | 中 | **已修复**（409 `KB_SCOPE_CONFLICT`；批量逐文件 failed 带 code） |
| D3 | `map` 对非 UUID 文本抛裸 `ValueError` | 低 | **仍存**（真实请求不可达，见 R2） |
| D4 | 未注册库位键 → kbs/wiki 500、chat SSE 断流 | 中低 | **仍存**（无 `DbRouteError` handler，见 R1） |
| D5 | 异常分支数据面/控制面重叠 | 低 | **已修复** |
| D6 | 开启态无接口级用例 | 中 | **部分改善**：新增 2 个接口级用例文件，但 `resolve`/`open_data_session` 仍用替身，无真实第二 database 端到端（R3） |
| D7 | 开关需等 S6-4（documents/wiki 详情/arq db_key/worker） | 高（时序） | **仍存**，属步骤前置要求（R4） |

---

## 3. 第二轮复验证据

### 3.1 代码审阅（不信任自述，逐条对照）

1. **D1**：删除重复的 `_RESOLVE_SQL`，`resolve_kb_db_key` 改为 `map_kb_db_keys` 的**唯一事实来源**（`db_scope.py:79-104`）。`map` 仍只查 `db_binding IS NOT NULL`（`_MAP_SQL:64-76`），未命中的 kb 由 `mapping` 初始化补 `default`（`db_scope.py:119`），随后 `sorted(set(mapping.values()))` 去重 → 混合即 2 个键 → `DB_SCOPE_MULTI_KEY`，`detail.db_keys` 明确含 `default`。0030 冲突函数口径（同一 kb 两库位）保持不变（`db_scope.py:136-141`）。
2. **关闭态零 SQL 保持**：`mapping` 初始化 + `scene_db_split_enabled` 早退（`db_scope.py:119-123`）在任何 `execute` 之前；真库探针实测 SQL 条数 0（§3.5 第 1 条）。
3. **D2**：`delete_kb` 捕获 `DbScopeError` → 409（`kbs.py:186-195`），且在 `db_key != DEFAULT_DB_KEY` 的 409 `KB_IN_BOUND_SCENE`（`kbs.py:196-203`）之前；批量上传把 `DbScopeError` 归入该文件 `failed`（`kbs.py:612-620`），成功项移到 `else` 分支（`kbs.py:621-622`）；单文件上传捕获 → 409（`kbs.py:555-562`）。
4. **D5**：`async with` 只包住 `retrieve_chain`；异常仅置 `retrieval_failed`（`chat.py:704-721`），`yield refused` 与 `_persist_assistant` 在块外（`chat.py:723-736`）。
5. **未偷偷改动**：`arq` 入队负载仍为 `run_parse_segment(job_id, 0, trace_id)`（`kbs.py:500-503`，无 `db_key`）；`documents.py`、`wiki.py` 详情、worker 未动；`db_router.py` 未动。
6. **新增用例质量**：`tests/scenario/test_step6_db_scope.py` 补 `test_enabled_mixed_default_and_bound_raises_multi_key`，断言 `db_keys == ["default","kb_finance"]`；原「单绑定」用例改为返回 `(kb_id, db_key)` 行形状（与新 SQL 一致），未删断言、未放宽。`tests/kbs/test_kb_scope_wiring.py` 新增单文件 409 且断言 `storage.put_keys == []`。

### 3.2 指定用例

```
python -m pytest tests/scenario/test_step6_db_scope.py tests/scenario/test_step5_db_router.py \
                 tests/chat/test_chat_db_scope.py tests/kbs/test_kb_scope_wiring.py \
                 tests/kbs/test_batch_upload.py tests/wiki/test_page_endpoints.py -q
→ EXIT=0，errors=0 failures=0 skipped=0 tests=58  time=2.688s
```

### 3.3 `tests/scenario` 真库全目录

```
$env:SCENARIO_TEST_DB_URL = "postgresql+asyncpg://user:<pw>@localhost:5433/kagent_test"
python -m pytest tests/scenario -q
→ EXIT=0，errors=0 failures=0 skipped=0 tests=397  time=55.371s   （初测 396，+1 为新增混合用例）
```

### 3.4 全量回归

```
python -m pytest tests -q
→ tests=1438（初测 1430，+8），failures=2，skipped=15，errors=0，time=95.778s
FAIL tests.test_health::test_openapi_contains_health                     （socket.gaierror：.env 的 postgres/redis 宿主不可解析）
FAIL tests.wiki.test_worker_and_script::test_rebuild_wiki_script_fails_closed_when_disabled（同上；.env WIKI_ENABLED=true）
```

两条失败均为**本机环境**问题（未被本轮任一 commit 触碰），与初测完全一致；新增 8 例在全量上下文亦通过，无回归。

### 3.5 真库探针：D1 六条断言（临时脚本，已清理数据）

构造：`kb_a` 仅默认库位场景（published）；`kb_b`、`kb_d` 同属 `kb_finance`；`kb_c` 同属 `kb_finance`+`kb_hr`。

```
1 关闭态 resolve = default   map 值集 = ['default']   SQL 条数 = 0
2a 全 default resolve = default
2b 全同绑定(kb_b,kb_d) resolve = kb_finance
3  混合 default+bound → DB_SCOPE_MULTI_KEY {'kb_ids': [...], 'db_keys': ['default', 'kb_finance']}
3b 混合 map = {'927b3839': 'default', 'be016b7e': 'kb_finance'}
4  kb_c(两库位)+kb_b → DB_SCOPE_MULTI_KEY {'657a9282-...': ['kb_finance', 'kb_hr']}
5  map(kb_c) → DB_SCOPE_MULTI_KEY {'conflicts': {'657a9282-...': ['kb_finance', 'kb_hr']}}
```

结论：混合必须 409 的要求达成；全 default / 单绑定 / 多绑定 / 同 kb 冲突四态均正确；关闭态仍 0 SQL。探针租户与 4+4+4 行数据已删除，复查残留 0。

### 3.6 路由级探针：D2（走真实 resolve→map→错误映射，不猴补丁 resolve）

假会话 `execute` 直接返回 `_MAP_SQL` 形状的行：

```
E1 delete_kb（kb 落两库位）→ 409 {"detail":"KB_SCOPE_CONFLICT: 库位解析冲突（DB_SCOPE_MULTI_KEY: {'conflicts': {...}}），无法判定该库是否已搬入场景库，拒绝删除"}
E2 delete_kb（单绑定）      → 409 {"detail":"KB_IN_BOUND_SCENE: 该知识库属于场景库位 kb_finance，..."}
E3 单文件上传（冲突）       → 409 {"detail":"KB_SCOPE_CONFLICT: ..."}   存储写入 = []
E4 批量上传（冲突，2 文件） → 201，succeeded=[]，failed=[{"detail":"DB_SCOPE_MULTI_KEY: 库位解析冲突，拒绝写入（..."}, {...}]
```

与初测同场景的 500 相比，code 保留、批量不再整批 500、单文件无对象存储副作用（`_upload_single` 内库位解析在 `kbs.py:437`、对象存储写入在 `kbs.py:443`，解析失败即抛出，故 `put_keys` 为空）。

### 3.7 D5 证据

- 代码：`chat.py:702` 的 `async with` 内只保留 `retrieve_chain` 与 `retrieval_failed` 标志；`refused` 与 `_persist_assistant` 位于块外（`chat.py:723-736`）。
- 用例：`tests/chat/test_chat_db_scope.py:267-291` 用 `data.enter / data.exit / persist` 顺序断言，实测通过。
- 另核：`resolve` 在 `chat.py:330`，早于 `session.commit()`（475）；故 409 路径无任何控制面写入副作用。

---

## 4. 仍存风险与未覆盖项

| 编号 | 严重度 | 内容 | 建议 |
|---|---|---|---|
| R1 | 中低 | **未注册库位键**仍不可诊断：`db_router.py:173-176` 抛 `DbRouteError(DB_ROUTE_KEY_UNKNOWN)`，全仓无对应 handler → `kbs`/`wiki` 为 500 `INTERNAL_SERVER_ERROR`；`chat` 因 `open_data_session` 在 `StreamingResponse` 生成器内（`chat.py:702`，`meta` 事件已在 533 下发）表现为 SSE 断流、占位消息停在 `generating` | 进流前用 `registered_db_keys()` 预检并 409/503；或全局注册 `DbRouteError` handler |
| R2 | 低 | `map_kb_db_keys:144` 对非 UUID 文本抛裸 `ValueError`（实测）。SQL 侧 `kb.value = ANY(:kb_ids)` 只可能返回请求中的规范 UUID 文本，故真实请求**不可达**，属防御缺口 | 改为抛 `DbScopeError("DB_SCOPE_BAD_KB_ID")` |
| R3 | 中（覆盖） | 新增的接口级用例均以替身替换 `resolve_kb_db_key`/`open_data_session`，**没有真实第二 database 的端到端 API 用例**；`list_kbs` 跨库分桶、`list_documents` 数据面→控制面、`chat` 跨库检索的真实落库/返回仍未覆盖 | S6-4 集成用例：建临时场景库 + `scene_db_surgery`，跑 `list_kbs`/`list_documents`/`chat` |
| R4 | 高（时序，非本 commit 缺陷） | 开启开关前必须完成 S6-4：`documents.py` 6 端点（`documents.py:41`）与 `wiki.py:181-190` 详情仍查控制库（开启态「列表有、详情 404」）；`arq` 未带 `db_key`、worker 仍轮询控制库 `parse_jobs`（开启态「上传成功但永不解析」） | 部署侧加硬门禁：仅当 S6-4 完成后才置 `SCENE_DB_SPLIT_ENABLED=true` |
| R5 | 低（脆弱点） | 两段式 `commit()` 后继续访问 ORM 属性，依赖 `expire_on_commit=False`（`app/database.py`、`db_router.py` 场景会话）；改参数会退化为 `MissingGreenlet` | 保持参数并在 `db_scope` 文档中钉住 |
| R6 | 低（口径一致） | `delete_kb`/`resolve` 只看**已发布**版本：仅被草稿/归档场景引用的绑定 kb 会解析为 `default` 而允许删除（与 0030 同口径） | 与 §4.7 的孤儿清理脚本配合 |
| R7 | 低（观测） | 批量上传只把 `DbScopeError`/`HTTPException` 归入文件失败；`storage` 等其它异常仍整批抛出（原有行为，未变） | 如需要可统一为逐文件失败，属后续增强 |

---

## 5. 通过判据对照（终态）

| # | 验证项 | 结论 |
|---|---|---|
| V1~V4 | 关闭态零 SQL / 零新增连接、复用控制面会话不 commit、端到端净减少 | PASS（单元 + 真库事件计数 0 SQL） |
| V5~V6 | `_MAP_SQL` 与 0030 同口径；0/1/≥2 三态；`default` 计为一个库位 | PASS（单测 SQL 契约 + 真库探针 + 0030 交叉核对） |
| V7 | 非 default 先 commit 控制面再开数据面，关闭顺序正确 | PASS（单测顺序 + 真库：进入数据面前 commit 计数 1、backend pid 不同） |
| V8 | 无「数据面未关即控制面写」路径 | PASS（D5 已修复；正常/异常分支均在块外持久化） |
| V9~V10 | T-5-8 不读开关；T-5-7 单点白名单且定义唯一 | PASS |
| V11~V12 | kb 校验在控制面；删除绑定库位 409 `KB_IN_BOUND_SCENE` | PASS（含新增接口用例 + 路由级探针） |
| V13 | 跨库检索 409 `CROSS_DB_RETRIEVAL_UNSUPPORTED`，不只查一半 | PASS（真库混合 → MULTI_KEY；`chat` 409 早于检索与控制面写） |
| V14~V16 | 分桶统计、`list_documents` 顺序、上传顺序与 arq 负载不变 | PASS（代码 + 用例） |
| V17~V18 | 错误路径可预期 | 删除/批量/单文件 PASS；非 UUID 脏值仍 `ValueError`（R2，不可达） |
| V19~V20 | 无假兜底/伪造数据/弱化断言；范围未越界 | PASS |

---

## 6. 环境信息

| 项 | 值 |
|---|---|
| OS / Shell | Windows（PowerShell 5） |
| Python | 3.13.14（项目声明 `>=3.12,<3.13`，本机超出上界；未出现 3.13 特有问题） |
| PostgreSQL | 16.15，容器 `code-postgres-1`，宿主 `localhost:5433`，账号 `user` |
| 测试库 | `kagent_test`（真库，alembic head，含 0030） |
| 工作目录 | `code/apps/api` |
| 口令 | 仅进程环境变量注入，未落盘 |

---

## 7. 备注

- 本报告全部结论基于代码审阅 + 仓库用例 + 独立探针，**未修改任何业务代码，未 git add/commit/push**（工作区仅本报告文件未跟踪）。
- 探针脚本置于系统临时目录，未写入仓库；探针写入 `kagent_test` 的租户/scenario/version/kb 行已全部清理并复查（残留 0）。
- 本机 `pytest -q` 不打印末尾汇总行，所有计数以 `--junitxml` 的 `tests/failures/skipped` 属性为准。
