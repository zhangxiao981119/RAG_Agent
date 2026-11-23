# 第二阶段 Step 1–2 详细设计（字段级）

> **本文件定位**：`02-design/统一RAG平台-场景化配置-框架设计-v1.md`（下称「框架稿」）的**下游施工图**。
> 框架稿回答「做不做、分几层、为什么」；本文件回答「**具体建哪些列、写什么代码、怎么验**」。
>
> **不做的事（边界）**：
> - 不覆盖 Step 3–7（检索策略接线 / 契约 meta 落地 / 图与 wiki 场景化 / 分库 / 灰度收口）。本文件只在必要处标注「该字段的生效步骤」。
> - **不修改 `code/` 下任何文件**。所有代码块是施工图，不是已落盘实现。
> - 不调用任何云端模型 API。
> - 不做排期估算。
>
> **上游裁决输入**：框架稿 §8.1 裁决记录（2026-11-20）。其中
> - **Q1 = ③（用户确认）**：场景 = 一组「检索策略 + 知识集合」；agent 身份由凭证表达；多 agent 可复用同一场景。→ 决定本文件的表结构（`scenario` 是独立对象，**没有 agent 表**）。
> - **Q11 = A（用户确认）**：一期直接分到 `database`（同实例、不同库）。→ 决定 `db_binding` 列的形态，且 schema 过渡方案作废。
> - 其余 Q 项「按推荐暂定」，本文件按推荐值施工。
>
> **一切现状值来自源码，已逐项登记在 §0.5 取值台账**，不猜、不凭记忆。凡是无法在字段级表达的约束，在 §A.6 / §C.5 / §G 显式声明并给替代方案 —— 不静默改设计。

---

## 0. 施工边界与验收基数

### 0.1 Step 1 / Step 2 各自交付什么

| | Step 1：单一 DNA + 场景表 | Step 2：Resolver |
|---|---|---|
| 一句话 | 把「场景」这个概念**落到表里**，并让默认场景恒等于当前平台口径 | 让「一次请求如何解析出场景」这件事**变成一个纯函数 + 一张真值表** |
| 交付物 | 迁移 `0028`：`scenarios` / `scenario_versions` 两张表、`integration_applications.scenario_id` 一列、每租户一行内置场景种子 | `app/services/scenario/` 四个模块：`policy_spec.py` / `snapshot.py` / `resolver.py` / `errors.py` |
| 读路径是否变化 | **完全不读**（表建好但无人查询） | 读：在 `/integrations/search`、`/integrations/ask` 的入口插入一次解析 |
| 检索行为是否变化 | 不变 | 不变（resolver 只做门禁与口径快照，**不接线 policy**；接线是 Step 3） |
| 失败态 | 迁移失败可整条回滚（`downgrade()` 完整） | 解析失败 fail-closed：400/500，**绝不静默退回默认场景** |
| 回滚动作 | `alembic downgrade 0027` → 两表一列消失，数据面无损 | 移除两个调用点 → 行为回到 Step 1 |

### 0.2 零影响承诺（四条可验证断言，全部进 §F 测试）

| # | 断言 | 验证手段 |
|---|---|---|
| Z-1 | 迁移前后，`chunks` / `documents` / `graph_*` / `wiki_pages` **行数与内容逐字节不变** | `SELECT count(*)` + `md5(string_agg(...))` 前后对比 |
| Z-2 | 未绑定场景（`scenario_id IS NULL`）的应用，解析出的 `IndexProfile` 与改动前 `current_index_profile()` **逐字段相等** | `test_resolve_default_returns_platform_dna` |
| Z-3 | 未绑定场景的应用，`kb_ids` 三重交集结果与改动前完全一致 | `test_effective_kb_ids_unchanged`（对照 `chat.py:243-255` 原实现） |
| Z-4 | MCP 工具签名、必填性、响应既有字段**零变化** | `test_mcp_contract_backward_compat`（§D.5） |

### 0.3 施工文件清单（Step 1 + Step 2 全量）

| 动作 | 路径 | 步骤 |
|---|---|---|
| 新增 | `code/apps/api/alembic/versions/0028_add_scenarios.py` | S1 |
| 新增 | `code/apps/api/app/models/entities.py` 追加 `Scenario` / `ScenarioVersion` 两个 ORM 类 | S1 |
| 修改 | `code/apps/api/app/models/entities.py` 的 `IntegrationApplication` 增加 `scenario_id` 映射 | S1 |
| 新增 | `code/apps/api/app/services/scenario/__init__.py` | S2 |
| 新增 | `code/apps/api/app/services/scenario/policy_spec.py` | S2 |
| 新增 | `code/apps/api/app/services/scenario/snapshot.py` | S2 |
| 新增 | `code/apps/api/app/services/scenario/errors.py` | S2 |
| 新增 | `code/apps/api/app/services/scenario/resolver.py` | S2 |
| 修改 | `code/apps/api/app/api/integrations.py`（两个 handler 各插 3 行） | S2 |
| 新增 | `code/apps/api/tests/scenario/test_default_scenario_dna.py` 等 8 个测试文件 | S1+S2 |
| **不动** | `code/apps/mcp/main.py`、`app/config/decisions.py`、`app/config/settings.py` | — |

> **`decisions.py` 为什么不动**：它是「手册 §3 → 代码投影」，改动须走手册流程（`decisions.py:3-4`）。场景化的所有新常量放在 `scenario/policy_spec.py`，与 `decisions.py` 形成「平台口径 / 场景可调」两层，不污染既有单一真源。

### 0.4 术语对齐（英文词首次出现给中文）

| 英文 | 中文 | 在本文件的具体含义 |
|---|---|---|
| `scenario` | 场景 | 一组「检索策略 + 知识集合」的命名绑定 |
| `DNA` | 口径基因 | 一变就必须重算派生数据的参数。**锁在 L0 平台层**，场景不可改 |
| `Policy` | 策略 | 改了不影响已落库派生数据的参数。场景可覆盖 |
| `Binding` | 绑定 | 知识集合范围 + 库地址 |
| `snapshot` | 口径快照 | 一次请求解析出的不可变值对象 |
| `resolution` | 解析 | 由（凭证, 请求）推出 snapshot 的过程 |
| `fail-closed` | 失败即拒绝 | 宁可报错，不可静默降级到更宽的范围 |
| `sparse override` | 稀疏覆盖 | 只存显式改过的键；缺省键 = 跟随平台 |
| `checksum` | 内容校验和 | 版本行内容的规范序列化哈希，用于乐观并发 |
| `builtin` | 内置 | 由迁移创建、不允许编辑的场景行 |

### 0.5 现状值取值台账（**全部来自源码，file:line 可核对**）

> 这张表是 §A.5 种子行的**唯一取值依据**。凡进种子行的值必须在此有出处；凡无出处的字段**不进种子行**（见 §A.6 障碍 1）。

| 值 | 实际值 | 出处（仓库相对路径:行） | 性质 |
|---|---|---|---|
| `embedding_model` | `"bge-m3"` | `code/apps/api/app/config/settings.py:79` | **环境绑定**（默认值，可被 env 覆盖） |
| `embedding_dim` | `1024` | `code/apps/api/app/config/settings.py:80` | 环境绑定 |
| `EMBEDDING_TEXT_VERSION` | `"etext-v1"` | `code/apps/api/app/config/index_profile.py:37` | **代码常量** |
| `chunker_version` | `"c400-800-60"` | 派生自 `index_profile.py:45-51`，取自 `decisions.py:439/440/441`（400 / 800 / 60） | **代码常量派生** |
| `llm_model` | `"deepseek-v4-flash"` | `code/apps/api/app/config/settings.py:77` | 环境绑定 |
| `graph_extractor_model` | `""`（空串） | `code/apps/api/app/config/settings.py:163` | 环境绑定；空串语义见下 |
| graph 有效抽取模型 | 空串 → `settings.llm_model` = `"deepseek-v4-flash"` | 解析在 `graph_profile.py:67` | 派生 |
| `GRAPH_PROMPT_VERSION` | `"gprompt-v1"` | `code/apps/api/app/config/graph_profile.py:30` | **代码常量** |
| `wiki_generator_model` | `""`（空串） | `code/apps/api/app/config/settings.py:189` | 环境绑定 |
| wiki 有效生成模型 | 空串 → `settings.llm_model` = `"deepseek-v4-flash"` | 解析在 `wiki_profile.py:46` | 派生 |
| `WIKI_PROMPT_VERSION` | `"wprompt-v1"` | `code/apps/api/app/config/wiki_profile.py:21` | **代码常量** |
| `CONTRACT_VERSION` | `"v1.1"` | `code/apps/api/app/config/decisions.py:20` | 代码常量 |
| `TOP_K_RECALL` | `50` | `code/apps/api/app/config/decisions.py:176` | 代码常量 |
| `TOP_K_RERANK` | `8` | `code/apps/api/app/config/decisions.py:177` | 代码常量 |
| `RRF_K` | `60` | `code/apps/api/app/config/decisions.py:178` | 代码常量 |
| `RELEVANCE_THRESHOLD_VECTOR` | `0.50` | `code/apps/api/app/config/decisions.py:34` | 代码常量（已标定，样本 30 条） |
| `RELEVANCE_THRESHOLD_RERANK` | `0.50` | `code/apps/api/app/config/decisions.py:37` | 代码常量（**未标定的占位值**） |
| `rerank_enabled` | `False` | `code/apps/api/app/config/settings.py:92` | 环境绑定 |
| `KEYWORD_TSQUERY_ENABLED` | `True` | `code/apps/api/app/config/decisions.py:56` | 代码常量 |
| `ALIAS_EXPANSION_ENABLED` | `True` | `code/apps/api/app/config/decisions.py:80` | 代码常量 |
| `ILIKE_MAX_TERMS` | `3` | `code/apps/api/app/config/decisions.py:73` | 代码常量 |
| `graph_enabled` | `False` | `code/apps/api/app/config/settings.py:153` | 环境绑定 |
| `graph_recall_selective` | `False` | `code/apps/api/app/config/settings.py:157` | 环境绑定 |
| `wiki_enabled` | `False` | `code/apps/api/app/config/settings.py:179` | 环境绑定 |
| `wiki_nav_selective` | `False` | `code/apps/api/app/config/settings.py:183` | 环境绑定 |
| `GRAPH_RETRIEVE_TOP_K` | `10` | `code/apps/api/app/config/decisions.py:912` | 代码常量 |
| PG 版本 / uuid 函数 | `pgvector/pgvector:pg16`，`gen_random_uuid()` 为 PG13+ 内置 | `code/docker-compose.yml:6`；`alembic/versions/0001_init_schema.py:20` | 平台事实 |
| 最新迁移 | `0027_add_integrations`（`down_revision="0026"`） | `code/apps/api/alembic/versions/0027_add_integrations.py:14-15` | 迁移链事实 |
| 集成模式现状 | `kb_ids` 必填；三重交集在 `_integration_kb_ids` | `code/apps/api/app/api/chat.py:243-255` | 行为事实 |
| 集成检索现状 | `retrieve(session, query, tenant_id, effective_kb_ids, clearance, subjects)` | `code/apps/api/app/api/integrations.py:297-304` | 行为事实 |

**关于两个 `""` 空串的 fallback 规则（不是猜测，是源码语义）**：
`graph_profile.py:61-65` 明确「`GRAPH_EXTRACTOR_MODEL` 留空时解析为 `settings.llm_model` 的**实际值**；落库必须是具体模型名，不能落空串」。所以
- **落库（stamp 到 `graph_entity_mentions` 等行上）** 时必须是解析后的 `"deepseek-v4-flash"`；
- **声明镜像（`dna_declared`）** 里不能写 `"deepseek-v4-flash"` —— 那是**部署环境**的值，写进迁移会导致「换个部署环境迁移内容就不对」。故 `dna_declared` 用 `env_bound` 声明这四项「由环境决定」，见 §A.5。

---

## A. 表设计（字段级）

### A.1 三个结构决策（先定形状，再填列）

| # | 决策 | 备选 | 选定理由（判据：改这个值，已落库的派生数据还成立吗？） |
|---|---|---|---|
| D-A1 | `scenarios` 只存**身份**（key/name/status/is_builtin/db_binding），**内容**（policy/kb_ids/dna）全部在 `scenario_versions` | 单表内联内容 + 版本历史冗余一份 | 单表内联会出现「`scenarios` 的内容」与「已发布版本的内容」两份真源，改一处漏一处。**归一化 = 让漂移不可能发生**，代价只是热路径多一次 join（有进程内缓存，见 §C.4） |
| D-A2 | `policy` 用 **JSONB 稀疏覆盖集**（只存显式改过的键），不建 20 个物理列 | 每个策略参数一列 | ① 策略参数会持续增长（E 表列了 16 个，Step 3 只接 4 个），逐列建表意味着每接线一个参数就要一次 DDL；② 稀疏语义天然表达「缺省 = 跟随平台」，而物理列必须为「未设置」再发明一个哨兵值（`NULL`/`-1`/`0`），历史教训是哨兵值分不清「没设」和「设成 0」；③ `IntegrationApplication.kb_ids` 已是 JSONB 先例（`entities.py:476`） |
| D-A3 | `dna_declared` 存**声明镜像**，但 **resolver 绝不读它**（DNA 只从平台函数读） | resolver 从行读 DNA | I-6：嵌入口径锁在 L0。若 resolver 从行读，一次 `UPDATE scenarios SET dna_declared=...` 就能让某场景偷偷换口径 —— 正是框架稿 §3A 要防的静默劣化。声明镜像的价值在于：**审计**（这个场景创建时口径是什么）+ **漂移检测**（代码 bump 了但迁移没跟） |

### A.2 `scenarios` 表（身份）

| 列 | 类型 | 空 | 默认 | 约束 / 索引 | 为什么需要 |
|---|---|---|---|---|---|
| `id` | `UUID` | NOT NULL | `gen_random_uuid()` | PK | 物理 ID 只在库内使用，**永不进配置**（I-4） |
| `tenant_id` | `UUID` | NOT NULL | — | FK `tenants.id` | 多租户隔离的第一道 |
| `scenario_key` | `TEXT` | NOT NULL | — | `~ '^[a-z][a-z0-9_]{1,63}$'`；`UNIQUE(tenant_id, scenario_key)` | **对外稳定标识**。凭证/运维文书写 key 而不是 UUID，避免「配置里出现物理 ID」 |
| `name` | `TEXT` | NOT NULL | — | `length BETWEEN 1 AND 128` | 人读显示名 |
| `status` | `TEXT` | NOT NULL | `'active'` | `IN ('active','disabled')` | 停用不复用 key（保留审计可追溯） |
| `is_builtin` | `BOOLEAN` | NOT NULL | `false` | 部分唯一索引 `WHERE is_builtin` → **每租户恰好 ≤1** | 内置默认场景：由迁移创建、内容不可编辑（见 §A.6 障碍 3） |
| `db_binding` | `TEXT` | **NULL** | — | `~ '^kb_[a-z0-9_]{2,40}$'` 或 NULL | Q11=A 的落点：场景→数据库地址。**Step 1 只建列不读列**（见下方决策 D-A4） |
| `description` | `TEXT` | NOT NULL | `''` | — | 运维备注：这个场景给谁用 |
| `created_at` | `TIMESTAMPTZ` | NOT NULL | `now()` | — | — |
| `updated_at` | `TIMESTAMPTZ` | NOT NULL | `now()` | — | 缓存失效依据（§C.4） |

**决策 D-A4：`db_binding` 现在建列，但 Step 1–6 都不读。**

- 支持：分库是 Q11=A 已确认的终点；先建列，后续每一步只是「开始读这一列」，不产生第二次 DDL。
- 反对（诚实记录）：这是典型的 YAGNI。一个从未被读的列，会让人误以为「配了就生效」。
- **对冲**：① 列注释与 API 层都标注「**Step 6 起生效，当前写入无效**」；② 启动自检检测「`db_binding IS NOT NULL` 的行数 > 0 且当前步骤 < 6」→ **WARN 并列出**，进能力横幅（沿用 `capabilities.py` 的既有模式：宁可警告也不静默）。这条对冲使 YAGNI 风险可观测。

**为什么把「默认场景」做成行而不是「`scenario_id IS NULL` 即默认」的纯代码分支**（这条要交代清楚，因为它决定 §C 的真值表形状）：

- 选行：`scenario_id` 有 FK、可审计、UI 可列出「你有 3 个场景」，且 Step 4 以后给内置场景加只读展示时不需要再改结构。
- 但 **`scenario_id IS NULL` 仍然必须合法**，且语义 = 内置默认场景：因为 0027 之前的存量应用都是 NULL，回填会引入一次不可逆的数据变更（§A.4）。
- 两者一致性由 §C.3 的断言锁死：`resolve(NULL)` 与 `resolve(内置行)` **字段全等**。

### A.3 `scenario_versions` 表（内容 + 版本）

生命周期：`draft → validated → published → archived`。**同一时刻每场景至多一行 `published`**。

| 列 | 类型 | 空 | 默认 | 约束 / 索引 | 为什么需要 |
|---|---|---|---|---|---|
| `id` | `UUID` | NOT NULL | `gen_random_uuid()` | PK | — |
| `tenant_id` | `UUID` | NOT NULL | — | FK `tenants.id` | 冗余但必要：所有查询带租户条件，避免只靠 join 施加隔离 |
| `scenario_id` | `UUID` | NOT NULL | — | FK `scenarios.id` **ON DELETE CASCADE** | 场景删除时版本一起走 |
| `version` | `INTEGER` | NOT NULL | — | `>= 1`；`UNIQUE(tenant_id, scenario_id, version)` | 单调递增整数，**不用时间戳**（可读、可 diff、可 pin） |
| `state` | `TEXT` | NOT NULL | `'draft'` | `IN ('draft','validated','published','archived')` | 状态机 |
| `policy` | `JSONB` | NOT NULL | `'{}'::jsonb` | 见 §A.3.1 约束组 | 稀疏覆盖集 |
| `kb_ids` | `JSONB` | NOT NULL | `'[]'::jsonb` | `jsonb_typeof='array'`；元素必须 string | 场景声明的知识集合 |
| `binding_mode` | `TEXT` | NOT NULL | `'explicit'` | `IN ('explicit','unrestricted')`；与 `kb_ids` 联动见 §A.3.1 | **fail-closed 的关键列**：`unrestricted` 只允许内置场景使用；`explicit` 必须非空 |
| `dna_declared` | `JSONB` | NOT NULL | — | 键闭集 + 必填键，见 §A.5 | 口径声明镜像 |
| `checksum` | `TEXT` | NOT NULL | — | `~ '^[0-9a-f]{16}$'` | 规范序列化哈希，乐观并发（`If-Match`）用 |
| `change_note` | `TEXT` | NOT NULL | `''` | — | 发布说明。**审计刚需**：半年后要能回答「谁为什么把阈值调到 0.7」 |
| `created_at` | `TIMESTAMPTZ` | NOT NULL | `now()` | — | — |
| `validated_at` | `TIMESTAMPTZ` | NULL | — | `state='validated' ⇒ NOT NULL` | 状态机的边必须有时间戳，否则状态不可审计 |
| `published_at` | `TIMESTAMPTZ` | NULL | — | `state='published' ⇒ NOT NULL` | 同上 |
| `archived_at` | `TIMESTAMPTZ` | NULL | — | `state='archived' ⇒ NOT NULL` | 同上 |

索引：
- `uq_scenario_versions_one_published`：`UNIQUE (scenario_id) WHERE state = 'published'` —— 保证「至多一个已发布」是**数据库事实**，不是应用纪律。
- `idx_scenario_versions_lookup`：`(tenant_id, scenario_id, state, version DESC)` —— 解析热路径。

#### A.3.1 `policy` / `binding_mode` 的数据库级约束组

> Step 1 的键闭集**只含 Step 3 已接线的 4 个键**（`recall_top_k` / `rerank_enabled` / `relevance_threshold_vector` / `relevance_threshold_rerank`）。理由：**未接线的键写进去就是静默 no-op** —— 运维改了阈值、行为没变、日志没有一行。宁可让写入被 CHECK 拒绝（400 带明确 message），也不要制造「配了不生效」。后续每步接线时，该步的迁移扩展这个闭集（一次可预期的 DDL）。

| # | 约束 | SQL | 覆盖什么失败态 |
|---|---|---|---|
| C-A1 | policy 是对象 | `jsonb_typeof(policy) = 'object'` | 存成数组/标量 |
| C-A2 | policy 键闭集 | `policy - ARRAY['recall_top_k','rerank_enabled','relevance_threshold_vector','relevance_threshold_rerank']::text[] = '{}'::jsonb` | 写入未接线键（静默 no-op 的根因） |
| C-A3 | `recall_top_k` 范围 | `(policy->>'recall_top_k')::int BETWEEN 1 AND 1000` | 0 / 负数 / 天文数字 |
| C-A4 | `rerank_enabled` 必须布尔 | `policy->'rerank_enabled' IS NULL OR jsonb_typeof(policy->'rerank_enabled') = 'boolean'` | 存 `"true"` 字符串 |
| C-A5 | 两个阈值范围 | `(policy->>'relevance_threshold_vector')::float8 BETWEEN 0.0 AND 1.0`（rerank 同） | 存 50 / 5.0 |
| C-B1 | `binding_mode` 与 `kb_ids` 联动 | `(binding_mode = 'explicit' AND jsonb_array_length(kb_ids) > 0) OR (binding_mode = 'unrestricted' AND kb_ids = '[]'::jsonb)` | 「忘了绑定知识库」= 静默放开全部范围（fail-open） |
| C-B2 | `kb_ids` 元素全是字符串 | `NOT jsonb_path_exists(kb_ids, '$[*] ? (@.type() != "string")')` | 存成对象数组 |
| C-V1 | 状态→时间戳一致 | `(state <> 'published' OR published_at IS NOT NULL) AND (state <> 'validated' OR validated_at IS NOT NULL) AND (state <> 'archived' OR archived_at IS NOT NULL)` | 状态机被裸 UPDATE 绕过 |

**关于 C-A2 的 NULL 语义**（容易写错，特意说明）：键不存在时 `policy->>'recall_top_k'` 为 `NULL`，`NULL::int` 为 `NULL`，`NULL BETWEEN 1 AND 1000` 为 `NULL`，**CHECK 对 NULL 判定为通过**。这正是稀疏覆盖集要的语义 —— 不设即不校验。

**关于 C-A3/A5 的错误码**（诚实标注）：值不是数字时（如 `"abc"`），`::int` 转换在 CHECK 求值期抛 `22P02 invalid_text_representation`，而不是 `23514 check_violation`。**写仍然被拒绝**（fail-closed 成立），但运维看到的是 SQL 错误码而非「超出范围」。→ **主校验放应用层**（pydantic + `policy_spec.py` 的 range），DB CHECK 只作兜底。这条差异登记为 §G 遗留项 G-4。

### A.4 `integration_applications.scenario_id`（绑定列）

| 项 | 设计 |
|---|---|
| 列 | `scenario_id UUID NULL`，`REFERENCES scenarios(id) ON DELETE RESTRICT` |
| 为什么 NULL 合法 | 存量应用全是 NULL；NULL 的语义**不是「没有场景」而是「内置默认场景」**（§C.3.1 真值表 R2/R5）。这样零影响成立，且不需要回填 |
| 为什么 `ON DELETE RESTRICT` 而不是 CASCADE | 删场景 = 删一批应用的运行配置。必须先在应用层把引用摘掉才允许删。CASCADE 会让「删一个没人看的场景」变成「一批 agent 悄悄退回默认口径」 |
| 索引 | `idx_integration_app_scenario (tenant_id, scenario_id)` |
| **回填策略：不回填** | ① 回填不改变任何行为（NULL 走 R2/R5，绑定内置行后走 R2/R6，而 R5 与 R6 在内置场景下产出同值快照），所以回填零收益；② 回填是**不可逆的数据变更**（`downgrade` 里无法区分「原本就绑了」和「被回填的」）；③ 反过来若回填，任何一次对内置场景行的误改会立刻作用于全部存量应用 —— 让 NULL 当安全默认值更稳 |
| 什么时候必须绑定 | Step 4 起提供「应用创建/编辑接口带 `scenario_key`」；届时新应用写非 NULL，存量保持 NULL。**不设淘汰期限**，因为 NULL 分支不是技术债，是内置场景的合法表达 |

### A.5 默认场景种子行（**取值全部来自 §0.5 台账**）

迁移 `0028` 执行时，对 `tenants` 表里的**每一个租户**插入一组（场景行 + 版本行），`version=1`、`state='published'`、`policy='{}'`。

#### A.5.1 `scenario_versions.dna_declared` 的字面量

```json
{
  "embedding_text_version": "etext-v1",
  "chunker_version": "c400-800-60",
  "graph_prompt_version": "gprompt-v1",
  "wiki_prompt_version": "wprompt-v1",
  "env_bound": [
    "embedding_model",
    "embedding_dim",
    "graph_extractor_model",
    "wiki_generator_model"
  ]
}
```

| 键 | 值 | 出处 | 为什么是这个形式 |
|---|---|---|---|
| `embedding_text_version` | `"etext-v1"` | `index_profile.py:37` | 代码常量，硬编码（同 `0011_add_chunk_index_profile.py:22-24` 的纪律：迁移是历史快照，必须可重放、不随未来配置漂移） |
| `chunker_version` | `"c400-800-60"` | 派生自 `index_profile.py:45-51` + `decisions.py:439/440/441` | 硬编码派生结果。硬编码的**代价**是「改 `CHUNK_*` 常量必须同时写迁移」，**收益**是不会出现「迁移里写 400、代码已跑 800」 |
| `graph_prompt_version` | `"gprompt-v1"` | `graph_profile.py:30` | 代码常量 |
| `wiki_prompt_version` | `"wprompt-v1"` | `wiki_profile.py:21` | 代码常量 |
| `env_bound` | 4 个字段名 | 见 §A.6 障碍 1 | **不能硬编码**：`embedding_model`/`embedding_dim` 来自 `settings.py:79/80`，两个模型名来自 `settings.py:163/189`（且当前为空串、运行时解析为 `settings.llm_model`）。写死等于把某个部署环境的事实写进所有部署的迁移 |

#### A.5.2 `scenarios` 行

| 列 | 值 | 依据 |
|---|---|---|
| `scenario_key` | `'default'` | 保留 key。**应用层 MUST 拒绝**其它场景使用这个 key（虽然 `UNIQUE` 已保证每租户唯一，但需额外拒绝「改名成 default」以保住可读性契约） |
| `name` | `'内置默认场景'` | 硬编码，纯显示 |
| `status` | `'active'` | — |
| `is_builtin` | `true` | — |
| `db_binding` | `NULL` | Step 6 前不读；NULL = 未分库 = 主库 |
| `description` | `'由迁移 0028 创建；policy 为空=完全等同平台默认口径，不可编辑'` | 让运维看到这行就知道为什么它不能改 |

#### A.5.3 `scenario_versions` 行

| 列 | 值 | 依据 |
|---|---|---|
| `version` | `1` | — |
| `state` | `'published'` | — |
| `policy` | `'{}'::jsonb` | **零影响的数学表达**：空覆盖集 ⇒ 解析结果 = 平台默认值 ⇒ Z-2 自动成立 |
| `kb_ids` | `'[]'::jsonb` | 内置场景不额外限制 |
| `binding_mode` | `'unrestricted'` | 语义 = 范围完全由应用白名单决定（**这就是现状**：`chat.py:249-255` 三重交集不含场景因子）。C-B1 保证 `unrestricted` 必须配空数组 |
| `checksum` | `sha256(canonical_json)[:16]` | 见 §A.5.4 |
| `change_note` | `'初始版本：空覆盖集，等价于平台默认'` | — |
| `published_at` | `now()` | C-V1 |

#### A.5.4 `checksum` 的规范序列化（DB 侧无法重算，登记为 §G 遗留项 G-5）

```
payload = {"binding_mode": ..., "dna_declared": ..., "kb_ids": ..., "policy": ...}
canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
checksum  = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
```

迁移里的种子行 checksum 由 **Python 在迁移代码内计算**（不在 SQL 里拼字符串）—— 保证与运行时 `compute_checksum()` 同一实现、同一结果。**验收**：`test_scenario_checksum_matches_runtime` —— 迁移插入的行读取后重算，必须相等。这条测试是「迁移与运行时代码同源」的守门人。

### A.6 字段级表达不了的约束（**诚实声明 + 替代方案**）

| # | 约束 | 为什么 DB 表达不了 | 替代方案（可验证） |
|---|---|---|---|
| **障碍 1** | `dna_declared` 必须精确等于代码里的口径常量 | 迁移是**静态 SQL 快照**，`embedding_model` / `embedding_dim` / 两个模型名来自 `settings.py`，**随部署环境变化**。硬编码 = 把某环境的配置写进所有环境的迁移 | ① 代码常量（4 项）硬编码进迁移；② 环境绑定项（4 项）以 `env_bound` 数组**声明**，由自检按当前 `settings` 解析后比较；③ `test_default_scenario_dna_matches_platform` 展开 `env_bound` 后逐字段比对 8 个值，不等则 **CI 红** |
| **障碍 2** | 「`dna_declared` 必须等于 `current_*_profile()`」是**代码 vs 数据**的一致性 | 数据库不知道代码常量是什么 | ① **CI 测试**（硬门禁，红了不能合）；② **启动自检 WARN**（不 raise —— 让它变成可观测告警而不是停机。理由：DNA bump 是合法变更，停机惩罚错的人）；③ 能力横幅显示「场景口径声明已过期」 |
| **障碍 3** | 「`is_builtin` 场景的已发布版本 `policy` 必须为空」是**跨表约束** | CHECK 不能引用其他表；触发器能但要引入 PL/pgSQL，与项目「逻辑在 Python」的既有风格冲突 | ① **单写入口**：内置场景的写入路径在应用层直接 403；② **启动自检**跑一条巡检 SQL（`JOIN scenarios s ON s.is_builtin JOIN scenario_versions v ON v.state='published' WHERE v.policy <> '{}'`，>0 行则 WARN 并打印租户/场景）；③ 该 SQL 进 `scripts/` 巡检清单（Step 1 只写 SQL，不写脚本，避免超范围） |
| **障碍 4** | CHECK 值类型错误的 SQLSTATE 是 22P02 不是 23514 | PG 的 CHECK 求值会先做类型转换 | 主校验在应用层（pydantic 模型 + `policy_spec.py` 的 `range`），DB CHECK 只兜底（见 §A.3.1 末段） |
| **障碍 5** | `checksum` 无法由 DB 重算 | 规范序列化（含 `sort_keys`、`ensure_ascii=False`、紧凑分隔符）是 Python 行为；`jsonb::text` 的键序/空格不受控 | 迁移内用 Python 计算（§A.5.4）；写入路径统一走 `compute_checksum()`；`test_scenario_checksum_matches_runtime` 锁死 |
| **障碍 6** | 「`scenario_id` 指向的场景必须同租户」跨表 | 同上 | `resolve_scenario()` 读行时**必带 `tenant_id` 条件**；读取为 0 行 ⇒ 500 `SCENARIO_UNRESOLVABLE`（**不静默退回默认**，见 §C.3 反例） |

---

## B. 迁移 `0028`（可直接使用的 Python）

### B.1 文件头与链位置

- 路径：`code/apps/api/alembic/versions/0028_add_scenarios.py`
- `revision = "0028"`，`down_revision = "0027"`（对照 `0027_add_integrations.py:14-15`）
- 依赖 PG ≥ 13（`gen_random_uuid()` 内置）；实际镜像 `pgvector/pgvector:pg16`（`docker-compose.yml:6`）

### B.2 完整代码

```python
"""场景化配置：scenarios / scenario_versions / integration_applications.scenario_id

Revision ID: 0028
Revises: 0027
Create Date: 2026-11-20

★ 本迁移只建结构 + 种子行，不读取任何业务表，不改任何派生数据。
  零影响的数学根据：种子行的 policy 是空覆盖集（'{}'::jsonb），
  解析结果在定义上等于平台默认值，因此「没绑场景的应用」行为逐字段不变。

★ dna_declared 的 4 个代码常量刻意**硬编码**（纪律同 0011_add_chunk_index_profile.py:7-10）：
  迁移是历史快照，必须可重放、不随未来配置漂移。
  4 个环境绑定项（embedding_model / embedding_dim / 两个模型名）**不落值**，
  以 env_bound 声明 —— 否则等于把某个部署环境的配置写进所有部署。
  一致性由 tests/scenario/test_default_scenario_dna.py 锁死：代码 bump 而未改本迁移 ⇒ CI 红。

★ db_binding 列本期建而不用（Step 6 起生效）。写入非 NULL 时由启动自检 WARN，
  避免「配了不生效」的静默失败。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ── 种子行字面量（唯一取值来源：见设计文档 §0.5 取值台账） ──────────────
# 全部为代码常量或其派生结果；环境绑定项以 env_bound 声明，不落值。
_SEED_DNA = {
    # index_profile.py:37
    "embedding_text_version": "etext-v1",
    # index_profile.py:45-51 派生自 decisions.py:439/440/441 → c{400}-{800}-{60}
    "chunker_version": "c400-800-60",
    # graph_profile.py:30
    "graph_prompt_version": "gprompt-v1",
    # wiki_profile.py:21
    "wiki_prompt_version": "wprompt-v1",
    # 由部署环境决定，迁移不落值（见文件头说明）
    "env_bound": [
        "embedding_model",       # settings.py:79（默认 bge-m3）
        "embedding_dim",         # settings.py:80（默认 1024）
        "graph_extractor_model", # settings.py:163（默认空串 → 解析为 llm_model）
        "wiki_generator_model",  # settings.py:189（默认空串 → 解析为 llm_model）
    ],
}

_SEED_POLICY = {}
_SEED_KB_IDS: list[str] = []
_SEED_BINDING_MODE = "unrestricted"

# policy 键闭集（Step 3 已接线的 4 个键；后续步骤的迁移在此扩展）
_POLICY_KEYS = [
    "recall_top_k",
    "rerank_enabled",
    "relevance_threshold_vector",
    "relevance_threshold_rerank",
]


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _compute_checksum(
    *, policy: dict, kb_ids: list[str], binding_mode: str, dna_declared: dict
) -> str:
    """与运行时 compute_checksum() 必须逐字节一致（tests 锁死）。"""
    raw = _canonical(
        {
            "binding_mode": binding_mode,
            "dna_declared": dna_declared,
            "kb_ids": kb_ids,
            "policy": policy,
        }
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


_SEED_CHECKSUM = _compute_checksum(
    policy=_SEED_POLICY,
    kb_ids=_SEED_KB_IDS,
    binding_mode=_SEED_BINDING_MODE,
    dna_declared=_SEED_DNA,
)


def upgrade() -> None:
    # ── 1. scenarios：只存身份 ────────────────────────────────────────
    op.create_table(
        "scenarios",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scenario_key", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("is_builtin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("db_binding", sa.Text()),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.CheckConstraint("status IN ('active','disabled')", name="ck_scenarios_status"),
        sa.CheckConstraint(
            "scenario_key ~ '^[a-z][a-z0-9_]{1,63}$'", name="ck_scenarios_key_format"
        ),
        sa.CheckConstraint("length(name) BETWEEN 1 AND 128", name="ck_scenarios_name_len"),
        sa.CheckConstraint(
            "db_binding IS NULL OR db_binding ~ '^kb_[a-z0-9_]{2,40}$'",
            name="ck_scenarios_db_binding_format",
        ),
        sa.UniqueConstraint("tenant_id", "scenario_key", name="uq_scenarios_tenant_key"),
    )
    # 每租户至多一个内置场景：用部分唯一索引表达（CHECK 无法表达聚合唯一）
    op.create_index(
        "uq_scenarios_one_builtin",
        "scenarios",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("is_builtin"),
    )

    # ── 2. scenario_versions：内容 + 版本 ─────────────────────────────
    op.create_table(
        "scenario_versions",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scenario_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="draft"),
        sa.Column("policy", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("kb_ids", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("binding_mode", sa.Text(), nullable=False, server_default="explicit"),
        sa.Column("dna_declared", postgresql.JSONB(), nullable=False),
        sa.Column("checksum", sa.Text(), nullable=False),
        sa.Column("change_note", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("validated_at", sa.DateTime(timezone=True)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["scenario_id"], ["scenarios.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "state IN ('draft','validated','published','archived')",
            name="ck_scenario_versions_state",
        ),
        sa.CheckConstraint("version >= 1", name="ck_scenario_versions_version_positive"),
        sa.CheckConstraint(
            "binding_mode IN ('explicit','unrestricted')",
            name="ck_scenario_versions_binding_mode",
        ),
        # C-A1 policy 是对象
        sa.CheckConstraint(
            "jsonb_typeof(policy) = 'object'", name="ck_scenario_versions_policy_object"
        ),
        # C-A2 policy 键闭集（未接线的键写进来就是静默 no-op，必须在写入时挡住）
        sa.CheckConstraint(
            "policy - ARRAY[" + ",".join(f"'{k}'" for k in _POLICY_KEYS) + "]::text[] = '{}'::jsonb",
            name="ck_scenario_versions_policy_keyset",
        ),
        # C-A3 / C-A5 范围兜底；主校验在应用层（SQLSTATE 差异见设计文档 §A.3.1）
        sa.CheckConstraint(
            "(policy->>'recall_top_k')::int BETWEEN 1 AND 1000",
            name="ck_scenario_versions_policy_recall_top_k",
        ),
        sa.CheckConstraint(
            "(policy->>'relevance_threshold_vector')::float8 BETWEEN 0.0 AND 1.0",
            name="ck_scenario_versions_policy_threshold_vector",
        ),
        sa.CheckConstraint(
            "(policy->>'relevance_threshold_rerank')::float8 BETWEEN 0.0 AND 1.0",
            name="ck_scenario_versions_policy_threshold_rerank",
        ),
        # C-A4 rerank_enabled 必须布尔
        sa.CheckConstraint(
            "policy->'rerank_enabled' IS NULL "
            "OR jsonb_typeof(policy->'rerank_enabled') = 'boolean'",
            name="ck_scenario_versions_policy_rerank_bool",
        ),
        # C-B1 binding_mode 与 kb_ids 联动（防「忘了绑定」= 静默放开全部范围）
        sa.CheckConstraint(
            "(binding_mode = 'explicit' AND jsonb_array_length(kb_ids) > 0) "
            "OR (binding_mode = 'unrestricted' AND kb_ids = '[]'::jsonb)",
            name="ck_scenario_versions_binding_consistent",
        ),
        # C-B2 kb_ids 元素必须全是字符串
        sa.CheckConstraint(
            "NOT jsonb_path_exists(kb_ids, '$[*] ? (@.type() != \"string\")')",
            name="ck_scenario_versions_kb_ids_strings",
        ),
        # dna_declared 键闭集 + 4 个代码常量必填
        sa.CheckConstraint(
            "jsonb_typeof(dna_declared) = 'object'",
            name="ck_scenario_versions_dna_object",
        ),
        sa.CheckConstraint(
            "dna_declared ? 'embedding_text_version' AND dna_declared ? 'chunker_version' "
            "AND dna_declared ? 'graph_prompt_version' AND dna_declared ? 'wiki_prompt_version' "
            "AND dna_declared ? 'env_bound'",
            name="ck_scenario_versions_dna_required_keys",
        ),
        sa.CheckConstraint(
            "dna_declared - ARRAY['embedding_text_version','chunker_version',"
            "'graph_prompt_version','wiki_prompt_version','env_bound']::text[] = '{}'::jsonb",
            name="ck_scenario_versions_dna_keyset",
        ),
        sa.CheckConstraint(
            "checksum ~ '^[0-9a-f]{16}$'", name="ck_scenario_versions_checksum_format"
        ),
        # C-V1 状态 → 时间戳一致
        sa.CheckConstraint(
            "(state <> 'published' OR published_at IS NOT NULL) "
            "AND (state <> 'validated' OR validated_at IS NOT NULL) "
            "AND (state <> 'archived' OR archived_at IS NOT NULL)",
            name="ck_scenario_versions_state_timestamps",
        ),
        sa.UniqueConstraint(
            "tenant_id", "scenario_id", "version", name="uq_scenario_versions_scenario_version"
        ),
    )
    # 「每场景至多一个已发布版本」= 数据库事实
    op.create_index(
        "uq_scenario_versions_one_published",
        "scenario_versions",
        ["scenario_id"],
        unique=True,
        postgresql_where=sa.text("state = 'published'"),
    )
    op.create_index(
        "idx_scenario_versions_lookup",
        "scenario_versions",
        ["tenant_id", "scenario_id", "state", "version"],
    )

    # ── 3. integration_applications.scenario_id ────────────────────────
    # 不回填：NULL 语义 = 内置默认场景，回填零收益且不可逆（见设计文档 §A.4）
    op.add_column(
        "integration_applications",
        sa.Column("scenario_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_integration_app_scenario",
        "integration_applications",
        "scenarios",
        ["scenario_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "idx_integration_app_scenario",
        "integration_applications",
        ["tenant_id", "scenario_id"],
    )

    # ── 4. 种子行：每租户一条内置场景（0 租户时插入 0 行，安全） ───────
    op.execute(
        sa.text(
            """
            WITH created AS (
                INSERT INTO scenarios
                    (tenant_id, scenario_key, name, status, is_builtin, db_binding, description)
                SELECT t.id, 'default', '内置默认场景', 'active', true, NULL,
                       '由迁移 0028 创建；policy 为空=完全等同平台默认口径，不可编辑'
                FROM tenants t
                ON CONFLICT (tenant_id, scenario_key) DO NOTHING
                RETURNING id, tenant_id
            )
            INSERT INTO scenario_versions
                (tenant_id, scenario_id, version, state,
                 policy, kb_ids, binding_mode, dna_declared, checksum, change_note, published_at)
            SELECT c.tenant_id, c.id, 1, 'published',
                   CAST(:policy AS jsonb), CAST(:kb_ids AS jsonb), :binding_mode,
                   CAST(:dna AS jsonb), :checksum,
                   '初始版本：空覆盖集，等价于平台默认', now()
            FROM created c
            """
        ).bindparams(
            policy=json.dumps(_SEED_POLICY, ensure_ascii=False),
            kb_ids=json.dumps(_SEED_KB_IDS, ensure_ascii=False),
            binding_mode=_SEED_BINDING_MODE,
            dna=json.dumps(_SEED_DNA, ensure_ascii=False, sort_keys=True),
            checksum=_SEED_CHECKSUM,
        )
    )


def downgrade() -> None:
    op.drop_index("idx_integration_app_scenario", table_name="integration_applications")
    op.drop_constraint(
        "fk_integration_app_scenario", "integration_applications", type_="foreignkey"
    )
    op.drop_column("integration_applications", "scenario_id")

    op.drop_index("idx_scenario_versions_lookup", table_name="scenario_versions")
    op.drop_index("uq_scenario_versions_one_published", table_name="scenario_versions")
    op.drop_table("scenario_versions")

    op.drop_index("uq_scenarios_one_builtin", table_name="scenarios")
    op.drop_table("scenarios")
```

### B.3 `downgrade()` 的完备性与零影响证明

| 断言 | 依据 |
|---|---|
| 降级后 `integration_applications` 的列集合与 0027 完全一致 | 只 `drop_column("scenario_id")` + 删同名 FK/索引，没碰其它列 |
| 降级**不会**丢业务数据 | 本迁移只 `CREATE TABLE` / `ADD COLUMN` / `INSERT INTO` 新建表。**没有任何 `UPDATE` 打在既有表上**（对比 `0011` 是在 `chunks` 上做 `UPDATE` + `NOT NULL` 收紧，那才是需要回滚预案的迁移） |
| 种子行随表消失，不留孤儿 | `scenario_versions` 先于 `scenarios` 删除；`scenario_versions.scenario_id` 的 CASCADE 只是保险 |
| 重放安全 | 种子用 `ON CONFLICT DO NOTHING`；`upgrade → downgrade → upgrade` 后 `scenarios` 行数 = 租户数 |

### B.4 迁移执行顺序与失败回滚（DQ2 的**部分**回答，完整版属 Step 6）

- **单库执行**（本期只动主库）：`alembic upgrade 0028` 在同一事务内完成建表 + 种子。PG 的 DDL 事务性保证「要么全成要么全不成」，无中间态。
- **失败排查点**（按出现概率排序）：
  1. 种子 `INSERT` 撞 `uq_scenarios_tenant_key` → 说明该租户已有 `scenario_key='default'` 的场景（手工建过），`ON CONFLICT DO NOTHING` 已兜住，不会失败；
  2. `tenants` 表不存在 / 0027 未跑 → 迁移链错位，`down_revision` 会先报错；
  3. `jsonb_path_exists` 在 PG < 12 不存在 → 环境不符（镜像固定 pg16，属部署错误）。
- **不在本期范围**：多库迁移排序、跨库一致性、备份窗口 —— 归 Step 6，见 §G 遗留项 G-1。

---

## C. Resolver

### C.1 模块清单与依赖方向

```
app/services/scenario/
├── __init__.py        # 只导出 resolve_scenario / ScenarioSnapshot / ScenarioError / COMPONENT
├── policy_spec.py     # Policy 参数规格表（唯一真源，供校验/文档/前端共用）
├── snapshot.py        # ScenarioPolicy / ScenarioSnapshot / build_snapshot（纯函数）
├── errors.py          # ScenarioError + 错误码枚举
└── resolver.py        # resolve_scenario（唯一 impure 入口：一次 DB 读）
```

依赖方向（严格单向，不得反向）：

```
api/integrations.py
      ↓
services/scenario/resolver.py  →  models/entities.py, config/{index,graph,wiki}_profile.py
      ↓
services/scenario/snapshot.py  →  policy_spec.py, errors.py, decisions.py, settings.py
      ↓
（不依赖 services/retrieve、不依赖 services/acl、不依赖 mcp）
```

**为什么 resolver 不依赖 `services/retrieve`**：Step 2 的 resolver 只产出「口径快照」，不调用检索。若它依赖检索，Step 3 接线时会出现「检索调 resolver、resolver 调检索」的循环。

### C.2 `snapshot.py` 字段级定义

```python
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Literal

from app.config import decisions
from app.config.graph_profile import GraphProfile
from app.config.index_profile import IndexProfile
from app.config.wiki_profile import WikiProfile


@dataclass(frozen=True, slots=True)
class ScenarioPolicy:
    """场景级策略覆盖。全部字段 None = 跟随平台（稀疏覆盖语义）。

    ★ 为什么每个字段都是 Optional 而不是给一个「等于平台默认」的字面量默认值：
      字面量默认值会把平台当前值**复制**一份进代码，平台一改就出现两份不同的「默认」。
      None 表示「不覆盖」，取值时现读 decisions/settings —— 单一真源。
    """

    recall_top_k: int | None = None
    rerank_enabled: bool | None = None
    relevance_threshold_vector: float | None = None
    relevance_threshold_rerank: float | None = None

    # ── 有效值解析（None 折叠为平台值） ────────────────────────────
    def effective_recall_top_k(self) -> int:
        return decisions.TOP_K_RECALL if self.recall_top_k is None else self.recall_top_k

    def effective_rerank_enabled(self, platform_enabled: bool) -> bool:
        """I-2「覆盖只能收紧」在取值层的落实：场景能把重排关掉，不能把关掉的重排打开。

        ★ 单向 AND 的理由：rerank 的开启成本由平台承担（模型常驻内存 / CPU 66s per 50 docs，
          见 retrieve/__init__.py:451-453）。若场景能自行打开，一个场景的配置就能把
          整台机器的检索 P95 拖垮，而平台侧的开关形同虚设。
        """
        if self.rerank_enabled is None:
            return platform_enabled
        return platform_enabled and self.rerank_enabled

    def effective_threshold(self, *, use_rerank: bool) -> float:
        """按当前打分口径取阈值。两个键分开的理由见 decisions.py:24-41 —— 两种分数分布不同。"""
        if use_rerank:
            return (
                decisions.RELEVANCE_THRESHOLD_RERANK
                if self.relevance_threshold_rerank is None
                else self.relevance_threshold_rerank
            )
        return (
            decisions.RELEVANCE_THRESHOLD_VECTOR
            if self.relevance_threshold_vector is None
            else self.relevance_threshold_vector
        )


@dataclass(frozen=True, slots=True)
class ScenarioSnapshot:
    """一次请求解析出的不可变口径快照。构造后不得修改（frozen）。"""

    # ── 身份 ────────────────────────────────────────────────────────
    # scenario_id / checksum 为 None 只发生在 source='builtin_fallback'
    # （本租户还没补种内置行）。这不影响任何行为：快照的其余字段与
    # 有行时逐字段相等，由 test_builtin_row_equals_builtin_fallback 锁死。
    scenario_id: uuid.UUID | None
    scenario_key: str
    version: int
    checksum: str | None
    is_builtin: bool

    # ── 口径基因（**只从平台函数读**，绝不从 scenario_versions.dna_declared 读） ──
    index: IndexProfile
    graph: GraphProfile
    wiki: WikiProfile

    # ── 策略 ────────────────────────────────────────────────────────
    policy: ScenarioPolicy

    # ── 绑定 ────────────────────────────────────────────────────────
    binding_mode: Literal["explicit", "unrestricted"]
    binding_kb_ids: tuple[uuid.UUID, ...]   # 场景声明的集合（审计用）
    kb_ids: tuple[uuid.UUID, ...]           # **已过场景门禁的请求集合**（下游直接用这个）
    db_binding: str | None                  # Step 6 前恒为 None 且无人读

    # ── 来源（审计/排障用；决定 R5 与 R6 在日志里可区分） ──────────
    source: Literal["builtin_row", "builtin_fallback", "application"]

    @property
    def fingerprint(self) -> str:
        """场景口径指纹 = sha1(scenario_key|version|index.fingerprint|canonical_policy)[:12]。

        用途：审计日志一行就能回答「这次回答是按哪个场景哪个版本哪个口径给的」。
        """
```

**字段级要点逐条**：

| 字段 | 为什么必须在这里 | 为什么不在这里 |
|---|---|---|
| `index` / `graph` / `wiki` | Step 4 要给 chunk 盖章、写 `meta`，需要一次拿到三个口径 | 不含 `dna_declared` 的原始 dict —— 声明镜像不是真源（D-A3） |
| `policy` | Step 3 接线时下游从快照读，不重读 DB | 不含「有效值」缓存字段 —— 有效值由方法算出，避免快照里出现「已经算过的值」和第二处真源 |
| `binding_kb_ids` 与 `kb_ids` 并存 | 前者审计（场景声明了什么），后者执行（这次真的查什么）。两者混为一谈会导致「越权排查时看不出是场景限制还是请求限制」 | 不含 `user.authorized_kb_ids` —— 那是请求主体属性，不是场景属性 |
| `source` | `builtin_fallback`（无行，走内置默认）与 `builtin_row`（有行）必须可区分，否则「种子行丢了」这件事永远查不出来 | 不含 `tenant_id` —— 调用方本来就有，快照里再放一份会让人误以为可以跨租户传 |
| `checksum` | 审计可回指到具体版本行 | 不含 `created_at` 等时间戳 |

### C.3 `resolver.py`：签名、真值表、错误码

```python
async def resolve_scenario(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    application: IntegrationApplication,
    requested_kb_ids: Sequence[uuid.UUID],
) -> ScenarioSnapshot:
    """由（凭证绑定的应用, 请求的 kb_ids）解析出本次请求的场景口径快照。

    失败一律 raise ScenarioError，**绝不返回默认快照兜底**（fail-closed）。
    """
```

#### C.3.1 六行解析真值表（**顺序即优先级，首个命中即返回**）

| # | 判定条件（**按顺序求值，首个命中即返回**） | 结果 | 错误码 / HTTP |
|---|---|---|---|
| **R1** | `app.scenario_id` **非 NULL**，但场景行查不到 / 无 `published` 版本 / 行不属于本租户 | 拒绝。**绝不退回默认场景** | `SCENARIO_UNRESOLVABLE` 500 |
| **R2** | `requested_kb_ids` 是**空数组**（与 `scenario_id`、`status` 无关） | 不额外判空；交由既有三重交集得出空集 → 403 | `FORBIDDEN` 403（**现状行为，不变**） |
| **R3** | `app.scenario_id` 非 NULL 且场景 `status != 'active'` | 拒绝。**status 先于范围判断** | `SCENARIO_NOT_ALLOWED` 400 |
| **R4** | `app.scenario_id` 非 NULL 且 `binding_mode = 'explicit'` 且 `K_r ⊄ binding_kb_ids` | 拒绝：请求要的库不在场景声明里 | `SCENARIO_MISMATCH` 400 |
| **R5** | `app.scenario_id IS NULL`（未命中 R1/R2） | 内置场景快照；`source='builtin_row'`；`kb_ids = K_r`（场景不额外限制） | —（200） |
| **R6** | `app.scenario_id = S`（未命中 R1–R4） | 应用场景快照；`source='application'`；`kb_ids = K_r` | —（200） |

**顺序的三条理由（顺序即语义，不是风格）**：

1. **R1 必须最先**：拿不到行就无法判 status / binding。若把它放在最后当兜底，等于「先假设默认场景跑完逻辑，最后发现行不存在」。
2. **R2 与 R3 的先后无所谓，但 R2 提到前面是为了让「空集」这一条对两个分支共用**：`binding_mode='unrestricted'`（内置）与 `'explicit'`（非内置）在空请求下结局相同，合并成一行可避免「内置场景空请求 403、非内置场景空请求 200」这种因实现分叉产生的行为差异。
3. **R3 必须在 R4 之前（安全）**：若先判 `K_r ⊄ binding` 再判 status，攻击者可用「提交一个必然 mismatch 的 `K_r`」探测某个停用场景的 `kb_ids` 边界（400 与 403 的差异泄露信息）。先判 status，停用场景对所有 `K_r` 返回同一个错误。→ §F `test_status_checked_before_scope`。

**R1 为什么必须是 500 而不是「退回默认」**（反例，见 §C.8 反例 1）：若退回默认，「有人误删场景行 / 漏写 published 版本」的表现就是「这批 agent 悄悄换成默认口径继续跑」—— 日志无红、指标无异常，只有答准率缓慢下滑。这正是框架稿 §12 反复点名的失败形态。

**R2/R5/R6 合起来 == 现状**（零影响的真值表证据）：未绑定场景的应用走 R2+R5，`policy` 为空覆盖集 ⇒ 策略全等平台值、`kb_ids` 原样传给既有三重交集 ⇒ 与改动前逐字节相同。这条在 §F 由 `test_effective_kb_ids_unchanged` + `test_resolve_default_returns_platform_dna` 双证。

**表外分支：内置场景行缺失（**不在 R1 管辖内**）**
`app.scenario_id IS NULL` 且本租户查不到 `is_builtin` 行时 —— 这**不是** R1，不走 500，而是返回 `source='builtin_fallback'` 的**同值内置快照**，并 `metrics.incr("rag.scenario.builtin_row_missing")`。判据：

| | 缺失的是「被引用的场景」 | 缺失的是「内置场景行」 |
|---|---|---|
| 语义 | 配置损坏，快照可以是任何值 | 冷路径漏补种（新租户未补种），快照**定义上等于平台默认** |
| 处理 | 500，**不可兜底**（兜底会让损坏不可见） | 同值兜底 + 打点（兜底不产生行为差异，因为同值有测试锁死） |
| 测试 | `test_missing_published_version_fails_closed` | `test_builtin_row_equals_builtin_fallback` |

#### C.3.2 错误码表（`errors.py`）

| 错误码 | HTTP | 触发条件 | `detail` 里给什么 | 不给什么 |
|---|---|---|---|---|
| `SCENARIO_NOT_ALLOWED` | 400 | R3（`status != 'active'`） | `场景不可用`（**不带**场景 key、不带 status、不带原因细分） | 不区分「停用」与「草稿」—— 细分即信息泄露 |
| `SCENARIO_MISMATCH` | 400 | R4（请求集合超出场景声明） | `请求的知识库不在场景范围内`（**不带**场景真实的 `kb_ids`） | 不回显场景声明集合，否则可被枚举 |
| `SCENARIO_UNRESOLVABLE` | 500 | R1（数据一致性破坏） | `场景配置不可解析` + `trace_id` | 不带 SQL/表名/`scenario_id`（避免把内部 ID 泄露到集成侧） |

```python
class ScenarioError(Exception):
    code: ScenarioCode
    http_status: int
    # 只用于日志，不进响应体
    diagnostic: dict[str, str] = {}
```

> **日志 vs 响应严格分离**：`diagnostic`（含 `scenario_id`、`tenant_id`、`reason`）只进结构化日志与 metrics；响应体只有上表第三列的固定文案。理由：集成侧是**外部系统**，`X-Application-Id` 已经足够定位，不需要也不应该知道租户内部 ID。

#### C.3.3 实现骨架（三函数分工，让真值表可单测）

```python
# ── 纯函数：无 IO，六行真值表全在这里，100% 可单测 ────────────────
def build_snapshot(
    *,
    row: ScenarioRow | None,
    tenant_id: uuid.UUID,
    requested_kb_ids: Sequence[uuid.UUID],
) -> ScenarioSnapshot:
    ...

# ── 唯一 impure 步骤：一次 DB 读（可能两次：场景行 + 已发布版本，用一条 JOIN 打平） ──
async def _load_published(session, *, tenant_id, scenario_id) -> ScenarioRow | None:
    ...

# ── 对外入口：load 后委托 build_snapshot ─────────────────────────
async def resolve_scenario(session, *, tenant_id, application, requested_kb_ids):
    row = await _load_published(session, tenant_id=tenant_id,
                               scenario_id=application.scenario_id)  # None → 取内置
    return build_snapshot(row=row, tenant_id=tenant_id,
                          requested_kb_ids=requested_kb_ids)
```

`_load_published` 的唯一 SQL（含内置分支）：

```sql
SELECT s.id, s.scenario_key, s.status, s.is_builtin, s.db_binding,
       v.version, v.policy, v.kb_ids, v.binding_mode, v.dna_declared, v.checksum
FROM scenarios s
JOIN scenario_versions v
  ON v.scenario_id = s.id AND v.state = 'published'
WHERE s.tenant_id = :tenant_id
  AND (:scenario_id IS NULL AND s.is_builtin OR s.id = :scenario_id)
```

> 单条语句是有意的：两个分支共用一次往返，且 `tenant_id` 条件**永远存在**（障碍 6 的落地）。`(:scenario_id IS NULL AND s.is_builtin OR s.id = :scenario_id)` 里未加括号的 `OR` 依赖 `AND` 优先级，**施工时必须写成显式括号** —— 这条写进 §F 的 SQL 断言测试（`test_lookup_sql_tenant_scoped`）。

### C.4 `policy_spec.py`：参数规格表是唯一真源

```python
@dataclass(frozen=True, slots=True)
class PolicyField:
    name: str
    type: Literal["int", "bool", "float"]
    default_semantics: str          # 人类可读：缺省时跟随什么
    min_value: int | float | None
    max_value: int | float | None
    wired_in_step: int              # ★ 生效步骤；> 当前步骤时禁止写入
    invalidates: str                # 改了之后什么派生数据失效
```

`PolicyField` 表既是 §E 的生成来源，也是写入校验与前端渲染的来源 —— **一份规格，三处消费**，避免「文档写的范围和代码校验的范围不一致」（这类不一致的典型后果：前端允许填 500、后端默默截断到 200）。

同时导出 `WIRED_KEYS` = `{f.name for f in POLICY_FIELDS if f.wired_in_step <= CURRENT_STEP}`，与迁移里的 `_POLICY_KEYS` 必须相等，由 `test_policy_keyset_matches_migration` 锁死（迁移无法 import 应用代码，所以只能靠测试对齐）。

### C.5 三个 `current_*_profile()` 的 refit：**推荐零改动**

| 方案 | 内容 | 评价 |
|---|---|---|
| **方案甲（推荐）零改动** | 三个函数一行不改。新增 `snapshot.py::current_dna()` 组合器，内部调用三个现有函数，返回 `(IndexProfile, GraphProfile, WikiProfile)`。`ScenarioSnapshot` 的三个字段就来自它 | 爆炸半径最小：三个函数被 chunk 落库、检索过滤、图/wiki 盖章等十几处调用（`index_profile.py:76`「MUST 作为唯一的取值入口」），动签名要审调用点。且 I-6 要求 DNA 锁在 L0，**场景根本改不了它**，所以「参数化」这个能力本身没有存在理由 |
| 方案乙：可选参数 | `current_index_profile(snapshot: ScenarioSnapshot \| None = None)`，`None` 走原逻辑，非 `None` 返回 `snapshot.index` | 引入一个**必须被忽略**的参数（因为它等于平台值），是 API 谎言；且一旦有人真传了不同值，静默生效 |
| 方案丙：场景真源 | 让 `scenarios.dna_declared` 成为真源，函数改成查库 | 违反 I-6，且把「口径」变成可 UPDATE 的数据 —— 框架稿的核心禁令 |

**选方案甲，并给它配三条断言**（进 §F）：

1. `test_current_dna_equals_three_profiles`：`current_dna()` 的三元组与三个 `current_*_profile()` 逐字段相等（这一条使「零改动」不会因未来重构而悄悄漂移）。
2. `test_snapshot_dna_is_platform_not_row`：把种子行的 `dna_declared` 改成明显不同的值（`etext-evil`），`resolve_scenario()` 返回的 `index.embedding_text_version` **仍是 `etext-v1`**。→ 证明 D-A3 真的落地了，不是文档口号。
3. `test_empty_model_resolved_not_persisted`：`GRAPH_EXTRACTOR_MODEL=""` 时，`snapshot.graph.extractor_model` 是 `settings.llm_model` 的实际值，**不是空串**（复制 `graph_profile.py:61-65` 的纪律）。

### C.6 fallback 规则表（"refit 的 fallback" 到底指什么）

| 场景 | fallback 行为 | 出处/理由 |
|---|---|---|
| `GRAPH_EXTRACTOR_MODEL` 空串 | 解析为 `settings.llm_model` 实际值 | `graph_profile.py:67`；空串落库后「换主模型」会悄悄生效 |
| `WIKI_GENERATOR_MODEL` 空串 | 同上 | `wiki_profile.py:46` |
| `policy` 缺键 | 现读 `decisions` / `settings` 的平台值 | 稀疏覆盖（C.2） |
| `app.scenario_id IS NULL` | 取本租户 `is_builtin=true` 的行 | §A.1 |
| 内置行缺失（租户新建后未补种） | **同值内置快照**（`source='builtin_fallback'`、`scenario_id=None`、`checksum=None`、`version=0`）+ `metrics.incr("rag.scenario.builtin_row_missing")` | 见 §C.8 反例 2 的取舍。`version=0` 是「无版本行」的显式表达，不占用 1（1 留给真实的第一个版本，避免 `fingerprint` 与真实 v1 撞车） |
| 场景行存在但无 `published` 版本 | **不 fallback**，抛 `SCENARIO_UNRESOLVABLE` | 这是数据损坏，不是「未配置」。静默 fallback 会让损坏不可见 |
| `db_binding IS NOT NULL` 但步骤 < 6 | 忽略该列 + 启动自检 WARN | 防「配了不生效」无感知 |

### C.7 进程内缓存（**必须做，且必须能被失效**）

- **键**：`(tenant_id, scenario_id_or_"builtin")`
- **值**：`ScenarioRow`（原始行，不是 `ScenarioSnapshot` —— 快照含请求态 `kb_ids`，不能缓存）
- **TTL**：60 秒（**站位值**。理由：Step 4 起会有「发布新版本 → 生效」的期望时延，60s 内的最终一致可接受；标定依据是发布操作的运维节奏，本项目尚无数据，标注为占位）
- **主动失效**：发布接口（Step 4）在事务提交后调 `invalidate(tenant_id, scenario_id)`；跨进程靠 TTL 兜底（单 uvicorn worker 现状，见 `AGENT_CONTEXT.md`；多副本时必须换 Redis 频道，登记为 §G 遗留项 G-2）
- **为什么不缓存 `ScenarioSnapshot`**：`kb_ids` 是请求态。缓存快照 = 把 A 用户请求的 kb 集合发给 B 用户，是最严重的一类越权。这条写进代码注释与 §F 测试（`test_snapshot_not_cached_across_requests`）。

### C.8 反例（魔鬼代言人，≥3 条）

**反例 1（针对 R1 fail-closed）：把「场景不可解析」做成「退回默认场景」会怎样。**
设某次运维误删了某个场景行（FK RESTRICT 保护了应用引用，但场景可能没被任何应用引用 —— 而新应用正要绑定它）。若 resolver 退回默认，下一个请求会拿到「默认口径」并成功返回。表现为：新应用答出来的东西和预期完全不一样、没有任何错误、`source` 字段如果没记就查不出原因。→ 因此 `source` 字段与 500 都是**必须的**，不是过度设计。

**反例 2（针对内置行缺失的 fallback）：`builtin_fallback` 会掩盖「种子没补」吗？**
会。这正是它必须带 metrics 的原因。反向选择（内置行缺失也抛 500）的代价是：任何**新租户创建后忘记补种**都会让该租户全部集成请求 500 —— 而这在「租户创建」这条冷路径上极容易漏。两害相权：选「同值 + 打点」，因为「同值」有 §F 测试锁死（`test_builtin_row_equals_builtin_fallback`），所以它**不会产生行为差异**，只可能产生「没补种」这个可观测事实。

**反例 3（针对稀疏覆盖 + 键闭集）：CHECK 键闭集会不会把 Step 3 卡死？**
会 —— 这正是设计意图的一半。若不加闭集，运维在 Step 2 就能往里写 `rerank_enabled: true`，但 Step 2 不读它，于是「我明明开了重排，为什么没生效」变成一张工单。加上闭集后，写入被拒、错误信息写明「该参数自 Step 3 起可配置」。代价：Step 3 必须写一个 `ALTER TABLE ... DROP CONSTRAINT / ADD CONSTRAINT` 的小迁移。**代价是一天，收益是永久消掉「配置静默无效」这个类别。**

**反例 4（针对「不回填 scenario_id」）：不回填会不会导致「内置场景在 UI 里显示 0 个应用在用」？**
会。这是真实的可用性代价：运维在内置场景详情页看到「使用中应用：0」，会以为没人用。→ 对冲：内置场景的「使用中应用数」必须按 `scenario_id IS NULL OR scenario_id = 本行` 统计。这条进 Step 4 的接口规格（本文件登记为 §G 遗留项 G-3），Step 2 不改任何 UI/接口。

**反例 5（对整章的釜底抽薪）：如果「场景」这个概念其实是伪需求呢？**
若企业的真实诉求只是「不同部门看不同知识库」，那么 `integration_applications` 的 `kb_ids` 白名单**已经够了**，整个 §A 是过度设计。反驳（也是本设计的前提）：框架稿 §1 的用户原话要求「同一平台、多场景配置」，且 Step 3 之后要按场景调阈值/召回宽度 —— 那是 `kb_ids` 表达不了的能力。**若此前提被推翻（用户说「只要库隔离」），本文件 A/C 两章应整体废弃，只保留 `scenario_id` 与内置行。** 这是本设计最大的单一风险，登记在 §G。

---

## D. MCP 工具契约（完整 schema，Step 2 不改代码）

> **Step 2 不修改 `code/apps/mcp/main.py`。** 本章是**契约冻结稿**：Step 4 落地 `meta` 时不得偏离本稿；本稿同时是「为什么没有 `scenario` 参数」的正式答复。

### D.1 为什么没有 `scenario` 参数（五条，逐条可反驳）

| # | 论证 | 如果反驳成立会怎样 |
|---|---|---|
| 1 | **身份即场景**：`scenario` 由 `X-Application-Id` + `X-Application-Key` 经双认证推出（`integrations.py:67-91`）。请求里再给一个 `scenario`，等于让调用方自己声明授权边界 | 反驳「模型需要动态选场景」→ 那等于让 LLM 决定数据访问范围，任何一次幻觉都是一次越权 |
| 2 | **参数与凭证冲突时无好答案**：参数赢 = 越权；凭证赢 = 参数是死参数（静默无效，正是 §C.8 反例 3 批的那类失败） | 无法反驳，这是二难 |
| 3 | **场景是治理维度，不是语义维度**：模型无法从「帮我查一下年假」推断该用 HR 库还是制度库 —— 那个映射关系由人在配置时决定 | 反驳「有映射规则就能推」→ 那规则本身就是场景，还是要人配 |
| 4 | **I-7/I-10 落地点**：场景→库的寻址只能来自已认证上下文；一期禁止跨库合并。一个 `scenario` 参数会让单次请求在语义上「可指向另一个库」，诱导调用方做跨库拼接 | 反驳「禁止跨库是调用方的事」→ 契约应当让错误用法**不可表达**，而不是靠规范约束 |
| 5 | **演进成本**：加可选参数对 MCP 客户端不破坏（`additionalProperties:false` 之下，旧客户端不传即可），但**一旦发布就无法收回**；而服务端加列/加绑定是可逆的 | 反驳「先加了再说」→ 收回参数 = breaking change，比当初不加贵得多 |

**结论**：`scenario` 不进入任何 MCP 工具签名。调用方表达「用哪个场景」的唯一方式 = **用哪个应用的凭证**。

### D.2 `knowledge_ask`（完整 schema）

请求（与现状完全一致，**三参数一个不改**）：

```json
{
  "name": "knowledge_ask",
  "description": "基于企业知识库回答一个问题。返回答案与引用来源。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "question": {
        "type": "string",
        "minLength": 1,
        "maxLength": 2000,
        "description": "用户问题原文"
      },
      "kb_ids": {
        "type": "array",
        "items": { "type": "string", "format": "uuid" },
        "minItems": 1,
        "maxItems": 50,
        "description": "请求检索的知识库 ID 集合；必须落在应用白名单与场景声明范围内"
      },
      "conversation_id": {
        "type": ["string", "null"],
        "format": "uuid",
        "description": "续聊时传入已有会话 ID；不传则新建会话"
      }
    },
    "required": ["question", "kb_ids"],
    "additionalProperties": false
  }
}
```

响应（**新增 `meta`，其余字段只增不改**）：

```json
{
  "type": "object",
  "properties": {
    "answer":        { "type": "string" },
    "refused":       { "type": "boolean" },
    "refuse_reason": { "type": ["string", "null"] },
    "citations": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "chunk_id":    { "type": "string", "format": "uuid" },
          "document_id": { "type": "string", "format": "uuid" },
          "score":       { "type": "number" },
          "snippet":     { "type": "string" }
        },
        "required": ["chunk_id", "document_id"]
      }
    },
    "meta": {
      "type": "object",
      "description": "本次请求的口径快照。**只增不改**：Step 2 上线 4 个字段，Step 4 追加其余。",
      "properties": {
        "scenario_key":       { "type": "string", "description": "生效场景。Step 2" },
        "scenario_version":   { "type": "integer", "description": "生效版本号。Step 2" },
        "scenario_fingerprint": { "type": "string", "description": "场景口径指纹。Step 2" },
        "contract_version":   { "type": "string", "description": "= CONTRACT_VERSION。Step 2" },
        "dna_fingerprint":    { "type": "string", "description": "嵌入口径指纹。Step 4" },
        "score_source":       { "type": "string", "enum": ["rerank", "vector"], "description": "Step 4" },
        "use_rerank":         { "type": "boolean", "description": "Step 4" },
        "policy_applied": {
          "type": "object",
          "description": "本次实际生效的策略值（已折叠平台默认）。Step 4",
          "properties": {
            "recall_top_k":            { "type": "integer" },
            "rerank_enabled":          { "type": "boolean" },
            "relevance_threshold_used": { "type": "number" }
          }
        }
      },
      "required": ["scenario_key", "scenario_version", "scenario_fingerprint", "contract_version"]
    }
  },
  "required": ["answer", "refused", "citations"]
}
```

### D.3 `knowledge_search`（完整 schema）

```json
{
  "name": "knowledge_search",
  "description": "在企业知识库中检索相关片段，返回片段与得分，不生成答案。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "query": {
        "type": "string",
        "minLength": 1,
        "maxLength": 2000,
        "description": "检索语句"
      },
      "kb_ids": {
        "type": "array",
        "items": { "type": "string", "format": "uuid" },
        "minItems": 1,
        "maxItems": 50,
        "description": "请求检索的知识库 ID 集合"
      }
    },
    "required": ["query", "kb_ids"],
    "additionalProperties": false
  }
}
```

响应：`{ "items": [ {"chunk_id","document_id","score","snippet"} ], "meta": { ...同 D.2 } }`

> `knowledge_search` **不新增 `conversation_id`**：检索无状态，加会话 ID 会让人误以为检索会受历史影响（改写/链式确实是 Step 3 之后才可能引入的能力，届时如需要再加，属纯增量）。

### D.4 错误码表（五类，含 MCP 侧映射）

| 类 | 码 | HTTP | 触发 | MCP 侧表现 | 调用方该做什么 |
|---|---|---|---|---|---|
| ① 认证 | `UNAUTHORIZED` | 401 | 缺 `X-Application-Id/Key`、key 不匹配、应用 `status != active`、租户不匹配（`integrations.py:75/79/90`） | `ToolError` | 检查凭证配置，**不要重试** |
| ② 场景 | `SCENARIO_NOT_ALLOWED` | 400 | R3 | 错误文本 `场景不可用` | 联系管理员启用场景 |
| ② 场景 | `SCENARIO_MISMATCH` | 400 | R4 | 错误文本 `请求的知识库不在场景范围内` | 收窄 `kb_ids`，**不要重试** |
| ③ 授权 | `FORBIDDEN` | 403 | 三重交集为空（`integrations.py:294`），含 R2 | 错误文本 `FORBIDDEN` | 检查用户授权与请求 kb |
| ④ 配额 | `RATE_LIMITED` | 429 | `check_integration_rate_limit` | 错误文本 | 退避重试 |
| ⑤ 依赖 | `SERVICE_UNAVAILABLE` | 503 | 检索抛异常（`integrations.py:305-309`） | 错误文本 `知识库暂时不可用，请稍后重试` | 指数退避重试 |
| ⑤ 依赖 | `SCENARIO_UNRESOLVABLE` | 500 | R1 | 错误文本 `场景配置不可解析` | 不重试，报障（带 trace_id） |

**MCP 侧零改动依据**：`mcp/main.py` 的 `_api_headers()` 只做头映射与缺失校验，`_post_integration()` 把非 2xx 转 `ToolError`。新增的 400/500 会自动走既有通路 —— **无需改一行**。这条进 §F 的 `test_mcp_error_passthrough`。

### D.5 向后兼容性证明（四条，可执行）

| # | 断言 | 证明 |
|---|---|---|
| B-1 | 三（两）个入参的名称、类型、必填性与现状逐字相同 | 逐字段对照 `code/apps/mcp/main.py` 的 `knowledge_ask(question, kb_ids, conversation_id=None)` / `knowledge_search(query, kb_ids)` 签名 |
| B-2 | 响应既有字段的类型与语义不变 | `answer`/`refused`/`refuse_reason`/`citations`/`items` 未改；`meta` 是**新增可选对象** |
| B-3 | 旧客户端（不解析 `meta`）行为不变 | JSON 反序列化的标准行为是忽略未知键；`additionalProperties:false` 只约束**请求**，不约束响应 |
| B-4 | `meta` 的 `required` 只含 Step 2 就能填的 4 个字段 | 若把 Step 4 的字段放进 `required`，Step 2 上线即违约 —— 这是最容易写错的一处，单独列一条测试 |

### D.6 `meta` 的填充点（Step 2 只填 4 个）

```python
# integrations.py（Step 2 的改动就是这几行，此处仅示意，不修改代码）
snapshot = await resolve_scenario(
    session,
    tenant_id=user.tenant_id,
    application=application,
    requested_kb_ids=payload.kb_ids,
)
effective_kb_ids = _integration_kb_ids(list(snapshot.kb_ids), user, application)
...
meta = {
    "scenario_key": snapshot.scenario_key,
    "scenario_version": snapshot.version,
    "scenario_fingerprint": snapshot.fingerprint,
    "contract_version": decisions.CONTRACT_VERSION,
}
```

> **`meta` 这一层在 Step 2 是否算超范围？** 严格说 Step 2 的最小实现只需「解析 + 门禁」。但 `ScenarioSnapshot` 已经在手，把它写进响应只需 5 行，且**它是 Z-2 之外唯一能让运维在生产上验证「场景真的解析对了」的手段**。若不加，Step 2 上线后没有任何可观测证据。→ 选加，并明确 `meta` 只含 Step 2 能保证为真的字段（B-4）。

---

## E. Policy 参数全表

### E.1 分层与「生效步骤」的总则

- **L0 平台层**（`decisions.py` / `settings.py`）：参数的真源。场景**只能收窄**，不能拓宽。
- **L2 场景层**（`scenario_versions.policy`）：**稀疏覆盖**，缺键 = 跟随平台。
- **`wired_in_step`**：该参数**开始被检索链路读取**的步骤。`wired_in_step > 2` 的参数在 Step 1–2 **写入会被 CHECK 拒绝**（§A.3.1 C-A2）。
- **收窄判据（覆盖只能收紧）**：见「方向」列 —— `收紧` 表示场景值只能让检索范围/成本更小；`锁定` 表示完全不可配。

### E.2 全表（16 项）

| # | 参数 | 类型 | 平台默认值（含出处） | 合法范围 | 层 | 方向 | `self_check` 不变量 | `wired_in_step` | 改了之后什么派生数据失效 |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `recall_top_k` | int | `50`（`decisions.py:176`） | `1..1000`（DB 兜底）；代码上限 `RECALL_TOP_K_MAX`（**站位值 200，待压测标定**） | L2 可覆盖 | 收紧（上限内） | `1 <= recall_top_k <= RECALL_TOP_K_MAX`；且 `recall_top_k >= TOP_K_RERANK`（少于重排取用条数则重排形同虚设） | **3** | **无**（纯请求内计算，不落库） |
| 2 | `rerank_enabled` | bool | `False`（`settings.py:92`） | `true/false` | L2 可覆盖（**单向 AND**） | 只能收紧 | `effective = platform AND scenario`；平台关时场景开 = **静默无效** → 启动自检必须 WARN（不允许静默） | **3** | **无**。但会切换 `final_score` 口径 → 必须同时切阈值（见 #3/#4） |
| 3 | `relevance_threshold_vector` | float | `0.50`（`decisions.py:34`，30 条样本标定） | `0.0..1.0` | L2 可覆盖 | 收紧（只准调高） | `0.0 < v <= 1.0`；调低属放宽 → 应用层拒绝（放宽拒答门槛 = 更容易答错） | **3** | **无**（不改数据，只改判定） |
| 4 | `relevance_threshold_rerank` | float | `0.50`（`decisions.py:37`，**未标定占位值**） | `0.0..1.0` | L2 可覆盖 | 收紧 | 同 #3；**额外**：`rerank_enabled` 从未标定过 → 若场景设了本键但平台 `rerank_enabled=false`，启动自检 WARN「设了不受标定的阈值」 | **3** | 无 |
| 5 | `final_top_n` | int | `8`（`decisions.py:177` `TOP_K_RERANK`） | `1..50` | L2 | 收紧 | `1 <= final_top_n <= TOP_K_RERANK`（不能超过平台允许进上下文的条数上限） | 4 | 无 |
| 6 | `rrf_k` | int | `60`（`decisions.py:178`） | — | **L0 锁定** | 锁定 | 改它等于改三路融合的分布，**跨场景结果不可比**，且收益未验证 → 不进策略表 | — | 无（但会使所有历史 A/B 结论失效） |
| 7 | `keyword_tsquery_enabled` | bool | `True`（`decisions.py:56`） | — | **L0 锁定** | 锁定 | 关掉它 = 丢掉一条召回路，属平台能力开关，不是场景策略 | — | 无 |
| 8 | `alias_expansion_enabled` | bool | `True`（`decisions.py:80`） | — | L2（Step 4 再议） | 收紧 | 别名扩展当前有「静默截断」问题（`02-design/C4-别名静默截断影响面量化.md`）→ 场景化前必须先修 | 4+ | 无 |
| 9 | `ilike_max_terms` | int | `3`（`decisions.py:73`） | `1..10` | L2（Step 4） | 收紧 | `1 <= x <= ILIKE_MAX_TERMS`（超过平台上限 → 拒绝） | 4+ | 无 |
| 10 | `graph_enabled` | bool | `False`（`settings.py:153`） | 单向 AND | L2 | 只能收紧 | 同 #2 | 5 | **不改已有数据**；但关→开后，该库范围内**未抽取的文档**需要回填才有效果（回填属 Step 5） |
| 11 | `graph_recall_selective` | bool | `False`（`settings.py:157`） | 单向 AND | L2 | 只能收紧 | 同 #2；阈值 `GRAPH_SKIP_SCORE=0.70` 已实测、`GRAPH_SKIP_MIN_CHUNKS=8` 仍是站位值（`decisions.py:930-940`） | 5 | 无 |
| 12 | `graph_retrieve_top_k` | int | `10`（`decisions.py:912`） | `1..50` | L2 | 收紧 | `<= GRAPH_RETRIEVE_TOP_K` | 5 | 无 |
| 13 | `wiki_enabled` | bool | `False`（`settings.py:179`） | 单向 AND | L2 | 只能收紧 | 同 #2 | 5 | 关→开后需生成 `wiki_pages`（Step 4/5） |
| 14 | `wiki_nav_selective` | bool | `False`（`settings.py:183`） | 单向 AND | L2 | 只能收紧 | 阈值 `WIKI_NAV_SELECTIVE_MIN_TERMS=3` 是站位值（`decisions.py:131`） | 5 | 无 |
| 15 | `table_recall_enabled` | bool | **源码无此开关**（只有 `TABLE_RECALL_MAX_CHUNKS=3` / `TABLE_RECALL_TOKEN_BUDGET=2048`，`decisions.py:811/815`） | 待定义 | L2 | 收紧 | 若要加开关，须先回答「关掉表召回对表格题答准率的影响」—— 目前无数据 | 4+ | 无 |
| 16 | `section_recall_enabled` | bool | **源码无此开关**（只有 `SECTION_RECALL_MAX_CHUNKS=3` / `SECTION_RECALL_TOKEN_BUDGET=1200` / `SECTION_RECALL_MIN_HEADING_CHARS=2`，`decisions.py:821/825/830`） | 待定义 | L2 | 收紧 | 同上 | 4+ | 无 |

> **#15/#16 写「待定义」而不编造默认值**：源码里只有上限/预算常量，**没有开关常量**。凭空写「默认 true」就是把推断当成现状 —— 这正是本文件开头承诺不做的事。是否要引入这两个开关，属 Step 4 的设计输入，不在本阶段决定。

### E.3 `self_check` 不变量清单（启动时执行，逐条可测）

| # | 不变量 | 违反时 | 为什么不是「静默通过」 |
|---|---|---|---|
| S-1 | `WIRED_KEYS == migration._POLICY_KEYS` | **CI 红**（`test_policy_keyset_matches_migration`） | 两份清单不一致 = 一类参数「能写不能读」或「能读不能写」 |
| S-2 | `1 <= recall_top_k <= RECALL_TOP_K_MAX and recall_top_k >= TOP_K_RERANK` | 启动 **RuntimeError** | 拿不到合理候选数就谈阈值 = 必然误判 |
| S-3 | `effective = platform AND scenario`；若 `platform=False and scenario=True` → WARN 列出 | WARN + 横幅 | 这是「配了不生效」的典型，必须可见 |
| S-4 | 两个阈值 `0.0 < v <= 1.0` | RuntimeError | 0 或负阈值 = 永不拒答 |
| S-5 | `relevance_threshold_rerank != RELEVANCE_THRESHOLD_RERANK` 且平台 rerank 关 → WARN | WARN | 提醒「你在用一个未标定的值」 |
| S-6 | `RECALL_TOP_K_MAX` 仍标注为站位值 → 横幅显示「含站位值参数」 | 横幅 | 沿用项目既有做法（`GRAPH_SKIP_MIN_CHUNKS` 至今标站位值） |
| S-7 | `db_binding IS NOT NULL` 的行数 > 0 且 `CURRENT_STEP < 6` → WARN | WARN | 见 D-A4 的对冲 |
| S-8 | 内置场景的 published 版本 `policy = '{}'` | WARN + 打印租户/场景 | 障碍 3 的替代方案 |
| S-9 | 内置场景 `dna_declared` 展开 `env_bound` 后 == `current_dna()` | WARN | 障碍 2 的替代方案 |

### E.4 「什么派生数据失效」的判据与结论

判据（框架稿 DNA/Policy 三段切）：**改了它，已落库的派生数据还成立吗？**

| 参数类别 | 派生数据 | 改后是否失效 | 依据 |
|---|---|---|---|
| 检索策略（#1–#4、#7–#9、#12、#15、#16） | 无（纯请求内） | **不失效** | 检索是「读 + 算」，不落结果。唯一例外：若开了结果缓存则失效 —— **本项目没有结果缓存**（已核实无此组件） |
| 图/wiki 开关（#10、#11、#13、#14） | `graph_entity_mentions` / `graph_relations` / `wiki_pages` | **数据本身不失效**，但「关→开」后该库范围内**未抽取的文档**无数据 → 功能对它们静默不生效 | `graph_profile.py:13-16` 的 fail-closed 语义：旧口径行被排除而非混算 |
| 嵌入口径（DNA，**不可配**） | `chunks.embedding`（全部向量） | **失效且不可逆**：必须重算 | `index_profile.py:10-16` |
| 分块参数（DNA，**不可配**） | `chunks` 全部行 | 失效：需重切重算 | `index_profile.py:17-21`（`chunker_version` 不在 fingerprint 里，只提示不硬过滤） |
| Prompt 版本（DNA，**不可配**） | 图/wiki 派生行 | 失效：需重抽/重生 | `graph_profile.py:30-36` |

> **这张表是「为什么策略参数能配、口径参数不能配」的完整答复**：不是「策略更安全」，而是**策略改了没有数据要重算**。

---

## F. 验收测试（可执行规格）

> 全部为 `pytest`。DB 相关用事务回滚 fixture（沿用项目既有 fixture 风格）。文件放 `code/apps/api/tests/scenario/`。

### F.1 Step 1（迁移与种子行）

| 测试函数 | 构造 | 断言 |
|---|---|---|
| `test_seed_dna_matches_platform_profiles` | 迁移后读内置行 | 展开 `env_bound` 后与 `current_index_profile()` / `current_graph_profile()` / `current_wiki_profile()` **8 个字段逐个相等**；`embedding_text_version=="etext-v1"`、`chunker_version=="c400-800-60"`、`graph_prompt_version=="gprompt-v1"`、`wiki_prompt_version=="wprompt-v1"` 为**字面量断言**（防止有人把断言写成「读常量再比常量」的自证式假测试） |
| `test_seed_policy_is_empty` | 同上 | `policy == {}`、`binding_mode == "unrestricted"`、`kb_ids == []` |
| `test_seed_checksum_matches_runtime` | 读行 → `compute_checksum(...)` | 等于 `row.checksum` |
| `test_one_builtin_per_tenant` | 尝试插第二个 `is_builtin=true` | 抛 `IntegrityError`（部分唯一索引生效） |
| `test_duplicate_scenario_key_rejected` | 同租户插同名 key | `IntegrityError` |
| `test_policy_keyset_rejects_unwired_key` | 插 `policy={'final_top_n': 3}` | `IntegrityError`，且 SQLSTATE `23514` |
| `test_policy_range_rejected` | 插 `recall_top_k=0` | `IntegrityError` |
| `test_binding_mode_consistency` | `binding_mode='explicit'` + `kb_ids=[]` | `IntegrityError`（**防 fail-open**） |
| `test_state_timestamp_consistency` | `state='published'` + `published_at=NULL` | `IntegrityError` |
| `test_one_published_per_scenario` | 同场景两个 published | `IntegrityError` |
| `test_downgrade_removes_only_0028_objects` | `upgrade → downgrade` | `scenarios`/`scenario_versions` 不存在；`integration_applications` 列集合 == 0027 后集合；三张业务表行数/校验和不变（Z-1） |
| `test_upgrade_downgrade_upgrade_idempotent` | 三次迁移往返 | `scenarios` 行数 == 租户数，无重复 |
| `test_scenario_id_fk_restrict` | 有应用引用时删场景 | `IntegrityError`（RESTRICT 生效） |

### F.2 Step 2（resolver）

| 测试函数 | 构造 | 断言 |
|---|---|---|
| `test_resolve_default_returns_platform_dna` | `app.scenario_id=NULL` | `snapshot.index` / `.graph` / `.wiki` 与三个 `current_*_profile()` 逐字段相等（**Z-2**） |
| `test_builtin_row_equals_builtin_fallback` | 同一请求分别走「内置行存在」与「内置行删除」 | 除 `source`（`builtin_row` vs `builtin_fallback`）与 `scenario_id` 外**全等**；且 `metrics` 计数 `rag.scenario.builtin_row_missing` 为 1 |
| `test_truth_table_R1..R6` | 六个参数化用例，逐个覆盖 §C.3.1 的 R1–R6（**含求值顺序**：R1 用「引用不存在的 scenario_id」，R2 用空 `kb_ids`，R3 用停用场景，R4 用超范围 `kb_ids`，R5 用 `scenario_id=NULL`，R6 用正常绑定） | 各自返回预期 `source`/`kb_ids`，或抛预期 `ScenarioError.code`；R5 与 R6 在内置场景下除 `source`/`scenario_id` 外全等 |
| `test_status_checked_before_scope` | 停用场景 + 必然 mismatch 的 `K_r` | 抛 `SCENARIO_NOT_ALLOWED`（**不是** `SCENARIO_MISMATCH`）——防信息泄露 |
| `test_missing_published_version_fails_closed` | 场景存在但无 published 版本 | 抛 `SCENARIO_UNRESOLVABLE`（500），**不得**返回默认快照 |
| `test_cross_tenant_scenario_unresolvable` | `scenario_id` 指向另一租户的场景 | `SCENARIO_UNRESOLVABLE` |
| `test_empty_model_resolved_not_persisted` | `GRAPH_EXTRACTOR_MODEL=""` | `snapshot.graph.extractor_model == settings.llm_model != ""` |
| `test_snapshot_dna_is_platform_not_row` | 把种子行 `dna_declared.embedding_text_version` 改成 `"etext-evil"` | `snapshot.index.embedding_text_version == "etext-v1"` |
| `test_current_dna_equals_three_profiles` | — | 组合器与三个原函数逐字段相等 |
| `test_rerank_cannot_be_enabled_against_platform` | `platform.rerank_enabled=False`，`policy.rerank_enabled=True` | `effective_rerank_enabled(False) is False` |
| `test_threshold_two_calibers` | `policy` 只设 vector 阈值 | `effective_threshold(use_rerank=False)` == 场景值；`use_rerank=True` == 平台 rerank 值 |
| `test_snapshot_not_cached_across_requests` | 两次请求 `K_r` 不同 | 两次 `snapshot.kb_ids` 各等于自己请求的集合（**防越权缓存**） |
| `test_snapshot_frozen` | `dataclasses.FrozenInstanceError` | 赋值抛异常 |
| `test_fingerprint_stable` | 同输入两次 | `fingerprint` 相同；改 `version` 则不同 |
| `test_effective_kb_ids_unchanged` | 与 `chat.py:243-255` 旧实现对照 | 对 10 组随机 `(requested, app.kb_ids, authorized)` 输出**顺序与内容**均相同（**Z-3**） |
| `test_lookup_sql_tenant_scoped` | 静态检查 `_load_published` 的 SQL 文本 | 必含 `s.tenant_id = :tenant_id`；`OR` 两侧有显式括号 |
| `test_policy_keyset_matches_migration` | `WIRED_KEYS` vs `0028._POLICY_KEYS` | 两集合相等（**S-1**） |
| `test_mcp_contract_backward_compat` | 导入 `apps/mcp/main.py` 的注册表 | `knowledge_ask` 参数名/顺序/默认值 == `("question","kb_ids","conversation_id")`；`knowledge_search` == `("query","kb_ids")`（**Z-4 / B-1**） |
| `test_mcp_error_passthrough` | stub `/integrations/search` 返回 400 `SCENARIO_MISMATCH` | MCP 工具抛 `ToolError`，消息含错误文本，**无需改 MCP 代码** |
| `test_meta_required_fields_only_step2` | 校验 `meta` 响应的 `required` 集合 | 恰为 4 个 Step 2 字段（**B-4**） |

### F.3 零影响回归（必跑，作为上线门禁）

| 测试函数 | 断言 |
|---|---|
| `test_zero_impact_full_retrieval_parity` | 对 217 条既有 eval_cases，用 `scenario_id=NULL` 的应用跑全链路，`chunks`/`score`/`refused` 与改动前**逐条相同**（同 seed、同模型 → 应确定性复现） |
| `test_zero_impact_derived_tables_untouched` | 迁移前后 `chunks`/`documents`/`graph_entity_mentions`/`graph_relations`/`wiki_pages` 的 `count(*)` 与 `md5` 不变（**Z-1**） |
| `test_existing_638_tests_still_green` | 既有测试集全绿（`AGENT_CONTEXT.md` 记录当前 638 通过） |

---

## G. 遗留问题与未知区

| # | 项 | 影响 | 归属 |
|---|---|---|---|
| **G-1** | 多库迁移排序 / 跨库失败回滚 / 备份窗口（DQ2 执行细节） | 阻塞 Step 6。本期单库执行，B.4 只给了单库结论 | Step 6 前置 |
| **G-2** | 场景缓存的多副本失效（当前单 worker，TTL 兜底够用） | 多副本上线后「发布新版本」最长 60s 后生效；若要求秒级必须换 Redis 频道 | Step 4 |
| **G-3** | 内置场景的「使用中应用数」必须按 `scenario_id IS NULL OR scenario_id = 本行` 统计 | 否则 UI 显示 0（反例 4） | Step 4 |
| **G-4** | policy 值的非数字类型在 DB CHECK 报 `22P02` 而非 `23514` | 运维错误信息不友好；写仍被拒 | 可延后（应用层已拦） |
| **G-5** | `checksum` 只能由 Python 重算，DB 无法校验 | 若有人绕过应用直写 SQL，`checksum` 可能不匹配内容 | 靠巡检 SQL（Step 1 只写 SQL 不落脚本） |
| **G-6** | `RECALL_TOP_K_MAX = 200` 是**站位值**，无压测依据 | S-2 的 `self_check` 上限是猜的 | 需压测（与 `M_scene_pg` 一起做） |
| **G-7** | 场景缓存的 TTL 60s 是站位值 | 生效时延预期未与用户确认 | Step 4 开工前确认 |
| **G-8** | 别名扩展的静默截断（`C4-别名静默截断影响面量化.md`）未修 | 场景化它会放大问题（每个场景一套别名口径） | Step 4 前置 |
| **G-9** | 表/段召回**没有开关常量**（源码事实），#15/#16 的默认值待定义 | E 表两项「待定义」，不编造 | Step 4 |
| **G-10** | `RELEVANCE_THRESHOLD_RERANK` 仍未标定（`decisions.py:37-41` 自述） | 场景设这个键 = 用一个未标定的值；S-5 只能 WARN | 需 GPU + `calibrate_threshold.py --mode rerank` |
| **G-11** | **本设计最大的单一风险**：若用户真实诉求只是「部门看不同库」，则 A/C 两章整体过度设计（§C.8 反例 5） | 可在 Step 2 完工后用一个问题证伪：「除库范围外，你还需要按场景改检索参数吗？」答否 → 砍掉 policy 相关全部设计 | 需用户确认 |
| **G-12** | `recall_top_k` 与 `TOP_K_RERANK` 的 `>=` 约束（S-2）在 Step 3 接线时才被真正使用；Step 2 只做校验不做接线 | 若 S-2 过严会误伤合法配置（如故意把 recall 设 5 做实验）→ 应以「WARN」而非「RuntimeError」处理 | Step 3 |

---

## 附录 A：本文件对框架稿的**偏移登记**（不静默改设计）

| # | 框架稿原表述 | 本文件 | 偏移理由 |
|---|---|---|---|
| 1 | Step 3 接线「`recall_top_k` / `rerank_enabled` / `relevance_threshold`」（3 个） | 拆成 **4 个键**：`relevance_threshold` → `relevance_threshold_vector` + `relevance_threshold_rerank` | `decisions.py:24-41` 白纸黑字写着「单一阈值不可预测：重排在线比 rerank 绝对分、降级比余弦分，两种口径会交替出现」。若场景只暴露一个阈值键，等于把源码明确修掉的 bug 从场景层重新引入。**这是加严，不是放宽**，且键数从 3 变 4 不改变 Step 3 的工作量 |
| 2 | 「默认场景种子行」 | 种子行的 8 个 DNA 字段中只有 **4 个**能硬编码，另外 4 个以 `env_bound` 声明 | `embedding_model`/`embedding_dim`/两个模型名来自 `settings.py`，是**部署环境**值。硬编码等于把某环境的配置写进所有环境的迁移。见 §A.6 障碍 1 |
| 3 | 「`is_default`」 | 改为 `is_builtin`（并改语义为「由迁移创建、不可编辑」） | 「default」暗示可切换；`builtin` 精确表达「它的内容就是平台默认值，改它等于改平台默认」。副产物：每租户唯一 + policy 必空两条约束都变得可表达 |
| 4 | 框架稿把 `kb_ids` 归为 Binding（未区分模式） | 增加 `binding_mode ∈ {explicit, unrestricted}` 一列 | 空 `kb_ids` 若表示「不限制」，则「忘配」与「故意放开」无法区分 = fail-open。加一列使这个区别成为**必答题**而非默认值 |

---

## 附录 B：施工顺序（依赖关系，非排期）

```
0028 迁移
   └─→ entities.py 两个 ORM 类 + IntegrationApplication.scenario_id
          └─→ tests/scenario/test_0028_*.py（F.1）      ← 此处可独立验收 Step 1
                 └─→ policy_spec.py
                        └─→ errors.py → snapshot.py
                               └─→ resolver.py
                                      └─→ integrations.py 两个调用点
                                             └─→ tests/scenario/test_resolver_*.py（F.2）
                                                    └─→ 零影响回归（F.3）  ← Step 2 验收
```

**Step 1 与 Step 2 之间有一个可交付的检查点**：F.1 全绿即 Step 1 完成（此时表已建、种子已插、无人读）。Step 2 的 code review 只看两件事：① `build_snapshot` 是否纯函数（无 IO）；② `_load_published` 的 SQL 是否带 `tenant_id`。
