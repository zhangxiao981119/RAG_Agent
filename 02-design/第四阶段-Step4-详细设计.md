# 第四阶段 Step 4：场景管理写接口 —— 详细设计

> **设计台账**：2026-11-20 · 统一RAG平台+场景化配置 · Step 4 · 6 个写接口 + 3 个读接口 · 真库验收 34 条用例
> **前置**：Step 1–2（schema + resolver + 六行真值表）、Step 3（policy 接线 + 自检 + T4 函数）已实现并独立验收。本文件**只设计、不改代码**。
> **本文件的两类内容严格分栏**：标【源】的逐条给 `file:line`（读到的）；标【推导】的是我基于源码得出的结论或取舍（推出来的）。二者混在一句话里的，拆成两句。

---

## 0. 边界与验收基数

### 0.1 输入：不可改动的既成事实

【源】以下八项是 Step 4 的输入，本设计不重新论证、不改动：

| # | 事实 | 出处 |
|---|---|---|
| I-1 | `scenarios` 六条 CHECK + `uq_scenarios_tenant_key` + 部分唯一索引 `uq_scenarios_one_builtin`（每租户至多一个 `is_builtin`） | `alembic/versions/0028_add_scenarios.py:131-149` |
| I-2 | `scenario_versions` 十四条 CHECK（含 `ck_..._policy_keyset`、`ck_..._binding_consistent`、`ck_..._state_timestamps`、`ck_..._checksum_format`）+ `uq_scenario_versions_scenario_version` + 部分唯一索引 `uq_scenario_versions_one_published` | 同上 `:181-271` |
| I-3 | 状态闭集 `draft / validated / published / archived`，`change_note` NOT NULL 默认 `''` | 同上 `:171`、`:181-184` |
| I-4 | `scenario_versions.scenario_id` → `scenarios.id` **ON DELETE CASCADE**；`integration_applications.scenario_id` → `scenarios.id` **ON DELETE RESTRICT** | 同上 `:180`、`:279-286` |
| I-5 | 每租户一行内置场景 `scenario_key='default'`、`is_builtin=true`、`state='published'`、`policy='{}'`、`binding_mode='unrestricted'` | 同上 `:294-322` |
| I-6 | 参数规格表 16 项是唯一真源；写入闭集 `WIRED_KEYS` = 4 键 = 迁移的 `_POLICY_KEYS`；`validate_policy()` 是唯一写入校验器 | `policy_spec.py:98-283`、`:287-289`、`:331-383`；迁移 `:64-69` |
| I-7 | `check_policy_publishable()` 已实现但**无调用方**，返回归一化 dict，不合规抛 `PolicyValidationError` | `self_check.py:634-660`；无调用方这一点由其模块 docstring `:639-640` 自述 |
| I-8 | `BUILTIN_SCENARIO_KEY = "default"` 且 docstring 明写「应用层 MUST 拒绝其它场景改用这个 key」 | `snapshot.py:35-36` |
| I-9 | `checksum = sha256(canonical{p-binding_mode,dna_declared,kb_ids,policy})[:16]` —— **只含内容四个字段，不含 `version` / `state`** | `snapshot.py:291-310`；迁移 `:72-96` |
| I-10 | 读路径唯一 SQL 带 `v.state='published'` 与双租户过滤；`invalidate()` docstring 要求**事务提交后**调用 | `resolver.py:71-93`、`:111-117` |
| I-11 | 三个步骤号并存：`CURRENT_STEP=2`、`POLICY_CLOSURE_STEP=3`、`WIRED_RETRIEVAL_STEP=3` | `policy_spec.py:24`、`:27`；`effective.py:47` |
| I-12 | 代码注释已把「场景写接口」归给 Step 4 | `policy_spec.py:36`、`self_check.py:11`、`resolver.py:43`、`snapshot.py:432` |

### 0.2 本步做 / 不做

**做（6 写 + 3 读 + 1 绑定）**：创建场景、创建 draft 版本、整体替换 draft 内容、校验（→validated）、发布（→published，接 T4）、归档（→archived）、绑定/解绑应用；以及为 `If-Match` 提供 checksum 的三个读接口。

**不做（逐条给理由，不许"顺手"做）**：

| 不做的事 | 理由 | 归到哪里 |
|---|---|---|
| 扩写入闭集到 Step 4 的 5 个键（`final_top_n` / `ilike_max_terms` / `alias_expansion_enabled` / `table_recall_enabled` / `section_recall_enabled`） | 【源】这 5 键 `wired_in_step=4`，但【源】`WIRED_RETRIEVAL_STEP=3` —— 检索链路**还没读它们**。【推导】此时扩闭集 = 允许写入一个改了行为不变的键 = 正是 S-1/I-? 要根除的静默 no-op（`policy_spec.py:10-11` 的自述纪律）。扩闭集必须与「检索接线」同一批交付 | Step 5/6 或单列一次「闭集扩到 N」的迁移 |
| 场景停用 / 启用（`scenarios.status`） | 【推导】`build_snapshot` 的 R3 已消费 `status != 'active'`（`snapshot.py:465-475`），但本步没有写它的入口。加了它等于同时引入「停用后已绑定应用全部 400」的新失败面，需要独立验收 | Step 7 治理面 |
| 删除场景 / 删除版本 | 【推导】`scenario_versions` 是 CASCADE，应用引用是 RESTRICT；删除的语义（审计留存？）本步定不下来 | 未知区 U4-6 |
| 场景改名 / 改描述 | 【推导】改名会动 `uq_scenarios_tenant_key` 的稳定标识语义（凭证与运维文书引用 key），本步不引入 | 未知区 U4-7 |
| 多 DNA / `needs_reindex` / 检索期 DNA 过滤 | 【源】框架稿 §7 把这三件写作 **Step 4** 的交付物（`统一RAG平台-场景化配置-框架设计-v1.md:799`）。【推导】**这是本文件与框架稿最大的一处不一致，见 §附录 A 偏移 1**：本文件把 Step 4 定义为「写接口 + T4 接线」，即框架稿 Step 4 的**载体与前置**；DNA 部分不在本文件 | 建议拆为 Step 4A（本文件）/ Step 4B（DNA），见 DQ4-1 |

### 0.3 验收基数（不许放宽）

【源】既有基线：`tests/scenario/` 下 **189 个 `def test_`**，按团队口径 **211 条 collected**（含 parametrize 展开；本机无 pytest 无法就地复算，故两个数都写出来，验收时以 `--collect-only` 的实际数为准）。其中 `test_0028_pg_constraints.py` 18 条是已验收的真库约束证据。

【源】既有 `tests/test_integrations.py` 12 条（集成侧认证/配额/审计/会话隔离）。

【推导】Step 4 的硬约束：

1. **上表 201 条（189+12）一条都不许改语义**。唯一允许的两处**事实变更**见 §C.7（`CURRENT_STEP` 的两条断言），且必须在新文件里逐字给出 before/after 与理由，不许"顺手放宽"。
2. 新增用例**不得复用被验证对象自身的常量做期望值**（反自证），具体判据见 §D.7。

### 0.4 施工文件清单

| 动作 | 文件 | 内容 |
|---|---|---|
| 新增 | `app/services/scenario/write_errors.py` | 写侧错误码（与 `errors.py` 的三码**不合并**，见 §A.4 末） |
| 新增 | `app/services/scenario/write_service.py` | 六个写用例 + 三个读用例的**事务实现**（唯一 impure 写入口） |
| 新增 | `app/services/scenario/dna_declare.py` | `build_dna_declared()` —— 从平台三个 profile 生成声明镜像（唯一生成处） |
| 新增 | `app/services/scenario/step4_check.py` | T4 的**发布期第二道**：`ensure_publishable(...)`（包装 `check_policy_publishable` + 绑定/内置交叉校验），保持 `self_check.py` 一行不改 |
| 新增 | `app/schemas/scenario.py` | 请求/响应模型（pydantic v2，`extra="forbid"`） |
| 新增 | `app/api/scenarios.py` | 9 个路由（3 读 + 6 写） |
| 修改 | `app/api/integrations.py` | 追加 `PUT /applications/{id}/scenario`；`_application_out` 增 `scenario_id`（**1 行**） |
| 修改 | `app/schemas/integration.py` | `IntegrationApplicationOut` 追加 `scenario_id: uuid.UUID \| None = None` |
| 修改 | `app/main.py` | 追加 `include_router(scenarios_router, prefix="/api")`（`main.py:403-427` 那一段） |
| 修改 | `app/services/scenario/policy_spec.py` | `CURRENT_STEP: 2 → 4`（§C.7，**唯一允许的常量改动**） |
| 修改 | `tests/scenario/test_effective.py:233`、`test_resolver_policy.py:49-51` | 两处事实变更 + 一条断言替换（§C.7） |
| 新增 | `tests/scenario/pg_helpers.py` | 真库夹具工具（从 `test_0028_pg_constraints.py:42-237` 提取，§D.1） |
| 修改 | `tests/scenario/test_0028_pg_constraints.py` | 改为 `from tests.scenario.pg_helpers import ...`（只删定义、加 import，18 条断言逐字不动） |
| 新增 | `tests/scenario/test_step4_api_pg.py` 等 4 个 | §D 的 34 条用例 |

### 0.5 术语

| 词 | 本文件里的确切含义 |
|---|---|
| **内容** | 版本行的四个字段 `policy / kb_ids / binding_mode / dna_declared` —— 即 `checksum` 的输入集【源 `snapshot.py:291-310`】 |
| **`If-Match` 值** | 客户端持有的 `checksum`（16 位小写 hex）。语义见 §C.2，**不是** HTTP 资源版本 |
| **锁定行** | 每个写用例第一步 `SELECT ... FROM scenarios WHERE id=:sid AND tenant_id=:tid FOR UPDATE`【推导：把同一场景的所有写串行化】 |
| **发布** | 把一行 `validated` 变成该场景唯一的 `published`，并自动把原 `published` 归档（§C.3） |
| **`meta`** | 集成接口响应里的场景信息四键【源 `integrations.py:227-239`】 |

---

## A. 写接口清单与契约

### A.1 路由总表

| # | 方法 + 路径 | 作用 | 成功码 | 幂等 | `If-Match` |
|---|---|---|---|---|---|
| R1 | `GET /api/scenarios` | 列场景（含当前 published 版本与 checksum） | 200 | 是 | — |
| R2 | `GET /api/scenarios/{scenario_id}` | 场景详情 + 版本摘要 | 200 | 是 | — |
| R3 | `GET /api/scenarios/{scenario_id}/versions/{version}` | 单版本详情；响应带 `ETag` | 200 | 是 | — |
| W1 | `POST /api/scenarios` | 创建场景（`is_builtin=false`） | 201 | **否**（重放 → 409） | — |
| W2 | `POST /api/scenarios/{scenario_id}/versions` | 创建 draft 版本（version 自增） | 201 | **否** | — |
| W3 | `PUT /api/scenarios/{scenario_id}/versions/{version}` | **整体替换**内容（仅 draft） | 200 | 是（同 `If-Match` 重放 → 409，见 §C.2.4） | 必须 |
| W4 | `POST /api/scenarios/{scenario_id}/versions/{version}/validate` | `draft → validated` | 200 | 是（已 validated → 200 `changed=false`） | 必须 |
| W5 | `POST /api/scenarios/{scenario_id}/versions/{version}/publish` | `validated → published`（接 T4） | 200 | 是（已 published → 200 `changed=false`） | 必须 |
| W6 | `POST /api/scenarios/{scenario_id}/versions/{version}/archive` | `published → archived` | 200 | 是（已 archived → 200 `changed=false`） | 必须 |
| W7 | `PUT /api/integrations/applications/{application_id}/scenario` | 绑定 / 解绑（`null` = 回内置） | 200 | 是（set 语义） | — |

**为什么必须有 R1–R3**：【推导】`If-Match` 的输入是 `checksum`，而 `checksum` 只存在于库里。没有读接口，W3–W6 在契约上不可用（客户端拿不到前置条件）。这不是"顺手加的 CRUD"，是并发机制的组成部分。

### A.2 通用约定

**A.2.1 认证与授权**：【源】全部走 `Depends(require_admin)`（`deps.py:142-152`，`clearance < 40` → 403 `{"detail":"FORBIDDEN"}`）。【推导】「场景改配置」影响该租户**所有**集成应用的回答口径，属管理员动作，不给普通用户。

**A.2.2 租户边界**：【推导】每个 handler 的第一条 DB 语句 MUST 同时带 `scenarios.tenant_id = :tenant_id`（取自 `CurrentUser`，**绝不**取请求体）。跨租户的 `scenario_id` 一律归到 **404**，不返回 403 ——【源】口径照 `integrations.py:181-183`（`application is None or tenant_id != user.tenant_id → 404 "应用不存在"`）。【推导】403/404 的差异本身就是存在性探针。

**A.2.3 错误体**：【推导】统一
```json
{"detail": {"code": "SCENARIO_STATE_CONFLICT", "message": "…", "field": "state", "context": {"state": "draft", "expected": "validated"}}}
```
`detail.code` 是**稳定机器键**（客户端按它分支），`detail.message` 是给人看的中文，`context` 只放**本次请求可推导**的信息（不回显库里的其它行、不回显其它键的值）。
【源】这不会与既有全局处理器冲突：`main.py:362-371` 只拦 `ValueError`，`:374-385` 只拦未捕获异常，`HTTPException` 原样透出（FastAPI 默认把 `detail` 原样序列化）。

**A.2.4 时间与审计**：【推导】`change_note` 是审计刚需（题设要求半年后能答"谁为什么把阈值调到 0.7"），因此 **W2/W3 的 `change_note` 必填**（`min_length=1, max_length=500`），W4–W6 允许缺省 `''`（状态跃迁的原因可由前一条内容变更的 note 覆盖）。
【推导】审计 MUST 用 `audit.record_with_session(session, ...)`（`audit.py:59-…`，**同事务**），MUST NOT 用 `audit.record`（`audit.py:29-54`）—— 后者自开 `SessionLocal()` 并**吞掉所有异常**，会造出两种错位：主事务回滚了但审计已提交、审计失败但没人知道。这条同时让审计在真库测试里可断言（§D）。

### A.3 逐个接口

#### R1 `GET /api/scenarios`

响应（200）：
```json
{"items": [
  {"id": "…", "scenario_key": "default", "name": "内置默认场景", "status": "active",
   "is_builtin": true, "db_binding": null, "description": "…",
   "published": {"version": 1, "checksum": "9f2c…", "binding_mode": "unrestricted", "kb_ids": []},
   "draft_version": null, "created_at": "…", "updated_at": "…"}
]}
```
【推导】`published` 可为 `null`：一个场景可能还没发布过任何版本（W2 建了 draft 但没 W5）。**这正是 §C.4 要覆盖的读路径状态**：`scenario_id` 已绑但无 published → 集成侧 500。
【推导】`draft_version` 只给「最新的 draft/validated 版本号」一个数，不给内容 —— 列表页不该带上全部 policy。

#### R2 `GET /api/scenarios/{scenario_id}`

在 R1 单项基础上追加 `versions: [{version, state, checksum, change_note, created_at, validated_at, published_at, archived_at, created_by}]`（按 version 倒序，最多 50 条）。【推导】`created_by` 需要 `scenario_versions` 增列 —— **本步不加列**（迁移已冻结），审计通过 `audit_logs.object_id` 关联，见 U4-3。

#### R3 `GET /api/scenarios/{scenario_id}/versions/{version}`

响应头：`ETag: "<checksum>"`。响应体：`{version, state, checksum, policy, kb_ids, binding_mode, dna_declared, change_note, 四个时间戳}`。
【推导】`policy` 回显的是**库里的稀疏覆盖集原文**（不是折叠后的有效值）——否则「我没设这个键」与「我设成了平台值」在客户端看起来一样，而它们 `checksum` 不同。

#### W1 `POST /api/scenarios`

请求体：
```json
{"scenario_key": "cs_bot", "name": "客服助手", "description": "…"}
```
- `scenario_key`：`pattern="^[a-z][a-z0-9_]{1,63}$"`（【源】同 `ck_scenarios_key_format`，`0028:132-134`）。**连字符不合法**，pydantic 层就拒（422），不必等 DB 报 CheckViolation。
- `name`：`min_length=1, max_length=128`（同 `ck_scenarios_name_len`）。
- 无 `is_builtin` 字段（`extra="forbid"` → 传了就 422）。

服务端顺序【推导】：
1. `scenario_key == BUILTIN_SCENARIO_KEY` → **409 `SCENARIO_KEY_RESERVED`**（§A.5.2 的理由）。
2. `INSERT`；撞 `uq_scenarios_tenant_key` → 409 `SCENARIO_KEY_EXISTS`（`detail.context.key`）。
3. 同事务写审计 `scenario.create`。
4. 响应 201，`Location: /api/scenarios/{id}`。

**幂等**：不做 upsert。【推导】「重放创建」若返回 200 + 已存在行，会把「客户端两次点提交」和「两个客户端用同一 key」压成同一结果，掩盖真实冲突。

#### W2 `POST /api/scenarios/{scenario_id}/versions`

请求体：
```json
{"policy": {"recall_top_k": 30, "relevance_threshold_vector": 0.62},
 "kb_ids": ["<uuid>", "…"],
 "change_note": "首版：收紧召回阈值"}
```
- `policy`：**可选**，缺省 `{}`；交给 `validate_policy()`（`policy_spec.py:331-383`），`PolicyValidationError` → **400 `SCENARIO_WRITE_INVALID`**（`detail.field` 取 `exc.field`）。【推导】不在这里复用 `check_policy_publishable`：T4 的两条判据（`recall >= TOP_K_RERANK`、平台重排关时禁开）都只在**发布**时有意义 —— 草稿允许暂时"配得不对"，这正是草稿存在的理由。
- `kb_ids`：`min_length=1`（非内置场景）。**服务端逐条校验存在性 + 归属本租户**。【源】口径照 `integrations.py:109-118`（查不到 → 404「部分知识库不存在」）。【推导】本接口改用 **400 `SCENARIO_WRITE_INVALID`，`field="kb_ids"`**：这是请求体的字段错误，不是路径资源缺失；`context` 只给 `{"missing": 2}` 计数，不回显 id 列表（id 已在请求体里，回显无益只增大日志）。
- `binding_mode`：**契约里没有这个字段**（`extra="forbid"`）。服务端恒按 `kb_ids` 非空推导为 `'explicit'`。【推导】这是「unrestricted 只允许内置」最彻底的实现方式 —— **不是校验，而是没有入口**。见 §A.5.1。
- `dna_declared`：**契约里没有**。服务端用 `build_dna_declared()` 生成（§B.3.2）。【推导】理由同 I-6：DNA 是 L0 平台事实，由场景指定即等于让场景改平台口径。

服务端顺序【推导】：锁 `scenarios` 行 → 取 `COALESCE(MAX(version),0)+1` → `validate_policy` → kb 归属校验 → 生成 `dna_declared` → 算 `checksum` → `INSERT` → 审计 → 201。

- 内置场景（`is_builtin=true`）→ **409 `SCENARIO_BUILTIN_IMMUTABLE`**（§A.5.3）。
- 并发两次 W2 → 都算出同一个 `version` → 后者撞 `uq_scenario_versions_scenario_version` → **409 `SCENARIO_VERSION_CONFLICT`**，`message` 明写「请重试」（不自动重试：【推导】自动重试会把一个客户端 bug 变成两行内容相同的版本）。

#### W3 `PUT /api/scenarios/{scenario_id}/versions/{version}`

必须头：`If-Match: "<checksum>"`。请求体：`{policy?, kb_ids, change_note}`（语义同 W2，**整体替换**）。

【推导】为什么是 `PUT` 整体替换而不是 `PATCH` 键级合并：稀疏覆盖集里「删除一个键」= 回到跟随平台，而 JSONB 里 `null` 与「键不存在」在写入后无法区分（写进 `{"recall_top_k": null}` 会立即撞 `ck_..._policy_recall_top_k` 的 `::int` 转换）。键级合并**表达不了删除**，只剩"写 null 当哨兵"这条一定会出事的歪路。整体替换让「内容 = 请求体」成为单射，`checksum` 才有确定语义。

顺序【推导】：锁场景行 → 锁版本行 → `state != 'draft'` → **409 `SCENARIO_STATE_CONFLICT`**（`context.state`）→ 比 `If-Match`（§C.2）→ 校验 → 写四字段 + **重算 `checksum`** → 审计（`scenario.version.update`，`detail` 里放 **旧 checksum + 新 checksum + 变了的键名**）→ 200 + 新 `ETag`。

【推导】`validated` 之后内容冻结：W3 对 `validated/published/archived` 一律 409。要改内容 → W2 新建 draft。这条让「校验过的内容」与「发布出去的内容」之间没有中间窗口。

#### W4 `POST …/validate`

- 必须 `If-Match`。【推导】理由：W4 做的事是把内容**冻结**，若期间有人改过内容，冻结的是别人写的东西。
- `draft → validated`：`state='validated'`、`validated_at=now()`。
- 已 `validated` → 200 `{changed:false, state:"validated"}`（幂等重放友好）。
- `published/archived` → 409 `SCENARIO_STATE_CONFLICT`。
- 【推导】W4 **不跑 T4**：校验期只做"内容自洽"（`validate_policy` 已在 W2/W3 做过），发布期的外部条件（平台重排是否开启）不该在校验期冻结 —— 否则「今天校验、明天平台关掉重排、后天发布」就会发布一个与校验结论相反的东西。判据放在**发布那一刻**，见 §B.5。

#### W5 `POST …/publish`

- 必须 `If-Match`。
- 只接受 `state='validated'`：`draft` → **409 `SCENARIO_STATE_CONFLICT`**（题设要求的"跳步必须被拒"）。
- 已 `published` 且 `If-Match` 匹配 → 200 `changed:false`。
- **T4 在此处执行**（§B.5）：不合规 → 400 `SCENARIO_NOT_PUBLISHABLE`（`detail.field` 透传 `PolicyValidationError.field`，或为 `"binding_mode"`）。
- 事务顺序见 §C.3（**先归档旧 published，再发布目标**）。
- 提交后失效缓存（§B.6）。
- 响应体带 `archived_previous_version`（被自动归档的旧版本号或 `null`）。【推导】让"发布"这个动作的**全部后果**在一个响应里说完；否则运维要在下一次 GET 才发现旧版本被归档了。

#### W6 `POST …/archive`

- 必须 `If-Match`。
- 只接受 `state='published'` → `archived`，`archived_at=now()`；已 `archived` → 200 `changed:false`；`draft/validated` → 409（§C.1 的线性状态机，草稿不提供归档）。
- **前置校验（本接口的核心，§C.4）**：`is_builtin` → 409 `SCENARIO_BUILTIN_IMMUTABLE`；存在 `integration_applications.scenario_id = :sid` 的行 → **409 `SCENARIO_IN_USE`**，`context.application_count` 给数量、**不回显应用名**。
- 归档后该场景**没有** `published` 版本 → 任何绑定它的应用在集成侧立刻 **500 `SCENARIO_UNRESOLVABLE`**（推演见 §C.4.2）。前置校验就是为堵这个。

#### W7 `PUT /api/integrations/applications/{application_id}/scenario`

请求体：`{"scenario_id": "<uuid>"}` 或 `{"scenario_id": null}`。
- `null` = 解绑 = 回内置（`_load_published` 的 `scenario_id IS NULL AND s.is_builtin` 分支，`resolver.py:91-92`）。
- 非 null 时必须校验：场景存在、属于本租户、**有 published 版本**、非内置、`status='active'`。【推导】「有 published 版本」这条是关键：否则绑定的那一刻就制造了一个必 500 的应用（§C.4.2 的同一个坑，只是从"归档"侧换到"绑定"侧）。
- 应用不存在 / 跨租户 → 404「应用不存在」（【源】口径同 `integrations.py:181-183`）。
- 响应：`IntegrationApplicationOut`（含新字段 `scenario_id`）。
- 【推导】不做「解绑」专用 DELETE 端点：`scenario_id: null` 已经表达，多一个动词就多一条语义分叉。

### A.4 错误码总表

【推导】12 个码，每个对应**客户端的一种不同处置**（这是"能不能合并"的唯一判据）。`SCENARIO_NOT_PUBLISHABLE` 是三处 T4 拒因的**同一个码**，靠 `detail.field` 区分（`recall_top_k` / `rerank_enabled` / `binding_mode`）—— 客户端对它们的处置完全相同（去改内容或改平台开关），拆成三个码只会让客户端写三个 `case`：

| code | HTTP | 客户端该做什么 | 触发点 |
|---|---|---|---|
| `SCENARIO_WRITE_INVALID` | 400 | 改请求体 | W1–W3（字段/策略/kb 归属） |
| `SCENARIO_NOT_PUBLISHABLE` | 400 | 改内容或改平台开关再发布 | W5（T4 三处拒因，`field` 区分） |
| `SCENARIO_NOT_FOUND` | 404 | 检查 id / 权限 | 全部（含跨租户） |
| `SCENARIO_KEY_RESERVED` | 409 | 换一个 key | W1 |
| `SCENARIO_KEY_EXISTS` | 409 | 换一个 key 或改用已有场景 | W1 |
| `SCENARIO_BUILTIN_IMMUTABLE` | 409 | 别再试，内置不可改 | W2 / W6 |
| `SCENARIO_STATE_CONFLICT` | 409 | 先 GET 看 state，按状态机走 | W3–W6 |
| `SCENARIO_CHECKSUM_MISMATCH` | 409 | 重新 GET 拿新 `ETag`，人工合并 | W3–W6 |
| `SCENARIO_PRECONDITION_REQUIRED` | 428 | 补 `If-Match` 头 | W3–W6 |
| `SCENARIO_IN_USE` | 409 | 先给应用换绑，再归档 | W6 |
| `SCENARIO_VERSION_CONFLICT` | 409 | 重试（版本号被并发占用） | W2 |
| `SCENARIO_PUBLISH_CONFLICT` | 409 | 重试（并发发布撞唯一索引） | W5 |

【推导】为什么**不并进** `errors.py` 的三码：那三码的读者是**外部集成系统**，其契约要求"响应体只有固定文案、诊断只进日志"（`errors.py:1-14`）。写接口的读者是**本租户管理员**，它需要 `field` 与 `context` 才能自助修配置。两者受众、文案策略、可泄露内容**全都不同**，合成一个枚举会让将来每个码都要写"这个码能不能带 context"的特例。故新增 `write_errors.py`，`errors.py` 一行不改。

### A.5 明确不允许的操作

#### A.5.1 客户端不能设 `binding_mode='unrestricted'`
【源】`build_snapshot` 读侧已拦：`unrestricted` 且非内置 → 500（`snapshot.py:426-440`），注释明写「Step 4 一旦开写接口，这里就是唯一防线」。
【推导】**写侧的实现不是"加一道校验"，而是"不留入口"**：W2/W3 请求体没有 `binding_mode` 字段，服务端由 `kb_ids` 非空推导 `'explicit'`（`ck_..._binding_consistent` 因此恒成立）。同时仍保留一道**发布期断言**（§B.5 的 `ensure_publishable` 里再判一次 `binding_mode == 'unrestricted' and not is_builtin`）—— 理由是防「绕过 API 直写 SQL 之后，再用 API 发布」这个组合路径。两道闸的**性质不同**（入口不存在 vs 状态复核），不是重复。

#### A.5.2 不允许把 `scenario_key` 用成 `default`
【源】`snapshot.py:35-36` 已声明「应用层 MUST 拒绝其它场景改用这个 key」。
【推导】只靠 `uq_scenarios_tenant_key` 挡不住：种子行是**按租户**插的（`0028:294-322`），若某租户的内置行缺失（`builtin_fallback` 这条路径**存在且有测试**），W1 用 `default` 会**插成功**，于是该租户出现一个"叫 default 但不是内置"的场景 —— 之后 `_load_published` 的内置分支（`is_builtin`）依然查不到它，但场景列表里多出一个同名项，运维与审计都会被带偏。所以必须在 W1 里**显式判等**，不能指望唯一约束。

#### A.5.3 内置场景不可写
【推导】三条，全部归到 `SCENARIO_BUILTIN_IMMUTABLE`：
1. 不能对内置场景 W2（创建版本）——【源】S-8 要求内置的已发布 `policy` 为空（`self_check.py:543-566`），任何新版本按定义都可能违反它。**与其让 S-8 在启动时喊，不如不让它发生**。
2. 不能 W6（归档内置的已发布版本）——否则该租户全部走 `builtin_fallback`（虽然同值，但会一直打 `rag.scenario.builtin_row_missing` 这个"该补种"的告警，把真告警淹掉）。
3. 不能 W3/W4/W5（前两条已覆盖，此处只是把错误码说全）。

#### A.5.4 不允许"发布一个没有 published 的场景给应用用"
见 W7 的校验与 §C.4.2。绑定与归档是同一枚硬币的两面，两处都要堵。

---

## B. 代码级设计

### B.1 分层与依赖方向

```
api/scenarios.py            ← HTTP 语义（状态码 / 头 / 错误体）
   ↓                        仅依赖 schemes + write_service
api/integrations.py         ← 追加 W7 一个 handler
   ↓
services/scenario/write_service.py   ← 事务边界 + 锁顺序 + 审计（唯一 impure 写入口）
   ↓
step4_check.py  ← T4 的第二道（纯函数）
dna_declare.py  ← 声明镜像生成（纯函数，读 config 层）
policy_spec.py / snapshot.py / errors.py / write_errors.py  ← 纯
   ↓
models/entities.py（Scenario / ScenarioVersion / IntegrationApplication / KnowledgeBase）
```

【推导】`write_service` 与 `resolver` **互不 import**：一个只读、一个只写，共享的只有 `snapshot.compute_checksum` 与 `dna_declare`。这样「写接口把读路径搞坏」在结构上不可能（它们没有边）。
【源】依赖方向纪律同 `decisions.py:123`（config 不得反向依赖 services）与 `resolver.py:3-10` 的模块 docstring。

### B.2 schemas（`app/schemas/scenario.py`）

```python
class ScenarioCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario_key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=500)

class VersionContent(BaseModel):
    """W2 / W3 共用的内容体。注意：**没有** binding_mode，**没有** dna_declared。"""
    model_config = ConfigDict(extra="forbid")
    policy: dict[str, object] = Field(default_factory=dict)
    kb_ids: list[uuid.UUID] = Field(min_length=1)
    change_note: str = Field(min_length=1, max_length=500)

class VersionOut(BaseModel):
    version: int
    state: Literal["draft", "validated", "published", "archived"]
    checksum: str
    policy: dict[str, object]
    kb_ids: list[uuid.UUID]
    binding_mode: Literal["explicit", "unrestricted"]
    dna_declared: dict[str, object]
    change_note: str
    created_at: datetime
    validated_at: datetime | None
    published_at: datetime | None
    archived_at: datetime | None

class VersionActionOut(BaseModel):
    version: int
    state: str
    checksum: str
    changed: bool                 # 幂等重放时为 False
    archived_previous_version: int | None = None   # 仅 publish 可能非空

class ScenarioBindingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario_id: uuid.UUID | None
```

【推导】`policy: dict[str, object]` 不用 pydantic 的嵌套模型：键闭集与范围**必须**由 `policy_spec.validate_policy` 判（唯一真源，`policy_spec.py:1-12`），在这里再写一遍类型就是第二个真源，两者必然分叉（S-1/S-4 已为此写过自检）。

### B.3 服务层骨架

#### B.3.1 `write_service.py` —— 六个写用例的公共骨架

```python
_SQL_LOCK_SCENARIO = """
SELECT id, is_builtin, status
FROM scenarios
WHERE id = :scenario_id AND tenant_id = :tenant_id
FOR UPDATE
"""
"""所有写用例的第一条语句（唯一的锁）。缺 tenant_id 条件 = 跨租户外键被锁住。"""

_SQL_LOCK_VERSION = """
SELECT v.version, v.state, v.checksum, v.policy, v.kb_ids, v.binding_mode,
       v.dna_declared, v.change_note
FROM scenario_versions v
WHERE v.scenario_id = :scenario_id
  AND v.tenant_id   = :tenant_id
  AND v.version     = :version
FOR UPDATE
"""

async def _lock_scenario(session, *, tenant_id, scenario_id) -> ScenarioLock:
    row = (await session.execute(
        sa.text(_SQL_LOCK_SCENARIO).bindparams(
            scenario_id=scenario_id, tenant_id=tenant_id))).first()
    if row is None:
        raise WriteError(WriteCode.NOT_FOUND)      # 跨租户与不存在同码 → 不可枚举
    return ScenarioLock(is_builtin=row.is_builtin, status=row.status)
```

【推导】锁顺序固定为 **`scenarios` 行 → `scenario_versions` 行**，全局唯一，不允许任何用例反过来。理由：两个写用例（发布 v3 与归档 v2）会同时碰这两张表；顺序不一致就是死锁的教科书条件。所有用例都先拿同一把「场景锁」，同一个场景的写天然串行。

【推导】`FOR UPDATE` 锁的是 `scenarios` 那一行（不是 `scenario_versions` 的行集），因此**「新版本号分配」也不会有并发冲突** —— 不需要重试循环，`SCENARIO_VERSION_CONFLICT` 只是给"有人绕过锁"留的最后一道网。

#### B.3.2 `dna_declare.py`

```python
_ENV_BOUND: Final[tuple[str, ...]] = (
    "embedding_model", "embedding_dim", "graph_extractor_model", "wiki_generator_model",
)

def build_dna_declared() -> dict[str, object]:
    """生成版本行的 dna_declared 声明镜像（与迁移 0028 的种子**同构**）。

    【源】4 个代码常量取自三个 profile（index_profile.py:37 / 45-51、graph_profile.py:30、
    wiki_profile.py:21）；4 个环境绑定项只声明字段名、不落值（0028:48-54）。
    【推导】为什么 API 创建的版本也不落环境值：
      ① 与种子同构 ⇒ 任何路径产出的 dna_declared 形状相同，`ck_..._dna_keyset` 之外
         不需要第二条一致性规则；
      ② 落值等于把「写这一行时的部署」冻进审计内容，模型一换这行就成了会过期的断言 ——
         而 S-9 的漂移检测只对 is_builtin 行生效（self_check.py:580），非内置行写值
         只会制造一堆永远没人看的差异。
    """
    index, graph, wiki = current_dna()      # snapshot.current_dna()，只读平台
    return {
        "embedding_text_version": index.embedding_text_version,
        "chunker_version": index.chunker_version,
        "graph_prompt_version": graph.prompt_version,
        "wiki_prompt_version": wiki.prompt_version,
        "env_bound": list(_ENV_BOUND),
    }
```

【推导】T1 需补一条自检（S-4c）：`set(build_dna_declared()) == set(_SEED_DNA)` —— 否则「写接口生成的形状」与「迁移种子的形状」会静默分叉，而 `ck_..._dna_keyset` 只保证了"不许多"，不保证"两边一样"。**本步不加这条自检**（会动 `self_check.py` 已验收文件），登记为 U4-1。

#### B.3.3 W2 与 W3 的公共体

```python
async def _apply_content(session, *, scenario, version_row, body, user) -> str:
    """写入四个内容字段并返回新 checksum。调用方已完成锁 + 状态 + If-Match 判定。"""
    policy = policy_spec.validate_policy(body.policy)          # 未知键 → 400（含 field）
    kb_ids = await _require_kb_ids(session, tenant_id=user.tenant_id, ids=body.kb_ids)
    dna = build_dna_declared()
    binding_mode = "explicit"                                  # kb_ids 必非空 ⇒ CK 恒成立
    checksum = snapshot.compute_checksum(
        policy=policy, kb_ids=[str(k) for k in kb_ids],
        binding_mode=binding_mode, dna_declared=dna,
    )
    ...
```

【推导】`compute_checksum` 的 `kb_ids` 传入**字符串列表**，顺序 = 请求体顺序去重后（`list(dict.fromkeys(...))`，【源】同 `integrations.py:108`）。**不去排序**：排序会让「A,B」与「B,A」得到同一 checksum，从而让一次真实的内容变更在 `If-Match` 上不可见。（`dna_declared` 由 `canonical_json` 的 `sort_keys=True` 归一，`snapshot.py:283-309`，无需在此处理。）

#### B.3.4 W5 发布（完整）

```python
async def publish(session, *, tenant_id, user_id, scenario_id, version, if_match) -> VersionActionOut:
    scenario = await _lock_scenario(session, tenant_id=tenant_id, scenario_id=scenario_id)
    row = await _lock_version(session, tenant_id=tenant_id, scenario_id=scenario_id, version=version)

    if row.state == "published":
        _require_if_match(if_match, row.checksum)              # 幂等重放仍要证明"你看到的就是当前"
        return VersionActionOut(version=version, state="published",
                                checksum=row.checksum, changed=False)
    if row.state != "validated":
        raise WriteError(WriteCode.STATE_CONFLICT,
                         context={"state": row.state, "expected": "validated"})
    _require_if_match(if_match, row.checksum)

    # ── T4：唯一能阻止坏配置进入系统的位置（§B.5）───────────────
    checked = step4_check.ensure_publishable(
        policy=row.policy, binding_mode=row.binding_mode,
        is_builtin=scenario.is_builtin,
        platform_rerank_enabled=bool(get_settings().rerank_enabled),
    )
    # T4 返回的归一化 policy 必须与库里逐键相等：不等 = 库里内容不合法（直写 SQL）
    if checked != dict(row.policy):
        raise WriteError(WriteCode.NOT_PUBLISHABLE,
                         message="库中策略与校验结果不一致，拒绝发布", context={})
    # 顺带复核 checksum 与库内容一致（发现"内容被改但 checksum 没跟上"）
    if snapshot.compute_checksum(
            policy=checked, kb_ids=list(row.kb_ids),
            binding_mode=row.binding_mode, dna_declared=dict(row.dna_declared)) != row.checksum:
        raise WriteError(WriteCode.CHECKSUM_MISMATCH)

    # ── 顺序不可调换：先归档旧 published，再发布目标（§C.3）────────
    archived = (await session.execute(sa.text(_SQL_ARCHIVE_PUBLISHED).bindparams(
        scenario_id=scenario_id, tenant_id=tenant_id, keep_version=version))).rowcount
    await session.execute(sa.text(_SQL_PUBLISH).bindparams(
        scenario_id=scenario_id, tenant_id=tenant_id, version=version))
    await audit.record_with_session(session, tenant_id, user_id, "scenario.version.publish",
        object_type="scenario_version", object_id=f"{scenario_id}:{version}",
        detail={"from_version": None, "archived": archived, "checksum": row.checksum})
    await session.commit()
    resolver.invalidate(tenant_id, scenario_id)                # ★ 提交之后（resolver.py:111-117）
    return VersionActionOut(version=version, state="published",
                            checksum=row.checksum, changed=True,
                            archived_previous_version=archived or None)
```

配套 SQL：

```python
_SQL_ARCHIVE_PUBLISHED = """
UPDATE scenario_versions
SET state = 'archived', archived_at = now()
WHERE scenario_id = :scenario_id
  AND tenant_id   = :tenant_id
  AND state       = 'published'
  AND version    <> :keep_version
"""
_SQL_PUBLISH = """
UPDATE scenario_versions
SET state = 'published',
    published_at = COALESCE(published_at, now())
WHERE scenario_id = :scenario_id
  AND tenant_id   = :tenant_id
  AND version     = :version
  AND state       = 'validated'
"""
```

【推导】`_SQL_PUBLISH` 带 `AND state='validated'`：即使上面的 Python 判定被将来的重构改错，UPDATE 也不会把 `archived` 拉回 `published`（`rowcount=0` → 抛 `STATE_CONFLICT`，需在实现里断言 `rowcount == 1`）。
【推导】`published_at = COALESCE(...)`：保持「首次发布时间」的语义（重放不刷新），与 `ck_..._state_timestamps` 只要求非空一致。

### B.4 路由层要点（`app/api/scenarios.py`）

```python
router = APIRouter(prefix="/scenarios", tags=["scenarios"])

def _fail(code: WriteCode, *, message: str | None = None,
          field: str | None = None, context: dict | None = None) -> HTTPException:
    return HTTPException(status_code=_HTTP[code],
                         detail={"code": code.value, "message": message or _MSG[code],
                                 "field": field, "context": context or {}})
```

- 所有 handler 用 `Depends(require_admin)` + `Depends(get_db)`。
- `If-Match` 用 `Header(default=None, alias="If-Match")`；【推导】取 `None` 时由 service 抛 `PRECONDITION_REQUIRED`（428），**不在路由层挡** —— 让"缺头"与"值不匹配"在同一个地方判，避免两处各判一半。
- `PolicyValidationError`（`ValueError` 子类）**必须**在 handler 内转成 400：`main.py:362-371` 的全局 `ValueError` handler 会把它变成 `{"detail": "<原文>"}`，丢掉 `field`。【推导】这是一处**真实的陷阱**：不显式捕获，错误体就与 §A.2.3 的契约不符，且客户端拿不到 `field`。

### B.5 T4 的接线点（本步的核心交付）

#### B.5.1 位置与顺序

【源】`check_policy_publishable(policy, *, platform_rerank_enabled) -> dict`（`self_check.py:634-660`）：
- `policy_spec.validate_policy(policy)`（`:644`）—— 键闭集/类型/范围/阈值 > 0
- `recall_top_k < decisions.TOP_K_RERANK` → 拒（`:646-652`）
- `rerank_enabled is True and not platform_rerank_enabled` → 拒（`:654-659`）
- 合规返回归一化 dict（`:660`）

【推导】调用点唯一：**W5 的 publish handler**，在「锁 + 状态判定 + If-Match 判定」之后、「归档旧 published」之前。三条理由：
1. **必须在 If-Match 之后**：并发场景下若先跑 T4 再比 checksum，两个请求可能都通过 T4，然后在 UPDATE 时才发现内容不一致 —— T4 的结论就失去了"针对我看到的那份内容"的语义。
2. **必须在归档之前**：T4 失败而旧 published 已被归档，会让场景进入"没有 published"的中间态（→ 绑定应用全 500，§C.4.2）。整个事务虽会回滚，但把"会失败的判定"放在最前面是零成本的正确做法。
3. **不在 W2/W3/W4**：理由见 §A.3 W4（发布期的外部条件不该在校验期冻结）。

#### B.5.2 失败返回什么码

| 失败判据 | code | HTTP | `detail.field` |
|---|---|---|---|
| `validate_policy` 抛（键闭集/类型/范围/阈值） | `SCENARIO_NOT_PUBLISHABLE` | 400 | `exc.field` |
| `recall_top_k < TOP_K_RERANK` | 同上 | 400 | `"recall_top_k"` |
| `rerank_enabled=true` 而平台重排关 | 同上 | 400 | `"rerank_enabled"` |
| `binding_mode=unrestricted` 且非内置（§A.5.1 的第二道） | `SCENARIO_NOT_PUBLISHABLE` | 400 | `"binding_mode"` |

【推导】不用 `SCENARIO_WRITE_INVALID` 而单列 `SCENARIO_NOT_PUBLISHABLE`：客户端的处置不同 —— 前者是"请求体写错了"，后者是"内容本身合法，但在当前平台配置下不能上线"（改法可能是去改平台开关，而不是改请求）。

#### B.5.3 与 DB `uq_..._one_published` 谁先拦

【推导】三道，顺序固定，**每道拦的是不同的事**：

| 顺序 | 谁 | 拦什么 | 结果 |
|---|---|---|---|
| 1 | 场景行 `FOR UPDATE`（§B.3.1） | 并发发布 | 串行化，后到者等前一个提交后再判状态 → 若前一个已发布同一版本 → `changed:false`；不同版本 → 正常走 |
| 2 | 应用层 T4 + 状态机 | 业务规则 | 400 / 409（可解释、可自助修） |
| 3 | DB 部分唯一索引 `uq_scenario_versions_one_published` | 应用层漏判 | `IntegrityError` → 捕 `constraint_name == 'uq_scenario_versions_one_published'` → **409 `SCENARIO_PUBLISH_CONFLICT`** |

【推导】关键结论：**在锁纪律成立时，第 3 道永远不会触发**。它存在的意义不是"兜底正常路径"，而是"当有人绕过 `write_service` 直接写库、或将来有人把锁删了时，系统仍然不会出现两个 published"。因此 §D.4 的并发用例必须**断言走的是第 1 道**（两个请求都 200 且最终只有一个 published），而不是"撞了唯一索引也算过"——否则第 1 道失效永远测不出来。

【推导】`IntegrityError` 的约束名提取必须是显式工具函数并单独测（asyncpg 的 `constraint_name` 藏在 `exc.orig.__cause__` 里，SQLAlchemy 不统一暴露）。**不许**写 `except IntegrityError: 409` —— 那会把 `ck_...` 的违反、FK 的违反全变成"并发冲突"，是最容易造成假绿的一类写法（`test_0028_pg_constraints.py:11-18` 已把这个教训写成纪律）。

### B.6 缓存失效

【源】`resolver.invalidate(tenant_id, scenario_id)` docstring：**事务提交后**调用（`resolver.py:111-117`）；`_CACHE` 的键是 `(tenant_id, scenario_id 或 None)`（`:69`）。

【推导】必须调用 `invalidate` 的写用例与参数：

| 用例 | 调什么 | 为什么 |
|---|---|---|
| W5 发布 | `invalidate(tenant_id, scenario_id)` | 读路径读的是 published 行 |
| W6 归档 | `invalidate(tenant_id, scenario_id)` | 让"没有 published"立刻可见，而不是 60s 后 |
| W2 / W3 / W4 | **不调** | 它们只改非 published 行，`LOOKUP_SQL` 带 `v.state='published'`（`resolver.py:88`），缓存里的行没有变 |
| W7 绑定/解绑 | **不调（且不能调）** | 改的是 `integration_applications` 行，缓存的是场景行；`resolve_scenario` 的键由 `application.scenario_id` 现算（`resolver.py:187-188`） |
| W1 创建场景 | 不调 | 新场景没有 published 版本，缓存里本来就没有它的命中行 |

【推导】**「多副本部署时 TTL 60s 内不一致」这件事在 Step 4 变成可感知的**：发布后另一个 worker 的最长不一致窗口 = `SCENARIO_CACHE_TTL_SECONDS`（站位值 60s，`resolver.py:40-46`）。本步**不引入** Redis 失效频道（那要改 `invalidate` 的签名与调用方，属跨进程基础设施），登记为 U4-2，并在 W5 响应里不做"已生效"的承诺（响应只说"已发布"）。

### B.7 审计

【推导】四张动作 + `detail` 字段表（审计的价值在于"半年后能答为什么"，所以 `detail` 必须带"变了什么"，不能只写"改了一次"）：

| 动作 | `action` | `detail` |
|---|---|---|
| W1 | `scenario.create` | `{scenario_key, name}` |
| W2 | `scenario.version.create` | `{version, checksum, policy_keys, kb_count, change_note}` |
| W3 | `scenario.version.update` | `{version, from_checksum, to_checksum, changed_keys, change_note}` |
| W4 | `scenario.version.validate` | `{version, checksum}` |
| W5 | `scenario.version.publish` | `{version, checksum, archived_version}` |
| W6 | `scenario.version.archive` | `{version, checksum, application_count}` |
| W7 | `integration.application.bind_scenario` | `{application_id, from_scenario_id, to_scenario_id}` |

【推导】`changed_keys` = 新旧 policy 键集的对称差（只给键名不给值）：值可能敏感度低，但审计表是全租户可查的（若将来做审计查询接口），键名已足够定位"谁动了阈值"。
【推导】审计 MUST 与主变更**同事务**（`record_with_session`），这也是 §D.6「审计可断言」的前提。

---

## C. 状态机与并发

### C.1 状态机

【源】状态闭集与时间戳约束：`0028:181-184`、`:249-254`。
【推导】允许的跃迁（**线性，无回退**）：

| 从 \ 到 | draft | validated | published | archived |
|---|---|---|---|---|
| **draft** | （W3 改内容，状态不变） | ✅ W4 | ❌ 409（题设要求的"跳步必须被拒"） | ❌ 409 |
| **validated** | ❌ 409（内容已冻结，要改 → W2 新建） | ⏸ W4 重放 → `changed:false` | ✅ W5 | ❌ 409 |
| **published** | ❌ | ❌ | ⏸ W5 重放 → `changed:false` | ✅ W6 |
| **archived** | ❌ 终态 | ❌ | ❌ | ⏸ W6 重放 → `changed:false` |

【推导】三条设计判断：

1. **没有回退**（`validated → draft`、`archived → published` 都不给）。回退会让"这条版本行曾经是什么"变成不可复现的历史，而 `scenario_versions` 的全部价值就是可复现。改主意的正确做法：W2 新建一个版本。
2. **草稿不提供归档**（`draft → archived` 被拒）。放弃草稿 = 不发布它，行留着（审计），无功能损害。代价是废弃草稿行会堆积，登记 U4-6。
3. **`archived` 是终态**，但**不阻止同场景发布新版本** —— `uq_..._one_published` 是 `WHERE state='published'` 的部分索引（`0028:260-266`），归档即释放。

【源】`ck_..._state_timestamps` 要求 `published/validated/archived` 各自时间戳非空。【推导】因此三条 UPDATE 都必须显式写对应时间戳，**不能**依赖任何触发器（本表没有 `updated_at`）。漏写 → `CheckViolationError`，`constraint_name='ck_scenario_versions_state_timestamps'`；§D.2 有一条用例专门按这个约束名断言，防止实现"顺手"改成"任意 CheckViolation 就 400"。

### C.2 乐观并发（`If-Match` + `checksum`）

#### C.2.1 `checksum` 到底标识什么

【源】`checksum = sha256(canonical{binding_mode, dna_declared, kb_ids, policy})[:16]`（`snapshot.py:291-310`）。
【推导】**它标识"内容"，不标识"版本""。由此三条推论，全部要写进契约：

1. **`draft`→`validated`→`published` 三次跃迁后 `checksum` 不变**（内容没变）。
2. **两个不同版本可以拥有同一个 `checksum`**（同内容、不同版本号）。因此 `If-Match` **必须与 path 里的 `version` 联合**才有定位能力；单独一个 `If-Match` 无法回答"你要改哪一行"。
3. **`If-Match` 不是 HTTP 意义上的 ETag**（ETag 标识"这个 URL 当前的内容"）。本设计借用其**比较语义**（不匹配即 412/409），不使用其"同一 URL 的当前表示"语义。这条差异**必须写进 API 文档**，否则客户端会把它当 ETag 用，在"发布没改内容所以 ETag 不变"时困惑。登记 U4-4。

#### C.2.2 何时重算

| 时机 | 重算？ | 理由 |
|---|---|---|
| W2 创建 draft | 算一次（初值） | 内容诞生 |
| W3 改内容 | **必须重算** | 内容变了 |
| W4 / W5 / W6 状态跃迁 | **不重算** | 内容没变（I-9 的输入集不含 state） |
| 任何读取路径 | **不重算** | 只比对（§B.3.4 里那条"复核"是防御，不是常规路径） |

#### C.2.3 三个失败码的分工

| 情形 | 码 | 理由 |
|---|---|---|
| 请求没带 `If-Match` | **428** `SCENARIO_PRECONDITION_REQUIRED` | "你没给条件" ≠ "条件不满足"。若混成 409，客户端无法区分"我拿的是旧值"与"我漏发头" |
| 带了但不匹配 | **409** `SCENARIO_CHECKSUM_MISMATCH`，`context {expected_prefix, got_prefix}` | 只给前 6 位：够定位、不够逆推内容 |
| 版本行不存在 | **404** | 顺序上先判存在性再判前置条件（否则 404 场景返回 428 会泄露"这个版本曾经存在过"） |

【推导】为什么用 409 而非 412：412 在 RFC 里属"条件请求"通用语义，本项目 `test_integrations.py` 与既有 API 从不用 412；统一用 409 让运维只需记一套。**这是取舍，不是标准**，登记 U4-5。

#### C.2.4 幂等语义（逐接口）

| 接口 | 同 `If-Match` 重放 | 依据 |
|---|---|---|
| W3 | **409**（第一次成功后 `checksum` 已变，第二次必然不匹配） | 【推导】这不是缺陷，是特性：重放意味着"基于同一份旧内容又写了一次"，若静默成功就无法发现"两次点击都提交了" |
| W4 / W5 / W6 | **200 `changed:false`** | 【推导】状态跃迁天然幂等：目标态已达，且 `If-Match` 仍匹配（内容没变）→ 告诉调用方"你要的结果已经成立" |
| W7 | 200（set 语义） | 【推导】绑定的效果与调用次数无关 |

### C.3 发布的事务顺序（为什么不能调换）

```sql
-- 事务内，顺序固定
-- 0) SELECT ... FROM scenarios      WHERE id=:sid AND tenant_id=:tid FOR UPDATE
-- 1) SELECT ... FROM scenario_versions WHERE ... version=:n                 FOR UPDATE
-- 2) （Python）状态机 + If-Match + T4
-- 3) UPDATE scenario_versions SET state='archived', archived_at=now()
--      WHERE scenario_id=:sid AND state='published' AND version<>:n
-- 4) UPDATE scenario_versions SET state='published', published_at=COALESCE(published_at,now())
--      WHERE scenario_id=:sid AND version=:n AND state='validated'
-- COMMIT
```

【推导】3 必须在 4 之前：`uq_scenario_versions_one_published` 是**立即生效**的唯一索引（不是 `DEFERRABLE`，`0028:260-266` 未声明 deferrable）。若先发布新版本，事务中间态会短暂存在两行 `published` → 索引在**该语句执行的瞬间**就报 `UniqueViolationError`，与"并发发布"这个合法场景混成同一个错误码。**先归档后发布**让索引在整个事务里恒满足（先 1 行 → 0 行 → 1 行）。
【推导】因此"事务内允许的中间态"也是设计的一部分：`archived` 与 `published` 在事务内可以短暂为零行，但**不允许为两行**。

### C.4 归档 × 绑定 × 读路径（必须推演到 SQL 层）

#### C.4.1 约束层面的可/不可

| 操作 | DB 会拦吗 | 说明 |
|---|---|---|
| `DELETE FROM scenarios`（有应用引用） | ✅ `fk_integration_app_scenario`（RESTRICT） | 【源】`0028:279-286`；`test_0028_pg_constraints.py:493-521` 已验 |
| `DELETE FROM scenarios`（无引用） | ✅ 顺带 CASCADE 删掉全部版本 | 【源】`0028:180` |
| `UPDATE scenario_versions SET state='archived'` | ❌ **不拦** | 【推导】**这是本步最危险的一条**：归档是 UPDATE，RESTRICT 完全不参与 |

#### C.4.2 归档已发布版本 → 绑定应用会看到什么（逐步推演）

【源的调用链】：`get_integration_principal`（`integrations.py:70-94`）取 `application.scenario_id` → `_resolve_or_reject`（`:199-224`）→ `resolve_scenario`（`resolver.py:176-213`）→ `_load_published`（`:153-173`）→ `build_snapshot`（`snapshot.py:362-…`）。

【推导】逐跳结局：

1. `_load_published` 的 SQL 带 `AND v.state = 'published'`（`resolver.py:88`）→ 归档后该场景**没有**满足条件的行 → 返回 `None`。
2. `resolve_scenario` 把 `None` 传给 `build_snapshot`，同时 `scenario_id` 非 `None`（应用绑着它）。
3. `build_snapshot` 的 R1：`scenario_id is not None and row is None` → `ScenarioError(UNRESOLVABLE)`（`snapshot.py:384-392`）。
4. `_resolve_or_reject` 转成 **HTTP 500**「场景配置不可解析」（`errors.py:39`）。

**结论：归档一个有应用引用的场景，等于让这批应用的所有 `/ask` 与 `/search` 立刻 500，且每一次都打一条 WARN（`integrations.py:218-223`）。** 而且用户侧看到的是 500 —— 不是 404、不是"场景已下线"，运维第一反应会是"服务挂了"。

【推导】三条处理，缺一不可：
1. **W6 前置校验**：`SELECT count(*) FROM integration_applications WHERE tenant_id=:tid AND scenario_id=:sid`（走 `idx_integration_app_scenario`，`0028:287-291`）> 0 → 409 `SCENARIO_IN_USE`。这把"必然 500"变成"明确拒绝"。
2. **W7 前置校验**：绑定时要求目标场景**有 published 版本**（§A.3 W7），堵住反方向。
3. **不提供"解绑后自动归档"的批处理**：那会在一句请求里同时改 N 个应用 + 1 个版本，失败时的中间态无法解释。运维先 W7 解绑（应用立刻回内置，`meta.scenario_version` 变 1），再 W6 归档 —— 两步各自可回滚。

#### C.4.3 绑定 / 解绑 / 归档后 `meta` 怎么变

| 时刻 | `_load_published` 分支 | `source` | `meta.scenario_key` | `meta.scenario_version` | `meta.scenario_fingerprint` |
|---|---|---|---|---|---|
| 绑定 ct_scene（v3 published） | `s.id = :scenario_id` | `application` | `ct_scene` | **3** | v3 内容指纹 |
| 解绑（`scenario_id=NULL`） | `s.is_builtin` | `builtin_row` | `default` | **1**（种子行版本） | 平台默认指纹 |
| 解绑 + 该租户内置行缺失 | 无行 | `builtin_fallback` | `default` | **0**（`BUILTIN_FALLBACK_VERSION`，`snapshot.py:38-43`） | **行为字段**与有行时逐字段相等（`index/graph/wiki/policy/binding_mode/kb_ids/is_builtin/scenario_key`）；但 `version` 与 **`fingerprint` 必须不同** —— 指纹公式含 `version`（`snapshot.py:267-274`），兜底取 0 正是为了不与真实 v1 撞车，否则审计无法区分「有 v1 行」与「漏补种」。`test_builtin_row_equals_builtin_fallback:529` 锁的正是这条**不相等** |
| 归档 v3（若已解绑） | `s.is_builtin`（绑的是 NULL） | 同上 | 同上 | 同上 | 同上 |
| 归档 v3（**未解绑**，被 W6 挡住） | —— | —— | —— | 请求 500，**没有 meta** | —— |

【推导】`meta` 的四个键是固定的（`integrations.py:227-239`），上表只列会变的那三个；`contract_version` 恒定（`decisions.CONTRACT_VERSION`）。**「归档后 meta 怎么变」的正确答案是"看不到 meta"** —— 所以 §D.5 的用例必须断言 500 + 固定文案，而不是断言某个版本号。

### C.5 跨租户

【推导】三层，每层的失败码不同：

| 层 | 做法 | 失败码 |
|---|---|---|
| 路由层 | `tenant_id` 只从 `CurrentUser` 取（`deps.py:127-139`） | —— |
| SQL 层 | 每条语句带 `tenant_id = :tenant_id`（含 `FOR UPDATE` 的那条） | 查不到 → `NOT_FOUND` |
| 应用层 | `scenario_id` 属于 B 租户、A 租户来操作 → 与"不存在"**同一码** | **404** |

【推导】不许 403：`integrations.py:87-93` 把"应用不存在 / 租户不符 / 已禁用 / 密钥不符"合并成一个 401，同一个思路。`scenario_versions` 的 SQL 也 MUST 带 `tenant_id`（【源】读侧已如此，`resolver.py:89`，理由是"加严：版本行租户不一致视为查不到"），写侧同理 —— 否则一个被篡改的 `scenario_id` 关联到别的租户的版本行时，`FOR UPDATE` 会锁住别人的行。

【推导】必测的一件事：A 租户对一个**确实存在**的 B 租户场景发 W5 → 404（不是 403、不是 500），且 B 租户的版本状态**未被改动**（用 B 的会话回读校验）。这一条同时证明"不泄露存在性"与"没造成副作用"。

### C.6 并发用例要在真库上证明什么

【推导】四条，全部无法用 FakeSession 证明：

1. **两个并发 W2** → 必须只产生一个版本号；另一个 409 `SCENARIO_VERSION_CONFLICT`（或串行化后 201 得到 n+1）。断言"库里该场景的版本号连续无重复"。
2. **两个并发 W5（同一 validated 版本，同一 `If-Match`）** → 一个 200 `changed:true`，另一个 200 `changed:false`（被场景锁串行化后看到 `published`）；**不允许**出现两个 published（§D.4 直接查库）。
3. **两个并发 W5（不同版本，各自 validated）** → 两者都 200，但**最终恰好一行 published**，且先提交者的版本被后者自动归档（`archived_at` 非空）。
4. **并发 W3（同一 draft，同一 `If-Match`）** → 一个 200、一个 409 `CHECKSUM_MISMATCH`；库里的 `checksum` 等于"其中一个请求体的内容"，**不可能是两者的拼接**。

【推导】并发用例的构造必须是**真并发**：`asyncio.gather` 两个独立的 `AsyncSession`（各自 `async with SessionLocal()`），不是同一个 session 顺序调用。用同一个 session 永远绿 —— 那才是这类测试最常见的假绿形态。

### C.7 `CURRENT_STEP` 的处置（题设第 4 条）

#### C.7.1 事实核对

【源】`policy_spec.py:24-25`：`CURRENT_STEP = 2`，docstring「本次交付的步骤号（Step 1–2）。仅用于错误文案里告诉运维『自哪一步起可配置』」。
【源】`test_resolver_policy.py:42-56`（函数 `test_closure_step_semantics_are_explicit`）：
```python
assert CURRENT_STEP == 2
assert POLICY_CLOSURE_STEP == 3
assert POLICY_CLOSURE_STEP > CURRENT_STEP          # ★ 这一条会成为 Step 4 的绊脚石
```
【源】`test_effective.py:228-233`：另一处 `assert policy_spec.CURRENT_STEP == 2`，docstring 写「MUST 保持 2 —— 它服务错误文案，且有既有断言硬钉住」。

#### C.7.2 结论：**改，改成 4**（推荐值，需你拍板）

【推导】理由：`CURRENT_STEP` 的语义是"本次交付到第几步"（`policy_spec.py:25`）。Step 4 交付后，这个数**在事实上就是 4**。不改它，等于让一个变量长期撒谎；而它的唯一消费点是错误文案（"自 Step N 起可配置"）—— 撒谎的直接后果是**运维按错误提示去等一个已经上线的能力**。

【推导】**不许静默绕过**，因此三处必须显式改，且都属"事实变更"而非"放宽"：

| 位置 | before | after | 性质 |
|---|---|---|---|
| `policy_spec.py:24` | `CURRENT_STEP = 2` | `= 4` | 事实变更 |
| `policy_spec.py:25` docstring | 「本次交付的步骤号（Step 1–2）」 | 「本次交付的步骤号（Step 4）」 | 同步 |
| `test_resolver_policy.py:49` | `assert CURRENT_STEP == 2` | `assert CURRENT_STEP == 4` | 事实变更 |
| `test_effective.py:233` | `assert ... == 2` | `== 4` | 事实变更；docstring 的「MUST 保持 2」改为「MUST 等于当前交付步骤，由 §C.7 的事实变更同步」 |
| `test_resolver_policy.py:51` | `assert POLICY_CLOSURE_STEP > CURRENT_STEP` | `assert POLICY_CLOSURE_STEP <= WIRED_RETRIEVAL_STEP` | **断言替换（加严）**，见下 |

#### C.7.3 为什么第 5 行不是"放宽"

【推导】`POLICY_CLOSURE_STEP > CURRENT_STEP` 这条断言**没有恒真的内容**：它只是 Step 1–2 那个特定状态（交付 2、闭集预置 3）的偶然产物。一旦交付进度推进到 3 或 4，它必然被迫删改 —— 一条"每次推进都要改"的断言，保护不了任何东西。

【推导】它想表达的真实内容是「**可写的键不能超过已被读取的键**」（否则就是可写但行为不变的静默 no-op，正是 `policy_spec.py:10-11` 的纪律）。这个内容的正确表达是：

```python
assert POLICY_CLOSURE_STEP <= WIRED_RETRIEVAL_STEP     # 3 <= 3 ✔
```

- 现状 `3 <= 3` 成立；
- Step 4 交付后仍成立（本步不扩闭集，§0.2）；
- **将来若有人把闭集扩到 4 而检索没接线 → 立刻红**。这是旧断言完全做不到的。
- 并且它是**双向有效**的：若 `WIRED_RETRIEVAL_STEP` 被误降（有人把检索接线回退），闭集 > 接线 → 也红。

【推导】判断"加严还是放宽"的可执行判据：**把新断言放进"故意制造错误"的用例里，看它是否变红**。§D.7 给三条注入用例（把 `POLICY_CLOSURE_STEP` monkeypatch 成 5 / 2 / 把 `WIRED_RETRIEVAL_STEP` 改成 2），断言新断言在三种情形下的红绿，其中"闭集 5、接线 3"必须红 —— 旧断言在该情形下是**绿**的，这直接证明新断言更强。

#### C.7.4 若你决定**不改**（备选）

【推导】唯一站得住的理由是"`CURRENT_STEP` 已被当作历史标记使用"。那么必须做两件事，否则不如改：
1. 在 `policy_spec.py` 的 docstring 里改成「历史标记：Step 1–2 交付时的值，**不再随交付推进**」，并把两条断言改成 `assert CURRENT_STEP == 2  # 历史值，见 docstring`；
2. 错误文案里**停止使用** `CURRENT_STEP`（否则它对运维撒谎），改为直接写 `wired_in_step`（`POLICY_FIELDS` 的字段，`policy_spec.py:93`）。
【推导】这个备选的代价大于收益：文案改写点在 `validate_policy` 的 `horizons` 分支（`:346-362`），属已验收逻辑。**推荐"改成 4"。**

---

## D. 测试规格（真库）

### D.1 环境与夹具

#### D.1.1 环境（照做，别自己发明）

```
docker run -d --name scen-pg -p 127.0.0.1:5433:5432 \
  -e POSTGRES_PASSWORD=pass pgvector/pgvector:pg16@sha256:ccc6e83d6e35e931dc7c5def2022729d5a6c370318d099181995567ff1fb4d6b
export SCENARIO_TEST_DB_URL=postgresql+asyncpg://postgres:pass@127.0.0.1:5433/kagent_test
```

【源】迁移注入 MUST 走 `config.attributes["sqlalchemy_url"]` 通道（`test_0028_pg_constraints.py:53-61` 的 `_alembic_config()`，注释 `:56-58` 记录了"只设 `set_main_option` 会被 `env.py` 的兜底覆盖 → 把 `.env` 指向的库降级到 0027"那次未遂事故）。

#### D.1.2 夹具分层

```python
# tests/scenario/pg_helpers.py —— 从 test_0028_pg_constraints.py:42-237 提取
#   _dsn / _alembic_config / _migrate / _ensure_seed_tenant / _tenant / _scenario / _version / _expect_violation
```
【推导】为什么提取而不是复制：那 8 个函数（约 120 行）定义了本项目的真库口径（`_version` 永远给全列、`_expect_violation` 钉约束名）。复制一份 = 两个口径，而口径分叉的第一个后果就是"某一条约束的负向用例在新文件里又开始假绿"。
【推导】`test_0028_pg_constraints.py` 的改动面 = 删 8 个 `def` + 加 1 行 import，**18 条用例的断言逐字不动**。验收证据：改动前后该文件 18 条全绿的输出对比。

```python
# tests/scenario/conftest_step4.py（或用 pytest_plugins 挂进 conftest）
@pytest_asyncio.fixture
async def api(pg_url):                      # pg_url = SCENARIO_TEST_DB_URL（无则 skip）
    engine = create_async_engine(pg_url, poolclass=NullPool)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def _get_db():
        async with Session() as s:
            yield s

    state = {"user": make_admin(tenant=await new_tenant())}
    async def _current_user():
        return state["user"]

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user] = _current_user
    transport = ASGITransport(app=app)      # ★ 不启 lifespan → 不连 Redis / 不跑 T2 扫描
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield Client(client, state)         # state 允许用例切换租户/权限
    app.dependency_overrides.clear()
    await engine.dispose()
```

【推导】三个必须写进实现的细节：

1. **`ASGITransport` 不跑 lifespan**（httpx 的默认行为）。本机没有 Redis、`main.py` 的 lifespan 里有 `check_llm_budget` / `check_capabilities` / `log_capability_banner`（`main.py:119-149`）。跑 lifespan 会让这批用例因为**与场景无关的原因**失败。这一条要在文件头写明，否则下一个人"顺手"改用 `LifespanManager` 就会炸。
2. **每个用例用独立租户**（`uuid4`），提交真实落库、不清理。容器用完即弃；用"外层事务回滚"会与 handler 内部的 `commit()` 冲突（那才是这类测试最经典的假绿来源：`SAVEPOINT` 没生效，用例之间互相污染）。
3. **`poolclass=NullPool`**：`asyncio.gather` 的两个并发请求各要一条连接，且 `FOR UPDATE` 会让其中一个等待 —— 连接池若只有 1~2 条，测的就不是锁而是池。默认池 20+20（`settings.py:58-59`）本来够，但显式 `NullPool` 让"每条会话一条连接"这件事不可能被池掩盖。

#### D.1.3 不依赖网络的断言

【推导】本步**不用** LLM / embedding / rerank。集成侧读路径的用例（§D.5）只走到 `resolve_scenario` 为止：用 `resolver.resolve_scenario` 直接调，或对 `/ask` 断言"在进链路之前就 500"（`integrations.py:316-320` 的注释说明场景解析在进链路之前）。

### D.2 约束用例（逐约束名，不是"任意 CheckViolation"）

【推导】每条都是"故意违反 → 断言**具体约束名** + 同用例内正向对照"。纪律照 `test_0028_pg_constraints.py:11-18`。

| # | 用例 | 违反什么 | 断言 `constraint_name` |
|---|---|---|---|
| T4-C1 | W1 用 `cs-bot`（带连字符） | pydantic `pattern` | 422（**不到 DB**；同时断言"库里没有新增行"） |
| T4-C2 | W1 用 `default` | 应用层判等 | 409 `SCENARIO_KEY_RESERVED`；正向：`default2` → 201 |
| T4-C3 | 直接 SQL 插同名 key | `uq_scenarios_tenant_key` | `uq_scenarios_tenant_key`；正向：另一个 key 成功 |
| T4-C4 | 直接 SQL 把某租户非内置场景设为 `is_builtin=true`（当该租户已有内置行） | `uq_scenarios_one_builtin` | `uq_scenarios_one_builtin` |
| T4-C5 | 直接 SQL 写入未接线键 `{"final_top_n":3}` | `ck_scenario_versions_policy_keyset` | 同名；正向：`{"recall_top_k":30}` 成功 |
| T4-C6 | 直接 SQL 写 `recall_top_k=0` / `=1001` | `ck_..._policy_recall_top_k` | 同名；正向：1 与 1000 成功 |
| T4-C7 | 直接 SQL 写阈值 `5.0` / `-0.1`（两个键各一条） | `ck_..._policy_threshold_vector` / `_rerank` | 各自名字；正向：0.62 / 0.71 |
| T4-C8 | 直接 SQL 写 `"rerank_enabled":"true"`（字符串） | `ck_..._policy_rerank_bool` | 同名；正向：JSON `true`/`false` |
| T4-C9 | 直接 SQL 写 `binding_mode='explicit'` + `kb_ids='[]'` | `ck_..._binding_consistent` | 同名；正向：explicit+非空、unrestricted+空 |
| T4-C10 | 直接 SQL 写 `kb_ids='[{"id":"x"}]'` | `ck_..._kb_ids_strings` | 同名 |
| T4-C11 | 直接 SQL 写 `state='published'` 而 `published_at IS NULL` | `ck_..._state_timestamps` | 同名；正向：带 `now()` |
| T4-C12 | 直接 SQL 写 `checksum='ABC'`（大写/非 16 位） | `ck_..._checksum_format` | 同名；正向：16 位小写 hex |
| T4-C13 | 直接 SQL 写 `dna_declared` 缺 `chunker_version` | `ck_..._dna_required_keys` | 同名；正向：`build_dna_declared()` 的产物 |
| T4-C14 | 直接 SQL 写 `dna_declared` 多一个 `extra` 键 | `ck_..._dna_keyset` | 同名；正向：`build_dna_declared()` 的产物 |
| T4-C15 | 同场景两个 published | `uq_scenario_versions_one_published` | 同名；正向：同场景多个 draft |
| T4-C16 | 有应用引用时 `DELETE FROM scenarios` | `fk_integration_app_scenario` | 同名；正向：无引用可删 |
| T4-C17 | 删场景（无应用引用） | `scenario_versions.scenario_id` CASCADE | **不报错**，断言版本行数归零（CASCADE 生效） |

【推导】T4-C5~C14 中"直接 SQL 违反"的用例**不是重复劳动**：它们证明的是"应用层校验被绕过时，库仍然拦得住" —— 而应用层用例（§D.3）证明的是"正常路径根本走不到那里"。两者拦不同的东西，缺任一都是单侧证据。

### D.3 状态跃迁用例（每条一个，含非法跃迁）

| # | 起点 | 动作 | 期望 | 库里最终 state |
|---|---|---|---|---|
| T4-S1 | 无版本 | W2 | 201，version=1，state=draft | draft |
| T4-S2 | 再 W2 | W2 | 201，version=2 | draft,draft |
| T4-S3 | draft v2 | W4 | 200 changed=true | validated |
| T4-S4 | **draft v1** | **W5 直接发布** | **409 STATE_CONFLICT**（题设要求） | draft |
| T4-S5 | validated v2 | W5 | 200 changed=true，`archived_previous_version=1`（若 v1 曾发布） | archived(v1), published(v2) |
| T4-S6 | published v2 | W5 重放 | 200 changed=false | 不变 |
| T4-S7 | published v2 | W6 | 200 changed=true | archived |
| T4-S8 | published v2 | W3（改内容） | 409 STATE_CONFLICT（已发布的不可改） | 不变 |
| T4-S9 | validated v2 | W3 | 409 STATE_CONFLICT（内容已冻结） | 不变 |
| T4-S10 | archived v2 | W6 重放 | 200 changed=false | 不变 |
| T4-S11 | archived v2 | W5 | 409 STATE_CONFLICT（终态，无回退） | 不变 |
| T4-S12 | draft v1 | W6 归档 | 409 STATE_CONFLICT（草稿不提供归档） | 不变 |
| T4-S13 | 内置场景 | W2 | 409 BUILTIN_IMMUTABLE | 无新行 |
| T4-S14 | 内置场景 published v1 | W6 | 409 BUILTIN_IMMUTABLE | 仍 published |
| T4-S15 | 无版本场景 | W7 绑定 | 400/409（**无 published 不可绑**） | 绑定未变 |
| T4-S16 | published v2 | W6（有 1 个应用绑定） | 409 IN_USE，`context.application_count=1` | 不变 |
| T4-S17 | 同上，先 W7 解绑再 W6 | W7 → W6 | 200 → 200 | archived |

【推导】每条用例的最后一步都是**用独立会话回读库**（不走 API），断言 `state` / 时间戳 / `archived_previous_version`。只用响应体断言是循环论证：响应体是 handler 自己算的。

### D.4 并发用例

| # | 场景 | 断言 |
|---|---|---|
| T4-P1 | `gather` 两个 W2 | 两个 version 号**不同且连续**；`count(distinct version) == 2` |
| T4-P2 | `gather` 两个 W3（同 draft、同 `If-Match`） | 一个 200、一个 409 `CHECKSUM_MISMATCH`；库里的 checksum ∈ {两个请求各自应得的值}，且 `policy` 与其中一个请求体**逐键相等**（不是混合） |
| T4-P3 | `gather` 两个 W5（同 validated、同 `If-Match`） | 两个都 200，`changed` 一 true 一 false；`SELECT count(*) WHERE state='published'` == 1 |
| T4-P4 | `gather` 两个 W5（不同 validated 版本） | 两个 200；最终恰好 1 published；另一个 `state='archived'` 且 `archived_at IS NOT NULL` |
| T4-P5 | 手动注入：把场景锁去掉（monkeypatch `_SQL_LOCK_SCENARIO` 为不加 `FOR UPDATE` 的版本）后重跑 T4-P3 | **必须出现 409 PUBLISH_CONFLICT 或仍只有 1 published** —— 这条证明第 3 道（DB 唯一索引）真的在工作 |

【推导】T4-P5 是唯一"故意破坏实现"的用例，价值在于：它把"锁失效时会不会静默出两个 published"从**推理**变成**观测**。没有它，§B.5.3 的第 3 道就只是一句注释。

### D.5 读路径影响用例（归档 / 绑定 / 解绑）

| # | 步骤 | 断言 |
|---|---|---|
| T4-R1 | 绑定 ct_scene(v3 published) → `/integrations/ask`（用假检索桩） | `meta.scenario_key=="ct_scene"`、`meta.scenario_version==3` |
| T4-R2 | W7 解绑 → 再 ask | `scenario_key=="default"`、`scenario_version==1`、`source=="builtin_row"` |
| T4-R3 | （可选）删掉内置行 + 解绑 → ask | `scenario_version==0`、`source=="builtin_fallback"`、**行为字段**与 R2 逐字段相等；`fingerprint` 与 R2 **MUST 不同**（见 §C.4.3 该行注） |
| T4-R4 | 直写 SQL 把 published 改成 archived（绕过 W6）→ ask | **500** + `detail=="场景配置不可解析"`；日志有一条 WARN 含 `reason=scenario_not_found` |
| T4-R5 | 归档后 `resolver._load_published` 直接调 | 返回 `None`（不是抛错、不是返回旧行） |
| T4-R6 | W5 发布新版本后（同一进程内）立刻 ask | 读到**新**版本号（证明 `invalidate` 在提交后被调用）；去掉 `invalidate` 调用则读到**旧**版本号（反向对照） |

【推导】T4-R6 的反向对照是关键：只断言"最终一致"无法区分"`invalidate` 生效"与"TTL 恰好过期"。做法：把 `CACHE_TTL_SECONDS` monkeypatch 成 3600，然后同一进程内连续读。

### D.6 审计与跨租户用例

| # | 用例 | 断言 |
|---|---|---|
| T4-A1 | W1~W7 各一次 | `audit_logs` 里 7 条 `action` 齐全，`detail` 含 §B.7 的字段；`trace_id` 非空 |
| T4-A2 | W3 改 policy | `detail.changed_keys == ["relevance_threshold_vector"]`（对称差） |
| T4-A3 | 让审计写入失败（monkeypatch `record_with_session` 抛错） | 主变更**随之回滚**（`state` 未变）→ 证明同事务 |
| T4-X1 | A 租户对 B 租户的**真实存在**的场景 W5 | 404；B 租户的版本 state **未变** |
| T4-X2 | A 租户对随机 UUID W5 | 404，响应体与 T4-X1 **逐字节相同**（不可枚举） |
| T4-X3 | A 租户 W7 绑定 B 租户的场景 | 404（场景不存在，按 A 的视角） |
| T4-X4 | 校验每条写 SQL 都带 `tenant_id` | 静态断言：`git grep` 式的文本守卫，见下 |

【推导】T4-X4 用**文本守卫**而不是行为断言：把 `write_service.py` 的 SQL 常量全部取出，断言每条含 `scenario_id` 的语句里都出现 `tenant_id = :tenant_id`。这类"少一行就出越权"的 SQL，行为用例覆盖不全（每个语句都要构造跨租户现场），文本守卫是唯一能全量覆盖的方式。**且它是对 SQL 文本断言，不是在断言被测函数自己返回的布尔**（反自证，§D.7）。

### D.7 反自证（防假绿）

【推导】本步最容易被写成假绿的三处，逐条给可执行判据：

1. **约束名断言不许写成"任意 CheckViolation"**：`assert exc.constraint_name == "ck_..."`。判据：把该 CHECK 从迁移里删掉后用例必须**变红**。§D.2 每条都在迁移副本上做一次这个注入（用 `op.drop_constraint` 在临时 schema 上，或直接用 `ALTER TABLE ... DROP CONSTRAINT` 在事务内 + 回滚）。
2. **`CURRENT_STEP` 的新断言（§C.7.3）不许写成 `assert True` 式**：用 monkeypatch 注入三种错误：
   - `POLICY_CLOSURE_STEP=5` 而 `WIRED_RETRIEVAL_STEP=3` → 新断言**红**（旧断言绿）
   - `POLICY_CLOSURE_STEP=2`、`WIRED_RETRIEVAL_STEP=3` → 新断言**绿**（合法：闭集小于接线是允许的）
   - `WIRED_RETRIEVAL_STEP=2`、`POLICY_CLOSURE_STEP=3` → 新断言**红**（检索接线被回退）
   三格的**预期红绿写进用例的 docstring**，跑完对照。
3. **状态机断言不许用被测代码的常量做期望**：期望状态一律写字面量 `"published"`，**不写** `ScenarioState.PUBLISHED`。判据：把枚举值改掉，用例应变红（若跟着变绿就是自证）。

【推导】另外两条纪律：
- **禁止 `assert resp.status_code == 200` 了事**：所有用例至少再断一个"库里的状态"或"响应体的具体字段"。
- **禁止 skip 变成静默通过**：`pytest.skipif(not SCENARIO_TEST_DB_URL)` 在 `pytest -q` 下会显示 `s`。验收时 MUST 给出 `--collect-only` 的条目数与 "0 skipped" 的证据（`SCENARIO_TEST_DB_URL` 未设时的运行**不算验收**）。

### D.8 必须逐条保持不变的既有测试

【推导】给出"没被放宽"的可核对清单：

| 文件 | 条数 | Step 4 允许的改动 |
|---|---|---|
| `tests/scenario/test_0028_pg_constraints.py` | 18 | 仅提取 helper（断言逐字不动） |
| `tests/scenario/test_resolver_*.py` | 48 | 仅 `test_closure_step_semantics_are_explicit` 的两行（§C.7.2） |
| `tests/scenario/test_effective.py` | 20 | 仅 `:233` 一行 |
| `tests/scenario/test_policy_wiring.py` | 18 | 无 |
| `tests/scenario/test_self_check.py` | 49 | 无（**T4 包装放在新文件 `step4_check.py`，不动 `self_check.py`**） |
| `tests/scenario/test_scenario_wiring.py` | 16 | 无 |
| `tests/scenario/test_0028_migration.py` / `test_0028_seed.py` | 20 | 无 |
| `tests/test_integrations.py` | 12 | 允许追加（新 handler），既有 12 条断言不动 |

---

## E. 遗留问题与未知区

| # | 项 | 为什么现在定不了 | 影响 | 建议 |
|---|---|---|---|---|
| **U4-1** | `build_dna_declared()` 与迁移 `_SEED_DNA` 的形状一致性无自检 | 加 S-4c 要动 `self_check.py`（已验收） | 两种形状漂移后 `ck_..._dna_keyset` 不会报 | Step 5 加 T1 判据 `set(build_dna_declared()) == set(_SEED_DNA)` |
| **U4-2** | 多副本下 `invalidate` 只作用于本进程 | `resolver.py:43-46` 已注明需 Redis 频道 | 发布后最长 60s（`CACHE_TTL_SECONDS`）不一致 | 本步在响应里不承诺"已生效"；Step 5 引 Redis pub/sub |
| **U4-3** | 版本行缺 `created_by` 列 | 迁移已冻结，加列要 0029 | 只能靠 `audit_logs.object_id` 反查"谁建的" | 0029 一并加（与闭集扩容同批） |
| **U4-4** | `If-Match` 与 HTTP `ETag` 语义不同（不含 version/state） | 改 `checksum` 的输入集会破坏与迁移的逐字节一致性（`snapshot.py:297-300`） | 客户端若按 ETag 理解会困惑 | 写进 OpenAPI 描述 + 本文 §C.2.1 |
| **U4-5** | 条件失败用 409 而非 412 | 项目内既无 412 先例 | 与网关/WAF 的默认语义可能不一致 | 若要改，一次性统一（含 `integrations`） |
| **U4-6** | 废弃 draft 行永久堆积（不提供删除/归档） | 删除的审计语义未定 | 长期垃圾行 | Step 7 治理面：加"废弃草稿"状态或定时清理 |
| **U4-7** | 无场景改名 / 改描述 / 停用接口 | 会引入新的失败面（停用 → 绑定应用 400） | 运维改描述只能写 SQL | Step 7 |
| **U4-8** | 归档后 `scenarios.status` 不变（仍 `active`） | 语义未定：`archived` 是版本态，`status` 是场景态 | 一个"没有任何 published"的场景在列表里仍显示 `active` | 建议列表页同时展示 `published: null`，由 UI 表达"未发布" |
| **U4-9** | `needs_reindex` / 多 DNA 过滤未覆盖 | 框架稿 §7 把这三件算作 Step 4（`:799`），本文件只做了载体 | 单 DNA 部署无影响 | **需你拍板**：拆 Step 4A/4B（DQ4-1） |
| **U4-10** | `scenario_versions.state` 的 `draft→validated` 是否有实际校验内容 | 本步 W4 只做 `validate_policy` 复算 | 若将来加"DNA 校验"，W4 会变成"变更点" | 与 U4-9 同批定 |
| **U4-11** | 跨租户守卫原为**子串计数**，`OR true` 可穿透（4A 的 `test_every_scenario_sql_is_tenant_scoped` 只查 `"tenant_id" in text.lower()`） | 4A 的写法分不清 `=`、`!=` 与 `OR true`；独立验证实测：把 `write_service.py` 的列表/详情 WHERE 改成 `(x.tenant_id = :tenant_id OR true)` 后 287 条用例全绿 | **已在 4B 前修复**：根因＝守卫判据过弱，非生产 SQL 缺陷（真实 HTTP 逐端点攻击现状全部 404）。修复位置 `tests/scenario/test_step4_guards.py:95`（正则 `\btenant_id\s*=\s*:tenant_id\b` ＋ 负向禁恒真析取）与行为层新增 `tests/scenario/test_step4_tenant_isolation.py`（9 条，真库 + HTTP） | 负向黑名单是**有限枚举**：新加语句若只写 `tenant_id` 字样、语义却是别的短路形式（如 `IS NOT DISTINCT FROM`），仍会漏。后续可改为解析 WHERE 的布尔结构，或改用 `current_setting('app.tenant_id')` + PG RLS 让隔离由库保证 |
| **U4-12** | `write_service.py:195` 的 `strip("W/")` 是**字符集剥离**而非前缀剥离 | RFC 7232 的 `W/` 是**前缀**且大小写敏感；`str.strip("W/")` 会把两端任意个 `W` / `/` 一次剥掉 | 当前**安全**：`checksum` 是纯 hex，不含 `W` 与 `/`，实测 `If-Match` 三态行为正确（含 `W/"..."` 与裸值）。风险只在 `checksum` 格式变更时才成立 | **本步只加一行注释，不改行为**（`write_service.py:195` 上方）。`checksum` 格式一旦变更（换 base64url / 引入 `W`），必须改成前缀判断 `re.sub(r"^W/", "", v)` |
| **U4-13** | 活库约束**无自动化守护** | `tests/scenario/pg_helpers.py:179` 的 `prepare_schema` 每个 module 都 `downgrade 0027 → upgrade 0028`，fixture 会把被删的约束**重建**回来 | 于是「`DROP CONSTRAINT` → 具名用例变红」这条验收**在现架构下不成立**（实测 DROP 后 29 条照样全绿，看上去像假绿，其实是夹具重建了约束）。迁移脚本才是唯一事实来源 | 替代证据：直接 SQL 打三条约束已验证为**唯一拦截者**（`test_0028_pg_constraints.py` 的 18 条断言逐字未动）。若要做活库守护，需另建一套「不重建 schema」的夹具，属独立议题 |
| **U4-14** | 归档后 `scenarios.status` 仍为 `active`，只有 `published_version=None` 可观测 | 归档改的是 `scenario_versions.state`，「场景态」与「版本态」是两个维度（与 U4-8 同源） | UI 若只看 `status` 会误判「场景正常」，而实际已无可服务的口径 | **本步不改**；UI MUST 看 `published_version`（列表已回显 `published: null`）。是否给 `scenarios.status` 一个 `archived` 取值属独立议题 |
| **U4-15** | 12 个测试文件用「整体替换 `get_settings` + 只列部分字段（`SimpleNamespace`）」的假配置 | 本次只迁移**已经变红**的 2 个（`test_graph_side_channel.py` 9 条红、`test_wiki_nav_off_state.py` 4 条红；这两个文件另有 4 条关闭态用例一直是绿的），其余 10 个文件一个字节未动。helper 已就位于 `tests/retrieve/conftest.py::fake_settings()`：以 `Settings.model_fields` 的**类默认值**为底（不用 `get_settings()` 实例，否则测试依赖 `.env`）、校验覆盖键必须存在（防拼错后静默失去覆盖）、每次调用自检 `set(fake().__dict__) == set(Settings.model_fields)`（防 helper 自身腐烂） | `retrieve/`（或任何被整体替换 `get_settings` 的模块）**新增一个配置项读取**，就会让一批"根本没提到该配置"的用例集体 `AttributeError`，且报错落在与改动看似无关的文件里。Step 3 加 `graph_recall_selective` / `wiki_nav_selective` 即是实例：13 条红全部由"替身少一个字段"引起，与图召回 / nav 的逻辑本身无关 | 后续把其余 10 个文件统一迁到 `fake_settings()`（各目录 conftest 薄封装一层即可）。建议在 **Step 5 动连接路由之前**完成 —— 那一步要加/改配置项，正是这条风险最容易兑现的时点 |

---

## 附录 A：偏移登记（与前几份设计/框架稿不一致处，逐条）

| # | 位置 | 前文怎么说 | 本文件怎么做 | 性质与理由 |
|---|---|---|---|---|
| **A-1** | 框架稿 §7 Step 4（`:799`） | Step 4 = **DNA 进场景**（chunk 指纹过滤 + `needs_reindex` + 场景 DNA 发布前校验） | Step 4 = **场景管理写接口 + T4 接线**；DNA 部分不在本文件 | **口径偏移（题设指定）**。这不算错：写接口是框架稿 Step 4 三项交付物的**载体**（没有写接口就没有"发布前校验"这个时点）。但两处事实必须对齐 —— 建议框架稿 §7 的 Step 4 拆 4A/4B，见 DQ4-1 |
| A-2 | 第二阶段 §A.3「C-A2 键闭集」 | 闭集覆盖 4 键（Step 3 范围） | 不改闭集；明确「Step 4 也不扩」 | 补充说明，非冲突 |
| A-3 | 第二阶段 §E.1「`wired_in_step > 2` 的键 Step 1–2 写入会被 CHECK 拒」 | 与 §A.3.1 自相矛盾（已由 `policy_spec.py:30-38` 登记） | 沿用「闭集 = 4 键」的可执行侧；本步再次确认不扩 | 继承既有登记 |
| A-4 | 第三阶段 §C.2（S-2b ~ S-9 时机表） | T4 标注为「Step 4 的写接口调用」 | 本文件把它落到 **W5 一个点**，并给出与 DB 索引的先后 | 落地细化 |
| A-5 | 题设「`uq_..._one_published` 在归档后是否释放」 | 未定 | **释放**（部分索引 `WHERE state='published'`） | 推导 + 源核实 |
| A-6 | 题设「归档后读路径看到什么」 | 未定 | **500 `SCENARIO_UNRESOLVABLE`**，且 `meta` 不存在 | 推演到 `resolver.py:88` → `snapshot.py:384` 逐跳 |
| A-7 | `errors.py` 三码 | 场景解析的对外错误码 | **不动**，新增 `write_errors.py` 11 码 | 受众/文案策略不同（§A.4 末） |
| A-8 | `checksum` 语义 | 只作为"版本行内容指纹" | 明确它**不含 version/state**，因此不能单独定位行，`If-Match` 必须与 path 的 version 联合 | 源核实（`snapshot.py:291-310`）+ 推导 |
| A-9 | `CURRENT_STEP=2` | 两处测试硬断言 2 | 改为 4（事实变更）+ 一条无内容断言替换为 `CLOSURE <= WIRED_RETRIEVAL` | §C.7 全文 |
| A-10 | `IntegrationApplicationOut` | 7 字段 | 追加 `scenario_id`（默认 `None`） | 否则 W7 的结果不可观测；既有 12 条测试无整型 payload 断言（已核） |
| A-11 | `audit.record` | 既有各接口的通用审计写法 | 写接口 MUST 用 `record_with_session`（同事务） | `audit.record` 自开会话且吞异常（`audit.py:39-54`），会造成"主事务回滚但审计已提交" |
| A-12 | `test_0028_pg_constraints.py` 内的夹具 | 定义在该文件 `if SCENARIO_TEST_DB_URL:` 块内 | 提取到 `pg_helpers.py` | 避免两份真库口径；断言逐字不动 |

---

## 附录 B：源核实 / 设计推导 分栏

| 类别 | 条目 | 出处 |
|---|---|---|
| **【源】读到的** | 六条 scenarios CHECK + 两个唯一索引 | `0028:131-149` |
| 【源】 | 十四条 scenario_versions CHECK + 两个部分唯一索引 + 一个普通索引 | `0028:181-271` |
| 【源】 | `integration_applications.scenario_id` FK RESTRICT + 索引 | `0028:275-291` |
| 【源】 | 种子行 = 每租户 1 行内置 + 1 行 published v1 | `0028:294-322` |
| 【源】 | `checksum` 的规范序列化只含四字段 | `snapshot.py:291-310`、`0028:72-96` |
| 【源】 | R1 在 `scenario_id is not None and row is None` → 500 | `snapshot.py:384-392` |
| 【源】 | `unrestricted` 且非内置 → 500 | `snapshot.py:426-440` |
| 【源】 | `LOOKUP_SQL` 的 `v.state='published'` 与双租户过滤 | `resolver.py:71-93` |
| 【源】 | `invalidate` 要求提交后调用 | `resolver.py:111-117` |
| 【源】 | T4 的两条判据与返回 dict | `self_check.py:634-660` |
| 【源】 | T4 无调用方 | `self_check.py:639-640` |
| 【源】 | 三个步骤号并存 + 两处 `==2` 断言 + `CLOSURE > CURRENT` 断言 | `policy_spec.py:24/27`、`effective.py:47`、`test_resolver_policy.py:42-56`、`test_effective.py:228-233` |
| 【源】 | `BUILTIN_SCENARIO_KEY` 与「应用层 MUST 拒绝」 | `snapshot.py:35-36` |
| 【源】 | 审计两套 API（自开会话+吞异常 / 同事务） | `audit.py:29-54`、`:59-…` |
| 【源】 | 跨租户 → 404 的既有口径 | `integrations.py:181-183`；凭证侧合并为 401 `:87-93` |
| 【源】 | kb 归属校验的既有做法 | `integrations.py:109-118` |
| 【源】 | 全局 `ValueError` → 400 会吞掉 `field` | `main.py:362-371` |
| 【源】 | 真库夹具与"断言约束名"纪律 | `test_0028_pg_constraints.py:11-18`、`:42-237`、`:222-231` |
| 【源】 | alembic 注入必须走 `attributes` 通道 | `test_0028_pg_constraints.py:53-61` |
| 【源】 | 框架稿 §7 的 Step 4 = DNA 进场景 | `框架设计-v1.md:799`、`:818` |
| **【推导】我判的** | 状态机线性无回退、草稿不提供归档 | §C.1 |
| 【推导】 | 锁顺序 `scenarios → scenario_versions`，全局唯一 | §B.3.1 |
| 【推导】 | 发布必须"先归档旧、再发布新"（部分唯一索引非 deferrable） | §C.3 |
| 【推导】 | 归档有引用 → 409 IN_USE，否则绑定应用全 500 | §C.4.2 |
| 【推导】 | 绑定时必须有 published，否则绑定即制造 500 | §A.3 W7 |
| 【推导】 | 内容更新必须是整体替换（JSONB 无法区分 null 与缺键） | §A.3 W3 |
| 【推导】 | `binding_mode` / `dna_declared` 不进请求体（不留入口 > 加校验） | §A.3 W2、§A.5.1 |
| 【推导】 | W3 用 409、W4–W6 用 `changed:false` 的幂等语义 | §C.2.4 |
| 【推导】 | 缺 `If-Match` → 428，不匹配 → 409 | §C.2.3 |
| 【推导】 | 闭集不扩到 Step 4 的 5 个键（会被 S-1 判为静默 no-op） | §0.2 |
| 【推导】 | `CURRENT_STEP` → 4，且替换一条无内容断言 | §C.7 |
| 【推导】 | 写接口审计用 `record_with_session` | §B.7、A-11 |
| 【推导】 | 真库夹具：独立租户 + `NullPool` + 不跑 lifespan | §D.1.2 |
| 【推导】 | 并发用例必须两个独立 session；并加 T4-P5 破坏性对照 | §C.6、§D.4 |
| **本机限制** | 无 pytest / 无 PG（据题设"真 PG 已验证可用"指另一环境）；本节所有"已核"均指**读代码**核实，未执行 | —— |

---

## 附录 C：需你拍板的决策点（推荐值 / 需你定）

| # | 决策 | 推荐值 | 备选 | 不定的后果 |
|---|---|---|---|---|
| **DQ4-1** | 框架稿 §7 的 Step 4 与本文件的 Step 4 口径冲突（A-1） | **拆 4A/4B**：4A = 本文件（写接口 + T4），4B = DNA 进场景（`needs_reindex` + 检索期过滤）；框架稿 §7 同步改一行 | ① 把本文件并入 Step 4 原定义（则本步验收要加 DNA 三项，工期翻倍）；② 不改框架稿，靠本文件附录 A-1 的说明兜住（下一个人会再问一遍） | 交付范围与验收标准对不上 |
| **DQ4-2** | `CURRENT_STEP` 改不改（§C.7） | **改成 4**（两处断言做事实变更 + 一条替换） | 保持 2（则必须改 docstring 说明它是历史值，并停止在文案里用它） | 错误文案永远说"自 Step 2 起可配置" |
| **DQ4-3** | 发布时旧 published 的处理 | **同一事务自动归档**，响应回 `archived_previous_version` | 要求先显式 W6 再 W5（两步，运维易忘 → 每次发布都 409） | 最常见路径每次都要两次往返 |
| **DQ4-4** | 条件失败码 | **409**（缺头 428） | 412（RFC 语义更标准，但项目内无先例） | 客户端实现要猜 |
| **DQ4-5** | 真库夹具提取（A-12） | **提取到 `pg_helpers.py`**，`test_0028_pg_constraints.py` 只改 import | 复制 120 行到新文件（零风险，两套口径） | 两份口径，第二条约束用例开始假绿 |
| **DQ4-6** | 内容更新语义 | **`PUT` 整体替换** | `PATCH` 键级合并 + `null` 当删除哨兵（必撞 `::int` 转换） | checksum 语义不确定，If-Match 失效 |
| **DQ4-7** | 是否现在就加 `scenario_versions.created_by`（U4-3） | **不加**（本步不加迁移），用审计反查 | 加 0029（则要重跑迁移往返验收） | 版本列表看不到"谁建的" |
