# 第六阶段 Step 5：数据层目录 + 连接路由 —— 详细设计

> **设计台账**：2026-11-22 · 统一RAG平台+场景化配置 · Step 5（= 框架稿 §3A.8 的 **D1 + D2**）· 1 个迁移（0030：**2 个视图 + 1 个函数，零列、零约束、零触发器**）+ 1 个新模块（`app/infra/db_router.py`，纯函数 + 启动闸门，**不进请求路径**）+ 3 项 settings + 1 个只读盘点脚本 · 目标真库用例 6 条 + 纯单测 11 条 + 静态守卫 5 条
> **前置**：Step 1-2（迁移 0028）、Step 3（policy 接线）、Step 4A/4B（写接口 + DNA 进场景）已实现并独立验收。本文件**只设计、不改代码**。
> **本文件的两类内容严格分栏**：标【源】的逐条给 `file:line`（我打开文件读到的）；标【推导】的是我基于源码得出的结论或取舍（推出来的）。
> **与主理人给定口径的关系**：主理人给的现状事实我逐条复核，**全部一致**；但框架稿 §3A.8 D1 的两项描述与本仓库现状**有 2 处必须修正**（`knowledge_bases` 加列已被 0028 提前实现为 `scenarios.db_binding`；「所有行库位 = `default`」与现有 CHECK 正则**直接冲突**，字面不可落地）。范围裁决我**部分反对**（不建 `kb_catalog`、一列都不加、不拆 DI 依赖）——理由全部写在 §A.3，是可执行判据，不是偏好。

---

## 0. 边界与验收基数

### 0.1 输入：本步继承的既成事实

【源】以下各项是 Step 5 的输入。本设计不重新论证、不改动，逐条给行号供实现时对照。

| # | 事实 | 出处 |
|---|---|---|
| **I-5-0a** | 连接层是**模块级单例**：`engine` 在 `app.database` 导入时构造一次，`SessionLocal = async_sessionmaker(engine, ...)` 也只有一个 | `app/database.py:22-31` |
| **I-5-0b** | 池参数全部取自 settings：`pool_size=20`、`max_overflow=20`、`pool_recycle=1800`、`pool_timeout=5.0`、`connect_args={"server_settings":{"plan_cache_mode":"force_custom_plan"}}` | `app/database.py:22-30`、`app/config/settings.py:58-62` |
| **I-5-0c** | `get_db()` 是 **2 行函数体**：`async with SessionLocal() as session: yield session` | `app/api/deps.py:48-50` |
| **I-5-0d** | `get_current_user` 依赖 `get_db`（`:65`）；`get_integration_principal` 又依赖 `get_current_user` + `get_db`（`app/api/integrations.py:77-79`）→ **控制面解析先于业务执行，且共用同一个 session** | `app/api/deps.py:64-65`、`app/api/integrations.py:77-90` |
| **I-5-0e** | 全仓 **96 处** `Depends(get_db)`（`grep -rn "Depends(get_db)" app/ \| wc -l` = 96） | 本次实测 |
| **I-5-0f** | `scenarios.db_binding` **已存在**（Text，nullable），CHECK `ck_scenarios_db_binding_format` = `db_binding IS NULL OR db_binding ~ '^kb_[a-z0-9_]{2,40}$'` | `alembic/versions/0028_add_scenarios.py:120`、`:137-138`；`app/models/entities.py:559`、`:575-578` |
| **I-5-0g** | `knowledge_bases` **没有任何**场景列或库位列（9 个字段：id/tenant_id/name/description/visibility/is_public/owner_id/acl_version/created_at） | `app/models/entities.py:129-146` |
| **I-5-0h** | 内置场景种子行 `db_binding` 写 **NULL**，每租户 1 行 | `0028_add_scenarios.py:290-301` |
| **I-5-0i** | 路由输入**已经在内存里**：`resolver.LOOKUP_SQL` 的**同一次** SELECT 已返回 `s.db_binding`；`ScenarioRow.db_binding` 与 `ScenarioSnapshot.db_binding` 两个字段都已存在 | `app/services/scenario/resolver.py:78`、`:143`；`snapshot.py:152`、`:253`、`:356` |
| **I-5-0j** | `db_binding` **没有写路径**：只出现在读模型 `ScenarioOut` 与四处读取（`api/scenarios.py:82`、`resolver.py:78`、`self_check.py:193`、`write_service.py:920`）。**任何 API 都改不了它**，只有运维 SQL 能写 —— 与框架 §7.2 Step 7「禁止把库位做成可编辑表单项」一致 | `app/schemas/scenario.py:63`；本次 `grep -rn db_binding app/` 逐条核对 |
| **I-5-0k** | S-7 自检已存在：`db_binding` 非 NULL 且 `max_step < DB_BINDING_WIRED_STEP(6)` → **WARN + metric** `rag.scenario.db_binding_configured`（「配了不生效」喊出来） | `app/services/scenario/self_check.py:50`、`:521-539`、`:252` |
| **I-5-0l** | `WIRED_RETRIEVAL_STEP = 3`（检索链路真正读到的步骤）；三个步骤号并存且**不得合并** | `app/services/scenario/effective.py:48-61` |
| **I-5-0m** | `resolver` 的行缓存是**进程内 dict**，TTL 60s，`invalidate()` 只影响当前进程；多副本靠 TTL 兜底（文档自认的 §G-2） | `app/services/scenario/resolver.py:40`、`:69`、`:111-118` |
| **I-5-0n** | `kb_catalog` 在**全仓零命中**（`grep -rn "kb_catalog" code/` → 0 行，含 .py/.sql/.md）；迁移目录 `0010`–`0029` 无同名视图/表创建 | 本次实测 |
| **I-5-0o** | 迁移最大号 = **0029**（`0029_add_chunk_reindex_views.py`，`down_revision="0028"`）→ Step 5 的迁移号 = **0030**，`down_revision="0029"` | `alembic/versions/0029_add_chunk_reindex_views.py:38` |
| **I-5-0p** | `tests/scenario/pg_helpers.py::prepare_schema()` 的基线是 **`head`** → 新增 0030 会被测试库自动吸收，**不需要改测试基建** | `tests/scenario/pg_helpers.py:179-215` |
| **I-5-0q** | `tests/config/test_db_pool.py` 有 4 条断言钉住池口径：`pool.size()==settings.db_pool_size`、`_max_overflow`/`_timeout` 同源、`timeout ≤ 10`、`pool+overflow ≥ 20 且 ≤ 45` | `tests/config/test_db_pool.py:25-59` |
| **I-5-0r** | `scenario_versions.kb_ids` 是 **JSONB 数组**（元素全为字符串，CHECK C-B2 守），**一个 kb 可出现在多个场景**；`state='published'` 由部分唯一索引保证**每场景至多 1 个已发布版本** | `0028_add_scenarios.py:166`、`:218-224`；`entities.py:476` |
| **I-5-0s** | 框架稿的容量算式：`40 × N × K + 10 ≤ 100 − 预留(10)`；`40 = pool_size 20 + max_overflow 20`；worker 侧 10 条（compose `DB_POOL_SIZE=5`+`DB_MAX_OVERFLOW=5`）；`预留=10` 是**设定值不是实测** | `统一RAG平台-场景化配置-框架设计-v1.md:307-426`（§3A.4） |

### 0.2 本步做 / 不做

**本步在六层框架里的落点**（H = E/T/C/S/L/V；写出来是为了让人一眼看出「本步几乎什么都不碰」）：

| 层 | 本步是否触碰 | 具体落点 |
|---|---|---|
| **E 执行循环** | **不碰** | 链拓扑、阶段顺序、早退条件一律不动（验收④ `git diff` 为空） |
| **T 工具注册** | **不碰** | 不新增工具、不新增路由（`app.openapi()["paths"]` 逐键相等） |
| **C 上下文管理** | **不碰** | 不做任何上下文拼装/截断/缓存前缀（本步零模型调用，无 prompt） |
| **S 状态存储** | ★ **主战场之一** | 「一次请求持有几条连接、生命周期多长」—— 即 session 的 acquire point 与 `close()` 时机（§A.3.2） |
| **L 生命周期钩子** | ★ **主战场之二** | 启动期：预算闸门（拒绝启动）+ 横幅一行；无运行期钩子（§B.4） |
| **V 评估接口** | **不碰** | 不触碰 `eval_runs` / `caliber` / 评测口径（§H-4 标 N/A） |

**先说清本步不涉及哪些框架维度**（避免下一轮以为漏了）：

| 维度 | 本步的结论 | 理由 |
|---|---|---|
| **架构范式 P** | **N/A**，沿用 Step 3/4 的「声明式配置 + 固定链路」。本步不改变链拓扑、不引入新阶段、不改任何编排 | 本步的唯一产物是「一个纯函数 + 一个启动闸门 + 两个只读视图」，全部在请求路径之外 |
| **模型组合 / 任务-模型分派表** | **N/A**，本步**零** LLM / embedding / rerank 调用 | 本步的判定全部是字符串比较、整数算术与 SQL；没有任何一处需要模型 |
| **多 Agent 编排** | **N/A** | 平台侧无自主循环（框架稿 `:1235` 已定性） |
| **经济性三视角** | 成本 = 0（无模型调用），但**连接预算表就是本步的成本账**（§A.3.4）；缓存 = 不新增、不失效任何既有缓存，但**必须暴露 resolver 行缓存与路由的耦合**（§G-R2）；体感 = Step 5 **零影响**（不进请求路径），Step 6 起每桶容量下降会推高排队（§A.3.4.5） | 见 §H-5 |

**本步做（4 项，全部「对现有基线零影响」）**：

1. **D1 的目录**：**不加列**（权威列 `scenarios.db_binding` 在 0028 已存在），改为落 **2 个只读视图 + 1 个冲突检测函数**（§B.1）——把「库位」从一个没人读的列变成**可盘点、可断言、可检测冲突**的对象。
2. **D2 的路由层**：新增 `app/infra/db_router.py`（**纯函数 + 启动期构建的注册表 + 上下文管理器**），**不进请求路径**。关闭态下它创建 **0 个** AsyncEngine，返回的永远是 `app.database.engine` 这**同一个对象**（§B.3）。
3. **连接预算闸门**：把框架 §3A.4 的算式从「文档里的公式」变成**拒绝启动的断言**，并在启动横幅打一行（§B.4）。
4. **只读盘点脚本** `scripts/db_routing_report.py`：退出码可进 CI/cron（§B.7）。

**不做（逐条给理由）**：

| 不做的事 | 理由 | 归到哪里 |
|---|---|---|
| 给 `knowledge_bases` 加「场景 + 库位」两列（框架稿 D1 原文） | ① 库位是**场景**的属性不是知识的属性（同一批 chunk 只在一个库里，而一个 kb 可被多个场景共用，I-5-0r）；② 第二份拷贝必然要防漂移，而三种防漂移手段在本项目全部踩雷（§A.3.1）。③ `db_binding` 已在 `scenarios` 上（I-5-0f），再加一列等于**造第二真源** | 见 DQ5-1（推荐「一列都不加」） |
| 建 `kb_catalog` 视图（框架稿 D1 原文） | ①「kb 属于哪个场景」在 Step 5 **没有唯一答案**（取决于「哪个版本算数」，未裁决）；② 它的 `db_key` 列在 Step 5 恒为 `'default'` → **不可证伪**（改坏派生逻辑也不会有任何用例变红） | 见 DQ5-4 与 §A.3.5（已给完整列契约作为备选） |
| 拆成 `get_db` + `get_data_db(db_key)` 两个依赖 | 会让**每在途请求的长期连接占用从 1 条涨到 2 条**（FastAPI 按 callable 缓存，两个 callable 就是两个 session）→ 40 条池的并发上限从 ~30 掉到 ~20。**分库还没开始，并发先掉三分之一** | 见 §A.3.2（否决，附算术） |
| 引入 contextvar（上下文变量）延后取连接 | `get_db` 在场景解析**之前**执行 → 已 checkout（从池检出）的 session 绑定的仍是控制引擎；且全仓还有直接 `SessionLocal()` 的调用点，contextvar 会**隐式改变它们**的引擎，影响面不可枚举。失败形态是**静默查错库**（召回 0 → 拒答，看起来像「知识库没内容」） | 见 §A.3.2 |
| 在 Step 5 打开路由（哪怕只对 1 个场景） | 违反框架 §7.2 Step 5 禁令「禁止在开启路由的同时迁库」 | Step 6 |
| 新增 admin 只读端点 | 会触碰 `tests/scenario/test_step4_guards.py:33-45` 的 `_EXPECTED_ROUTES`（那是「集合精确相等」的守卫）。本步要的是**路由表零改动**这种最强的等价证据 | 见 DQ5-8（用脚本代替） |
| 改 `retrieve/` 的任何 SQL | 验收信号④是 `git diff` 为空；且 `app/services/retrieve/` 与本步无任何关系 | —— |
| 改 `app/database.py` / `app/api/deps.py` | 这两个文件是「关闭态逐字节等价」的**锚点**。改它们 = 让 C-3 的金标准哈希失效 | §B.8 |

### 0.3 验收基数（不许放宽）

【源】当前基线（2026-11-22 实测，Step 4B-1 收尾后）：

| 环境 | tests | failed | errors | skipped |
|---|---|---|---|---|
| 有 `SCENARIO_TEST_DB_URL`（真库） | 1343 | 2 | 0 | 15 |
| 无 `SCENARIO_TEST_DB_URL` | 1343 | 2 | 0 | 107 |

两条 failed 均为环境类（本机无网 `socket.gaierror [Errno 11001]`）：`tests/test_health.py::test_openapi_contains_health`、`tests/wiki/test_worker_and_script.py::test_rebuild_wiki_script_fails_closed_when_disabled`。**基线里保留它们** —— 它们代表「这台机器没有那个外部依赖」，是环境事实，不是代码结论。

【源】框架稿 §7.3 已把验收口径从「N passed」改为**失败集合**。Step 5 起**强制**按该口径验收：

- 改动**前**跑一次全量、junit xml 留档；改动**后**再跑一次；
- 判据 = `failed_after − failed_before = ∅`（按 nodeid 集合做差，**不比数量**）；
- 同时 `skipped_after ≤ skipped_before`：**禁止用新增 skip 换绿**；
- 跨环境只能比 `failed`，不能比 `skipped`（有库 15 / 无库 107）。

### 0.4 施工文件清单

| 动作 | 文件 | 内容 |
|---|---|---|
| 新增 | `alembic/versions/0030_add_db_routing_catalog.py` | 视图 `v_scenario_db_routing` + 函数 `fn_kb_db_binding_conflicts()` + 视图 `v_kb_db_binding_conflicts`（DDL 见 §B.1）；**不加列、不加约束、不加触发器、不写数据** |
| 新增 | `app/infra/db_router.py` | `DEFAULT_DB_KEY` / `DbRouteError` / `resolve_db_key()` / `registry()` / `engine_for()` / `data_session()` / `check_connection_budget()` / `log_db_router_banner()`（§B.3） |
| 修改 | `app/config/settings.py` | 追加 5 项：`scene_db_split_enabled` / `scene_db_dsn` / `db_max_connections_for_budget` / `db_conn_reserved` / `db_worker_conn_total`（§B.2） |
| 修改 | `app/config/placeholders.py` | 追加 owner=`db_router` 的 3 条站位值（§B.2.3） |
| 修改 | `app/config/config_profile.py` | 快照追加脱敏后的路由项；`CROSS_ROLE_FIELDS` 追加 2 项（**这是本文件最重要的一行改动**，§B.6） |
| 修改 | `app/main.py` | lifespan 内追加 2 行：`check_connection_budget()`（T1，违反拒绝启动）+ `await log_db_router_banner()`（§B.4.3） |
| 新增 | `scripts/db_routing_report.py` | 只读盘点 + 退出码语义（§B.7） |
| 新增 | `tests/scenario/test_0030_migration.py` | 迁移往返 + 三个对象存在性 + 单头（**不新增约束名 → 不动 `_PG_CONSTRAINT_SOURCES`**） |
| 新增 | `tests/scenario/test_step5_db_router.py` | 纯单测 T-5-1…T-5-8（不连库，任何机器必须跑） |
| 新增 | `tests/scenario/test_step5_db_router_pg.py` | 真库用例 T-5-9…T-5-13 |
| 新增 | `tests/scenario/test_step5_guards.py` | 静态守卫 T-5-14…T-5-18（AST / 哈希） |
| **零改动** | `app/database.py`、`app/api/deps.py`、`app/services/retrieve/**`、`app/api/integrations.py`、`app/services/scenario/*`、`tests/config/test_db_pool.py`、`tests/scenario/test_step4_guards.py` | 这些都是「关闭态等价」的证据锚点（§D.5） |

### 0.5 术语（英文首次出现即带中文解释，下文不再重复）

| 词 | 本文件里的确切含义 |
|---|---|
| **库位 / `db_key`**（database key，库位逻辑键） | 一个**逻辑名**，形如 `default` 或 `kb_finance`。它是「数据住在哪个 database」的**配置层名字**，不是连接串、不是物理 ID（框架 I-4：物理 ID 不进配置）。真源：`scenarios.db_binding`；`NULL` 表示「未绑定 = 跟随平台默认库」 |
| **默认库** | 指现有的单库 `kagent`（`settings.database_url`）。Step 5 里**所有**请求都落它 |
| **Bucket（分桶）** | 每个库位一份独立的 `AsyncEngine` + 独立 `Connection Pool（连接池）`。`K` = 桶数。Step 5 的 `K` 恒为 **1** |
| **Connection Pool（连接池）** | `sqlalchemy` 的 `AsyncAdaptedQueuePool`，容量 = `pool_size + max_overflow`。本项目现状 = 40 |
| **checkout（检出）** | 从池里借出一条连接给某个事务；`pool.checkedout()` 是当前被借出的条数。本项目每个在途问答请求**长期检出 1 条**（`get_db` 的 yield 覆盖整条 SSE 流） |
| **SSE（Server-Sent Events，服务端推送）** | `/chat/ask` 等流式回答的传输方式。它决定了「一个请求会长期占用连接」 |
| **DI（Dependency Injection，依赖注入）** | 本文件特指 FastAPI 的 `Depends(...)`：**按 callable 缓存**，同一个 callable 在一次请求内只算一次，不同 callable 各算一次（这正是「拆依赖 = 多一个 session」的机制） |
| **contextvar（上下文变量）** | Python `contextvars.ContextVar`，一种隐式的「当前请求的全局变量」 |
| **acquire point（取连接点）** | 代码里**显式**创建 session 的那一行。本设计要求它可 grep、可 AST（抽象语法树）检查 |
| **fail-closed（失败即拒绝）** | 输入不合法就报错，绝不降级为「返回空」或「回落默认值」 |
| **TTL（Time To Live，存活期）** | 缓存行的最长有效期。本项目 resolver 行缓存 = 60s |
| **fast default（快速默认值）** | PG 11+ 的 `ADD COLUMN ... DEFAULT` 优化：只改目录、**不重写表** |
| **ACCESS EXCLUSIVE（排他锁）** | PG 最强表锁；`ALTER TABLE` 需要它，且它必须等所有已持有该表读锁的事务结束 |
| **LRU（Least Recently Used，最近最少使用）** | 一种「池满了就淘汰最久未用的那个」策略。**本设计明确不做**，理由见 §A.3.4.4 |
| **DSN（Data Source Name，数据源名称）** | 一条完整连接串，形如 `postgresql+asyncpg://user:pw@host:5432/dbname`。**含凭据** → 只活在 L1 部署层（环境变量），不进场景配置、不进日志明文 |
| **JSONB（PostgreSQL 的二进制 JSON 类型）** | `scenario_versions.kb_ids` 就是它（`0028:166`）。注意：在 JSONB 数组上做「包含某元素」的查询默认**走不了普通索引**（需 GIN，见下） |
| **GIN 索引（Generalized Inverted Index，倒排索引）** | 让 `JSONB`/数组的「包含」查询可走索引的索引类型。本项目**没有**为 `kb_ids` 建 GIN（所以 §A.3.1.5 的 join 会全表扫 —— 但表只有几十行） |
| **CHECK 约束** | 表级布尔表达式约束。本文件最关心的一条：`ck_scenarios_db_binding_format`（`entities.py:576-577`），它决定了 `'default'` **写不进去** |
| **golden（金标准）** | 把某段源码/SQL 的 sha256 钉死在测试里，改了就必须显式更新。本项目的先例：Step 4B 的 `T-4B-9` |
| **head（迁移头）** | `alembic heads` 的输出；当前 `0029`，本步变成 `0030` |
| **registry（注册表）** | 启动期构建、运行期只读的「库位键 → 引擎」映射。**运行期不增长**（§A.3.4.4） |

---

## A. 决策契约

### A.1 不变式（Step 5 新增，后续步骤不得违反）

| 编号 | 不变式 | 违反后的可观测症状 | 守卫 |
|---|---|---|---|
| **I-5-1** | 库位**唯一权威** = `scenarios.db_binding`。任何第二份「kb → 库位」或「场景 → 库位」的**可写**拷贝都不得存在 | 两份值分叉后，「这个 kb 在哪」有两个答案且都「看起来对」 | T-5-15（AST 守卫：除 `scenarios` 与 0030 的两个视图外，**任何**模型/迁移都不得新增名称含 `db_binding`/`db_key` 的**列**） |
| **I-5-2** | `NULL` = 未绑定 = 默认库。**禁止**把哨兵值 `'default'` 写进 `scenarios.db_binding` | 写入被 `ck_scenarios_db_binding_format` 拒绝（`'default'` 不匹配 `^kb_`）→ 报错信息指向「约束冲突」而非「你写错了值」 | T-5-9（真库：`INSERT ... db_binding='default'` 必须被同一个约束名拒绝） |
| **I-5-3** | 关闭态（`SCENE_DB_SPLIT_ENABLED=False`）下：`db_router` 创建 **0 个** AsyncEngine、`data_session` 被调用 **0 次**、`app.database.engine` 是**同一对象**、`get_db` 源码**逐字节不变** | 「零影响」退化为口号，无人能证 | T-5-14（AST 哈希）/ T-5-15（对象恒等）/ T-5-16（零调用计数） |
| **I-5-4** | **禁止猜库**：启用态下 `engine_for(key)` 对未注册键**raise**，绝不返回默认引擎、绝不返回空结果 | 查错库 → 召回 0 → 拒答 → 用户以为「知识库没这内容」，**没有任何报错** | T-5-6（未注册键必须 raise，且异常类型/错误码精确） |
| **I-5-5** | 路由**不新增任何查询**：库位键取自 `ScenarioSnapshot.db_binding`（已在内存，I-5-0i）。禁止为了路由而查 `kb_catalog` / `scenarios` / `pg_database` | 热路径多一次往返；且一旦缓存未命中就是「每次问答多一条 SQL」 | T-5-17（语句多重集金标准，§D.3） |
| **I-5-6** | 池预算闸门在**启动期**判定并**拒绝启动**；运行期**不建池、不淘汰池** | 运行期建池 → PG 连接数从应用侧不可枚举 → 连接预算形同虚设 | T-5-4（算式的纯函数单测）/ T-5-5（超预算必须 raise） |
| **I-5-7** | 路由开关与 DSN 进 `CROSS_ROLE_FIELDS`（跨角色一致性白名单）：api 与 worker 取不同值 → 指纹不同 | 「api 开了路由、worker 没开」→ 文档写进控制库、检索查场景库 → **一半文档查不到且不报错** | T-5-18（`CROSS_ROLE_FIELDS` 必须包含这两个键名） |
| **I-5-8** | `v_scenario_db_routing` 与 `resolver.LOOKUP_SQL` 的**版本口径必须一致**（都只取 `state='published'` 且 `tenant_id` 双端匹配） | 运维在 psql 里看到的路由与实际路由不同 | T-5-12（真库逐行比对视图的 `published_version` 与 `resolve_scenario()` 返回的 `snapshot.version`） |

### A.2 事实锚定（逐条自己核过；与主理人/框架稿的差异标出）

| # | 事实（【源】） | 出处 | 与给定口径是否一致 |
|---|---|---|---|
| **F-1** | 框架 Step 5 交付物 = 「`knowledge_bases` 增『场景 + 库位逻辑键』；连接路由层按库位取连接（默认全部落现有库）；池按库位分桶」；验收信号 4 条 | `框架设计-v1.md:800` | 一致（但交付物**形态需修正**，见 F-3/F-4） |
| **F-2** | §3A.8 D1 = 「`knowledge_bases` 增两列；`kb_catalog` 视图；全部行指向现有库」；D2 = 「连接路由层 + 池按库位分桶」；两者 = Step 5 | `:502-518` | 一致 |
| **F-3** | **修正 1**：`scenarios.db_binding` 早在 **0028** 就建好了（Step 1-2 的产物），且带格式 CHECK；`knowledge_bases` 则**一列都没有**。所以「库位列」不是待建项，而是**已存在但没人读** | `0028:120`、`:137-138`；`entities.py:559`、`:129-146` | **需修正**：框架 D1 的「KB 加两列」在 0028 已被**更正确地**实现成「场景加一列」 |
| **F-4** | **修正 2**：D1 验收信号①「所有行库位 = `default`」**字面不可落地** —— CHECK 是 `^kb_[a-z0-9_]{2,40}$`，`'default'` 会被 DB 拒绝 | `entities.py:576-577` | **需修正**：改为「`db_binding IS NOT NULL` 的行数 = 0」，展示层用虚拟键 `'default'`（§A.3.1.4） |
| **F-5** | §3A.9 三章对齐：`db_binding` = **库位逻辑键**（不是连接串）；与 `kb_logical_key` 集合**成对出现**，「库位由知识集合所属场景推出，不单独配置」 | `:520-531` | 一致。**这句话正是否决「KB 加列」的原文依据**（§A.3.1） |
| **F-6** | §3A.9：库位**不新增 MCP 入参**；`凭证 → 场景 → 库位` 三段全在服务端完成；调用方无任何方式指定库 | `:520-531`、I-7 `:489` | 一致。本设计把这条落成 I-5-5（不新增查询）与「无 API 写路径」（I-5-0j） |
| **F-7** | §3A.4 容量算式：`40 × N × K + 10 ≤ 100 − 预留(10)`；`预留=10` 是设定值；`30 并发` 是占位值且「换池必须重测」 | `:307-426` | 一致。本设计把它落成启动闸门（§B.4） |
| **F-8** | §8 Q10/Q11：隔离档位 = **温档**（同实例不同 database），一期就分到 database | `:855-856` | 一致。**这条决定了「桶的粒度 = database」**，也决定了 DSN 必须按 database 给 |
| **F-9** | §7.2 Step 5 禁止事项：「禁止在开启路由的同时迁库」；「禁止让路由层猜库（解析不到就报错，不回落默认库）」 | `:834` | 一致，落成 I-5-4 + §E 的顺序约束 |
| **F-10** | 路由输入**零新增查询**（I-5-0i）：`s.db_binding` 已在 `LOOKUP_SQL` 里，`ScenarioSnapshot.db_binding` 已在快照里 | `resolver.py:78`、`snapshot.py:152/253/356` | **主理人未提，但这是本步最关键的一条**：没有它，「不新增任何查询」根本无法达成 |
| **F-11** | `db_binding` **没有 API 写路径**（只在 `ScenarioOut` 读模型里），且 S-7 已在 WARN「配了不生效」 | `schemas/scenario.py:63`；`self_check.py:521-539` | 主理人未提。**它决定了「库位变更 = 运维 SQL = 部署动作」**，也决定了 §G-R2 那个翻车点 |
| **F-12** | `resolver` 行缓存是**进程内 dict**（60s TTL），多副本各一份；`invalidate()` 只在当前进程生效 | `resolver.py:40`、`:69`、`:111-118` | 主理人未提。**Step 6 第一个翻车点**（§G-R2、U-5-1） |
| **F-13** | `tests/config/test_db_pool.py` 的 4 条断言要求 `pool.size() == settings.db_pool_size` | `tests/config/test_db_pool.py:25-31` | 主理人未提。**它决定了池参数必须是「每桶」而非「全局」**（§B.3.4：若做全局预算除法，这 4 条必红） |
| **F-14** | `kb_catalog` 全仓 0 命中，无同名对象 | 本次 `grep` | 一致（§A.3.5） |

### A.3 六个硬问题的裁决

> 每个问题给：**裁决（一句话）→ 三方案对照 → 算术/证据 → 代价（必须承认）**。标「反对」的是我不采纳框架稿/主理人给定方向的地方。

---

#### A.3.1 硬问题 A：两列的语义与权威性

> **裁决：`scenarios.db_binding` 是**唯一权威值**（不是缓存）。`knowledge_bases` **一列都不加**。不存在「派生缓存 → 防漂移」的问题，因为**没有第二份拷贝可以漂移**。`NULL` = 默认库，不写哨兵值。**

##### A.3.1.1 三方案对照

| | **方案 1：只存场景（= 现状，推荐）** | **方案 2：KB 冗余一列 `db_key`** | **方案 3：两列 + 生成列 / 触发器** |
|---|---|---|---|
| 权威性 | `scenarios.db_binding` 单一权威 | 两个可写源（`scenarios` 与 `knowledge_bases`），必须指定谁覆盖谁 | 同上 + 一个隐式写入者（触发器） |
| 防漂移机制 | **不需要**（无第二份） | 四种可选：① 数据库约束 —— **做不到**（PG 无跨表 CHECK）② 应用层单点写 —— 但 `db_binding` 现在**没有 API 写路径**，加了 KB 列就等于新开一条写路径（I-5-0j 的纪律被破）③ 定期校验任务 —— 漂移窗口 = 校验周期，期间无人知道 ④ 触发器 | 生成列（generated column）**只能引用本行**，跨表派生在 PG 里不可能 → 只能触发器 |
| 迁移成本 | **0**（列已在） | 1 次加列 + 1 次回填 + 1 次 SET NOT NULL（§A.3.6 的全部锁代价） | 同方案 2 + 一个触发器（触发器是**不可见的写入者**，本项目已有一次教训来源：`entities.py:1118-1130` 的 ORM event listener 之所以被接受，是因为它**只校验不派生**） |
| 最坏失败形态 | 无（单一值） | 「两个值都有理、都看起来对」 | **触发器把「库位变更」标记成已完成，而数据没搬** → 目录说在 A 库、数据在 B 库 → 一半文档查不到、且**没有任何报错** |
| 是否引入「搬数据」的假信号 | 否 | 是（只要有人以为「改了列就算搬了」） | **是，而且自动完成**（这正是触发器唯一会做的事） |

##### A.3.1.2 为什么「库位是场景的属性」而不是「知识库的属性」（这是判据，不是偏好）

- 【源】温档隔离单位 = 场景（框架 Q10/Q11）。一个场景的**全部** kb 集合在**同一个** database 里（因为一次检索只连一个库，跨库检索是 §3A.7 明确「一期不做」的选项 B）。
- 【源】`scenario_versions.kb_ids` 是**集合**且**同一 kb 可出现在多个场景**（I-5-0r）。
- 【推导】两条合起来给出一条**硬约束**：`kb_id → db_key` 必须是**函数**（至多一个值）。否则「同一批 chunk 被两个场景声明在不同库」→ 无解。
- 而这个函数**不是配置出来的，是推出来的**：`kb` 的库位 = 「它所属场景的库位」；一旦它出现在两个库位不同的场景里，那是**配置冲突**，必须被**检测并报错**，不是被「最后写入者赢」覆盖。
- 【源】框架 §3A.9 原文：「库位由知识集合所属场景推出，**不单独配置**」—— 这句话本身就是「不要给 KB 加一个可单独配置的库位列」。方案 2/3 都是在**违反这句话**。

##### A.3.1.3 冲突检测（方案 1 必须补的那一块）

方案 1 的代价是：**冲突不再是「不可能发生」，而是「可能发生且必须被检测」**。所以 Step 5 交付一个**可判定**的检测器（§B.1.3）：

> 健康态谓词：`SELECT count(*) FROM fn_kb_db_binding_conflicts() = 0`。
> 违反态的判据：同一 `tenant_id` 下，同一个 `kb_id` 在 **≥2 个已发布场景**里被绑到 **≥2 个不同**的非 NULL `db_key`。

在 Step 5（全部 NULL）里这个谓词恒为真 —— 所以**必须靠构造违反态来证明它有牙**：T-5-11 会手工造出「同一 kb 被两个 scene 绑到两个不同 db_binding」的行，断言函数返回恰好 1 行。**若函数写错（例如漏了 `state='published'` 或漏了 `tenant_id` 分组），这条用例必须红。**

##### A.3.1.4 「默认库位」怎么表示（三选，这是 D1 验收信号①唯一能落地的形态）

| 表示法 | 优点 | 致命问题 |
|---|---|---|
| **A. `NULL` = 默认（推荐）** | 零迁移；与「未绑定」语义重合；与 0028 的种子行一致（I-5-0h）；不触发任何 CHECK | 表达「默认」需要一个 `COALESCE`（只在视图/脚本里做，不进存储） |
| B. 存哨兵 `'default'` | 查询直白 | **被 `ck_scenarios_db_binding_format` 直接拒绝**（`'default'` 不匹配 `^kb_[a-z0-9_]{2,40}$`）。要用它必须先改 CHECK（删旧建新 = 一次 ACCESS EXCLUSIVE + 一次全表校验），且要把所有种子行 UPDATE 一遍 |
| C. 存哨兵 `'kb_default'` | 能过 CHECK，零约束改动 | 造出一个**看起来像真库位**的假键：`kb_default` 会被 `engine_for()` 当成必须注册的键 → 启动闸门要么要求为它配 DSN（荒谬），要么特判（多一个分支） |

**推荐 A**，并把 D1 的验收信号①改写成**两条可执行断言**：

```sql
-- ① 存储层：没有任何行绑定到非默认库位
SELECT count(*) FROM scenarios WHERE db_binding IS NOT NULL;      -- 必须 = 0
-- ② 展示层：虚拟键全部为 default（视图负责 COALESCE）
SELECT count(*) FROM v_scenario_db_routing WHERE db_key <> 'default';  -- 必须 = 0
```

##### A.3.1.5 代价（必须承认）

- 「这个 kb 在哪个库」需要一次 join，且 `kb_ids` 是 **JSONB 数组**、没有 GIN 索引 → 全表扫（`scenarios` 每租户 1 行、`scenario_versions` 每场景数行 → 实际是几十行级，**可忽略**）。
- 冲突只能**检测**不能**阻止**（因为冲突由「两个场景各自合法」组合而成，PG 约束表达不了跨行聚合）。→ 依赖检测器 + 发布期/迁移前的 SOP。

---

#### A.3.2 硬问题 B：路由的输入从哪来（依赖顺序的死结）

> **裁决：** 库位键来自 **`ScenarioSnapshot.db_binding`**（已在内存，I-5-0i）；数据面 session 由**显式 acquire point（取连接点）**创建，**不新增 DI 依赖、不用 contextvar**。
> **死结不存在** —— 因为「凭证 → 场景 → 库位」这三段**全部住在控制库**（框架 I-8：控制面元数据永远留在控制库），所以用控制库会话去解析凭证与场景是**正确实现**，不是妥协。需要路由的只有**数据面**。

##### A.3.2.1 现状链路（先把它画清，否则下面三条方案的代价无法比较）

```
POST /api/integrations/ask                      app/api/integrations.py:350
 ├─ Depends(get_integration_principal)          :77-79
 │    ├─ Depends(get_current_user)  → Depends(get_db)   ← 控制面：users + 主体解析（deps.py:102-111）
 │    └─ Depends(get_db)                                ← 控制面：integration_applications（:90）
 └─ Depends(get_db)                            :353     ← ★ 与上面是**同一个** session（FastAPI 按 callable 缓存）
      ├─ resolve_scenario(...)                  :368     ← 控制面：scenarios + scenario_versions（LOOKUP_SQL 已含 s.db_binding）
      └─ _chat_ask_impl(..., session, ...)      :374     ← 数据面：chunks / messages / audit_logs
```

【推导】**同一个 session 身兼控制面与数据面**。这是 Step 6 必须拆的东西，也是 Step 5 必须**原样保留**的东西。

##### A.3.2.2 三方案对照

| | **方案 1：拆 DI（`get_db` + `get_data_db(db_key)`）** | **方案 2：contextvar** | **方案 3：显式 acquire point（推荐）** |
|---|---|---|---|
| 键从哪来 | `get_data_db` 只能 `Depends(get_integration_principal)` 才能拿到场景 → 依赖图变成 `get_data_db → principal → get_db` | 场景解析后 `contextvar.set(db_key)`，`SessionLocal` 的工厂读它 | `snapshot.db_binding`（已在内存） |
| 对关闭态等价的影响 | 关闭态下 `get_data_db` 仍必须存在（否则 Step 6 要改 96 个签名）→ 它必须返回"默认库 session" → 但它与 `get_db` 是**两个** callable → **两个 session** | 关闭态下"没人 set" → 默认值 `'default'` → 表面等价，但**正确性依赖执行顺序** | **零影响**：函数没被调用（I-5-3），引擎是同一个对象 |
| 连接开销（算术） | 每在途请求**长期占用 2 条**（+1 瞬时 = 3）。按实测口径（40 条池 ≈ 30 并发、2 条/请求）外推，同样 40 条池只能撑 **~20 并发** → **并发掉 1/3**【推导，需压测确认】 | 同上（内部 `SessionLocal()` 直调点也会跟着换引擎，影响面不可枚举） | 解析完成后 `await session.close()` 归还控制连接 → 稳态 **1 条数据连接**（今天是 1 条控制连接）→ **不增反降** |
| 失败形态 | 池耗尽 → 503 变多（可观测，算"好"的失败） | **静默查错库** → 召回 0 → 拒答 → 看起来像「知识库没内容」（最坏的失败） | 未注册键 → 500 raise（可观测） |
| 变更面 | 96 处 `Depends(get_db)` 需要逐个裁决「这是控制面还是数据面」 | 全仓 `SessionLocal()` 调用点（含 worker、脚本）都要审计 | **Step 5 零处**；Step 6 只在 2 个 MCP 端点（`integration_ask` / `integration_search`）+ worker 解析入口 |
| 可 grep / 可断言 | 可以（`Depends(get_data_db)` 计数） | **不行**（隐式） | 可以（`data_session(` 调用点计数 + AST 守卫） |

##### A.3.2.3 为什么否决方案 2（contextvar）—— 三条，逐条可验

1. **时序依赖的正确性**：`get_db` 在 `get_current_user` 里就执行了（I-5-0d），此时场景还没解析。它 checkout 出的 session 绑定的是**控制引擎**；之后的 `set()` 只影响**之后创建**的 session。所以「谁先谁后」决定了查哪个库 —— 这种正确性无法用类型或测试穷举守住，只能靠人记住。
2. **静默错库**：contextvar 的错误表现是「查询成功、结果为空」，而**空结果在这个系统里是合法业务态**（`retrieve` 回 0 行 → 拒答，见 4B 文档 §F-18）。也就是说：**错误与合法状态在观测上不可区分**。
3. **影响面不可枚举**：`SessionLocal` 在仓库里被直接调用（`app/main.py:139` 的启动自检、`workers/*`、`scripts/*`）。contextvar 会**隐式**改变所有读取它的工厂的行为 —— 而「忘了考虑某处」正是本项目反复记录的失败形态。

##### A.3.2.4 方案 3 的装配（Step 6 才接线，Step 5 只定形状）

```python
# Step 6 的接线形态（本步不实现，写在这里是为了让 Step 5 的接口形状被验证过）
snapshot = await _resolve_or_reject(session, principal, payload.kb_ids)   # 控制库
db_key = db_router.resolve_db_key(snapshot.db_binding)                   # 纯函数，零 IO
await session.close()                                                    # ★ 归还控制连接
async with db_router.data_session(db_key) as data_session:               # 数据库
    response = await _chat_ask_impl(..., data_session, ...)
```

**「先 `close()` 再 `data_session()`」为什么成立**：`AsyncSession.close()` 结束当前事务、把连接还回池，对象本身仍可用（下次 `execute` 会重新 begin）。今日之所以「每在途请求长期占 1 条」，正是因为控制面 session 在 `get_current_user` 里开了只读事务、**整条 SSE 期间既不 commit 也不 close**（`database.py:11-14` 与 `settings.py:50-56` 记录的实测根因）。

【推导】**这是 Step 6 的额外红利**：`await session.close()` 一行就把「每在途 1 条长期占用」的语义从「控制连接」换成「数据连接」，连接总数不变。它同时是 §D.3 的 C-5（池检出峰值）能守住的原因。

##### A.3.2.5 代价（必须承认）

- `await session.close()` 之后 `session` 的 identity map（身份映射）被清空 → 之前加载过的 ORM 对象会过期，需要重查。对 `integration_ask` 这类「解析完就不再需要 ORM 对象」的端点无影响；但对于「解析场景后还要改 application 行」的写端点（`bind_application_scenario`）**不能**这么做 → 那些端点属控制面，不走路由。
- 「显式 acquire point」意味着**没有任何机制阻止**新代码继续用 `get_db` 去查数据面。守卫只能靠 T-5-16（AST：`retrieve(` 的 session 参数必须来自 `data_session(...)`）—— 而这条守卫在 Step 5 无法生效（因为接线还没发生），只能登记为 **Step 6 的守卫**（U-5-2）。

---

#### A.3.3 硬问题 C：「关闭态逐字节等价」的可测定义

> **裁决：5 条强制断言 + 1 条可选。每条都能被一条命令/一条断言验证；「看代码没变」不是断言。**
> 原则：**「没改」无法用行为断言证明**（行为断言只覆盖某组输入），所以核心用**对象恒等 + 源码金标准哈希 + 调用计数**三种「结构断言」。

| # | 断言（可执行） | 它到底证明了什么 | **破坏了会怎样红** |
|---|---|---|---|
| **C-1** | `assert db_router.engine_for("default") is app.database.engine`（**`is`**，不是 `==`） | 关闭态用的是**同一个引擎对象**（同一个池、同一套参数、同一份连接） | 在 `db_router` 里写 `create_async_engine(settings.database_url, ...)`（"反正参数一样"）→ 出现第二个池 → `is` 为假 → 红。**这是最容易滑进去的一步**：参数一样 → 行为看起来一样 → 只有对象恒等能抓住它 |
| **C-2** | `assert db_router.created_engine_count() == 0` 且 `assert db_router.registered_keys() == ("default",)` | 关闭态下**一个引擎都没建**（计数器在任何 `create_async_engine` 调用处 +1） | 模块导入期预建桶（"先把 K 个桶建好，反正 lazy 不连"）→ 计数 > 0 → 红。注意：**建引擎不等于建连接**，所以这条不能靠"看有没有连上"发现 |
| **C-3** | `hashlib.sha256(inspect.getsource(deps.get_db).encode()).hexdigest()` 与测试里钉死的 64 位十六进制相等（**该字面量由实现者首次跑测试后填入，属交付物的一部分**，同 4B 的 golden 哈希） | `get_db` 的**函数体逐字节未变** | 给它加 `read_only=True` 参数、加 `try/finally`、加 `if settings.scene_db_split_enabled:` 分支 → 哈希变 → 红。**这是"我没改连接获取路径"的唯一可执行证据**（先例：Step 4B 的 `T-4B-9` 用同一个手法守 SQL 文本） |
| **C-4** | 抓一次 `/api/integrations/search`（stub 上游）全链的 SQL 语句，断言**多重集与基准相等**（`collections.Counter` 之差为空），基准存 `tests/scenario/golden/step5_sql_multiset.json` | **不多一条也不变一条** SQL（= 不新增查询） | 路由实现里查一次 `scenarios`（"确认一下这个场景在哪"）→ 多一条 → 红。因 `pg_helpers`/`app.database` 的 SQL 文本会随无关改动漂移，**这条必须配一条形状断言**（见下） |
| **C-4b** | 同一次抓取的语句文本里：**不得出现** `kb_catalog`、`current_database()`、`FROM pg_database`、`information_schema.tables` | 不读路由表 / 不探测当前库 | 加一句 `SELECT current_database()`（"打个日志看看连的哪个库"）→ 红。**这条比 C-4 稳**（不受无关 SQL 漂移影响） |
| **C-5** | 一次请求结束后 `engine.pool.checkedout() == 0`；且在 c=10 稳态下 **峰值检出数 == 基线峰值** | 连接占用没变 | 多持有一个 session（`Depends` 多一层）→ 峰值 +10 → 红。**需要并发夹具 → 标「需你定验证方式」**（若不上压测夹具，本项降级为「只断 `checkedout()==0`」） |

**补充一条判据（关闭态落现有库 vs 猜库的区别）**：

| | 关闭态「全部落现有库」 | 「猜库」 |
|---|---|---|
| 本质 | **路由功能不存在**：`data_session` 无人调用 → 落现有库是**构造事实**，不是判定结果 | 路由功能存在，输入缺失/键未注册时 `COALESCE` 到默认库 |
| 后果 | 与改动前**逐字节相同** | 查询成功但查错库 → 召回 0 → 拒答 → **看起来像「知识库没这内容」** |
| 可判定谓词 | T-5-16：生产代码里 `data_session` 的**唯一**定义处是 `db_router`，**零调用点** | 启用态下 `engine_for("kb_unknown")` 必须 raise（T-5-6）；`resolve_db_key` 是**纯函数**、不读 settings → 「键怎么解析」与「键能不能用」彻底分离，前者可穷举单测，后者只有一处实现 |

---

#### A.3.4 硬问题 D：池分桶的容量算术与超限行为

> **裁决：Step 5 桶数 `K = 1`，总连接 `= 40`（与现状**逐字节相同**）；`K` 的上限由启动闸门算出并在**启动期拒绝**；**不做 LRU 淘汰、不做运行时建池**；未注册键 → **500 fail-closed**；桶耗尽 → 沿用 `pool_timeout=5s` 快速 503。**

##### A.3.4.1 算术（每个代入值都标来源）

```
per_bucket_total = db_pool_size + db_max_overflow = 20 + 20 = 40      # settings.py:58-59
K                = 1 + 非 default 库位键数（启动期由白名单算出，运行期不变）
needed           = 40 × N × K + worker_total(10)                      # 框架 §3A.4 算式
闸门              needed ≤ max_connections(100) − reserved(10) = 90
→ K_max (N=1, P=40) = floor(80 / 40) = 2
→ K_max (N=2, P=40) = floor(80 / 80) = 1
→ 若每桶降到 P=20：K_max(N=1)=4、K_max(N=2)=2
```

| 场景 | 代入 | `needed` | 判定 |
|---|---|---|---|
| **Step 5（本步）** | N=1, K=1, P=40 | 40×1×1+10 = **50** | ✅ 通过（占预算 50/90 = 56%，与今天**完全一样**） |
| Step 6 首个场景迁出（推荐配置） | N=1, K=2, **P=20** | 20×1×2+10 = **50** | ✅ 通过（**连接总数不变**：把 40 摊成 2×20） |
| Step 6 首个场景迁出（图省事不调池） | N=1, K=2, P=40 | 40×1×2+10 = **90** | ⚠️ 恰好触顶，**零余量**（PG 还有 `superuser_reserved_connections` 默认 3；任何临时连接都会挤爆） |
| 2 副本 + 分库 | N=2, K=2, P=40 | **170** | ❌ 超限 → **拒绝启动** |
| 2 副本 + 分库（调池后） | N=2, K=2, P=20 | 20×2×2+10 = **90** | ⚠️ 触顶 |
| 3 库位 | N=1, K=3, P=40 | **130** | ❌ 超限 → 拒绝启动 |

**Step 5 的硬断言**（D1 验收信号①「连接池实例数与现状相同」的可执行形态）：

```python
rep = db_router.register_default()           # 关闭态：把现有 engine 登记为 default 桶
assert rep.bucket_count == 1
assert rep.total_connections == settings.db_pool_size + settings.db_max_overflow == 40
assert rep.needed_connections == 50 and rep.budget == 90
assert db_router.created_engine_count() == 0  # 一个引擎都没多建（C-2）
```

##### A.3.4.2 「每桶参数」而不是「全局预算再分摊」（一个必须说清的实现选择）

**诱人的错法**：加一个 `DB_CONN_BUDGET_TOTAL=40`，启动时按 `K` 平分给每桶（`40 // K`）。它会让 Step 6 的 K=2 变成「每桶 20」，看起来"自动省事"。

**为什么不行**：`tests/config/test_db_pool.py:29` 断言 `database.engine.pool.size() == settings.db_pool_size`。一旦按 K 平分，K=2 时默认桶的 `pool.size()` 变成 10 ≠ 20 → **4 条既有断言全红**（I-5-0q / F-13）。而去改那 4 条 = 放宽一条已验收的守卫，正是框架 §7.3 明确禁止的形态。

**正确做法**：`db_pool_size` / `db_max_overflow` 保持「**每桶**」语义（K=1 时它就是总量，与今天完全一致）；预算闸门只做**约束**不做**分配**：

```python
per_bucket = settings.db_pool_size + settings.db_max_overflow      # 每桶
needed     = per_bucket * replicas * bucket_count + worker_total
assert needed <= settings.db_max_connections_for_budget - settings.db_conn_reserved
```

Step 6 要分两个库，就由**部署**改 `DB_POOL_SIZE=10 DB_MAX_OVERFLOW=10`（env，不改代码、不改任何测试）。**每桶同参**也顺手消掉一个隐患：不存在"某个桶悄悄更大"。

##### A.3.4.3 `replicas` 与 `bucket_count` 从哪来（不许猜）

| 值 | 来源 | 为什么不能猜 |
|---|---|---|
| `bucket_count` | `1 + len(registry) - 1`，即**启动期白名单**（`SCENE_DB_DSN` 的键集 + `default`） | 若从「库里现有多少个 db_binding」算，就变成**读路由表**（违反 I-5-5 与关闭态不读表） |
| `replicas` | **新 settings `DB_REPLICAS_EXPECTED`（默认 1）**，部署声明 | 应用**无法**知道自己有几个副本（无共享状态，同 4A 的 U4-2）。默认 1 是**保守**值：它让闸门在"其实有 2 副本"时偏松 —— 这是刻意的：宁可闸门偏松（用户能自己发现撞限），也不要偏紧（明明能跑却拒绝启动） |
| `max_connections` | **新 settings `DB_MAX_CONNECTIONS_FOR_BUDGET`（默认 100）** | 它是 PG 服务端参数，应用读不到（除非查库）。**启动时会真的查一次 `SHOW max_connections` 并与此值比对**，取**较小者**用于闸门（§B.4.2）；不一致打 WARN |

##### A.3.4.4 超限行为：为什么**不做 LRU**（这是本节最硬的裁决）

**裁决：库位键集合在启动期**封闭**。运行期遇到未注册键 → `DbRouteError(DB_ROUTE_KEY_UNKNOWN)` → HTTP **500**（不是 400、不是空结果、不是 200+空 citations）。绝不"淘汰一个池腾地方"。**

四条理由：

1. **库位键是部署事实**：哪个 database 存在，启动时全可知（DSN 就是启动期环境变量）。运行期出现新键 = 启动校验漏了，不是"正常增长"。
2. **淘汰 = 中断在途会话**：池不能"淘汰"而保留它的连接 —— 淘汰意味着丢掉引擎，而该引擎上还有正在跑的 SSE 请求。用户会看到**半截答案**（本项目最反对的静默错误类：前端不报错，只是答案突然断了）。
3. **连接数从应用侧不可枚举**：运行时建池 + 淘汰，等于把「PG 连接总数」这个唯一的硬约束交给一个动态过程 → 约束失效。
4. **本项目不需要**：一期温档、白名单单场景（框架 D3「白名单单场景」）。**K=2 是本项目的现实上限**（§A.3.4.1 表），2 个桶用不着淘汰策略。

> **若你仍要 LRU，请先回答这三问**（任意一问答不上来，LRU 就是把"启动期可算的错误"换成"运行期不可解释的错误"）：
> ① 被淘汰池上在途的 SSE 允许被中断吗？中断后前端看到什么？
> ② 淘汰后重新连回时，`plan_cache_mode=force_custom_plan`（`database.py:16-21` 记录的 1~2ms/语句规划开销）与池预热成本谁承担？
> ③ 淘汰事件写进哪条审计、谁能查到？

**桶耗尽的正面价值（这是分桶唯一可度量的收益）**：故障域隔离。一个场景的洪峰只饿死**它自己的**桶，不再拖垮全平台。这条收益在 `K=1` 时**恒为 0** —— 也就是 Step 5 一分钱收益都没有（诚实登记于 §G-R3）。

##### A.3.4.5 对体感的影响（Step 6 起，本步只登记）

- **Step 5：零影响**。路由不进请求路径，池参数一字未变。
- **Step 6（推荐配置 P=20、K=2）**：`池 20` 的实测崩点是 **c=20**（`settings.py:52-56` 的一手记录：「pool_size=10 + max_overflow=10 时 c=20 并发 76 请求失败 36 个（47%）」）。所以每库在途上限从 ~30 降到 ~20，**必须重跑阶梯压测**（框架 §3A.4 注 1：「换池必须重测」）。这是分库的真实代价，不是可选项。

---

#### A.3.5 硬问题 E：`kb_catalog` 视图

> **裁决：Step 5 **不建** `kb_catalog`。改为建 `v_scenario_db_routing`（场景级）+ `fn_kb_db_binding_conflicts()` + `v_kb_db_binding_conflicts`。**

##### A.3.5.1 先答事实：不存在同名对象

【源】`grep -rn "kb_catalog" code/` → **0 命中**（.py/.sql/.md 全部）；迁移目录 `0010`–`0029` 无同名对象创建；`alembic heads` = `0029`。→ **无冲突、无同名残留**。

##### A.3.5.2 为什么 Step 5 不建它（三条，第一条是判据）

1. **「kb 属于哪个场景」在 Step 5 没有唯一答案。** 【源】绑定在 `scenario_versions.kb_ids`（JSONB 数组），**一个版本一个集合**；同一 kb 可出现在多个场景、多个版本（含 `archived`）（I-5-0r）。要建 kb 级视图，先得裁决：取哪个版本算数？只取 `published` 还是包含 `draft`（预发布会把「还没生效的库位」暴露成事实）？`archived` 算不算？——**这是 Step 6 的决定**（因为 Step 6 才需要「搬哪些 kb 的数据」这个清单）。在 Step 5 拍一个口径，等于给 Step 6 留一个必然要改的契约。
2. **它在 Step 5 不可证伪。** 全部 `db_binding` 为 NULL → 视图的 `db_key` 列**恒等于** `'default'`。此时「派生逻辑写对了」与「派生逻辑写死了常量」输出**完全相同**。可证伪性判据：把派生逻辑改坏（例如 join 到 `archived` 版本、漏掉 `tenant_id`）必须让某条用例变红 —— 在 Step 5 **怎么改都输出同一行**。一条永远只输出一个值的视图无法被测试 → 交付一个不可证伪的对象。
3. **它不是路由的输入**，未来也不该是。路由的输入是 `ScenarioSnapshot.db_binding`（I-5-0i），否则每次问答都要查一次目录（违反 I-5-5，且关闭态会变成"读路由表"）。所以 `kb_catalog` 的**唯一**消费者是「运维盘点」与「搬库脚本」—— 两者都是 Step 6 的事。

##### A.3.5.3 它属于哪个面（这个问题的答案没有疑义）

**控制面视图**。依据【源】框架 I-8：「控制面元数据永远留在控制库，不随场景分裂」。`knowledge_bases` / `scenarios` / `scenario_versions` 全在控制库，所以任何跨它们的视图只能建在控制库。**推论（重要）**：这条视图**永远不会**在场景库里存在 → 搬库脚本不能靠"目标库里也有 kb_catalog"来判断数据是否到位（那会是一个假信号）。

##### A.3.5.4 若你仍要 `kb_catalog`：完整列契约（照这个建即可，不要自己发明）

| 列 | 类型 | 派生口径（必须写死） |
|---|---|---|
| `tenant_id` | `uuid` | 直取 |
| `kb_id` | `uuid` | 直取 `knowledge_bases.id` |
| `kb_name` | `text` | 直取 |
| `scene_count` | `bigint` | `count(DISTINCT s.id)`，**只数 `state='published'`** |
| `db_key` | `text` | 若 `count(DISTINCT COALESCE(s.db_binding,'default')) = 1` → 该值；否则 `NULL`（**冲突态用 NULL 表达，不要用 `'MULTIPLE'` 这类魔法字符串**） |
| `db_key_distinct` | `bigint` | `count(DISTINCT COALESCE(s.db_binding,'default'))` —— **冲突看这一列，不要靠 `db_key IS NULL` 反推**（`db_key NULL` 还有"无任何场景"这一种含义） |
| `scene_keys` | `text[]` | `array_agg(DISTINCT s.scenario_key ORDER BY s.scenario_key)` |

**同批必须一起落的三条裁决**（否则这个视图会腐烂）：① 版本口径 = **仅 published**；② 无任何 published 场景的 kb 必须**仍出现**（`LEFT JOIN`，`scene_count=0`），否则"孤儿 kb"从视图里消失；③ `db_key_distinct > 1` 的行数必须进盘点的退出码（否则冲突无人看）。

---

#### A.3.6 硬问题 F：0030 的可回滚性与「加列」的代价

> **裁决：0030 **不加任何列**（`db_binding` 已在 0028 落地），因此「加列的锁代价 / fast default / 分两次迁移」在本步**不发生**。下面把「若一定要加」的完整代价算清，并把「删列 = 数据销毁」这条纪律写死。**

##### A.3.6.1 0030 实际做了什么（代价 = 可忽略）

| 操作 | 锁 | 说明 |
|---|---|---|
| `CREATE OR REPLACE VIEW v_scenario_db_routing` | 视图自身 `ACCESS EXCLUSIVE`；基表（`scenarios` / `scenario_versions`）仅 `ACCESS SHARE` | **不阻塞**对基表的读写 |
| `CREATE OR REPLACE FUNCTION fn_kb_db_binding_conflicts()` | 函数无锁 | `STABLE`、`SECURITY INVOKER`、只读 |
| `CREATE OR REPLACE VIEW v_kb_db_binding_conflicts` | 同上 | 包装函数 |

**无列 → 无回填 → 无 NOT NULL → 无触发器 → 无写入者 → `downgrade` 是纯 DDL 且无数据可丢。**

##### A.3.6.2 若一定要加列：五个数字/事实（这是本节的价值）

1. **`ADD COLUMN ... NOT NULL DEFAULT 'x'` 在 PG 11+ 走 fast default（快速默认值）**：只改 `pg_attribute` 的 `atthasmissing`，**不重写表**，耗时 **O(1)**、与行数无关。
2. **但它仍然取 `ACCESS EXCLUSIVE`（排他锁）**，且该锁必须**等所有持有该表 `ACCESS SHARE` 的现存事务结束**才能拿到。
3. **本项目特有的放大器**：事务边界 = 「一个 HTTP 请求」（`get_db` 的 yield 覆盖整条 SSE 流，I-5-0c/0d）。而 `scenarios` 被 `LOOKUP_SQL` 读（`resolver.py:78`）—— 一次在途问答最长可把 `ALTER` **卡住整条流的时长**。**这才是本项目加列的真实代价，与表多大无关**（`scenarios` 每租户 1 行，表小得可以忽略）。
4. **锁队列头阻塞（head-of-line blocking）**：`ALTER` 等锁期间，**后续所有**对 `scenarios` 的查询排在它后面（PG 锁队列 FIFO）→ 请求超时雪崩。所以这类迁移必须：`SET lock_timeout = '2s'`（快速放弃并重试）+ **无流量窗口**。**这一条比 fast default 重要一个量级。**
5. **`downgrade` 会丢数据吗：会，而且是不可恢复地丢。** `DROP COLUMN` 删除该列自 upgrade 以来写入的**全部值**（没有别的副本）。所以在 Step 6 写过 `db_binding` 之后 downgrade 0030（假如 0030 加过列）= **丢掉全部路由配置**，而数据可能已经在别的库里 → 从「路由配置丢了」升级成「**数据在哪没人知道**」。

##### A.3.6.3 是否应该分两次迁移（若一定要加 NOT NULL 列）：**是，三步**

| 步 | 语句 | 锁 | 为什么必须单独一步 |
|---|---|---|---|
| 1 | `ADD COLUMN db_key text NULL` | `ACCESS EXCLUSIVE`（瞬时，无回填） | 一步到位写 `NOT NULL DEFAULT` 也能过，但**回填值从哪来**必须显式（不能靠 DEFAULT 假装"已填"） |
| 2 | `UPDATE scenarios SET db_key = ... WHERE db_key IS NULL`（本表每租户 1 行 → 一次 UPDATE） | 行锁 | 「回填」是一次**业务判断**（默认库 = `NULL` 还是 `'kb_default'`？见 §A.3.1.4），不是 DDL |
| 3 | `ALTER COLUMN db_key SET NOT NULL` | `ACCESS EXCLUSIVE`（PG 12+ 可复用已验证 CHECK 避免全表扫；本表极小，直接 SET） | 与步 1 分开，让"忘了回填"表现为一个**明确的失败**（`column contains null values`）而不是一条静默的默认值 |

**但三步都解决不了「等锁」**（第 4 点）——只解决「长事务重写表」。→ 既然本设计不加列，这个问题在本步不存在。

##### A.3.6.4 与「存哨兵」的联动成本（选 `NULL` 的第四条理由）

若选「存 `'default'`」：必须删旧建新 `ck_scenarios_db_binding_format`（一次 `ACCESS EXCLUSIVE` + 一次全表校验），并 UPDATE 所有种子行。**为表达一个"未绑定"的状态，付出一次改约束的代价** —— 而 `NULL` 一个字都不用改。

---

### A.4 待你裁决 DQ5-x

> 格式：**加粗是我的推荐**；备选与「不裁决的后果」逐条给。标「反对」的是我不采纳框架稿/主理人给定方向的地方。

#### DQ5-1 库位的权威位置（**反对框架稿 D1「KB 加两列」**）

| 项 | 内容 |
|---|---|
| **推荐** | `scenarios.db_binding` 单点权威（**已在 0028**）；KB **一列都不加**；`NULL` = 默认库 |
| 备选 1 | 按框架 D1 原文：`knowledge_bases` 加「场景 + 库位」两列（需 1 次加列 + 回填 + SET NOT NULL + 防漂移机制，且与 §3A.9「不单独配置」矛盾） |
| 备选 2 | 加一列 `kb.db_key`，靠**应用层单点写**防漂移 —— 但 `db_binding` 现在**没有 API 写路径**，这条等于新开一条写路径 |
| 不裁决的后果 | 若按备选 1 落地，会造出**两个可写的库位真相**：改了 `scenarios.db_binding` 忘了改 `kb.db_key` → 路由走 A 库、盘点显示 B 库。而这个错**没有任何报错**，只在"某天查询少了一半文档"时暴露 |
| 我反对备选 1 的理由 | 见 §A.3.1.2 的三条（库位是场景属性 / 一个 kb 可属多场景 / §3A.9 原文） |

#### DQ5-2 「默认库位」的表示法

| 项 | 内容 |
|---|---|
| **推荐** | `NULL` = 默认；展示层用虚拟键 `'default'`（`COALESCE` 只在视图/脚本里） |
| 备选 1 | 存哨兵 `'default'`（**会被现有 CHECK 拒绝**，必须同批改约束） |
| 备选 2 | 存哨兵 `'kb_default'`（能过 CHECK，但会造出一个"看起来像真库位"的假键，逼迫 `engine_for` 为它特判） |
| 不裁决的后果 | D1 的验收信号①「所有行库位 = `default`」**无法被任何一条 SQL 验收**（哨兵不存在、`'default'` 不可写）→ 这条验收信号会永远悬空 |

#### DQ5-3 路由输入的通道（**反对「拆 DI」与「contextvar」**）

| 项 | 内容 |
|---|---|
| **推荐** | 显式 acquire point（取连接点）+ 键来自 `ScenarioSnapshot.db_binding`（已在内存，零新增查询） |
| 备选 1 | 拆 `get_db` / `get_data_db(db_key)` 两个依赖 |
| 备选 2 | contextvar 延后取连接 |
| 不裁决的后果 | 若按备选 1，**每在途请求长期连接占用 1→2 条**，40 条池的并发上限 ~30→~20（推算）；若按备选 2，错误形态是**静默查错库**，与"知识库没内容"在观测上不可区分 |
| 我为什么反对 | §A.3.2.2 的三方案表 + §A.3.2.3 的三条原因 |

#### DQ5-4 `kb_catalog` 建不建（**反对框架稿 D1 现在就建**）

| 项 | 内容 |
|---|---|
| **推荐** | Step 5 **不建**；建 `v_scenario_db_routing` + `fn_kb_db_binding_conflicts()`；`kb_catalog` 推到 Step 6 与「版本口径」一起定 |
| 备选 1 | 现在就建（列契约见 §A.3.5.4，可直接照做） |
| 备选 2 | 永不建（运维直接用 SQL 写 ad-hoc 查询） |
| 不裁决的后果 | 若选备选 1 而不裁决版本口径：视图里 kb 的库位会随「哪个版本」变化 —— 有人按它搬数据 → 搬错 |
| 我为什么反对 | §A.3.5.2 的三条（口径未定 / 不可证伪 / 不是路由输入） |

#### DQ5-5 池超限行为

| 项 | 内容 |
|---|---|
| **推荐** | **启动期硬失败**（`RuntimeError`，拒绝启动）+ 未注册键 **500 fail-closed**；**不做 LRU、不做运行时建池** |
| 备选 1 | LRU 淘汰最久未用的池 |
| 备选 2 | 运行期按需建池（去掉闸门） |
| 不裁决的后果 | 若选备选 1/2，Step 6 一开就可能出现「用户答案被截断」与「PG 连接数不可枚举」两类问题；且这两类问题**都不产生告警** |
| 若你仍选 LRU | 必须先回答 §A.3.4.4 的三问 |

#### DQ5-6 Step 6 的每桶池容量（这条决定"分库后还能不能扛"）

| 项 | 内容 |
|---|---|
| **推荐** | `K=2` 时把 `DB_POOL_SIZE=10 / DB_MAX_OVERFLOW=10`（每桶 20），**连接总数保持 50** |
| 备选 1 | 不调池（每桶 40）→ `needed=90`，**恰好触顶、零余量** |
| 备选 2 | 分库同时加副本（N=2, K=2, P=40 → 170，**直接超限**） |
| 不裁决的后果 | 在 Load 之前不会有人发现。**推荐值的代价必须一起接受**：每库并发 ~30 → ~20（池 20 的实测崩点是 c=20），**必须重跑阶梯压测** |

#### DQ5-7 开关 `SCENE_DB_SPLIT_ENABLED` 的语义优先级

| 项 | 内容 |
|---|---|
| **推荐** | 关闭态 = **路由功能不存在**（`data_session` 零调用）；且关闭态下若存在非 NULL 的 `db_binding`，**启动 WARN**（复用 S-7 的既有 WARN，不新增一套） |
| 备选 1 | 关闭态 = 「忽略 `db_binding`，全部落默认库」但保留 `data_session` 可被调用 |
| 备选 2 | 不加开关（`db_binding IS NULL` 本身就是关闭态 —— 先例：Step 3 的 policy 靠"空覆盖集"实现等价，代码里**没有** `SCENARIO_POLICY_ENABLED`） |
| 不裁决的后果 | 若选备选 1，「关闭」与「打开但全部未绑定」在行为上无法区分 → 排障时无法回答"当时开关是开还是关" |
| 说明 | 我**接受**主理人给定的开关（默认 `False`），但必须**同时**接受两条实现约束：① 开关只**启动期读一次**（不每请求读），② 进 `config_fingerprint`（I-5-7）。否则它就是一个"运行期可变、且不在任何横幅里"的第二状态 —— 正是 Step 4B 的 §E-4 明确反对的形态 |

#### DQ5-8 是否现在就加 admin 只读端点

| 项 | 内容 |
|---|---|
| **推荐** | **不加**，用 `scripts/db_routing_report.py`（退出码可进 CI/cron） |
| 备选 1 | 加 `GET /api/admin/db-routing` |
| 不裁决的后果 | 若加，必须**追加一行**到 `tests/scenario/test_step4_guards.py:33-45` 的 `_EXPECTED_ROUTES`（集合精确相等，属"加严"）。代价小，但本步要的是「路由表零改动」这种最强的等价证据 |
| 为什么推荐脚本 | 库位是**部署事实**，验收者是 CI/cron/部署者，不是运维随时点开的页面。先例：Step 4B 的 `--check` 退出码（唯一能"自动发现"的通道） |

---

### A.5 待定项（现在定不了，须诚实标注）

| # | 项 | 为什么现在定不了 | 影响 | 临时处置 |
|---|---|---|---|---|
| **U-5-1** | 多副本下的库位键失效 | 【源】resolver 行缓存是**进程内 dict**（60s TTL），`invalidate()` 只影响当前进程（I-5-0m）。而 `db_binding` **没有 API 写路径**（I-5-0j）→ 改它只能靠运维 SQL → **不会触发任何 invalidate** → 多副本各有各的 60s 窗口 | 改完 `db_binding` 后 60s 内，不同副本可能路由到不同库 → 「一半文档查不到」 | **Step 6 的迁移 SOP 必须含「改完重启 api 或停流量」**；正解（Redis 失效通道）属 Step 6+ |
| **U-5-2** | worker 侧路由 | ★ **Step 6 若只给 api 加路由，worker 仍把 chunk 写进控制库** → 「检索查场景库、解析写控制库」→ 新文档永远检不到。本步**只预置**：`scene_db_split_enabled` 与 `scene_db_dsn` 进 `CROSS_ROLE_FIELDS`（I-5-7），让配置不一致**可被发现** | 数据分叉且不报错 | Step 6 必须同步改 worker 的解析写入；I-5-7 的指纹是本步能做的全部 |
| **U-5-3** | `db_binding` 要不要补 API 写路径 | 补了就把「部署动作」变成「运行期可变」，与框架 §7.2 Step 7 冲突；不补则变更只能靠 SQL（且没有审计） | 「谁在什么时候把场景搬到哪个库」不可查 | **推荐不补**；若要审计，给它一条**部署审计**（同 Step 4B 的 U-4B-6） |
| **U-5-4** | `max_connections` 的真实值 | 本项目未覆盖 PG 该参数（框架 §3A.4 明确「默认 100」）。启动期会查 `SHOW max_connections` 并以**较小者**为准（§B.4.2），但若 DSN 无权限读它，就退回配置值 | 闸门偏松（不报警）或偏紧（拒绝启动） | 配置值 + 启动期实测比对 + WARN |
| **U-5-5** | 跨库审计的落点 | 【源】`audit.record()` 与业务写在**同一个 session**。Step 6 拆 session 后，审计必须留在**控制库**（I-8），而业务写进场景库 → **一次请求两次写不同库** | 若把审计跟着搬到场景库，跨场景审计一条 SQL 就查不了了（框架 D4 的验收信号①） | Step 6 必须拆；本步登记 |
| **U-5-6** | kb 多场景冲突的**处置** | 检测器有了（§B.1.3），但"怎么修"没定：改 `kb_ids`？拆 kb？还是拒绝发布？ | 检测到冲突后运维无标准动作 | Step 6 裁决；本步只保证「冲突可见」 |
| **U-5-7** | 每桶池容量的真实标定 | 需压测（框架 §3A.4 注 1 的方法：固定池、阶梯升并发、取失败率首次 >0 的前一档） | DQ5-6 的推荐值是**外推**不是实测 | 标「站位值」，进 placeholders（§B.2.3） |
| **U-5-8** | 「这条连接连的是哪个库」如何观测 | `pg_stat_activity.datname` 可得，但本项目没有把它暴露到 `/metrics`（且 4B 已裁决不加 Gauge） | Step 6 排障时无法快速回答"连错库了吗" | Step 6 用脚本查（不进 metrics） |

---

## B. 实施规范

### B.1 迁移 0030（DDL 全文）

#### B.1.1 文件头

```python
"""数据层目录：场景 → 库位 的可盘点视图 + kb 级冲突检测

Revision ID: 0030
Revises: 0029
Create Date: 2026-11-22

★ 本迁移**不加列、不加约束、不加触发器、不写数据**，只建 2 个视图 + 1 个函数。
  零影响的数学根据：没有写入者（全是 STABLE 只读对象）、检索链路一行不改、
  没有任何代码在关闭态下读它们（I-5-3 / I-5-5）。

★ 为什么**不给 knowledge_bases 加库位列**（与框架稿 §3A.8 D1 的偏移）：
  · 库位是**场景**的属性：温档隔离单位是场景（框架 §8 Q11），一次检索只连一个库；
    而一个 kb 可出现在多个场景的 kb_ids 集合里（0028:166 是 JSONB 数组）。
  · 给 kb 加可写库位列 = 两个可写真相 → 必然分叉，而分叉不报错。
  · 框架 §3A.9 原文：「库位由知识集合所属场景推出，**不单独配置**」。
  · 权威列 scenarios.db_binding 早在 0028 就存在（:120），本迁移只是让它**可盘点**。

★ 为什么默认库位用 NULL 而不是哨兵 'default'：
  ck_scenarios_db_binding_format = "db_binding IS NULL OR db_binding ~ '^kb_[a-z0-9_]{2,40}$'"
  → 'default' 会被 DB 直接拒绝（0028:137-138）。展示层用 COALESCE(db_binding,'default')
    得到虚拟键 'default'（本迁移的两个视图负责这件事）。

★ 为什么 kb 冲突只能"检测"不能"约束"：
  冲突由"两个各自合法的场景"组合而成（同一 kb 被两个场景绑定到两个不同库位），
  这是**跨行聚合**条件，PG 的 CHECK/UNIQUE 表达不了。故落成函数 + 视图：
  健康态 = 0 行。
"""
```

#### B.1.2 场景级路由视图（可断言的恒等式：行数恒等于 `scenarios` 行数）

```sql
CREATE OR REPLACE VIEW v_scenario_db_routing AS
SELECT
    s.tenant_id,
    s.id                                                  AS scenario_id,
    s.scenario_key,
    s.status,
    s.is_builtin,
    v.version                                             AS published_version,
    s.db_binding                                          AS db_binding_raw,
    COALESCE(s.db_binding, 'default')::text                AS db_key,
    CASE WHEN s.db_binding IS NULL THEN 'default' ELSE 'bound' END AS db_key_source,
    (s.db_binding IS NULL)                                AS is_default
FROM scenarios s
LEFT JOIN scenario_versions v
       ON v.scenario_id = s.id
      AND v.tenant_id   = s.tenant_id      -- 加严：与 resolver.LOOKUP_SQL 同款（跨租户读到 = fail-closed）
      AND v.state       = 'published';
```

【推导】四条实现细节，每条都有理由，不许"顺手"改：

1. **`LEFT JOIN` 而非 `INNER JOIN`**：无已发布版本的场景（刚建、或全归档）必须**仍在视图里**，否则「这个场景路由到哪」在盘点里直接消失。而 `LEFT JOIN` 能成立的关键前提是【源】`uq_scenario_versions_one_published`（每场景至多 1 个 published）→ **不会放大行数** → 「视图行数 == `scenarios` 行数」成为一条可断言的恒等式（T-5-10）。
2. **`v.tenant_id = s.tenant_id` 必须显式**：`resolver.LOOKUP_SQL` 里已把它定义为"加严"（版本行租户不符视为查不到）。视图若漏了它，就会出现「视图说有已发布版本、resolver 说没有」的**第二真源**（I-5-8）。
3. **`state = 'published'` 是**唯一**版本口径**。不要加 `draft`/`validated`：draft 会随写接口频繁变，把它放进"数据住哪"的盘点里会让视图每秒都可能变。
4. **`db_key_source` 用两个离散值**（`default` / `bound`），不要用 `NULL` 表达 `default` —— 否则「为什么是默认」与「查不到」在同一个列上混淆（与 §A.3.5.4 的 `db_key_distinct` 是同一类教训）。

#### B.1.3 kb 级冲突检测（健康态 = 0 行）

```sql
CREATE OR REPLACE FUNCTION fn_kb_db_binding_conflicts()
RETURNS TABLE (
    tenant_id   uuid,
    kb_id       text,
    db_keys     text[],
    scene_count bigint
)
LANGUAGE sql
STABLE
AS $$
WITH bound AS (
    SELECT
        s.tenant_id,
        kb_id.value            AS kb_id,
        s.db_binding           AS db_key,
        s.id                   AS scenario_id
    FROM scenarios s
    JOIN scenario_versions v
      ON v.scenario_id = s.id
     AND v.tenant_id   = s.tenant_id
     AND v.state       = 'published'
    CROSS JOIN LATERAL jsonb_array_elements_text(v.kb_ids) AS kb_id(value)
    WHERE s.db_binding IS NOT NULL          -- 只判"绑定了非默认库位"的行
)
SELECT
    tenant_id,
    kb_id,
    array_agg(DISTINCT db_key ORDER BY db_key) AS db_keys,
    count(DISTINCT scenario_id)                AS scene_count
FROM bound
GROUP BY tenant_id, kb_id
HAVING count(DISTINCT db_key) > 1;         -- 同一 kb 被绑到 ≥2 个不同库位 = 冲突
$$;
```

```sql
CREATE OR REPLACE VIEW v_kb_db_binding_conflicts AS
SELECT * FROM fn_kb_db_binding_conflicts();
```

【推导】四个细节：

1. **`jsonb_array_elements_text`**：`scenario_versions.kb_ids` 是 `JSONB NOT NULL DEFAULT '[]'`（`0028:166`），元素由 CHECK C-B2 保证全是字符串（`:218-224`）。用 `_text` 变体直接得 `text`，避免 `::uuid` 转换失败（`kb_ids` 里理论上可能有非 UUID 的历史脏数据 —— 那时**盘点不该 500**，该把它当字符串暴露出来）。
2. **`WHERE s.db_binding IS NOT NULL`**：`NULL = 默认库`，默认库与任何库都不冲突（否则"全部默认"这个健康态会被误判成冲突）。
3. **`HAVING count(DISTINCT db_key) > 1`**：判据是"**≥2 个不同的库位**"，不是"≥2 个场景"。同一 kb 被两个场景绑到**同一个**非默认库位是**合法**的（那正是"温档"的意图：多个场景同库）。
4. **`SELECT * FROM fn_...`**：包一层视图是为了 `psql \d+` / ORM 反射友好；函数是唯一实现（先例：Step 4B 的 `fn_chunk_reindex_candidates` + 视图，理由同「判定只有一份」）。

#### B.1.4 迁移里的三条守卫（不是可选的）

1. **单头**：`down_revision = "0029"`，且 `0029` 之后无其它分支。由 `test_0030_migration.py` 的 `_render("up", "0029:0030")` 断言存在 `-- Running upgrade 0029 -> 0030`。
2. **不加任何 CHECK / UNIQUE 约束** → `tests/scenario/test_0028_migration.py::_pinned_constraint_names()`（`:192-199`）与 `test_pg_tests_have_no_unnamed_violation_assertions`（`:225-264`）**均不受影响**，`_PG_CONSTRAINT_SOURCES`（`:183`）**不需要追加注册**。这条要写进迁移 docstring，否则下一个人会以为"新迁移必须登记"。
3. **`ck_scenarios_db_binding_format` 必须仍在且语义不变**（本迁移不碰它，但要在迁移里加一条只读断言，把「'default' 不可写」这件事变成迁移期事实）：
```python
op.execute("""
DO $$
DECLARE v_def text;
BEGIN
  SELECT pg_get_constraintdef(oid) INTO v_def
  FROM pg_constraint WHERE conname = 'ck_scenarios_db_binding_format';
  IF v_def IS NULL OR v_def NOT LIKE '%kb_%' THEN
    RAISE EXCEPTION '0030: ck_scenarios_db_binding_format 缺失或已改口径；'
                    '默认库位统一用 NULL 表示，禁止把 ''default'' 写入该列（I-5-2）';
  END IF;
END $$;
""")
```
【推导】这不是"约束"而是"**前置条件断言**"：如果将来有人把 CHECK 改成允许 `'default'`，本视图的 `COALESCE(...,'default')` 与存储层就会同时存在两个"默认"表示法 —— 而注释不会失败，断言会。

#### B.1.5 `downgrade()`

```python
def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_kb_db_binding_conflicts")
    op.execute("DROP VIEW IF EXISTS v_scenario_db_routing")
    op.execute("DROP FUNCTION IF EXISTS fn_kb_db_binding_conflicts()")
```
【推导】**无数据、无写入者 → 降级是纯 DDL，没有数据回滚问题。** 这是本步选「不加列」换来的最大好处（对比 §A.3.6.2 第 5 点：加过列的 downgrade 必然丢数据）。

---

### B.2 settings 与站位值

#### B.2.1 六项（全部为部署期常量，不进请求路径）

```python
# ── 数据层路由（Step 5 建目录与路由层，Step 6 才接线）────────────
# ★ 默认 false：关闭态下 db_router 不创建任何引擎、不被任何生产模块调用（I-5-3）。
# ★ 关闭态 ≠ 迁移回滚：若数据已经搬到别的库，关开关会让查询全部落回默认库
#   → 「一半文档查不到」。安全顺序见设计文档 §E.2。
scene_db_split_enabled: bool = False

# 库位键 → DSN。例（JSON）：{"kb_finance": "postgresql+asyncpg://user:pw@postgres:5432/kagent_finance"}
# ★ 键必须匹配 ^kb_[a-z0-9_]{2,40}$（与 ck_scenarios_db_binding_format 同口径），
#   启动期校验；'default' 是保留字，不得作为键出现（它指现有库）。
# ★ 值含凭据 → MUST 逐值 _redact_url 后才进快照（config_profile），永不进日志明文/响应体。
scene_db_dsn: dict[str, str] = {}

# ── 连接预算闸门（数值全部来自框架 §3A.4 的算式，见设计文档 §A.3.4）──
# ★ 100 是 PG 默认值，本项目未覆盖该参数；启动期会 SHOW max_connections 比对，取较小者。
db_max_connections_for_budget: int = 100
# ★ 预留 10 是**设定值不是实测**（框架 §3A.4 原文）；已登记为站位值。
db_conn_reserved: int = 10
# ★ worker 侧 10 条（compose DB_POOL_SIZE=5 + DB_MAX_OVERFLOW=5）。
db_worker_conn_total: int = 10
# ★ 副本数应用自己不知道（无共享状态）→ 只能由部署声明；默认 1 是保守值（闸门偏松）。
db_replicas_expected: int = 1
```

#### B.2.2 校验（`@field_validator`，启动期失败即拒绝启动）

| 检查 | 失败动作 | 理由 |
|---|---|---|
| `scene_db_dsn` 的每个键都匹配 `^kb_[a-z0-9_]{2,40}$` | `ValueError` → 拒绝启动 | 键会与 `scenarios.db_binding` 的 CHECK 同口径比较；不同口径 = 写进去的值永远匹配不上 DSN |
| 键集里不得出现 `'default'` | 同上 | `default` 是**保留虚拟键**（指现有库），它没有、也不该有 DSN |
| `scene_db_dsn` 的每个值必须 `postgresql+asyncpg://` 开头 | 同上 | 驱动不匹配是运行期才炸的错误，挪到启动期 |
| `db_pool_size + db_max_overflow ≥ 20`（复用既有口径 I-5-0q） | 沿用 `tests/config/test_db_pool.py:46` 的既有断言，不加新代码 | 不重复实现 |

#### B.2.3 站位值登记（`app/config/placeholders.py`，owner=`db_router`）

| 名字 | 值 | note（写给运维看） |
|---|---|---|
| `DB_CONN_RESERVED` | `10` | 待实测标定 · 框架 §3A.4 明确它是「设定值不是实测」；给 PG 预留的连接数 |
| `DB_WORKER_CONN_TOTAL` | `10` | 待编排核对 · 当前 compose 给 worker 的是 5+5；改编排必须同步改它，否则闸门算错 |
| `DB_REPLICAS_EXPECTED` | `1` | 待部署标定 · 应用不知道自己有几个副本，闸门偏松是刻意的（宁可偏松也不要拒绝启动） |

**并加一条 import 期一致性断言**（与 `resolver.py:47-64`、`policy_spec.py` 同款）：镜像值必须与 `settings` 默认值相等，否则启动炸。

#### B.2.4 为什么开关只读一次

`db_router` 在**导入期/启动期**读 `settings.scene_db_split_enabled` 并固化到 registry。**不在每次取连接时读**：① 每请求读一次 settings（lru_cache 命中，成本≈0）但**语义上**会造出"运行期可变"的假象；② 本项目对「运行期可变的第二状态」有明确纪律（Step 4B §E-4：开关要么进指纹要么别加）。既然它进了 `CROSS_ROLE_FIELDS`（I-5-7），就**必须**是启动期常量 —— 否则指纹会描述一个已经变了的状态。

---

### B.3 `app/infra/db_router.py`（接口签名 + 关键约束）

#### B.3.1 接口（这是 Step 6 唯一需要 import 的东西）

```python
DEFAULT_DB_KEY: Final[str] = "default"
_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(r"^kb_[a-z0-9_]{2,40}$")

class DbRouteError(Exception):
    """库位解析/取连接失败。★ 只有一个来源：部署配置缺陷，不是调用方错误。"""
    code: Final[str]          # DB_ROUTE_KEY_UNKNOWN / DB_ROUTE_BUDGET_EXCEEDED / DB_ROUTE_DSN_INVALID
    detail: dict[str, object]

def resolve_db_key(db_binding: str | None) -> str:
    """场景行上的 db_binding → 库位逻辑键。**纯函数、零 IO、不读 settings**。
    None → 'default'；非空 → 原样返回（**不校验注册表**，见 §B.3.2 第 1 条）。
    """

@dataclass(frozen=True, slots=True)
class RouterReport:
    bucket_count: int          # Step 5 恒为 1
    per_bucket_connections: int
    total_connections: int      # Step 5 恒为 40
    needed_connections: int     # total × replicas + worker
    budget: int                # max_connections - reserved
    keys: tuple[str, ...]      # ('default',) 或 ('default','kb_finance')
    created_engines: int       # 关闭态恒为 0（C-2 的锚点）
    notes: tuple[str, ...]     # 用于横幅：不一致/WARN 的文字

def register_from_settings() -> RouterReport: ...
    """启动期调用一次。关闭态：只把 app.database.engine 登记为 default，不建任何引擎。"""

def engine_for(db_key: str) -> AsyncEngine: ...
    """default → 现有 engine（**同一对象**）；注册过的 kb_* → 其引擎；
    其它 → raise DbRouteError(DB_ROUTE_KEY_UNKNOWN)（I-5-4）。"""

@asynccontextmanager
async def data_session(db_key: str) -> AsyncIterator[AsyncSession]: ...
    """数据面 session。**Step 5 零调用点**（T-5-16 守）。"""

def check_connection_budget() -> None: ...
    """T1 纪律：违反即 raise（拒绝启动）。纯算术，不连库。"""

async def log_db_router_banner(session_factory) -> None: ...
    """启动横幅一行 + 与 SHOW max_connections 比对（§B.4.2）。异常只 WARN，不阻塞启动。"""
```

#### B.3.2 五条实现约束（每条都有"为什么不能那样写"）

| # | 约束 | 为什么（反例） |
|---|---|---|
| 1 | `resolve_db_key` **不查注册表、不读 settings** | 若它校验注册表，那么"键的解析"与"键的可用性"就混在一处 → 关闭态下调用它（例如日志里打一句）就会 raise，或需要 `if enabled` 分支 → 破坏关闭态等价。**分离后**：「解析」可穷举单测（T-5-1）；「可用性」只有 `engine_for` 一处（T-5-6） |
| 2 | 关闭态**绝不** `create_async_engine`；`engine_for('default')` 返回 `app.database.engine` 本体 | 参数一样的新引擎**行为看起来一样**（同一个 DSN、同一套参数），只有 `is` 能抓住它（C-1）。而多一个池 = PG 连接预算翻倍，**在 QPS 低时完全看不出来** |
| 3 | 任一 `create_async_engine` 调用点必须同时 `_CREATED_ENGINES += 1` | C-2 的计数器依赖它。这是唯一能证明"关闭态没建池"的机制（"没连上"不等于"没建池" —— 池是 lazy 的） |
| 4 | `data_session` 用 `@asynccontextmanager`，**不是** FastAPI 依赖 | 它是 DQ5-3 的裁决落地：DI 会按 callable 缓存并**多一个 session**（§A.3.2.2） |
| 5 | 所有路径都**不新增查询** | 库位键已在 `ScenarioSnapshot.db_binding`（I-5-0i）。连"打日志时 `SELECT current_database()`"都不许 —— C-4b 会红 |

#### B.3.3 代码骨架（规范级，可照抄）

```python
def register_from_settings() -> RouterReport:
    s = get_settings()
    keys: tuple[str, ...] = (DEFAULT_DB_KEY, *sorted(s.scene_db_dsn))
    if not s.scene_db_split_enabled:
        # 关闭态：只登记现有 engine，**不建任何引擎**（I-5-3 / C-1 / C-2）
        _REGISTRY[DEFAULT_DB_KEY] = _EngineBinding(
            engine=app_database.engine,
            sessionmaker=app_database.SessionLocal,
            source="default",
        )
        return _report(keys=(DEFAULT_DB_KEY,))
    for key, dsn in sorted(s.scene_db_dsn.items()):
        _REGISTRY[key] = _EngineBinding(
            engine=_build_engine(dsn),        # 内部分配，唯一 create 点（+计数器）
            sessionmaker=async_sessionmaker(_REGISTRY[key].engine, expire_on_commit=False),
            source=f"env:SCENE_DB_DSN[{key}]",
        )
    _REGISTRY[DEFAULT_DB_KEY] = _EngineBinding(
        engine=app_database.engine, sessionmaker=app_database.SessionLocal, source="default"
    )
    return _report(keys=keys)
```

```python
def check_connection_budget() -> None:
    """纯算术，无 IO。违反 → RuntimeError（T1：配置/代码错就别启动）。"""
    s = get_settings()
    per_bucket = s.db_pool_size + s.db_max_overflow
    k = 1 + len(s.scene_db_dsn) if s.scene_db_split_enabled else 1
    needed = per_bucket * s.db_replicas_expected * k + s.db_worker_conn_total
    budget = s.db_max_connections_for_budget - s.db_conn_reserved
    if needed > budget:
        raise RuntimeError(
            f"PG 连接预算超限：{per_bucket}×{s.db_replicas_expected}(副本)×{k}(库位)"
            f"+{s.db_worker_conn_total}(worker) = {needed} > {budget}"
            f"（= {s.db_max_connections_for_budget} max_connections − {s.db_conn_reserved} 预留）。"
            f"处置三选一：① 降 DB_POOL_SIZE/DB_MAX_OVERFLOW；② 减库位；③ 减副本。"
            f"★ 不要靠调大 max_connections 解决 —— 那只是把雪崩点推后。"
        )
```

【推导】错误文案里必须给**三条处置**，而不是只说"超限"。本项目已记录过教训（`capabilities.py:66-72`）：一个永远红且不给动作的闸门，运维很快就学会无视它。

#### B.3.4 与 `tests/config/test_db_pool.py` 的兼容（必须逐条核过）

| 既有断言 | 本设计后是否仍成立 | 说明 |
|---|---|---|
| `pool.size() == settings.db_pool_size` | ✅ | 关闭态用的是同一个 engine；开启态每桶都用同一组 settings |
| `pool._max_overflow == settings.db_max_overflow` | ✅ | 同上 |
| `pool._timeout == settings.db_pool_timeout_s` | ✅ | 每桶同参 |
| `pool+overflow ≥ 20` / `≤ 45` | ✅ | 推荐配置下每桶 20（§A.3.4.1），落在 [20,45] 内 |

**这四条零改动**是本设计的硬目标（F-13）：任何"全局预算再分摊"的实现都会让第一条红（§A.3.4.2）。

---

### B.4 预算闸门与启动横幅

#### B.4.1 闸门位置与动作

| 时机 | 动作 | 失败动作 |
|---|---|---|
| `settings` 校验期 | 键格式 / DSN scheme / 保留字（§B.2.2） | `ValueError` → 拒绝启动 |
| `lifespan` 早期（`check_scenario_constants()` 之后） | `check_connection_budget()`（纯算术） | `RuntimeError` → 拒绝启动 |
| `lifespan` 中期 | `register_from_settings()` → `RouterReport` | 同上（DSN 非法） |
| `lifespan` 后期（横幅） | `await log_db_router_banner(...)` | **只 WARN**，不阻塞启动 |

【推导】为什么闸门放在 `check_scenario_constants()` 之后、任何 DB 访问之前：它不需要 DB，越早失败越好；而它需要的 `settings` 在 import 期就有了。T1/T2 分流纪律的先例见 `app/main.py:120-160`。

#### B.4.2 启动时用 `SHOW max_connections` 校正预算（一次查询，仅在启动期）

【推导】`db_max_connections_for_budget=100` 是**假设值**（本项目未覆盖 PG 参数）。用一次 `SHOW max_connections` 把它变成**实测值**，只花一次往返、只在启动期、不进请求路径 —— 而它把"闸门偏松/偏紧"这个风险从"未知"降到"可观测"：

```python
actual = await session.scalar(text("SHOW max_connections"))   # 启动期一次
budget = min(settings.db_max_connections_for_budget, int(actual)) - settings.db_conn_reserved
if int(actual) != settings.db_max_connections_for_budget:
    logger.warning("PG max_connections=%s 与配置 %s 不一致，闸门按较小者计算",
                   actual, settings.db_max_connections_for_budget)
```

**取较小者**（不是较大者）：保守方向。若 `SHOW` 因权限失败 → 退回配置值 + WARN（**不阻塞启动**：一个连不上的实例也会在别处失败）。

#### B.4.3 横幅一行（谁看：部署者）

```
DB 路由 : 关闭（全部落默认库）· 桶=1 · 每桶 40 · 总计 40 · 预算 50/90 ·
          max_connections 实测=100 · 站位值 DB_CONN_RESERVED=10 …
```

关闭态**必打**（哪怕全是默认值）：它是"当时开关是开还是关"的唯一留痕。开启态额外打一行键清单 + 每个键的**来源**（`env:SCENE_DB_DSN[kb_finance]`），**不打 DSN 值**。

#### B.4.4 非 NULL `db_binding` 在关闭态的可见性

复用 S-7 的既有 WARN（`self_check.py:521-539`），**不新增一套**。理由：两套 WARN 会对同一件事报两次，而其中一套的口径会漂。Step 5 对 `WIRED_RETRIEVAL_STEP=3` 与 `DB_BINDING_WIRED_STEP=6` **均零改动** —— 因为在 Step 5 里「db_binding 还没到生效的步骤」这句话**依然为真**（本步不接线）。

---

### B.5 错误码

| 码 | HTTP / 异常 | 含义 | 可重试 | 谁能修 |
|---|---|---|---|---|
| `DB_ROUTE_KEY_UNKNOWN` | **500** | 场景指向的库位键不在启动期注册表里 | 否 | 部署者（补 DSN 或改 db_binding，见 U-5-1 的 60s 窗口） |
| `DB_ROUTE_DSN_INVALID` | 启动期 `RuntimeError` | DSN 格式/驱动不对 | —— | 部署者 |
| `DB_ROUTE_BUDGET_EXCEEDED` | 启动期 `RuntimeError` | 连接预算超限 | —— | 部署者（三条处置见 §B.3.3） |
| `DB_ROUTE_DISABLED_BUT_BOUND` | **WARN + metric**（不是错误） | 关闭态下有人绑了非 NULL 库位（复用 S-7） | —— | 部署者 |
| （沿用）`pool_timeout` | **503** | 某个桶耗尽，等池超时 5s | 是（退避后） | 容量（DQ5-6） |

【推导】**为什么未知键是 500 而不是 400**：调用方（外部 agent）**没有能力**指定库位（框架 I-7：`凭证 → 场景 → 库位` 全在服务端，无入参）。所以这个错误**一定是服务端配置缺陷**。给 400 会让调用方去改自己的请求（徒劳），而给 200+空 citations 会把配置错误伪装成"知识库没内容"（本项目最反对的静默错误类）。

---

### B.6 `config_profile` 接线（本文件最重要的一行改动）

```python
# runtime_config_snapshot() 追加：
"scene_db_split_enabled": s.scene_db_split_enabled,
"scene_db_keys": sorted(s.scene_db_dsn),                    # ★ 只放键名，不放 DSN
"scene_db_dsn_redacted": {k: _redact_url(v) for k, v in s.scene_db_dsn.items()},
"db_max_connections_for_budget": s.db_max_connections_for_budget,
"db_conn_reserved": s.db_conn_reserved,
"db_worker_conn_total": s.db_worker_conn_total,
"db_replicas_expected": s.db_replicas_expected,

# CROSS_ROLE_FIELDS 追加 2 项（判据见下）：
"scene_db_split_enabled",
"scene_db_keys",          # 键**集**，不是 DSN 值
```

**为什么这两项必须进 `CROSS_ROLE_FIELDS`**（I-5-7）。【源】该白名单的判据原文（`config_profile.py:93-99`）：

> 「一个字段进白名单，当且仅当『两侧取不同值会导致**行为或数据不可解释**』。」

逐条命中：

- **`scene_db_split_enabled`**：api 开、worker 关 → api 检索查场景库、worker 解析写控制库 → **新文档永远检不到**。这是"数据不可解释"的教科书形态。
- **`scene_db_keys`**：两侧键集不同 → 同一份 `.env` 派生出两个"库位宇宙"。worker 认得 `kb_finance`、api 不认得（或反之）→ 一侧 500。

**为什么只放"键集"不放 DSN 值**：DSN 含凭据，进指纹就进了日志（即使是哈希，也把"凭据变更"这件事的强度绑到了 12 位 sha1 上 —— 没必要）。键集足够回答"两侧是不是同一个库位宇宙"。

【推导】**脱敏必须是逐值 `_redact_url`**（`config_profile.py:36-49` 已有该函数）。`_redact_url` 会把 `user:pw@` 去掉只留 host:port —— 而"是不是同一个实例"恰恰只需要 host:port。这正好与框架 §3A.2.2 纪律 3（连接串不下沉到场景配置）同向：连接串只活在 L1 部署层。

---

### B.7 `scripts/db_routing_report.py`（只读盘点，退出码可进 CI）

| 检查项 | 判据 | 退出码 |
|---|---|---|
| ① 存储层默认态 | `SELECT count(*) FROM scenarios WHERE db_binding IS NOT NULL` == 0（Step 5） | 非 0 → **1** |
| ② 视图恒等式 | `count(v_scenario_db_routing)` == `count(scenarios)` | 不等 → **1** |
| ③ kb 冲突 | `count(v_kb_db_binding_conflicts)` == 0 | > 0 → **1** |
| ④ 注册表自洽 | 关闭态：`registered_keys == ('default',)` 且 `created_engines == 0` | 否 → **1** |
| ⑤ 预算 | `needed <= budget`（与闸门同一函数，**不重写算术**） | 否 → **1** |
| ⑥ 启用态键覆盖 | `set(db_binding 非 NULL 的值) ⊆ set(registry keys)` | 有缺口 → **1**（并列出缺口键） |

输出：每项一行 `PASS/FAIL`，FAIL 时给"下一步动作"（不是只给数字）。**默认不打印任何 DSN**；`--show-dsn-hosts` 才打印 host:port 且**永不打印 user/pw**。

【推导】⑥ 是 Step 6 最有用的一项：**改完 `db_binding` 但没配 DSN** 会让所有请求 500，而这个检查能在部署前发现它。它同时是 §G-R2（缓存窗口）的缓解手段之一。

---

### B.8 关闭态等价的四条实现约束（写进代码注释，不是写进文档）

1. **`app/database.py` 与 `app/api/deps.py` 一行不改**。它们是 C-1/C-3 的锚点。
2. **`app/infra/db_router.py` 在关闭态被 import 时，模块级不得有任何 IO、不得创建引擎**（允许读 `settings`，因为 `get_settings` 已是 lru_cache）。
3. **没有任何生产模块 import `data_session`**（Step 5）。唯一 import 者允许是 `tests/`（T-5-16 会精确列出）。
4. **不新增任何路由**（`app.openapi()["paths"]` 与改动前逐键相等 → 顺带让 `_EXPECTED_ROUTES` 零改动）。

---

### B.9 路由输入链的完整装配图（把"凭证→场景→库位"一条链落到代码位置）

```
请求头 X-Application-Id / X-Application-Key  +  Authorization: Bearer <JWT>
   │
   ├─ get_current_user        (deps.py:64)           控制库：users / 主体解析
   ├─ get_integration_principal (integrations.py:77)  控制库：integration_applications（验哈希）
   │        ↓  principal.application.scenario_id
   ├─ resolve_scenario(session, tenant_id, application, requested_kb_ids)   (resolver.py:176)
   │        └─ LOOKUP_SQL（resolver.py:71-96）：**同一次 SELECT 已返回 s.db_binding**
   │        └─ ScenarioSnapshot.db_binding（snapshot.py:152/253/356）
   │        ↓
   ├─ db_router.resolve_db_key(snapshot.db_binding)     ← 纯函数，零 IO，零查询
   │        ↓  'default' | 'kb_finance'
   └─ db_router.data_session(db_key)                    ← Step 6 才接；Step 5 零调用点
```

【推导】这张图说明三件事，每件都是对某个硬问题的回答：

1. **死结不存在**（硬问题 B）：凭证与场景都在控制库，所以它们的解析**必须**用控制库会话 —— 这不是"用错库"，这是 I-8 的规定。
2. **零新增查询**（I-5-5）：`db_binding` 与 `policy` / `kb_ids` / `binding_mode` 在**同一次** SELECT 里返回（F-10）。若有人图省事在路由层再查一次 `scenarios`，C-4 会红。
3. **无入参**（框架 I-7 / §3A.9）：整条链上没有任何一处读取请求体/查询串来决定库位。→ "请求里传 `?db=`" 在结构上**不可能**发生。

---

## C. Step 6 的接线点与不可逆边界（本期只设计，不实现）

| # | Step 6 必须改的（本步**已定的形状**） | 不可逆性 | 回滚 |
|---|---|---|---|
| C-1 | `integration_ask` / `integration_search`：解析后 `await session.close()` → `async with data_session(db_key) as ds:` → 把 `ds` 传给 `_chat_ask_impl` / `retrieve` | 无（代码） | revert 两个端点 |
| C-2 | **worker 解析写入也要走 `data_session`**（否则写读分叉，U-5-2） | 无（代码） | revert worker |
| C-3 | **搬数据**（`pg_dump -d` 单场景 → 新 database） | **有：数据位置变了** | 把 `db_binding` 改回 NULL（数据留新库，只切连接）→ 立刻恢复 |
| C-4 | `audit.record()` 必须留在控制库（U-5-5）→ 一次请求两库写 | 无（代码） | revert |
| C-5 | 补建脚本支持 `--db-key` / `--all-scenes`（框架 §3A.6.2 判定点 8：禁止"默认连默认库") | 无 | 脚本开关回退 |

**Step 6 的操作顺序（顺序错了就是"一半文档查不到"）**：

```
① 建目标 database + 跑迁移到 head（空库，零风险）
② 配 SCENE_DB_DSN[kb_x] + 开 SCENE_DB_SPLIT_ENABLED（此时 profile 里还没有场景指向它）
   → 断言：db_routing_report ⑥ 通过（无缺口、无多余键）
③ 搬数据（dump/restore 该场景全部 kb 的文件 + chunks + documents）
④ 校验搬完（目标库行数 == 源库该场景行数，逐表比对）
⑤ 最后一步才改 db_binding = 'kb_x'
   → ★ 此处必须重启 api（或等 > CACHE_TTL_SECONDS=60s），否则进程内缓存的旧行
     会把一部分请求仍路由到默认库（U-5-1 / G-R2）
⑥ 观察：该场景检索结果与搬前逐条一致（框架 D3 验收信号①）
```

**回滚顺序（与 E.2 一致）**：⑤ 改回 NULL → 等 60s 或重启 → 再关开关。**先关开关再改 NULL 会有一段"全部落默认库"的窗口**。

---

## D. 测试规格（有牙）

### D.1 环境与反假绿纪律

真库用例环境同一 4A/4B：`SCENARIO_TEST_DB_URL` + `pgvector/pgvector:pg16`；`pg_helpers.prepare_schema()` 推到 **head**（I-5-0p）→ 0030 自动被吸收，**测试基建零改动**。

【推导】**本项目已抓到四种假绿**，Step 5 的每条用例必须对号入座地避开：

| # | 假绿形态 | Step 5 的具体规避 |
|---|---|---|
| ① | 断言过宽（`pytest.raises(Exception)` 接住别的错） | 全部负向用例断言**精确类型 + 精确 code**（`DbRouteError.code`）；**不新增任何 `pytest.raises` 于约束断言**（受 `test_0028_migration.py:225-264` 约束：跨源文件只允许一处 `pytest.raises`，在 `expect_violation` 内） |
| ② | 常量相等导致不可观测 | 期望值**一律写字面量**（`40` / `50` / `90` / `'default'`），**不用** `settings.db_pool_size + settings.db_max_overflow` 当期望（那会随 settings 变而自动通过 → 测的是 const == const） |
| ③ | 接缝被 monkeypatch 掉（测的是"我传了"不是"链路收到了"） | `resolve_db_key` 的用例**不 monkeypatch settings**（它是纯函数）；`engine_for` 的用例**断言对象身份**而不是"参数相等" |
| ④ | 并发测试其实没并发 | **没有并发用例**（C-5 的峰值检出需要压测夹具 → 明确标「需你定验证方式」）。**这一项诚实标注为 N/A**，不凑数 |

### D.2 纯单测（`tests/scenario/test_step5_db_router.py`，不连库）

> 每条给：断言 / **把哪一行改坏它必须变红**。第二列是"有牙"的唯一判据。

| # | 用例 | 断言 | **破坏方式（必须变红）** |
|---|---|---|---|
| **T-5-1** | `resolve_db_key` 真值表（**穷举**） | `resolve_db_key(None) == 'default'`；`resolve_db_key('kb_finance') == 'kb_finance'`；`resolve_db_key('kb_x'*1) == 原样`（不做规范化）；且**对未注册键也原样返回**（不 raise） | 把 `resolve_db_key` 改成"校验注册表"（`if key not in registry: raise`）→ 第二格红。**这条用例就是 DQ5-3 的可执行形态**：它把"解析"与"可用性"钉成两件事 |
| **T-5-2** | 关闭态不建引擎（**I-5-3 / C-2**） | `register_from_settings()` 后 `report.created_engines == 0`、`report.bucket_count == 1`、`report.total_connections == 40` | 在 `register_from_settings` 里加 `_build_engine(dsn)`（"先把桶建好"）→ `created_engines` > 0 → 红 |
| **T-5-3** | 默认引擎对象恒等（**C-1**） | `engine_for('default') is app.database.engine` | 改成 `engine_for` 内部 `create_async_engine(settings.database_url, ...)`（参数完全一样）→ `is` 为假 → 红。**这是本步最容易滑进去的一步** |
| **T-5-4** | 预算算术（**I-5-6**） | 4 组参数化：`(P=40,N=1,K=1) → needed 50`、`(40,1,2) → 90`、`(20,1,2) → 50`、`(40,2,2) → 170`；断言 `needed` 逐字面量相等 | 把 `+ worker_total` 漏掉 → 第一格 40 ≠ 50 → 红。把 `× replicas` 漏掉 → 第四格 90 ≠ 170 → 红 |
| **T-5-5** | 超预算必须 raise（**I-5-6**） | `(40,2,2)` 时 `check_connection_budget()` raise `RuntimeError`，且消息里**含三个数字与三条处置**（正则断言 `170`、`90`、`DB_POOL_SIZE`） | 把 `if needed > budget` 改成 `>=` 或删掉 → `(40,1,2)=90` 也会 raise（应通过）或 170 也通过 → 红 |
| **T-5-6** | 未注册键 fail-closed（**I-5-4**） | 关闭态 `engine_for('kb_unknown')` raise `DbRouteError`，`code == 'DB_ROUTE_KEY_UNKNOWN'`；**且**断言它**不是**返回默认引擎 | 把 `except` 分支改成 `return _REGISTRY['default'].engine`（"容错"）→ 红。**这条是"禁止猜库"的唯一守卫** |
| **T-5-7** | `data_session` 零调用点（**C-5 的结构版**） | AST 扫 `app/` 全部 `.py`：`data_session` 的**定义**只在 `db_router.py`；**调用**为 0（`tests/` 除外） | 把 `data_session` 接进 `integration_ask`（哪怕只加一行注释掉不算，得真调用）→ 红。**这条同时是 Step 5「开路由」的物理门禁** |
| **T-5-8** | 开关只启动期读（**§B.2.4**） | AST 扫 `db_router.py`：`scene_db_split_enabled` 只出现在 `register_from_settings` 与 `_report` 内，**不得**出现在 `engine_for` / `data_session` 的函数体里 | 在 `engine_for` 里加 `if get_settings().scene_db_split_enabled:` → 红（那会把开关变成每请求读的运行期状态） |

### D.3 真库用例（`tests/scenario/test_step5_db_router_pg.py`，`requires_db`）

| # | 用例 | 断言 | **破坏方式（必须变红）** |
|---|---|---|---|
| **T-5-9** | `'default'` 不可写（**I-5-2**） | 用既有 `expect_violation(conn, exc, "ck_scenarios_db_binding_format", ...)`（`pg_helpers` 的唯一 `raises` 入口）插 `db_binding='default'` → 必须被**该约束名**拒绝 | 把 CHECK 改成 `db_binding ~ '^([a-z]+|kb_[a-z0-9_]+)$'`（"顺手放宽"）→ 不再抛 → 红。**这条用例的存在本身就是 I-5-2 的守卫** |
| **T-5-10** | 视图行数恒等（**I-5-8 / §B.1.2 细节 1**） | 造 3 个场景：A 有 published（db_binding NULL）、B 有 published（db_binding='kb_x'）、C 无 published → 断言 `count(v_scenario_db_routing) == count(scenarios)`，且 C 行 `published_version IS NULL` 且 `db_key == 'default'` | 把 `LEFT JOIN` 改成 `JOIN` → C 消失 → 行数 2 ≠ 3 → 红。把 `COALESCE(s.db_binding,'default')` 改成 `s.db_binding` → A/C 的 `db_key` 变 NULL（应 `'default'`）→ 红 |
| **T-5-11** | 冲突检测有牙（**§A.3.1.3**） | 造：同租户 kb₁ 被场景 P（`kb_x`）与场景 Q（`kb_y`）同时绑定 → `fn_kb_db_binding_conflicts()` 返回 **1 行**，`db_keys == ['kb_x','kb_y']`；再让 Q 改成 `kb_x` → 返回 **0 行** | 把 `HAVING count(DISTINCT db_key) > 1` 改成 `> 0` → 第二格变 1 行 → 红。把 `WHERE s.db_binding IS NOT NULL` 删掉 → 造两个 NULL 场景也会冲突 → 红（默认库不该冲突） |
| **T-5-12** | 视图口径 == resolver 口径（**I-5-8**） | 同一租户下，对每个有 published 版本的场景：`v_scenario_db_routing.published_version` == `(await resolve_scenario(...)).version`；`db_key` == `resolve_db_key(snapshot.db_binding)` | 把视图的 `v.state='published'` 改成 `v.state<>'archived'`（"多算一点"）→ draft 版本进来 → 版本号不等 → 红 |
| **T-5-13** | 迁移往返 + 对象存在 + 单头 | `upgrade 0029:0030 → downgrade 0030:0029 → upgrade` 无错；三个对象在 `pg_views`/`pg_proc` 里存在；`_render("up","0029:0030")` 含 `-- Running upgrade 0029 -> 0030` | 把 `down_revision` 写成 `"0028"`（复制粘贴）→ 单头断言红 |

### D.4 静态守卫（`tests/scenario/test_step5_guards.py`，不连库，任何机器必须跑）

| # | 用例 | 断言 | **破坏方式（必须变红）** |
|---|---|---|---|
| **T-5-14** | `get_db` 源码金标准（**C-3**） | `sha256(inspect.getsource(deps.get_db))` == 文件里钉死的 64 位十六进制（**实施时首次运行后填入**；先例：Step 4B `test_step4b_guards.py` 里钉死的三段 SQL 哈希） | 给 `get_db` 加 `try/finally` 或 `if settings.scene_db_split_enabled` → 哈希变 → 红。**"我没改连接获取路径"的唯一证据**（先例：4B 的 `T-4B-9`） |
| **T-5-15** | 第二真源禁止（**I-5-1**） | AST 扫全部迁移文件与 `app/models/`：除 `0028`/`entities.py` 的 `scenarios.db_binding` 外，**不得**出现列名 `db_binding` / `db_key` 的 `sa.Column(...)` 或 `mapped_column(...)` | 在 0031 里给 `knowledge_bases` 加 `db_key` → 红。**这是 DQ5-1 的可执行形态**：将来谁想加第二列，它先红 |
| **T-5-16** | 零新增查询的形状断言（**C-4b**） | SQL 常量（迁移/视图/模块里）**不得**出现 `current_database()` / `FROM pg_database` / `information_schema.tables` / `kb_catalog`；`db_router.py` 里**不得**出现 `select(` / `text("SELECT` | 在横幅里加 `SELECT current_database()`（"打日志看看连哪个库"）→ 红 |
| **T-5-17** | 语句多重集金标准（**C-4**） | 抓 `/api/integrations/search` 全链 SQL，`Counter` 与 `tests/scenario/golden/step5_sql_multiset.json` 相等 | 路由实现里查一次 `scenarios` → 多一条 → 红。**代价必须承认**：任何无关的 SQL 改动都会正当变红（同 4B 的 R-5），正确流程是显式更新 golden 并在 commit message 里引用本文件 |
| **T-5-18** | 跨角色一致性（**I-5-7**） | `"scene_db_split_enabled" in config_profile.CROSS_ROLE_FIELDS` 且 `"scene_db_keys" in ...` | 从白名单里删掉任一项 → 红。**这条拦的是"加了开关忘了进指纹"**（Step 4B §E-4 记录过这个形态） |

### D.5 必须逐条保持不变的既有测试

| 文件 | 条数 | Step 5 允许的改动 |
|---|---|---|
| `tests/config/test_db_pool.py` | 4 | **无**（§B.3.4 逐条核过；这是选「每桶参数」的直接收益） |
| `tests/scenario/test_step4_guards.py` | 10 | **无**（本步不加路由，`_EXPECTED_ROUTES` 零改动） |
| `tests/scenario/test_0028_migration.py` | 11 | **无**（`_render` 是显式区间；0030 不新增约束名 → `_PG_CONSTRAINT_SOURCES` 不动） |
| `tests/scenario/test_0028_pg_constraints.py` | 18 | **无** |
| `tests/scenario/test_step4b_guards.py` | 4 | **无**（本步不碰 `retrieve/` 与 `needs_reindex.py`） |
| `tests/scenario/test_step3_*` / `test_effective.py` / `test_resolver_*.py` | 68+ | **无** |
| `tests/test_metrics_and_rerank.py` | 全部 | **无**（本步不打点、不加 Gauge） |

---

## E. 回滚点（顺序不可调换）

| # | 动作 | 撤回方式 | 可撤回性 | **顺序约束** |
|---|---|---|---|---|
| **E-1** | 迁移 0030（2 视图 + 1 函数） | `alembic downgrade 0029` | **无损**（无数据、无写入者） | 必须先 revert 脚本（`db_routing_report.py` 会查这两个对象）→ 否则脚本 `UndefinedTable` |
| **E-2** | `db_router.py` + settings + `config_profile` + `main.py` 两行 | `git revert` 这批文件 | **无损**（关闭态本就零行为） | **必须先确认 `db_binding` 全为 NULL**，再 revert（见下） |
| **E-3** | 盘点脚本 | `git revert` | 无损 | 独立 |
| **E-4** | `db_binding` 已被写入（Step 6 之后） | `UPDATE scenarios SET db_binding = NULL` | **无损**（数据仍在新库，只切连接） | ★ **改 NULL 先于关开关**。若先关开关：开关关 → 全部落默认库 → 而数据已搬走 → 「一半文档查不到」 |
| **E-5** | 数据已搬到新库 | 不能"撤"（数据位置的事实） | **不可撤回**，只能再搬回来 | 搬回 = 另一个 D3 流程；期间**必须**保证 `db_binding` 指向数据所在的库 |

【推导】**为什么 E-2 不提供"翻开关"这类运行期回滚**（与 Step 4B §E-4 同款论证，但这里更硬）：

- 开关的关闭态是「路由功能不存在」，它**只能**在「数据还没搬」时安全。一旦数据搬走，关开关就**等于**制造"一半文档查不到"。
- 一个"在某些状态下安全、在另一些状态下致命"的开关，是最危险的一类开关 —— 因为它**看起来**总是可用。
- 所以回滚的**正解**是两步且有序：先把事实改回（`db_binding = NULL`），再关功能（revert 代码）。**开关只用于"事故止血 + 数据未搬"这一种情形**，且必须在 SOP 里写清前提。

---

## F. 分步开发路线（每步给硬验收信号）

| 步 | 交付 | **硬验收信号（可执行）** | 回滚 |
|---|---|---|---|
| **1** | 迁移 0030（2 视图 + 1 函数 + 三条守卫） | ① `alembic heads` 输出**恰好一行** `0030 (head)`；② `alembic upgrade head → downgrade 0029 → upgrade head` 往返无错；③ `psql -c "SELECT count(*) FROM v_scenario_db_routing"` == `psql -c "SELECT count(*) FROM scenarios"`；④ `psql -c "SELECT count(*) FROM v_kb_db_binding_conflicts"` == 0；⑤ `pytest tests/scenario/test_0030_migration.py -q` 全绿 | E-1 |
| **2** | settings 六项 + 校验 + placeholders 三条 | ① `python -c "from app.config.settings import get_settings; print(get_settings().scene_db_split_enabled)"` → `False`；② 故意把 `SCENE_DB_DSN` 设成 `{"default": "..."}` → 进程**起不来**（保留字校验）；③ 启动第一屏出现 `DB 路由 :` 那一行（横幅） | E-2 |
| **3** | `db_router.py`（纯函数 + 注册表 + 闸门） | ① `pytest tests/scenario/test_step5_db_router.py -q` 全绿（T-5-1…T-5-8）；② `python -c "...register_from_settings(); print(r.created_engines)"` → `0`；③ `python -c "...engine_for('default') is app.database.engine"` → `True` | E-2 |
| **4** | `config_profile` 接线（含 `CROSS_ROLE_FIELDS` 两项） | ① `pytest tests/scenario/test_step5_guards.py -q -k T_5_18` 全绿；② 启动日志里 `config_fingerprint` **因本次改动而变**（并在交付报告里记录新旧值）；③ `runtime_config_snapshot()` 里 `scene_db_dsn_redacted` 的每个值**不含** `@` 前的账号密码（正则断言） | E-2 |
| **5** | 真库用例 T-5-9…T-5-13 | ① `pytest tests/scenario/test_step5_db_router_pg.py -q` 全绿（需 `SCENARIO_TEST_DB_URL`）；② **T-5-11 必须先看到它红**（造冲突前断言 0 行、造冲突后断言 1 行 —— 两步都跑一遍） | E-1 |
| **6** | 盘点脚本 | ① `python -m scripts.db_routing_report; echo $?` 在干净库上 → `0`；② 手工插一行 `db_binding='kb_x'` → 退出码 `1` 且输出含 `⑥` 缺口项 → 恢复后重新 `0` | E-3 |
| **7** | 文档与注释收口 | ① 框架稿 §3A.8 D1/D2 的表述按 §附录 A 更新（2 处修正）；② 启动横幅与 §B.4.3 的文字逐字一致 | —— |

【推导】**步序为什么是这个**：1 → 2 是"先有可盘点的对象，再谈路由参数"；3 必须在 2 之后（它读 settings）；**4 必须紧跟 3**（否则开关进了运行期却不在指纹里，正是 4B §E-4 反对的形态）；5/6 是"证明它有牙"（真库 + 退出码）；7 最后。**1 和 3 之间不存在依赖反转**，可以并行 —— 但不要合并提交（回滚点不同）。

---

## G. 魔鬼代言人：反例（每条指明成立条件）

| # | 反例 | 判定 |
|---|---|---|
| **R-1** | **Step 5 交付的东西在 Step 6 之前无法用行为验收**：零行为变化 = 没有一条用户可见的验收信号。所有"验收"都是结构断言（对象恒等 / 哈希 / AST），而结构断言只能证明"我没改"，不能证明"将来能用" | **成立，且是本步最大的诚实点**。缓解（三条，缺一不可）：① 把 Step 6 的三个**不可逆前置件**固化成可断言对象（键的表示法、acquire point 的形状、预算闸门）；② 承认本步**零用户可见收益**（§D 的 C-5 峰值检出都要靠压测夹具）；③ 用 Step 6 的"首个场景迁出能按图施工"作为唯一真实的验收。**若你要求"现在就看到效果"，本步无法满足 —— 请裁掉它，直接做 Step 6**（代价：没有目录与预算闸门就搬库 = 框架 §3A.8 警告的"谁是哪个库的口口相传"） |
| **R-2** | **`db_binding` 没有 API 写路径 + resolver 行缓存 60s 进程内 = Step 6 第一个翻车点**：运维用 SQL 改完 `db_binding`，`invalidate()` 不会被调用；单副本要等 60s，**多副本各自等 60s**。窗口内同一场景被路由到两个库 → 「一半文档查不到」且**不报错** | **成立**。缓解：① Step 6 的 SOP 强制"改完 `db_binding` 必须重启 api 或停流量"（§C 的操作顺序 ⑤）；② `db_routing_report` 的第 ⑥ 项在部署前抓"键缺口"；③ **正解**（Redis 失效频道）登记为 U-5-1，不在本步做（框架稿自己把多副本 Redis 化列为 §G-2 的遗留）。**若不接受"重启"这个动作，就必须先做 Redis 失效通道 —— 那是另一件事的工作量** |
| **R-3** | **本步的成本立刻发生、收益要等 Step 6**：分桶的收益（故障域隔离）在 `K=1` 时**恒为 0**；而"预算从此变成乘法"的成本立刻存在 | **成立，接受**。这是"opt-in 纪律"的必然形态（框架 §7 共同纪律：每步要么零影响、要么新能力默认关）。可度量的唯一即时收益：**「连接数会不会撞穿 PG」从"文档里的公式"变成"拒绝启动的断言"** —— 这条在 Step 5 就有价值，因为它把 Step 6 的一个不可测风险变成可测 |
| **R-4** | **`v_scenario_db_routing` 是第二真源的最佳入侵点**：它重复了 `resolver.LOOKUP_SQL` 的谓词（`state='published'` + 双端 `tenant_id`）。有人改 resolver 忘了改视图 → 运维在 psql 看到的路由与实际路由不同 | **成立**。缓解：T-5-12 逐行比对视图的 `published_version` 与 `resolve_scenario()` 返回的 `snapshot.version`（同一租户、全部场景）→ 任一谓词漂移必红。**残余**：视图还多了 `db_key_source` 这个 resolver 没有的派生列，它**没有**对照物 → 只能靠 T-5-10 的 `db_key == COALESCE(...)` 断言（可接受） |
| **R-5** | **预算闸门依赖一个假设值**：`db_max_connections_for_budget=100` 是 PG 默认值，本项目未覆盖。若生产是 200，闸门偏松（不报警）；若 50，闸门偏紧（拒绝启动而系统其实能用） | **部分成立**。缓解：§B.4.2 启动期 `SHOW max_connections` 实测 + **取较小者** + 不一致 WARN。**残余**：`SHOW` 需要权限；失败时退回配置值（U-5-4） |
| **R-6** | **C-3（`get_db` 源码哈希）会阻断正当改动**：将来任何人给 `get_db` 加只读标记、加重试、加 `pool_pre_ping` 外的参数，都必须先改哈希 | **成立，且是设计意图**（同 4B 的 R-5）。正确变更流程写进哈希旁边：改 `get_db` → 跑哈希 → 在 commit message 引用本文件 §A.3.3 → 说明"连接获取路径变更点"与影响面。**若没有这道门，"关闭态等价"就退化成一句承诺** |
| **R-7** | **本步零 LLM 调用、零用户可见变化，却新增 1 迁移 + 1 模块 + 5 配置 + 6 守卫 + 1 脚本**：复杂度真实、收益延迟 | **成立**。最小可交付裁剪（我认可）：只做 **`db_router` 的纯函数 + 预算闸门 + `v_scenario_db_routing`**（把 settings 五项压到 3 项：开关 + DSN + 预算上限；砍掉盘点脚本与冲突检测）。**不能砍的是**：`resolve_db_key` 的纯函数性、预算闸门、C-1/C-3 两条守卫 —— 砍掉任意一个，"关闭态等价"或"禁止猜库"就没有守卫了 |
| **R-8** | **`'default'` 这个虚拟键会漏进存储**：视图、脚本、前端都展示 `'default'`，迟早有人把它写回 `scenarios.db_binding`；而 DB 会拒绝 → 报错是"约束冲突"，不是"你写错了值" | **成立**。缓解：① T-5-9 用**该约束名**断言（把错误信息变成用例的一部分）；② 0030 的 `DO` 块断言 CHECK 仍含 `kb_`（§B.1.4 第 3 条）；③ 视图里用 `db_key_source` 让"这是虚拟的"显式可见（而不是靠列名的注释） |

---

## H. 生产级必答项（只答与本步相关的；其余标 N/A + 理由）

| # | 必答项 | 结论 |
|---|---|---|
| **1** | **执行环境 / 权限**：新增对象在哪跑、谁能读？**凭证是否进执行环境**？**写操作影响半径**？ | ① 对象在**控制库**（`kagent`），两个视图 + 一个函数均 `SECURITY INVOKER`（不提升权限，跟随调用方）；函数 `STABLE` + 只读 → **不可能被用来写**。② **凭证是否进执行环境：是，但必须**—— `SCENE_DB_DSN` 的值**必须**进 api/worker 进程环境（否则连不上库），这是不可避免的。硬约束是它**不进**另外三个地方：**不进场景配置表**（框架 §3A.2.2 纪律 3 / §3A.9 §5.5）、**不进日志/指纹明文**（`_redact_url` 逐值脱敏，§B.6）、**不进响应体**（脚本只输出键名与 host:port）。③ **写操作影响半径：Step 5 = 0**（零写入者：无触发器、无列、无写路径，`db_binding` 今天只能被运维 SQL 写）。Step 6 起，**运维脚本**的影响半径从 1 个库变成 N 个库 → 脚本必须显式 `--db-key`（框架 §3A.6.2 判定点 8 / §13 第 1 项）。 |
| **2** | **错误回喂**：失败如何回到调用方，如何分类？ | 三分类（§B.5）：① **代码/部署错** → 启动期 `RuntimeError` **拒绝启动**（T1 纪律：`main.py:120-125`）——预算超限、DSN 非法、键格式非法；② **数据错** → WARN + metric，**永不 raise**（T2 纪律）—— 关闭态下 `db_binding` 非 NULL（复用 S-7）；③ **运行期路由错** → `DB_ROUTE_KEY_UNKNOWN` **500 fail-closed**，**禁止**返回空结果或回落默认库（I-5-4）。**禁止**把"查不到库位"变成 200+空 citations：那会让配置错误伪装成"知识库没内容"，而后者在本系统里是**合法业务态**（`retrieve` 回 0 行 → 拒答），两者在观测上不可区分。 |
| **3** | **幂等** | 全部对象**天然幂等**：两个视图 + 一个函数是只读；`resolve_db_key` 是纯函数；`register_from_settings` 重复调用是"重建同一张注册表"（Step 5 关闭态无副作用，因为它不建引擎）；`check_connection_budget` 是纯算术；盘点脚本只读。**唯一的非幂等动作是 Step 6 的搬数据** → 不在本步。 |
| **4** | **证据版本绑定** | 四层绑定里，Step 5 属于**第 0 层**（「数据住哪」），不进 §4 的四层（向量 / 分块 / 抽取 / wiki）。**评估结论的版本绑定 N/A**：本步不触碰 `eval_runs` / `caliber` / `messages.caliber`。**但**它给未来的评估引入一个新的可比性边界：**跨库的两次评测不可直接比较"库性能"**（不同的 `shared_buffers` / 数据量 / 索引状态）→ 登记为 U-5-9。 |
| **5** | **成本 / 缓存 / 体感（经济性三视角）** | **成本**：模型成本 **= 0**（本步零 LLM/embedding/rerank 调用 → **模型组合与任务-模型分派表 N/A**）。真实成本是**连接预算**（§A.3.4.1 全表代入）：Step 5 `50/90`，与今天**完全相同**；Step 6 推荐配置仍 `50/90`（把 40 摊成 2×20）。**缓存**：不新增、不失效任何既有缓存；但**必须指出一个新耦合** —— resolver 的**进程内 60s 行缓存**是库位键的事实来源，所以「改 `db_binding` → 生效」有一个 **60s × 副本数** 的不可控窗口（F-12 / R-2 / U-5-1）。**体感**：Step 5 **零影响**（不进请求路径）；Step 6 起每桶容量下降 → 每库在途上限 ~30 → ~20（池 20 的实测崩点），**必须重跑阶梯压测**（框架 §3A.4 注 1：换池必须重测）。 |
| **6** | **可观测性的"谁看"** | 三处按**读者**分工：① **启动横幅一行** → 部署者（改 `.env` 后必然看第一屏；也是"当时开关是开还是关"的唯一留痕）；② **盘点脚本退出码** → CI/cron（唯一能"自动发现"的通道）；③ **复用 S-7 的既有 WARN** → 运维在启动日志里看到"db_binding 配了不生效"。**没有一处依赖"有人主动盯着"**。**没有新增 metrics 打点**（本步不产生运行期指标；且 4B 已裁决不加 Gauge，避免动 `tests/test_metrics_and_rerank.py:80` 的键集断言）。 |
| **7** | **迁移 / 锁** | §A.3.6：**不加列** → 无 fast default、无 `SET NOT NULL`、无回填；`CREATE VIEW` 只取基表 `ACCESS SHARE` → **不阻塞基表读写**；`downgrade` 纯 DDL **无数据可丢**。若将来必须加列：fast default 是 **O(1)** 但**仍取 ACCESS EXCLUSIVE**，且本项目的事务边界是"一个 HTTP 请求（含整条 SSE 流）"→ **锁等待可长达整条流的时长**，并触发**锁队列头阻塞**；因此必须 `lock_timeout` + 无流量窗口 + 分三步（§A.3.6.3）。 |
| **8** | **回滚** | §E：**有序的两步**（先改事实 `db_binding=NULL`，再关功能）。开关**不能**单独当回滚用（数据已搬时关开关 = 制造"一半文档查不到"）。 |
| **9** | **多 Agent 编排 / 意图识别 / 检索链拓扑** | **N/A**：本步不改变任何编排、不引入任何新阶段、不触碰 `retrieve/`（验收④ `git diff` 为空）。平台侧无自主循环（框架 `:1235`）。 |
| **10** | **打转检测（同一操作的重复循环）** | **N/A**：本步没有任何"操作循环"。路由是**一次纯函数调用**（`resolve_db_key`），不重试、不回退、不迭代；Step 6 起 `data_session` 也是"取一次连接、用完即还"。唯一的重复是连接池的复用，而 `pool_pre_ping=True`（`database.py:24`）已覆盖"拿到的是死连接"这一类。 |
| **11** | **记忆治理（长期记忆的写入/淘汰/污染）** | **N/A**：本步不读不写任何记忆结构（`messages` / `conversations` / 会话上下文）。库位键**不是记忆**：它是部署事实，随场景行而非随对话变化。 |
| **12** | **评估者分离（生成者与判定者不是同一个模型/同一次调用）** | **N/A**：本步零模型调用，不存在"生成者/判定者"。本步的判定只有两类：**纯算术**（预算闸门）与**字符串比较**（`resolve_db_key`），两者都无判定偏差可言。 |
| **13** | **前缀稳定（prompt 前缀字节稳定以命中缓存）** | **N/A**：本步不构造任何 prompt。**但有一条同构的风险值得登记**：库位键会影响**数据内容**（不同库里是不同 chunk 集），所以它天然改变"事实前缀"——如果有任何一层把检索结果拼进 prompt 缓存，改 `db_binding` 必须视为**缓存全失效**。本步不引入缓存，故只登记（见 U-5-1 的 60s 窗口同源）。 |

---

## I. 遗留问题与未知区

| # | 项 | 为什么现在定不了 | 影响 | 建议 |
|---|---|---|---|---|
| **U-5-1** | 多副本下的库位键失效（R-2 的根因） | resolver 行缓存是进程内 dict（I-5-0m），且 `db_binding` 没有 API 写路径 → 改它不触发 `invalidate()` | 60s×副本数 窗口内路由分叉 | Step 6 SOP 强制"改完重启"；正解是 Redis 失效频道（框架 §G-2 的遗留） |
| **U-5-2** | worker 侧路由 | 本步只预置指纹可见性（I-5-7），不接线 | api 开、worker 关 → 写读分叉，**新文档永远检不到** | Step 6 必须同步改 worker 解析写入；`db_routing_report` ⑥ 只能发现"键缺口"，发现不了"角色没接线" |
| **U-5-3** | `db_binding` 要不要补 API 写路径 + 审计 | 补了就把部署动作变成运行期可变（与框架 §7.2 Step 7 冲突）；不补则"谁搬到哪个库"不可查 | 审计缺口 | 推荐不补；若要，给一条**部署审计**（同 4B 的 U-4B-6） |
| **U-5-4** | `max_connections` 真值与 `SHOW` 权限 | 未实测；`SHOW` 可能无权限 | 闸门偏松/偏紧 | §B.4.2 实测 + 取较小者 + WARN |
| **U-5-5** | 跨库审计落点 | `audit.record()` 与业务写同 session；Step 6 拆 session 后审计必须留控制库 | 一次请求两库写；若搬错则跨场景审计查不了 | Step 6 必须拆；本步登记 |
| **U-5-6** | kb 多场景冲突的**处置** | 检测器有了，"怎么修"没定（改 `kb_ids`？拆 kb？拒绝发布？） | 检测到冲突后无标准动作 | Step 6 裁决；本步只保证"冲突可见" |
| **U-5-7** | 每桶池容量的真实标定 | 需阶梯压测（框架 §3A.4 注 1 的方法）；本步的 20 是**外推** | DQ5-6 的推荐值可能偏乐观 | 已登记站位值（§B.2.3）；Step 6 前必须重测 |
| **U-5-8** | 「这条连接连的是哪个库」如何观测 | 需读 `pg_stat_activity.datname`；4B 已裁决**不加 Gauge** | Step 6 排障无法快速回答"连错库了吗" | Step 6 用脚本查（不进 metrics） |
| **U-5-9** | 跨库的评测可比性 | 不同 database 的 `shared_buffers` / 数据量 / 索引状态不同 → "库性能"不可直接比较 | 跨场景 A/B 的结论会混入库差异 | 评测报告必须标注"该场景在哪个库位"；Step 7 的容量看板需按库位分组 |
| **U-5-10** | `v_kb_db_binding_conflicts` 在 `kb_ids` 含非 UUID 脏数据时的行为 | `jsonb_array_elements_text` 会原样返回字符串（不校验 UUID）→ 视图不 500，但 `kb_id` 列可能不是 UUID | 盘点里出现"不是 kb 的 kb_id" | 这是**故意的**（盘点不该因为脏数据 500）；若要严格，另加一条 CHECK/告警 |

---

## 附录 A：本文件与框架稿 §3A.8 / §7 的偏移清单

| # | 位置 | 前文怎么说 | 本文件怎么做 | 性质与理由 |
|---|---|---|---|---|
| **A-1** | 框架 §3A.8 D1（`:505`）、§7 Step 5（`:800`） | 「`knowledge_bases` 增『场景 + 库位逻辑键』两列」 | **一列都不加**；权威列用 0028 已建的 `scenarios.db_binding` | **实质修正**。理由见 §A.3.1.2（库位是场景属性 + §3A.9 原文「不单独配置」） |
| **A-2** | 框架 §3A.8 D1 验收信号①（`:505`） | 「现有单库下所有行库位 = `default`」 | **字面不可落地**（CHECK `^kb_`）；改为「`db_binding IS NOT NULL` 行数 = 0」+ 视图虚拟键 `'default'` | **必须修正**（否则这条验收永远悬空）。理由见 §A.3.1.4 |
| **A-3** | 框架 §3A.8 D1（`:505`） | 「`kb_catalog` 视图」 | Step 5 **不建**；改 `v_scenario_db_routing` + `fn_kb_db_binding_conflicts()`；`kb_catalog` 推到 Step 6 | **范围修正**。理由见 §A.3.5.2（口径未定 / 不可证伪 / 不是路由输入） |
| **A-4** | 框架 §3A.8 D2（`:507`） | 「`get_db` 支持按库位取连接」 | `get_db` **一行不改**（C-3 金标准）；新增 `data_session()` 上下文管理器，Step 5 零调用点 | **实质修正**。改 `get_db` 会同时破坏"关闭态等价"与"每在途 1 条"两个前提（§A.3.2.2） |
| **A-5** | 框架 §3A.8 D2 验收信号①（`:507`） | 「关闭态下连接池数量与现状相同（断言 `pool` 实例数不变）」 | 落成 **6 条**可执行断言（C-1…C-5 + C-4b），其中核心是**对象恒等**与**源码金标准哈希** | 细化。"断言池实例数"单独一条不足以抓"建了第二个池但参数一样"（§A.3.3） |
| **A-6** | 框架 §3A.8 D2（`:507`） | 「池按库位分桶」 | 桶数**启动期封闭**、`K=1`；超限**拒绝启动**、**不做 LRU**、**不做运行时建池** | 细化 + 一处裁决。理由见 §A.3.4.4 |
| **A-7** | 框架 §3A.4 算式（`:330-360`） | `40 × N × K + 10 ≤ 100 − 预留` | 落成 `check_connection_budget()`，并把 `N`（副本数）显式化为 settings（应用**不知道自己有几个副本**） | 落地细化。原文的 `N` 是一个"读者心里的数"，代码里必须有一个来源（§A.3.4.3） |
| **A-8** | 框架 §3A.9（`:520-531`） | 「库位不新增 MCP 入参；`凭证 → 场景 → 库位` 全在服务端」 | 落成 I-5-5（**零新增查询**：键已在 `ScenarioSnapshot.db_binding`）+ I-5-4（无入参在结构上不可能） | 落地细化。原文只说了"不加参数"，没说"键从哪来的零成本路径"（F-10） |
| **A-9** | 框架 §7.2 Step 5（`:834`） | 「禁止让路由层猜库（解析不到就报错）」 | 落成 `engine_for` 对未注册键 **500 raise**，且 `resolve_db_key` 是纯函数**不校验注册表**（解析与可用性分离） | 细化。"解析不到"有两种（键未注册 / 键为空），前者 raise、后者是**合法默认态**（NULL），必须分开（§A.3.2.2 表末行） |
| **A-10** | 框架 §7.3（`:846-856`） | 验收改用**失败集合** | 本文件 §0.3 全盘照用，**不再出现"N passed"这种绝对数** | 一致（无偏移，登记在此以免下一轮退回旧口径） |
| **A-11** | 主理人给定：`SCENE_DB_SPLIT_ENABLED` 默认 `False` | 「关闭时连接获取路径逐字节等价」 | **接受**，但补两条实现约束：① 只**启动期读一次**；② 必须进 `CROSS_ROLE_FIELDS`（I-5-7） | 加严。理由见 DQ5-7（运行期可变且不在横幅里的开关 = 4B §E-4 反对的形态） |
| **A-12** | 主理人给定：`kb_catalog` 要查重 | 「先查现有库里是否已存在同名视图/表」 | **已查：0 命中**（`grep -rn "kb_catalog" code/`，I-5-0n） | 一致（事实已核实） |

---

## 附录 B：源核实 / 设计推导 分栏

| 类别 | 条目 | 出处 |
|---|---|---|
| **【源】读到的** | `engine` 单例 + 池参数全来自 settings | `app/database.py:22-31`、`app/config/settings.py:58-62` |
| 【源】 | `get_db` 两行函数体；`get_current_user` 依赖它；`get_integration_principal` 再依赖两者 | `app/api/deps.py:48-50`、`:64-65`；`app/api/integrations.py:77-90` |
| 【源】 | 96 处 `Depends(get_db)` | 本次实测（`grep -c`） |
| 【源】 | `scenarios.db_binding` 已存在 + 格式 CHECK `^kb_[a-z0-9_]{2,40}$` | `0028_add_scenarios.py:120`、`:137-138`；`entities.py:559`、`:575-578` |
| 【源】 | `knowledge_bases` 无任何场景/库位列 | `entities.py:129-146` |
| 【源】 | 种子行 `db_binding = NULL`，每租户 1 行 | `0028_add_scenarios.py:290-301` |
| 【源】 | `s.db_binding` 已在 `LOOKUP_SQL`；`ScenarioSnapshot.db_binding` 已存在 | `resolver.py:78`、`:143`；`snapshot.py:152`、`:253`、`:356` |
| 【源】 | `db_binding` 无 API 写路径（只在 `ScenarioOut` 读模型） | `app/schemas/scenario.py:63`；`app/api/scenarios.py:82` |
| 【源】 | S-7 自检 WARN「配了不生效」+ metric 名 | `self_check.py:50`、`:252`、`:521-539` |
| 【源】 | `WIRED_RETRIEVAL_STEP=3`；三个步骤号不得合并 | `effective.py:48-61` |
| 【源】 | resolver 行缓存 60s、进程内 dict、`invalidate` 仅当前进程 | `resolver.py:40`、`:69`、`:111-118` |
| 【源】 | `kb_catalog` 全仓 0 命中 | 本次 `grep` |
| 【源】 | 迁移头 = 0029；`prepare_schema()` 基线 = `head` | `0029_...py:38`；`tests/scenario/pg_helpers.py:179-215` |
| 【源】 | `test_db_pool.py` 4 条池断言 | `tests/config/test_db_pool.py:25-59` |
| 【源】 | `scenario_versions.kb_ids` 是 JSONB 数组；published 部分唯一 | `0028_add_scenarios.py:166`、`:218-224`、`:181-271` |
| 【源】 | 容量算式与 `预留=10` 是设定值；`30 并发` 是占位值 | 框架稿 `:307-426` |
| **【推导】得出的** | 「同一 session 身兼控制面与数据面」及其连接账目 | §A.3.2.1 / §A.3.2.2 |
| 【推导】 | 「每在途长期占 1 条」的机制是"只读事务整条流不结束" | §A.3.2.4（依据 `database.py:11-14` 的实测注释） |
| 【推导】 | `K_max = floor(80 / (P × N))` 及其四组代入 | §A.3.4.1 |
| 【推导】 | 「每桶参数」而非「全局预算分摊」（否则 `test_db_pool.py:29` 必红） | §A.3.4.2 |
| 【推导】 | 不做 LRU 的四条理由与三问 | §A.3.4.4 |
| 【推导】 | `NULL` 表示默认库的四条理由（含 CHECK 冲突这一硬事实） | §A.3.1.4 |
| 【推导】 | 「kb → 库位」必须是函数，冲突只能检测不能约束 | §A.3.1.2 / §A.3.1.3 |
| 【推导】 | 加列的真实代价是「锁等待 = 整条 SSE 流时长」，不是表大小 | §A.3.6.2 |
| 【推导】 | `db_binding` 无写路径 + 进程内缓存 = Step 6 第一个翻车点 | §G-R2 / U-5-1 |

---

## 附录 C：待裁决项汇总（推荐值一览）

| 编号 | 待裁决 | **推荐** | 一句话后果（不裁决时） |
|---|---|---|---|
| **DQ5-1** | 库位权威位置 | `scenarios.db_binding` 单点 + KB 一列不加 | 造出两个可写真相 → 分叉且不报错 |
| **DQ5-2** | 默认库位的表示 | `NULL` + 视图虚拟键 `'default'` | D1 验收信号①永远悬空（`'default'` 过不了 CHECK） |
| **DQ5-3** | 路由输入通道 | 显式 acquire point，键取自 `ScenarioSnapshot.db_binding` | 拆 DI → 并发 ~30→~20；contextvar → 静默查错库 |
| **DQ5-4** | `kb_catalog` | Step 5 不建，改 `v_scenario_db_routing` + 冲突检测 | 交付一个不可证伪的视图，且版本口径必改 |
| **DQ5-5** | 池超限行为 | 启动期拒绝启动 + 未注册键 500；**不做 LRU** | 运行期池不可枚举 + 用户答案可能被截断 |
| **DQ5-6** | Step 6 每桶池容量 | `K=2` 时每桶 20（总连接仍 50） | 每桶 40 → 90/90 零余量；不调池则并发直接减半 |
| **DQ5-7** | 开关语义 | 关闭态 = 路由功能不存在；只启动期读一次；进 `CROSS_ROLE_FIELDS` | 「开关是开是关」不可回答；api/worker 不一致无人发现 |
| **DQ5-8** | 是否加 admin 端点 | 不加，用盘点脚本退出码 | 要动 `_EXPECTED_ROUTES`（加严但破坏"路由表零改动"证据） |
