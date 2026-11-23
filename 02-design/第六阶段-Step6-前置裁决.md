# 第六阶段 Step 6（多库/分库接线）前置裁决

> 台账：文档类型 = 前置设计裁决（P1）；状态 = **已拍板（2026-11-23）**；产物 = 本文件；不改代码、不建测试。
>
> **拍板记录（2026-11-23，用户逐项确认）**：
> 1. 连接预算口径 = **B：`db_conn_reserved` 默认 10 → 20，预算 80**（91 分）。
> 2. Step 6 层级 = **温档（同实例独立 database）**（93 分）。
> 3. U10 = **不阻塞温档，仅作观测项**（96 分）。
> 4. 场景库 schema = **M-B 跨库外键手术 + 白名单守卫 + 控制面禁写触发器**（92 分）。
> 5. 其余决策按 §6 推荐默认执行：搬迁只搬检索数据面（91）、演练用本机容器（92）、kb 硬删除返回 409 + 清理脚本、会话历史本期留控制库、备份以「每日一次全库 dump」为 RPO 政策初值、B 口径下 N=2/K=2 每桶降到 15。
> 阅读前置：`02-design/第六阶段-Step5-详细设计.md`（已实现验收口径）、`02-design/统一RAG平台-场景化配置-框架设计-v1.md`、`02-design/部署标定手册.md`、`交接文档-2026-11-22.md`。
> 标注约定：`【源】` = 直接读到的事实（给 file:line）；`【推导】` = 由事实计算的结论；`【待标定】` = 无实测出处，禁止当作阈值使用。

---

## 0. 事实锚定（先对齐事实，再做裁决）

### 0.1 三处口径冲突（核心矛盾）

| 处 | 原文口径 | 出处 |
|---|---|---|
| 代码默认 | `db_max_connections_for_budget=100`、`db_conn_reserved=10` → 预算 90 | `code/apps/api/app/config/settings.py:80`、`:82` |
| 框架稿 | "预留 取 10 —— 这是设定值，不是实测出来的数字" | `02-design/统一RAG平台-场景化配置-框架设计-v1.md:341` |
| 部署标定手册 | "预留 = 20（迁移 / psql 排障 / 监控）……预算 80" | `02-design/部署标定手册.md:158`、`:171` |

结论：代码与框架写 10，手册写 20，且三处都自称"设定值"。这不是笔误，是**同一常量没有单一来源**。【源】

### 0.2 预算算术与闸门的已实现形态

- `_arithmetic()` = `(pool_size + max_overflow) × N × K + db_worker_conn_total`，`budget = max_connections_for_budget − db_conn_reserved`；返回四元组 `(per_bucket, N, needed, budget)`。【源 `db_router.py:141-158`】
- 闸门判据是 `needed > budget` 才 raise，**`needed == budget` 放行**（零余量放行）。【源 `db_router.py:266`】
- 启动横幅打印 `per_bucket / needed / budget` 与每桶建议值。【源 `db_router.py:288-318`】
- `placeholders.py` 的默认值必须与 `Settings` 默认值逐字相等，有导入期断言。【源 `db_router.py:331-354`、`placeholders.py:74-90`】
- `data_session()` 在 `app/` 内**零调用点**，且有 AST 守卫 T-5-7 断言"零调用点 + 只定义一次"。【源 `db_router.py:247`、`tests/scenario/test_step5_db_router.py:202-218`】

### 0.3 部署现实

- 应用连接使用容器初始化账号 `POSTGRES_USER`（compose 默认 `user`），即**集群超级用户**。【源 `code/docker-compose.yml:9`、`:135`】
  - 【推导】`superuser_reserved_connections`（PG 默认 3）**不会**挤压应用可用连接数；预留量的唯一用途是给迁移、psql、监控、pg_dump 等临时连接。这一点决定了本文不拿"超用户预留"当论据。
- postgres 服务未设置 `max_connections`，即 PG 默认 100。【源 `docker-compose.yml:5-17`（无 `command`）、`docker-compose.prod.yml:24-38`（无 `command`）】
- api 池 20+20，worker 池 5+5。【源 `docker-compose.prod.yml:198-199`、`:293-294`、`docker-compose.yml:269-270`】

### 0.4 迁移链与数据面

- alembic 当前单头，最大版本 `0030`（Step 5 已落地的 db_routing 视图迁移）。【源 `code/apps/api/alembic/versions/` 目录】
- alembic CLI 使用 `get_settings().database_url`，可用 `DATABASE_URL` 环境变量指向任意库执行 `upgrade head`。【源 `alembic/env.py:18-30`】
- **数据面表带着控制面外键**：`documents.tenant_id → tenants.id`、`documents.kb_id → knowledge_bases.id ON DELETE CASCADE`、`documents.uploaded_by → users.id`。【源 `app/models/entities.py:170-171`、`:185`】
- kb 硬删除是真 `DELETE`（依赖 CASCADE）。【源 `app/api/kbs.py:139-158`】
- 检索 SQL 只读 `chunks` + `documents`，不 join 控制面表。【源 `app/services/retrieve/__init__.py:112-124`】
- 备份脚本只 dump 一个库（`$POSTGRES_DB`），且只在 release 时调用。【源 `code/scripts/backup.sh:16-18`、`scripts/release.sh:11`；S6-7 已扩展为 N 库，见 §4.8】

> 【推导】0.4 是本文最重要的发现：**"温档 = 同实例换个 database"不是改个 DSN 就能跑**。只要往场景库写 `documents`，`kb_id/tenant_id/uploaded_by` 外键就必须在场景库里有对应行；而"把控制面行复制进场景库"会造出第二个可写真相。Step 5 只做了路由件（`data_session` 零调用点），没有触碰这条线，所以 Step 6 的真实工作量远大于"接线"。

---

## 1. 范围与层级裁决

### 1.1 裁决：Step 6 = 温档（同实例、独立 database）

依据（全部是既有已确认事实，不是本文新提）：

1. 框架稿用户已确认的分库形态是"一期 = 温档（同实例不同 database），按场景键分库"。【源 框架稿 `:897`（Q11）、`:900`（Q10）、`:550`（DQ1）】
2. 热档触发条件（1.5 倍独立实例 / 95% 负载 / 30% 容量）全部是占位值，且依赖 U10 实测（框架 `:301`、`:1294`）。【源】
3. 交接文档对 Step 6 的表述是"首个场景迁出（同实例独立 database）"。【源 `交接文档-2026-11-22.md` §2】

Step 6 内**不**做：独立实例、PgBouncer、跨库检索、`db_binding` 的写接口（U-5-3）、会话历史搬移（DQ4）、迁移链拆分。

### 1.2 Step 6 的真实工程量（7 项，后文逐项裁决）

| # | 工作项 | 落点 |
|---|---|---|
| S6-1 | 预算口径收敛为 20/80（见 §2） | settings / placeholders / 2 个测试文件 / 4 处文档 |
| S6-2 | 场景库建库 + 全链迁移 + 跨库外键手术 + 守卫（见 §4.2） | 脚本 + 测试 |
| S6-3 | 读写两段式：控制面先解析、数据面后执行（见 §4.6） | 11 个控制面调用点 |
| S6-4 | `data_session()` 接线 + 重写 T-5-7 守卫（见 §4.6） | `db_router.py` / 请求层 / 测试 |
| S6-5 | 存量搬迁 1 个场景 + 校验 + 回滚（见 §4.3-4.5） | 运维脚本 |
| S6-6 | kb 硬删除门禁 + 跨库清理（见 §4.7） | `api/kbs.py` |
| S6-7 | 备份扩展到 N 库 + 备份窗口记录（见 §4.8） | `scripts/backup.sh` |

### 1.3 层级方案评分

评分维度与权重（全文统一）：与已实现代码/已验收口径的连续性 20、与部署现实一致 25、可验证性 20、改动面 15、可回滚性 10、口径单一性 10。

| 方案 | 维度得分 | 总分 |
|---|---|---|
| S-温：同实例独立 database | 20 + 24 + 18 + 12 + 9 + 10 | **93** |
| S-热：独立实例 | 12 + 8 + 12 + 6 + 6 + 8 | **52** |

推荐 S-温（93 ≥ 90，可进入开发）；S-热 留待 U10 实测后立项。

---

## 2. 连接预算口径统一（核心矛盾）

### 2.1 候选口径

**口径 A：全仓统一 10/90（改手册）**
- 算式不变：`needed = 40×N×K + 10`，`budget = 100 − 10 = 90`。
- 改动：只改 `部署标定手册.md:158`、`:171` 与 §6 第 9 项（`:258`）的 20 → 10。
- 代码、框架、Step 5 测试全部不动。

**口径 B：全仓统一 20/80（改代码默认）**
- `db_conn_reserved` 默认 10 → 20；`budget = 80`。
- 改动：`settings.py:82`、`placeholders.py:75`、`tests/config/test_step5_settings.py:24`、`:78`、`tests/scenario/test_step5_db_router.py:112/122/132/142`、`:169-171`、`:173-181`，以及 §2.5 列的文档。
- 手册 §4 保持不变（本来就是 20）。

**口径 C：代码默认留 10，开启分库时强制 ≥20（加启动校验）**
- 关闭态沿用 10/90；`scene_db_split_enabled=True` 且 `db_conn_reserved < 20` → 启动拒绝（`DB_CONN_RESERVED_TOO_LOW`）。
- 改动：`db_router.check_connection_budget()` 增加一条判定 + 1 个新测试；默认值不动。
- 代价：同一常量存在"默认态/生产态"两个有效值，横幅必须打印"生效值"，否则运维看到的 90 与实际闸门 80 不一致。

### 2.2 四档占用与判定（`needed == budget` 放行，`needed > budget` 拒绝）

默认每桶 40（`DB_POOL_SIZE=20 + DB_MAX_OVERFLOW=20`）：

| 档 | 算式 | needed | A（90） | B（80） |
|---|---|---|---|---|
| N=1, K=1 | 40×1×1+10 | 50 | 通过（余 40） | 通过（余 30） |
| N=1, K=2 | 40×1×2+10 | 90 | **通过（余 0）** | 拒绝（超 10） |
| N=2, K=1 | 40×2×1+10 | 90 | **通过（余 0）** | 拒绝（超 10） |
| N=2, K=2 | 40×2×2+10 | 170 | 拒绝 | 拒绝 |

Step 6 推荐的每桶 20（`DB_POOL_SIZE=10 + DB_MAX_OVERFLOW=10`）：

| 档 | 算式 | needed | A（90） | B（80） |
|---|---|---|---|---|
| N=1, K=1 | 20×1×1+10 | 30 | 通过（余 60） | 通过（余 50） |
| N=1, K=2 | 20×1×2+10 | 50 | 通过（余 40） | 通过（余 30） |
| N=2, K=1 | 20×2×1+10 | 50 | 通过（余 40） | 通过（余 30） |
| N=2, K=2 | 20×2×2+10 | 90 | **通过（余 0）** | **拒绝（超 10）**，须降到每桶 15（70/80） |

【推导】关键差异只有一处：**needed 落在 81~90 的配置**。A 全部放行（余量为 0），B 全部拒绝。Step 5 自己的文档把 N=1/K=2/每桶 40 的 90/90 描述为"恰好触顶，零余量，任何临时连接都会挤爆"（Step 5 §A.3.4.2）；一个把该配置放行的闸门，等于没有闸门。这是 B 的核心理由，也是 A 的致命弱点。

【源】交接 §5.1 "needed ≤ 100−10 = 90、推荐 Step 6 每桶降到 20"；在 B 口径下，"每桶 20 + N=2 + K=2" 会被拒，推荐值必须下调为 15（推导，非源）。

### 2.3 对既有测试的影响

| 文件 | 位置 | A | B |
|---|---|---|---|
| `tests/config/test_step5_settings.py` | `:24`、`:78` | 不动 | 10 → 20 |
| `tests/scenario/test_step5_db_router.py` | `:112/122/132/142` 的 expected 第四元 | 不动 | 90 → 80 |
| 同上 | `:170` 断言 `"90" in message` | 不动 | 改为 `"80"` |
| 同上 | `:173-181` 边界用例（needed 90 == budget 90） | 不动 | 必须重设计：`db_max_connections_for_budget=70` + reserved 20 → budget 50，配 `pool=20/overflow=20/N=1` → needed 50 |
| `tests/scenario/test_step5_db_routing_report.py` | `:29`、`:137-152` | 不动 | 不动（`budget=90` 是报告夹具的独立入参，不读 Settings） |

【源】`test_step5_db_routing_report.py:29` 的 `budget: int = 90` 是构造参数，不是 Settings 派生物；B 不需要改它。

另外两处与预算无关、但 Step 6 必然要动的既有测试：
- T-5-7 断言 `data_session` 零调用点（`test_step5_db_router.py:202-218`）——接线后必然失败，必须改写为"调用点白名单"。
- T-5-8 断言 `engine_for/data_session` 不读开关（`:221-241`）——接线方案应保持成立（路由判定在请求层，不在 engine 层），不要为了省事破坏它。

### 2.4 评分

| 方案 | 连续性 20 | 部署现实 25 | 可验证 20 | 改动面 15 | 可回滚 10 | 单一性 10 | 总分 |
|---|---|---|---|---|---|---|---|
| A 10/90（改手册） | 20 | 11 | 20 | 14 | 10 | 10 | **85** |
| B 20/80（改代码） | 18 | 23 | 19 | 12 | 9 | 10 | **91** |
| C 默认 10/开启态 20 | 18 | 21 | 18 | 12 | 9 | 7 | **85** |

- A 的 11 分：手册一改，三处"一致"了，但闸门在 90/90 放行零余量配置，等于把 Step 5 自己标注的"必然挤爆"合法化。
- B 的 18 分：公式、函数签名、调用点全不变，只改一个**代码注释已自认"设定值不是实测"**的常量；代价是 2 个测试文件的字面量重基线。
- C 的 7 分：同一常量两个有效值，横幅与实际闸门可能不一致，属于"两个真相"的轻量版。

### 2.5 推荐：口径 B（91）

落地清单（按顺序执行，全部完成才算收敛）：
1. `settings.py:82` `db_conn_reserved: int = 20`，注释改为"预留 20 = 迁移 1 / pg_dump 1~2 / 监控 1~2 / psql 排障若干，设定值非实测"。
2. `placeholders.py:75` 的 `DB_CONN_RESERVED` 默认值同步为 20（导入期断言会强制两者相等）。
3. `tests/config/test_step5_settings.py:24`、`:78`。
4. `tests/scenario/test_step5_db_router.py:112/122/132/142`、`:170`、`:173-181`（边界用例按 §2.3 重设计）。
5. 文档：Step 5 设计 §A.3.4.1 表、§B.4.3 横幅示例、附录 B 的"预留由 10 改为 20"；框架稿 `:341`；交接 §5.1。
6. 新增回归：`Settings().db_conn_reserved == 20`、`_arithmetic(默认) == (40,1,50,80)`、`N=1/K=2/每桶 40` 必须 raise。
7. 手册 §4 不动；在 §4.1 补一句"应用以集群超级用户连接，superuser_reserved 不挤压应用"。

验收：三个 Step 5 测试文件全绿；启动横幅打印 `50/80`；`N=1/K=2/每桶 40` 启动被拒且提示三个可调项。

> 若用户选 A：必须在手册里写明"90/90 属零余量、禁止上线"，并把它降级为"文档纪律"而非闸门约束——这正是 A 只有 85 分的原因。若选 C：横幅必须打印生效值，否则视为未完成。

---

## 3. U10 是否 Step 6 硬前置的裁决

### 3.1 拆成两问

| 问题 | 裁决 | 依据 |
|---|---|---|
| Q-U10-a：Step 6 温档是否必须实测 `M_scene_pg`？ | **否** | 温档增量只有 catalog + WAL + 连接，不乘 `M_scene_pg` |
| Q-U10-b：热档升级 / 实例规格规划是否必须实测？ | **是** | 热档要独立实例，`M_scene_pg` 直接决定规格与"N 个热场景"上限 |

### 3.2 依据（源）

- `M_scene_pg = 300MB` 明确是"示例假设"。【源 框架稿 `:401`】
- 温档定义："同实例只增 catalog 与 WAL/连接，增量远小于 `M_scene_pg`"。【源 框架稿 `:422`】
- "只有热场景才乘 N，温场景共享 shared_buffers"。【源 框架稿 `:415`】
- Step 5 只登记 U10 为"待标定"，未把它作为闸门条件。【源 Step 5 §G-R2、§E】

【推导】把 U10 列为 Step 6 硬阻塞，等于用热档的内存模型去卡温档的上线；而温档在结构上不可能出现"每场景 300MB shared_buffers"——shared_buffers 是实例级单例。因此"U10 阻塞 Step 6"这一表述应修正为"U10 阻塞热档与容量规划，不阻塞温档 Step 6"。

### 3.3 温档不阻塞的替代验收信号（可执行）

| 信号 | 命令/观测 | 通过判据 |
|---|---|---|
| shared_buffers 单例 | 在两个库的会话里各执行 `SHOW shared_buffers` | 两值完全相同（证明共享，不是每库一份） |
| 库级体积 | `SELECT datname, pg_database_size(datname) FROM pg_database` | 场景库体积 ≈ 搬迁数据量 + catalog；记录为基线 |
| 连接落点 | `SELECT datname, count(*) FROM pg_stat_activity GROUP BY 1` | 检索期间只有场景库连接增长，控制库不增长 |
| 缓存命中（分库） | `SELECT datname, blks_hit, blks_read FROM pg_stat_database` | 记录基线；命中率不得因分库出现断崖（断崖 = 需升级热档） |
| 检索等价 | 同一查询在控制库与场景库各跑一次，逐条比对 id 与内容 | 结果集逐条相等 |

阈值口径：以上均为"记录基线 + 相对比较"，**不设绝对 MB 阈值**（无实测出处）。U10 的数字回填只在热档立项时进行。

### 3.4 若仍主张阻塞：最小实测清单（阈值全部标注）

1. 同实例新建空库 → 记录容器 RSS 增量（`docker stats --no-stream`）。
2. 灌入本次真实搬迁量 → 跑 200 次检索 → 记录 RSS 峰值与 `blks_hit`。
3. 两库并发检索 → 记录 `shared_buffers` 争用（命中率下降幅度）。
4. `pg_database_size` 与 catalog 行数增量。

判定阈值：**待标定**（本文不编造）。若必须设门禁，阈值须由用户或部署方给出书面值，并写入手册；在此之前 U10 只能作为观测项。

### 3.5 评分

| 方案 | 连续性 20 | 部署现实 25 | 可验证 20 | 改动面 15 | 可回滚 10 | 单一性 10 | 总分 |
|---|---|---|---|---|---|---|---|
| U-1 不阻塞温档，U10 仅作观测 | 20 | 24 | 19 | 14 | 10 | 9 | **96** |
| U-2 U10 硬阻塞 Step 6 | 10 | 8 | 15 | 10 | 10 | 8 | **61** |

推荐 U-1（96）。

---

## 4. 多库迁移顺序与回滚清单（DQ2）

### 4.1 搬迁表集（推导 + 守卫，不手写死清单）

候选集 = 以 `documents` / `chunks` 为根、按 `kb_id` / `document_id` / `chunk_id` 归属场景的表。【推导，逐表核对 `entities.py`】

`documents`（`:167`）、`chunks`、`kb_terms`、`graph_entities`、`graph_entity_mentions`、`graph_relations`、`wiki_pages`、`parse_jobs`、`parse_segments`、`governance_jobs`。

不搬（留控制库）：`tenants`、`users`、`knowledge_bases`、`kb_members`、`scenarios`、`scenario_versions`、`integration_*`、`audit_logs`、`tenant_quotas`、`feature_flags`、`prompt_versions`、`sensitive_words`、`departments`、`groups`、`user_*`、`eval_*`、`sync_sources`、`conversations`、`messages`。

守卫（防未来迁移漏表）：一条 SQL 列出所有含 `kb_id`/`document_id`/`chunk_id` 列、却不在搬迁白名单里的表，非空即 FAIL，纳入演练与 CI 的 schema 检查。

> 会话历史（`conversations`/`messages`）本期留控制库，与框架 DQ4"放场景库可接受"存在偏移；这是**缩小本期改动面**的主动选择，需用户确认（见 §9）。

### 4.2 场景库 schema 策略（必须先裁决，否则迁移步骤无从写）

| 方案 | 做法 | 总分 |
|---|---|---|
| M-A 锚点行 | 全链建表 + 把被引用的 tenants/kb/users 行复制进场景库 | 70 |
| **M-B 跨库外键手术** | 全链建表 + 删除指向控制面的 FK + 白名单守卫 + 写入触发器 | **92** |
| M-C 拆控制链/场景链 | alembic 分支，场景库只建数据面表 | 81 |

推荐 **M-B**，理由：
1. 不改迁移历史（30 个 revision 的 id 与 `alembic_version` 不动），控制库零风险。
2. 不复制控制面行，不产生第二个可写/可读真相（M-A 的致命点）。
3. 跨库 CASCADE 本来就不可用（不同 database 之间 CASCADE 永远不触发），保留 FK 只会制造"完整性成立"的假象；M-B 直接承认并改由 SOP 承担。
4. 分叉可被机器检测：每次 `alembic upgrade` 后跑一次手术脚本，再跑白名单守卫；守卫非空即失败，不靠承诺。

M-B 的具体规则：
- 手术脚本：查询 `pg_constraint` 中 `confrelid ∈ {tenants, knowledge_bases, users}` 且 `conrelid ∈ 搬迁集` 的外键，逐条 `ALTER TABLE ... DROP CONSTRAINT ...`。按查询结果动态执行，不硬编码约束名（约束名由 SQLAlchemy 生成）。
- 白名单守卫：手术后上述查询必须返回 0 行；升级后再查，非 0 即说明新迁移重新引入了跨库 FK，须重跑手术。
- 场景库控制面表写入触发器：对 `tenants`/`knowledge_bases`/`users`/`scenarios`/`audit_logs` 建 `BEFORE INSERT OR UPDATE OR DELETE ... FOR EACH STATEMENT` 的拒绝触发器。这是把框架 I-8"控制面元数据不随场景分裂"从"文档承诺"变成"数据库强制"，且可测（写入必须报错）。
- I-8 措辞需修订：原句是"场景库里不得出现 audit_logs / scenario_profile / tenant_quotas"；全链建表会物理存在这些表（空表）。建议改为"场景库的控制面表必须存在但恒为空且禁止写入"。此项须用户批准（见 §9），否则 M-B 字面上违反 I-8。

实现补强（P3 实测发现）：
- 白名单守卫的列集合必须包含 `tenant_id`：只查 `kb_id`/`document_id`/`chunk_id` 时，只含 `tenant_id` 的新表会逃逸守卫。metadata 现有表全部落在搬迁集或控制面集内，加入后不产生误报。
- 手术前必须拒绝控制面非空的库：先统计控制面各表行数，任一非空即抛异常并中止，不执行任何 DDL。这是 fail-closed 的前置条件校验，防止 `--dsn` 误指向控制库时删掉控制库外键并装上禁写触发器。
- 触发器校验必须过滤 `pg_trigger.tgenabled`（仅认 `O`/`A`）：被 `ALTER TABLE ... DISABLE TRIGGER` 禁用的触发器实际不生效，必须视为缺失，否则 verify 会假 PASS。

### 4.3 迁移顺序（确定顺序，含控制库与场景库）

不假设在线迁移。全程维护窗口。顺序的核心原则：**被写入方的库先就绪，权威方的事实最后改**。

**Phase 0 前置（只读 + 备份）**
1. 跑 `db_routing_report`，八项全 PASS。
2. 全库备份：控制库 + 现存所有场景库（当前 `backup.sh` 只备份一个库，必须先扩，见 §4.8）。
3. 记录基线快照：两库 `SELECT version_num FROM alembic_version`、搬迁集逐表行数、`SHOW max_connections / shared_buffers`、`config_fingerprint`。
4. 停写：`docker compose stop worker`，暂停该场景的上传与解析入口（当前无"冻结单场景"开关，只能靠停 worker + 运维通知）。

**Phase 1 建库与迁移（场景库先到 head，控制库不动）**
5. `CREATE DATABASE kagent_<scene>`（`CREATE DATABASE` 不能在事务块内，用 `psql -c`）。
6. 场景库执行 `alembic upgrade head`（全链 0030）。
7. 断言：场景库 `version_num='0030'`；控制库 `version_num` 仍为 `0030`（未被动过）；场景库控制面表行数为 0。
8. 执行 M-B 手术脚本；再跑白名单守卫，必须 0 行；安装控制面写入触发器。

**Phase 2 接线（不切流量）**
9. 配 `SCENE_DB_DSN={"kb_<scene>": "..."}`，先保持 `SCENE_DB_SPLIT_ENABLED=false`，启动 api，断言报告项⑥（键集覆盖、无缺口）PASS。
10. 断言预算：`needed ≤ budget`（B 口径下 50 ≤ 80）。
11. 打开开关、重启，断言横幅生效值与报告一致、`created_engines=1`。此时无 `db_binding`，没有请求会进场景库。

**Phase 3 数据搬迁（幂等、可重跑）**
12. 按依赖顺序 `BEGIN; TRUNCATE <搬迁集> CASCADE;` 再逐表 COPY；单场景过滤条件为 `kb_id = ...`（或经 `documents` 关联）。
13. 行级导出**不能用 `pg_dump`**：pg_dump 只有 `-t/--table` 与 `--exclude-table-data`，没有行过滤（本机已核实 `pg_dump --help`，无 `--where`）。必须用 `COPY (SELECT ... ) TO STDOUT | psql -c "COPY ... FROM STDIN"`。
14. 校验：逐表 `count(*)` 与 `md5(string_agg(id::text, ',' ORDER BY id))` 双库相等；`chunks` 另加 `sum(length(content))`。
15. 校验检索等价（见 §3.3 最后一行）。

**Phase 4 切流量与观察**
16. 最后一步才改事实：`UPDATE scenarios SET tier='warm', db_binding='kb_<scene>' WHERE ...`（单行 update）。0031 起 `tier` 与 `db_binding` 受 CHECK 同句约束（cold ⇔ NULL），只改一列会被数据库拒绝。
17. 重启 api（或等 `>=60s × 副本数` 的路由刷新窗口），观察 `pg_stat_activity` 的 datname 分布与检索延迟。
18. 恢复 worker，观察解析写入是否落场景库。

### 4.4 "部分成功"的判定点（5 个）

| 判定点 | 判据 | 状态 |
|---|---|---|
| P1 建库成功但迁移失败 | `alembic_version` 为空或 < 0030；`information_schema.tables` 数量少于 head 基线 | 半建库，目标库无流量 |
| P2 迁移成功但手术未做 | 白名单守卫返回非 0 行 | 存在跨库 FK，写入会因缺锚点失败 |
| P3 搬迁中断（某表行数不等 / md5 不等） | 逐表 count/md5 与源不一致 | 目标库数据不完整 |
| P4 绑定已改但副本未全部刷新 | 各副本 `pg_stat_activity.datname` 分布不均；部分请求报缺表/空结果 | 路由分叉（窗口 = 60s × 副本数） |
| P5 开关已开但 DSN 缺键 | 报告项⑥ FAIL；启动被拒或请求 500 | 配置缺口 |

处置原则：P1/P2/P3 时目标库尚无业务流量，直接 `DROP DATABASE` 重建（P1/P2）或 `TRUNCATE` 重搬（P3），不涉及回滚控制库；P4/P5 走 §4.5 的回滚六步。

### 4.5 回滚六步（命令 + 状态判据）

以"已改 `db_binding`、已开开关"的最坏情况为例：

| 步 | 动作 | 命令/判据 |
|---|---|---|
| R1 停写 | 停 worker，暂停上传 | `docker compose stop worker`；控制库 `documents` 的 `n_tup_ins` 不再增长 |
| R2 快照 | 记录现场 | 两库 `alembic current`、逐表 count、`scene_db_split_enabled`、`db_binding` 值、`config_fingerprint` |
| R3 改事实 | 先清绑定（同句降回 cold） | `UPDATE scenarios SET tier='cold', db_binding=NULL WHERE id=...` |
| R4 重启并验证落点 | 重启 api | `pg_stat_activity` 只出现控制库 datname；无 500 |
| R5 校验数据 | 控制库行数 == R2 快照 | 逐表 count 相等；不等说明搬迁窗口有漏写，需补差后再继续 |
| R6 关开关 | 关 `SCENE_DB_SPLIT_ENABLED` + 重启 | 横幅显示"关闭"；`created_engines=0`；场景库**保留不删**作为证据，用户确认后再 `DROP DATABASE` |

顺序纪律：**必须在同一句把 `tier='cold', db_binding=NULL` 改回，再关开关**（0031 的 CHECK 不允许只清 `db_binding`）。反过来会出现"开关已关、绑定仍在"的半状态，报告项会 FAIL 且请求可能落到已关池的键上（虽然 `engine_for` 不读开关，但此时语义已混乱）。

### 4.6 读写两段式（否则分库写不进去）

现状：`data_session()` 零调用点，所有请求走控制库。【源 `db_router.py:247`、`test_step5_db_router.py:202-218`】

Step 6 必须把"路由到场景库"的请求改成两段：
1. 控制库会话：解析 `kb_id → tenant/kb/权限`，取完即关（不做跨库事务）。
2. 场景库会话：执行数据面读写（`documents`/`chunks`/`parse_jobs` 等）。

要改的控制面调用点（逐个确认是"先控制后数据"还是"纯控制"）：`api/chat.py:317`、`api/wiki.py:104`、`api/kbs.py:50,149,179,204,231,280,461`、`api/sync.py:66,113`。【源】

纪律：两段**顺序执行、不重叠**，所以在途连接仍是 1 条；不得为了省一次连接把 kb 校验放进场景库（场景库的控制面表恒为空，必然查不到）。

### 4.7 删除门禁（跨库 CASCADE 失效的必然结果）

`documents.kb_id` 是 `ON DELETE CASCADE`，但跨 database 永不触发。【源 `entities.py:171`、`kbs.py:158`】

规则：
1. `DELETE /kbs/{kb_id}` 若该 kb 属于某个 `db_binding` 非空的场景 → 返回 409，附清理脚本入口。
2. 清理脚本：维护窗口内先删场景库 `documents`（连带 `chunks` 等搬迁集），再删控制库 kb。
3. 守卫：删除后跑一次"孤儿检查"——场景库中存在 `kb_id` 已不在控制库 `knowledge_bases` 的行数必须为 0。

### 4.8 备份窗口与 RPO/RTO

原状：`scripts/backup.sh` 只 dump `$POSTGRES_DB` 一个库，plain SQL 格式，且只在 `release.sh:11` 调用。【源】

- 【推导】分库后若不改脚本，场景库**完全没有备份**。这是 Step 6 的阻断项，不是优化项。

**S6-7 落地（2026-11-23）**：`code/scripts/backup.sh` 已扩展为 N 库备份。

- 枚举口径：容器内 `psql -Atc "SELECT datname FROM pg_database WHERE datistemplate = false AND datallowconn = true"`，宿主侧再排除维护库 `postgres`。不读 `SCENE_DB_DSN` 配置清单——温档同实例下，只有"服务器上真实存在的库"能保证不漏库，配置清单会与现场漂移。
- 格式：每库一个 `db_<name>.dump`，`pg_dump -Fc`（custom，压缩且可 `pg_restore` 选择性恢复）。任一库导出失败即整次备份非零退出，`release.sh` 随之中止（不会带着无备份状态发布）。
- 记录：`manifest.txt` 写入 `backup_stamp / started_at / finished_at / window_seconds / database_count / rpo_policy`，以及每库的 `seconds / bytes / file`（Tab 分隔行，便于汇总 U13 窗口）。RPO 政策值随清单落盘，不依赖口头约定。
- 恢复口径同步：`pg_restore` 按库恢复，见 `code/docs/DEPLOY.md` §5。

**RPO 政策初值（用户 2026-11-23 拍板）**：每日一次全库 dump（政策值，非实测，不可当 SLA）。实施方式为 crontab 每日调用 `bash scripts/backup.sh` 并转存异地；`release.sh` 的发布前备份保留，二者不冲突。

- 备份窗口：待标定（U13）。测法：按 `manifest.txt` 的每库 `seconds` 汇总，分别观察串行（当前实现）与并行两种方式的窗口；把实测值回填手册 §6 第 13 项。
- RTO：待标定。测法：`pg_restore` 到临时库并计时，判据 = 恢复后可完成一次检索且行数校验通过。

### 4.9 单库演练测不出的三类状态

1. **跨库部分提交**：审计写控制库成功、业务写场景库失败。单库同事务永远同生共死，测不出"一边有一边没有"。
2. **两个池的预算竞争**：控制池与场景池在同一实例上同时打满，互相挤压 `max_connections`。单库只有一套池。
3. **跨库 CASCADE 失效**：删 kb 后场景库残留 documents。单库演练中 CASCADE 正常工作，问题被掩盖。

另有两点也应记录：N 库串行备份的窗口叠加；60s × 副本数的路由分叉窗口。三者（含这两点）只有真实例/多副本演练才能覆盖。

### 4.10 S6-3 实现补强（2026-11-23，用户拍板）

设计 §4.6 只覆盖读路径的 11 个 kb_id 键控调用点，未覆盖两条必需路径；开工前核对发现并以打分交用户选择：

| 决策 | 选择 | 分数 | 落点 |
|---|---|---|---|
| D-S6-3-1 写路径库位来源 | 队列负载携带 db_key | 90 | 入队点（upload/sync/governance/graph/wiki）+ worker 全链 data_session(db_key)，缺失即失败 |
| D-S6-3-2 按 ID 寻址端点 | 控制面副本优先 + 缺失时探测（2026-11-23 修订，原「顺序探测」作废） | 91 | documents.py 6 端点 + wiki 详情：控制库有副本 → 按其 kb_id 解析库位（不做全库扫描，避免与迁移副本冲突）；控制库无副本（切绑后新文档）→ 探测已注册场景库，0 命中 404、1 命中该键、≥2 命中 fail-closed。wiki 详情经 document_id 转 kb；控制面副本只用于取归属，内容取自库位会话 |

未选项与分数：写路径「worker 按 ID 探测」84、「本期不接写路径」66；ID 端点「新增控制库派生索引表」74、「本期不支持」66。

实施纪律：
- 关闭态（`SCENE_DB_SPLIT_ENABLED=false`）零新增查询、零新增连接：库位解析直接返回 `default`，数据面会话复用控制面会话。
- 仅绑定库位才走「控制面 commit 释放连接 → 场景库会话」两段，顺序不重叠。
- 库位只由已认证上下文推出（I-7），不接受调用方指定。
- `default` 也计为一个库位：一次请求里同时出现默认库 kb 与绑定库 kb，即判为跨库 → 409，不得只查场景库而丢掉默认库那一半结果。

### 4.11 S6-5 实施口径（2026-11-23，落地为脚本）

落点：`code/apps/api/app/infra/scene_db_migrate.py`（表集、过滤键、校验 SQL 的唯一来源）＋ `code/apps/api/scripts/scene_db_migrate.py`（CLI）。实现已按 §4.3 Phase 3 完成并经真库双库测试。

- 搬迁方式：逐表 `COPY (SELECT <列清单> FROM <表> WHERE <kb 过滤>) TO STDOUT` → 临时文件 → 目标库 `COPY <表>(<列清单>) FROM STDIN`；列清单从 `information_schema.columns` 发现并排除生成列（`chunks.fts_vec`、`graph_entities.name_vec` 显式写入会直接失败）。**不使用 pg_dump 行过滤**（其无 `--where`）。
- 过滤键逐表显式声明：7 张 `kb_id` 直连；`parse_jobs` / `governance_jobs` 经 `documents`；`parse_segments` 经 `parse_jobs → documents`。COPY 查询只用 `$1` 绑定 kb，不拼字符串。
- 目标前置（缺一即拒绝，不降级）：先判「是否同一个库」，再判 M-B 手术三项 PASS、控制面表全空、目标库现有 `documents` 只属于本次 kb、源库该 kb 有文档（0 篇通常是 `--kb-id` 写错）、两侧 `alembic_version` 相同。同库判定用**服务端视角**（`current_database()` + `inet_server_addr()` + `inet_server_port()`），不比较 DSN 字符串 —— `localhost` 与 `127.0.0.1` 指向同一实例时必须判为同库。
- 目标清空用 `TRUNCATE <搬迁集>`（**不写 CASCADE**）：搬迁集内部外键已由列表覆盖；将来若冒出外部表引用搬迁集，这里应显式失败而不是连带清空未列出的表。这是对 §4.3 第 12 步原文 `CASCADE` 的有意收窄。
- 校验：逐表 `count(*)` + `md5(string_agg(id::text, ',' ORDER BY id))`；`chunks` 另加 `sum(length(content))` 与按 `(document_id, chunk_index, id)` 排序的正文摘要。可选 `--probe-term` 在两侧各跑一次 ILIKE 探测并比对命中 id 摘要 —— 这是**关键词谓词等价**，不代表整条检索链路等价；未提供时报告写「未执行」，不得写成 PASS。
- 写暂停覆盖 COPY 与切绑：脚本在 COPY 前后各取一次源库摘要，不等即回滚并报 `S6_MIGRATE_SOURCE_CHANGED`。停写窗口必须同时覆盖 Phase 3 的 COPY 与 Phase 4 的改绑 —— 改绑完成后写入若仍落控制库，场景库会缺新行；正向校验失败时目标事务整体回滚，重试靠 TRUNCATE 重搬，不手工补行。
- 回滚前回拷增量：`--reverse` 走「源行 COPY → 目标临时表（LIKE） → `INSERT ... ON CONFLICT (id) DO NOTHING`」，只增不删、可重跑；两侧不同库且 `alembic_version` 相同才执行；逐表报告 stage / existing / inserted 作为 R5 差集留痕。同一 id 内容已被改写的行不会被覆盖（脚本不静默改数据），需人工比对后处理。
- 绑定版本化与 Phase 4 分离：脚本**不修改** `scenarios.db_binding`；改绑是单独一步，R2 快照必须记录改动前的 `db_binding` 原值，回滚按 §4.5 R3「先置 NULL、再关开关」执行。

### 4.12 S6-6 实施口径（2026-11-23，落地为脚本）

落点：`code/apps/api/app/infra/scene_db_cleanup.py`（清理与孤儿判定的唯一来源）＋ `code/apps/api/scripts/scene_db_cleanup.py`（CLI）。API 侧 409 门禁（`KB_IN_BOUND_SCENE` / `KB_SCOPE_CONFLICT`）在 S6-3 已实现，本节只补运维侧。

- 清理：`--kb-id` + `--apply` 时，按 `MIGRATION_ORDER` 的**严格逆序**在单事务内删除该 kb 的搬迁集行（先子表后父表），删完立即复核各表行数必须全为 0，否则报 `S6_CLEANUP_VERIFY_MISMATCH`。不带 `--apply` 只做只读统计，用于工单确认。
- 身份校验复用搬迁侧：先判非同一库（服务端视角）、再判手术 PASS、控制面表全空；控制库不存在的 kb 默认拒绝（`S6_CLEANUP_KB_MISSING`），孤儿清理必须显式 `--allow-missing-kb`。
- 控制库不删：脚本不触碰控制库任何行；kb 行仍由 API 删除以保留审计（`kb.delete`），其数据面残留靠原有 FK CASCADE 清掉。
- 孤儿检查（§4.7 规则 3）两个方向，任一非零即退出码 1：
  · 场景库：7 张 `kb_id` 直连表按 kb 分组，凡 kb 不在控制库 `knowledge_bases` 即孤儿；`parse_jobs`/`governance_jobs` 经 `documents`、`parse_segments` 经 `parse_jobs → documents`，父实体缺失的行记为 `<missing_parent>` 同样算孤儿；
  · 控制库：数据面行若父实体（kb / document / job）已不存在，记为陈旧副本（正常恒为 0，非零说明 FK 被破坏）。

---

## 5. 本机容器演练方案与验收标准

### 5.1 三档分类

| 档 | 可验证内容 |
|---|---|
| 纯逻辑可验（无需 DB） | 预算算术四档与边界；迁移顺序拓扑（两库 version 断言脚本）；回滚六步的状态机；`db_routing_report` 的缺口判定；DSN 脱敏 |
| 本机容器可验（同实例多 database） | 多库 `alembic upgrade`；`pg_stat_activity.datname`；`pg_database_size`；锁等待（`pg_locks`）；dump/restore 回滚；M-B 外键手术与守卫；`COPY` 行级搬迁与 md5 校验；两库会话 `SHOW shared_buffers` 相同 |
| 必须真独立实例 | 独立 shared_buffers 下的缓存争用与 RSS 峰值；独立 `max_connections` 隔离；独立故障域；N 库并行备份窗口的真实值 |

### 5.2 当前机器前置（实测）

| 项 | 状态 | 处置 |
|---|---|---|
| docker daemon | 可用（Server 29.4.0） | 无需处置 |
| `code-postgres-1` | 运行中（2026-11-23 演练时 `pg_isready` 通过） | 无需处置；此前的 Exited(255) 已恢复 |
| `code-worker-1` | 演练未依赖 worker | 本次 D0-D9 不经过解析写入，未复核其状态 |
| 独立实例 | 无 | 本机不验，留热档 |

### 5.3 演练步骤与判据

| 步 | 命令（PowerShell） | 通过判据 |
|---|---|---|
| D0 环境 | `docker start code-postgres-1`；`docker exec code-postgres-1 pg_isready -U user -d kagent` | 输出 `accepting connections` |
| D1 基线 | `docker exec code-postgres-1 psql -U user -d kagent -c "SHOW max_connections; SHOW shared_buffers; SELECT version_num FROM alembic_version;"` | version=0030；记录 max_connections/shared_buffers |
| D2 建库 | `docker exec code-postgres-1 psql -U user -d postgres -c "CREATE DATABASE kagent_finance"` | 返回 `CREATE DATABASE` |
| D3 迁移 | `docker compose -p code run --rm -e DATABASE_URL=postgresql+asyncpg://user:<口令>@postgres:5432/kagent_finance migrate` | 场景库 version=0030；控制面表 0 行 |
| D4 手术 + 守卫 | 执行 M-B 手术 SQL；再查跨库 FK | 手术后跨库 FK 查询 0 行 |
| D5 触发器 | 对场景库执行 `INSERT INTO tenants ...` | 必须报错（写入被拒） |
| D6 预算 | 跑 `pytest tests/scenario/test_step5_db_router.py tests/config/test_step5_settings.py -q` | 全绿；横幅 needed/budget = 50/80 |
| D7 搬迁 | `psql -c "COPY (SELECT ... WHERE kb_id=...) TO STDOUT" \| psql -d kagent_finance -c "COPY ... FROM STDIN"` | 逐表 count + md5 相等 |
| D8 观测 | `SELECT datname, count(*) FROM pg_stat_activity GROUP BY 1`；`SELECT pg_database_size('kagent'), pg_database_size('kagent_finance')`；`docker stats --no-stream code-postgres-1` | 连接落点只在目标库；体积/ RSS 增量记录为基线 |
| D9 回滚 | 按 §4.5 R1-R6 执行；用 `pg_restore`/`psql` 恢复控制库备份 | R5 行数相等；R6 横幅"关闭"、`created_engines=0` |

> 命令里的口令从 `.env` 取，不写入文档或日志。`docker compose run` 会新建一次性容器，不在项目里留痕。

### 5.4 阻塞项声明

- ~~`code-postgres-1` 异常退出（255）原因未查明前，D1 之后的所有步骤不做。~~ 2026-11-23 演练时容器已恢复运行，D0-D9 已完成。
- worker 的 redis 不通是**既有环境问题**，与 Step 6 无关；本次演练不经过 worker，未涉及。
- 真独立实例、多副本 60s 分叉、N 库并行备份窗口三项本机不可验，明确标"不覆盖"。

### 5.5 演练结果（2026-11-23，本机 `code-postgres-1`）

| 步 | 结果 | 证据（摘要） |
|---|---|---|
| D0 环境 | PASS | `pg_isready` → `accepting connections` |
| D1 基线 | PASS | `max_connections=100`、`shared_buffers=128MB`、`version_num=0030` |
| D2 建库 | PASS | `CREATE DATABASE kagent_finance` |
| D3 迁移 | PASS | 场景库 `alembic upgrade head` → `0030`；控制面表 `tenants=0 / knowledge_bases=0` |
| D4 手术 + 守卫 | PASS | 删除跨库 FK 17 条、安装触发器 5 个；三项校验全 PASS（FK 0 / 守卫 0 / 触发器 5/5） |
| D5 禁写触发器 | PASS | `INSERT INTO tenants` 被拒（`控制面表 tenants 在场景库恒为空且禁止写入`），事后 `tenants=0` |
| D6 预算回归 | PASS | `test_step5_db_router.py + test_step5_settings.py` → 25 passed |
| D7 搬迁 | PASS | 技术知识库 10 表逐表 PASS（documents 8 / chunks 561 / kb_terms 467 / entities 365 / mentions 387 / relations 158 / wiki 6 / parse_jobs 8 / segments 0 / governance 24）；关键词探测 `control_hits=2 == scene_hits=2` |
| D8 观测 | PASS（基线） | 体积 `kagent=88.5MB`、`kagent_finance=26.7MB`；两库 `SHOW shared_buffers` 均为 128MB；演练时场景库连接 0、控制库 2（未接入检索流量，连接落点待上线观测） |
| D9 恢复 | PASS | 控制库 `pg_dump -Fc` → 临时库 `pg_restore`：`version=0030 / kbs=4 / docs=26 / chunks=580 / users=30` 两侧完全相等；随后清理并 DROP 演练库 |

演练收尾：用 S6-6 清理脚本把场景库 10 表删至 0、孤儿检查两向为空，再 DROP `kagent_finance` 与 `kagent_restore_check`；控制库行数保持 `kbs=4 / docs=26 / chunks=580` 未变。

未覆盖（本机不可验）：真独立实例的缓存争用/RSS 峰值、独立故障域、N 库并行备份窗口、多副本 60s 分叉；以及"接入检索流量后的连接落点"（需应用以分库态启动）。

---

## 6. 待用户拍板的决策清单

| # | 决策项 | 候选 | 分数 | 推荐 | 影响文件 | 不选它的后果 |
|---|---|---|---|---|---|---|
| 1 | 预算口径 | A 10/90、B 20/80、C 双态 | 85 / 91 / 85 | **B** | `settings.py`、`placeholders.py`、2 个测试文件、4 处文档 | 选 A：90/90 零余量配置合法化，闸门形同虚设；选 C：两个有效值，横幅与闸门可能不一致 |
| 2 | Step 6 层级 | 温档 / 热档 | 93 / 52 | **温档** | 本文件、迁移脚本 | 选热档：U10 未标定、实例翻倍、运维翻倍，且与 Q10/Q11 已确认结论冲突 |
| 3 | U10 是否阻塞 | 不阻塞（观测项）/ 硬阻塞 | 96 / 61 | **不阻塞** | 本文件、交接 §5.2 | 选阻塞：用热档内存模型卡温档上线，排期被无实测依据的数字阻断 |
| 4 | 场景库 schema 策略 | M-A 锚点 / M-B 外键手术 / M-C 拆链 | 70 / 92 / 81 | **M-B** | 手术脚本、守卫测试、迁移 SOP | 选 M-A：复制控制面行，产生第二可写真相；选 M-C：改写迁移历史，控制库风险不可接受 |
| 5 | 搬迁范围 | 只搬检索数据面 / 连会话历史一起搬 | 91 / 76 | **只搬检索面** | 搬迁脚本、`db_binding` 解读 | 选全搬：本期改动面翻倍，且 `conversations` 带更多控制面外键 |
| 6 | 演练范围 | 本机容器 / 追加真独立实例 | 92 / 88 | **本机容器**（真实例留热档） | 演练脚本 | 选真实例：本期无热档需求，成本换不来结论 |

> 第 1 项需要用户明确批准"修改 Step 5 已验收测试的字面量"；这是唯一会动到已验收基线的决策。

---

## 7. 魔鬼代言人（至少三个反例）

1. **反例 1：口径 B 会不会把本来能跑的部署挡在门外？**
   会，且仅限 needed 81~90 的配置。这类配置的共性是把 PG 的 100 条连接用到 90 条，只剩 10 条给迁移、pg_dump、监控、psql。Step 5 自己判定该区间"零余量、任何临时连接都会挤爆"。挡住它们不是误伤，是闸门本来的职责。代价是推荐每桶值要从 20 降到 15（N=2,K=2），并发上限下降 25%——这是必须让用户看到的真实代价。

2. **反例 2：M-B 的手术会不会在下次 `alembic upgrade` 后被悄悄还原？**
   会。任何新增/重建跨库 FK 的迁移都会把它加回来。所以守卫不是可选项：升级后必须跑白名单查询，非 0 即失败。若团队不愿意在每次升级后跑手术，M-B 就会退化成"某天突然写入失败"的定时炸弹——此时应选 M-C（拆链），而不是省掉守卫。

3. **反例 3：两段式（先控制后数据）是否违反"每在途 1 条连接"？**
   不违反。两段是顺序、不重叠的，同一时刻只占 1 条。但它把每个请求的**连接获取次数**从 1 次变成 2 次，池的获取队列压力上升。若某接口 QPS 很高，P=20 的池可能出现排队；这是 P 值的真实约束，需要在压测里看，而不是靠公式推。

4. **反例 4：场景库控制面表"存在但为空"是否已经违反 I-8？**
   字面上是。"全链迁移 + 触发器"把"I-8 的数据约束"变成了"I-8' 的写入约束"。若用户认为表存在本身不可接受，则必须选 M-C 拆链，工期另计。本文不替用户做这个价值判断。

5. **反例 5：备份窗口/RPO 全部"待标定"，是否意味着 Step 6 不可上线？**
   不是不可上线，而是**不可宣称有备份保障**。若场景库不进备份脚本，一次误删就是不可恢复。因此 §4.8 的脚本扩展是 Step 6 的准入项，RPO/RTO 的数字可以待标定，但"N 库都被备份到"必须在上线前验证。

---

## 8. 遗留与未知区

| 编号 | 内容 | 状态 |
|---|---|---|
| U10 | `M_scene_pg` 实测 | 待标定，仅热档需要 |
| U13 | N 库备份窗口 | 待标定，测法见 §4.8 |
| DQ2-① | 跨库迁移先后顺序 | 本文已定（§4.3） |
| DQ2-② | 部分成功回滚 | 本文已定（§4.4/§4.5） |
| DQ2-③ | N 库备份窗口数字 | 待标定 |
| 新 | 跨库 FK 与 CASCADE 失效 | 本文新增，M-B 裁决 + 删除门禁 |
| 新 | `data_session` 零调用点与两段式改造 | 本文新增，S6-3/S6-4 |
| 新 | I-8 措辞需修订为"存在但禁写" | 待用户批准 |
| 新 | 会话历史留在控制库，与 DQ4 推荐偏移 | 待用户确认 |

---

## 9. 需用户确认的问题

1. 是否批准把 `db_conn_reserved` 默认从 10 改为 20（预算 80），并同步重基线 2 个 Step 5 测试文件的字面量？这是唯一触及已验收基线的改动。
2. 是否确认 Step 6 只做温档（同实例独立 database），热档另立项？
3. 是否批准"全链迁移 + 跨库外键手术 + 白名单守卫 + 控制面写入触发器"（M-B），并把 I-8 的措辞从"不得出现"改为"必须存在但禁止写入"？若不批准，需改选拆链（M-C），工期另计。
4. 分库后 kb 硬删除如何处理：端点拒绝（409）+ 清理脚本，还是先把 kb 删除改成软删除？后者是额外功能，不在本裁决范围。
5. 会话历史（`conversations`/`messages`）本期留在控制库，是否可以接受这一与 DQ4 推荐的偏移？
6. 备份策略是否接受"每日一次全库 dump"作为 RPO 的政策初值（数字仍待实测回填）？
7. 推荐每桶值在 B 口径下需从 20 下调到 15（N=2,K=2），是否接受该并发上限？
