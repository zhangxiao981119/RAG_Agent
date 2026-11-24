# 第七阶段 Step 7（租户层收敛 + 治理面 + 容量分级）前置裁决

> 台账：文档类型 = 前置设计裁决（P1）；状态 = **待拍板**；产物 = 本文件（spec）+ `第七阶段-Step7-tasks.md`；本文件不改代码、不建测试。
> 阅读前置：`02-design/统一RAG平台-场景化配置-框架设计-v1.md` §3.3、§3A.4、§3A.5、§3A.8 D5、§6、§7、§7.2；`02-design/第五阶段-Step5-详细设计.md`；`02-design/第六阶段-Step6-前置裁决.md`；`交接文档-2026-11-22.md`。
> 标注约定：`【源】` = 直接读到的事实（给 file:line）；`【推导】` = 由事实计算的结论；`【待标定】` = 无实测出处，禁止当作阈值或承诺值使用。
> 打分维度与权重（全文统一）：与已实现代码/已验收口径的连续性 20、与部署现实一致 25、可验证性 20、改动面 15、可回滚性 10、口径单一性 10。

---

## 0. 一句话结论

Step 7 不是“再加一张租户表”，而是把已有三件资产收紧成一条可解释的覆写链：

- `scenarios` 已带 `tenant_id / status / db_binding`；`scenarios.status` 就是本租户的“可用场景清单”，不另造一张 enabled 表。
- `scenario_versions` 的状态机已能表达“钉定旧版本”，缺的只是“钉在哪一行、解析时认不认 archived”。
- `IntegrationApplication` 已有凭证、kb 白名单、应用级日限额；租户级日/月额度已跨场景汇总，配额不需要新概念。

推荐 package 得分 **95**：新增 **7 个 nullable 列** + **2 个只读/写治理接口** + **1 个容量接口**，不新建治理表、不改检索拓扑、不碰框架 §6 的 8 类维度、不把 `db_binding` 变成表单项。

---

## 1. 事实锚定

### 1.1 可直接复用的既有资产

| 资产 | 位置 | 现状 | Step 7 复用方式 |
|---|---|---|---|
| 租户单行 + 全表 `tenant_id` | `app/models/entities.py:31-45` | `MULTI_TENANT_ENABLED=False`，L1 实际只有一行 | 只做单租户收敛；不引入 RLS/分区（框架 Q2 已否决） |
| `scenarios` | `app/models/entities.py:549-587` | 有 `tenant_id / scenario_key / status(active,disabled) / is_builtin / db_binding` | 清单直接用 `status`；分级与 `db_binding` 绑定；不新增 `enabled` |
| `scenario_versions` | `app/models/entities.py:590-720` | `draft→validated→published→archived`；部分唯一索引保证单 published；`checksum` 乐观并发 | 钉定只加一个版本号；解析在钉定时接受 `published|archived` |
| 场景解析单查询 | `app/services/scenario/resolver.py:71-103` | `LOOKUP_SQL` 只取 `state='published'` | 保留原 SQL 作为关闭态；新增 governed SQL 走钉定 |
| 场景行缓存 + 失效 | `app/services/scenario/resolver.py`（`clear_cache`） | 进程内 TTL 60s | 治理写入后调用现有失效入口 |
| `ScenarioSnapshot` | `app/services/scenario/snapshot.py:224-275` | 冻结值对象；`fingerprint` 由身份+版本+校验和组成 | 增加 `caps` 与 `version_source`；指纹只在 caps 非空时追加段 |
| 策略折叠纯函数 | `app/services/scenario/effective.py:88-127` | 平台 + 场景两段折叠 | 扩为 L0 平台 → L2 场景 → L1 租户三段，并返回来源 |
| 检索链折叠点 | `app/services/retrieve/__init__.py:275-298` | 全链唯一读取策略处 | 只改这一处传参；拓扑与语句块不动 |
| 读写两段式库映射 | `app/services/db_scope.py:91-157` | `scene_db_split_enabled` 时查 kb→库位 | 增加钉定感知 SQL + 租户白名单校验 |
| 连接路由 | `app/infra/db_router.py:43-92,238-267` | default + 已注册场景库键 | 增加“键→库名”只读查询，供容量看板 |
| 凭证与限额 | `app/models/entities.py:466-500`、`app/services/quota.py` | 应用级 `rate_limit_per_minute / daily_token_limit / daily_message_limit`；租户级日/月 token 与请求数 | 配额沿用现状，只补跨场景聚合回归 |
| 审计 | `app/models/entities.py:837-855`、`app/services/audit.py`（`record_with_session`） | detail 是 JSON，可追加字段 | 治理动作与 provenance 写 detail |
| 场景写接口 + checksum 并发 | `app/services/scenario/write_service.py:194-213` | `If-Match` 缺失 428、不匹配 409 | 验收③已满足，Step 7 只加回归引用，不重写 |
| 只读系统接口 | `app/api/system.py:27-48` | 已有 capabilities 端点 | 容量看板挂同一 router |
| 占位值登记纪律 | `app/config/placeholders.py`、`app/services/scenario/policy_spec.py:64-75` | 未登记/镜像不一致即导入失败 | 容量常量必须登记，禁止散落注释 |
| 路由注册点 | `app/main.py:430-463` | 每个 router 显式 `include_router` | 新治理 router 注册在同一处 |

### 1.2 Step 7 交付物与现状差距

| 框架 §7 Step 7 交付物 | 现状 | Step 7 缺口 |
|---|---|---|
| L1 可用场景清单 | `scenarios.status` 已是本租户场景启停；`GET /api/scenarios` 已能列出 | 缺一个治理视角的清单接口（含 pin/caps/tier），不新增 enabled 表 |
| 库可见性上限 | 无 | 新增 `tenants.allowed_db_keys`，请求层校验，越界 400 |
| 版本钉定 | 解析恒取 published | 新增 `scenarios.pinned_version`；关闭态忽略，开启态认 pinned/archived |
| 配额 | 租户级日/月额度已跨场景；应用级日限额按凭证 | 不新增聚合键；补跨场景聚合测试与文档口径 |
| 草稿→发布→审计 | Step 4A 已验证并落审计；checksum 409 已实现 | 只补治理动作审计与全链回归 |
| provenance | 只有 `scenario_key/version/fingerprint` | 折叠函数返回 `sources`；管理端预览 + 检索日志 + 审计 detail |
| 热/温/冷分级与阈值监控 | 无 tier、无容量口径 | 新增 `scenarios.tier`；容量接口按 `M_A(N)` 返回下界与标定状态；阈值只做可解释的只读观测 |

### 1.3 三个必须先说清的现状判断

1. **“可用场景清单”与 `scenarios.status` 语义重叠。** 现有模型是“场景属于租户”，不是“平台目录 + 租户勾选”。再造一张 `tenant_scenario_grants(enabled)` 会得到两把锁、两个真源。Step 7 的清单 = `status='active'` 的场景 + 治理字段，清单接口只做聚合视图。
2. **钉定不只影响 policy，还影响 kb→库位映射。** `db_scope` 的映射 SQL 当前只读 published。若钉定到旧版本而旧版本的 kb 集合与新版不同，仍按 published 映射会把请求引到错误库位（且不报错）。因此 Step 7 的钉定必须同时改造 `db_scope` 的映射口径，不能只改 resolver。
3. **验收③“checksum 并发保存被拦”已经存在。** `If-Match` 428/409 与 `tests/scenario/test_step4_concurrency.py` 是既有资产；Step 7 不重复实现，只把它纳入回归清单。

---

## 2. 范围与禁止事项

### 2.1 本期做

- 迁移 `0031`：租户白名单列、场景钉定列、四个收紧列、分级列与约束。
- L1 读取与快照：治理值进入 `ScenarioRow` / `ScenarioSnapshot`，关闭态走原 SQL。
- 钉定解析：开启态按 `pinned_version` 解析；库映射同步。
- 策略收紧与 provenance：三段折叠，返回每个键的来源。
- 库可见性上限：请求层校验，越界 400。
- 治理面：租户库白名单、场景治理（pin/caps/tier）读写接口 + 审计。
- 容量看板：`M_A(N)` 下界、tier 计数、按库观测、阈值可评估性。
- 回归：跨场景配额、审计覆盖、全量失败集合对比。

### 2.2 本期不做（每条给理由）

| 不做 | 理由 |
|---|---|
| pgvector 分区 + RLS | 框架 A1/Q2 已定私有化单租户；引入后成本高一个量级 |
| 平台场景目录 + 租户勾选模型 | 现有模型是“场景属于租户”，没有目录概念；新增等于改数据模型 |
| 热档独立实例、PgBouncer、自动升降级 | U10 未实测；框架 §3A.4.4 明确“热档是下线触发的短期状态” |
| 跨场景检索 | 框架 DQ3 已否决；会引入跨库授权与召回口径问题 |
| 请求级覆写字段集扩展、保留期策略 | 不在框架 §7 Step 7 交付物列表内；先把 L1 收敛做对 |
| 把 `db_binding` 做成可编辑表单项 | 框架 §7.2 明令禁止；库位是部署动作 |
| 用 `WHERE tenant_id` 当隔离 | 框架 §7.2 明令禁止；pgvector 近似索引在过滤前扫描，会静默少召回 |
| 应用级版本钉定 | 本步主题是 L1；应用级钉定留到有真实多客户端诉求时再加列，不影响本层 |

### 2.3 不新增的概念（防重复造轮子）

- 不新增 `tenant_scenario_grants` 表。
- 不新增 `enabled` 字段。
- 不新增 `client_key` 配额聚合键。
- 不新增第二套“库位注册表”；`db_router` 仍是唯一注册点。
- 不新增第二套策略折叠函数；`effective_retrieval` 仍是唯一折叠点。

---

## 3. 关键裁决（逐项打分）

### D1 租户治理开关

| 方案 | 连续性 20 | 部署现实 25 | 可验证 20 | 改动面 15 | 可回滚 10 | 单一性 10 | 总分 |
|---|---|---|---|---|---|---|---|
| A `TENANT_GOVERNANCE_ENABLED` 默认 False，opt-in | 20 | 25 | 19 | 14 | 10 | 10 | **98** |
| B 无开关，始终查治理值（无配置=现状） | 12 | 20 | 16 | 12 | 6 | 8 | 74 |
| C 默认 True | 14 | 18 | 16 | 12 | 7 | 8 | 75 |

**推荐 A（98）。**
- 关闭态：resolver 走原 `LOOKUP_SQL`，`db_scope` 走原 `_MAP_SQL`，不读 `tenants`，零新增查询；与 Step 6 关闭态逐字节等价。
- 开启态：resolver 走 `LOOKUP_GOVERNED_SQL`，`db_scope` 走 `_MAP_SQL_GOVERNED`，各多一个 `JOIN` 或一次缓存读，可测量。
- 回滚：环境变量置 false，行为回到 Step 6，不需要改数据。
- 配置但未开启 = 静默 no-op，因此治理写接口在关闭态必须返回 400（见 D5/S7-5），不允许“配了不生效”。

### D2 治理字段的存储形态

| 方案 | 连续性 20 | 部署现实 25 | 可验证 20 | 改动面 15 | 可回滚 10 | 单一性 10 | 总分 |
|---|---|---|---|---|---|---|---|
| A 现有表加 nullable 列 | 20 | 24 | 19 | 13 | 10 | 9 | **95** |
| B 新建 `tenant_governance` + `tenant_scenario_grants` 两张表 | 14 | 22 | 18 | 10 | 8 | 9 | 81 |
| C 单表 + JSONB 治理包 | 10 | 18 | 14 | 12 | 8 | 6 | 68 |

**推荐 A（95）。** 具体列：

| 表 | 列 | 类型 | 语义 |
|---|---|---|---|
| `tenants` | `allowed_db_keys` | `text[] NULL` | NULL=不限；`{}`=全部禁止；数组=白名单 |
| `scenarios` | `pinned_version` | `integer NULL` | NULL=跟随 published；非空=钉定版本号 |
| `scenarios` | `recall_top_k_max` | `integer NULL` | L1 收紧上限 |
| `scenarios` | `threshold_vector_min` | `double precision NULL` | L1 收紧下限 |
| `scenarios` | `threshold_rerank_min` | `double precision NULL` | L1 收紧下限 |
| `scenarios` | `rerank_allowed` | `boolean NULL` | NULL/true=不额外收紧；false=强制关 |
| `scenarios` | `tier` | `text NOT NULL DEFAULT 'cold'` | `hot|warm|cold` |

约束：
- `CHECK ((tier='cold' AND db_binding IS NULL) OR (tier IN ('warm','hot') AND db_binding IS NOT NULL))`。
- `CHECK (pinned_version IS NULL OR pinned_version >= 1)`。
- `CHECK (recall_top_k_max IS NULL OR recall_top_k_max > 0)`。
- 两个阈值列 `BETWEEN 0 AND 1`。
- `tier` 迁移回填：`CASE WHEN db_binding IS NULL THEN 'cold' ELSE 'warm' END`。

不建 `scenarios(pinned_version) → scenario_versions` 外键：两表已互相引用，再加会形成循环外键；版本没有删除路径，写入时校验 + 解析期 fail-closed 更简单，也不给回滚制造障碍。该决定写在迁移注释里。

`allowed_db_keys` 的元素格式由应用层校验（`default` 或 `^kb_[a-z0-9_]{2,40}$`）；数据库只保证类型是 `text[]`。理由：CHECK 不能写子查询遍历元素，加触发器等于把一条简单规则藏进数据库。

### D3 版本钉定的落点与状态语义

| 方案 | 连续性 20 | 部署现实 25 | 可验证 20 | 改动面 15 | 可回滚 10 | 单一性 10 | 总分 |
|---|---|---|---|---|---|---|---|
| A 场景级钉定；写入只允许 published；解析接受 published/archived | 20 | 24 | 19 | 13 | 9 | 10 | **95** |
| B 应用级钉定（`integration_applications.pinned_version`） | 16 | 22 | 18 | 11 | 9 | 8 | 84 |
| C 场景级钉定，但解析仍只认 published | 12 | 16 | 16 | 14 | 10 | 8 | 76 |

**推荐 A（95）。**
- 钉定写在 `scenarios.pinned_version`：场景属于租户，租户内所有应用看到同一个钉定，符合 L1 的“收敛”主题；不动凭证表。
- 写入：只能钉 `state='published'` 的版本（`400 PIN_VERSION_NOT_PUBLISHED`）；禁止钉 draft/validated，避免把未发布配置泄漏给客户端。
- 解析：钉定生效时接受 `published|archived`。发布新版本后旧版本转 archived，钉定客户端仍取旧版本，满足验收④；未钉定客户端取新 published，行为与今天一致。
- 版本不存在 = 数据损坏：`SCENARIO_UNRESOLVABLE` 500，fail-closed，不回落 published（回落会静默改变客户端行为）。
- 关闭开关：不读 `pinned_version`，钉定残留数据被忽略，行为回到 Step 6。
- 库映射：`db_scope` 必须同步认钉定（见 1.3 第 2 条），否则 kb 集合跨版本变化时会把请求引到错误库位。

### D4 “可用场景清单”的语义

| 方案 | 连续性 20 | 部署现实 25 | 可验证 20 | 改动面 15 | 可回滚 10 | 单一性 10 | 总分 |
|---|---|---|---|---|---|---|---|
| A 复用 `scenarios.status` + 治理清单接口 | 20 | 24 | 18 | 15 | 10 | 10 | **97** |
| B 新增 `tenant_scenario_grants.enabled` | 14 | 22 | 18 | 11 | 9 | 7 | 81 |
| C 平台目录 + 租户勾选 | 6 | 12 | 14 | 5 | 6 | 6 | 49 |

**推荐 A（97）。**
- 清单 = 本租户 `scenarios` 中 `status='active'` 的行；治理接口 `GET /api/admin/scenario-governance` 返回 `status / pinned_version / caps / tier / db_binding`，就是可评审的清单。
- 越界使用 disabled 场景已被 resolver R3 拒绝（`SCENARIO_DISABLED`）；不重复加锁。
- 如果未来要做“平台统一目录、租户只勾选子集”，那是新数据模型，不在 Step 7 偷渡。

### D5 库可见性上限的落点与语义

| 方案 | 连续性 20 | 部署现实 25 | 可验证 20 | 改动面 15 | 可回滚 10 | 单一性 10 | 总分 |
|---|---|---|---|---|---|---|---|
| A 请求层唯一映射入口 `db_scope.resolve_kb_db_key` 校验 | 20 | 24 | 19 | 13 | 10 | 10 | **96** |
| B 在 `db_router.resolve_db_key` 校验 | 10 | 18 | 16 | 10 | 8 | 7 | 69 |
| C 只在治理面展示，不拦截 | 12 | 16 | 10 | 15 | 10 | 6 | 69 |

**推荐 A（96）。**
- 校验点放在 `db_scope.resolve_kb_db_key`：它是请求层唯一“kb→库位”映射入口；`resolve_document_db_key / resolve_wiki_page_db_key` 的控制副本命中路径由它派生，控制副本缺失时的场景库探测同样先按 `allowed_db_keys` 过滤已注册库位（被拒库位不打开，全部被拒 → 404）。
- 不在 `db_router` 校验：那一层没有租户上下文，硬塞参数会把部署层和治理层耦合。
- 语义：`allowed_db_keys IS NULL` 不限；`'{}'` 全部禁止；数组为白名单。映射结果里任一库位不在白名单 → `400 DB_SCOPE_NOT_ALLOWED`，不是回落 default、不是静默过滤。
- 关闭开关：不读 `tenants`，零查询。
- 缓存：`get_allowed_db_keys` 进程内 TTL 60s，治理写接口提交后失效；与 resolver 现有缓存纪律一致。
- 允许配置尚未注册的库键（先配置、后部署）；真正使用时由 `db_router` 抛 `DB_ROUTE_KEY_UNKNOWN` fail-closed，不在这里做“是否已部署”的猜测。

### D6 配额口径

| 方案 | 连续性 20 | 部署现实 25 | 可验证 20 | 改动面 15 | 可回滚 10 | 单一性 10 | 总分 |
|---|---|---|---|---|---|---|---|
| A 沿用租户级跨场景聚合；应用级按凭证；补回归测试 | 20 | 25 | 18 | 15 | 10 | 9 | **97** |
| B 新增 `client_key` 聚合应用配额 | 14 | 20 | 17 | 9 | 8 | 7 | 75 |
| C 按 `created_by` 聚合应用配额 | 10 | 16 | 15 | 10 | 7 | 6 | 64 |

**推荐 A（97）。**
- 现状：租户级日/月 token 与请求数已经是跨场景汇总；应用级日限额本来就是“按凭证”的语义。
- 框架 Q6 已定“一期一个 agent 多场景 = 多凭证”，所以“一个 agent 三个凭证各拿一份应用限额”是设计事实，不是漏洞；租户级封顶仍然生效。
- Step 7 只补两条测试：同一租户两个应用命中同一租户键；同一应用跨场景请求不产生第二套租户键。
- `client_key` 是产品概念，没有调用方需求前不引入。

### D7 provenance 的暴露面

| 方案 | 连续性 20 | 部署现实 25 | 可验证 20 | 改动面 15 | 可回滚 10 | 单一性 10 | 总分 |
|---|---|---|---|---|---|---|---|
| A 管理端预览 + 检索日志 + 审计 detail；不改 MCP 响应 | 20 | 24 | 19 | 14 | 10 | 10 | **97** |
| B 同时加 MCP 响应 `meta.provenance` | 16 | 22 | 18 | 12 | 10 | 9 | 87 |
| C 只写启动横幅 | 12 | 18 | 12 | 13 | 9 | 8 | 72 |

**推荐 A（97）。**
- 唯一计算点：`effective_retrieval` 返回 `sources: (key, layer)`；layer 为 `platform / scenario / tenant`。
- 管理端：`GET /api/admin/scenario-governance/{scenario_id}` 返回每个键的 `scenario 值 / cap / effective / source`，可直接回答“这个 top_k 来自场景还是租户”。
- 检索期：`retrieve` 现有“场景策略生效”info 日志追加上 `sources`；不新增日志行，关闭态不产生噪音。
- 审计：治理变更写 before/after；检索审计 detail 追加 `policy_sources`（JSON 内部字段，不改响应结构）。
- 不改 MCP 响应：旧客户端逐字段不变，符合验收④的“行为不变”。

### D8 容量看板与阈值监控

| 方案 | 连续性 20 | 部署现实 25 | 可验证 20 | 改动面 15 | 可回滚 10 | 单一性 10 | 总分 |
|---|---|---|---|---|---|---|---|
| A 只读管理接口：公式下界 + 按库观测 + 可评估性标记 | 20 | 23 | 18 | 12 | 10 | 9 | **92** |
| B 只做公式算术，不查实时指标 | 16 | 22 | 14 | 15 | 10 | 8 | 85 |
| C 完整后台告警 + 自动升降级 | 8 | 10 | 14 | 5 | 4 | 6 | 47 |

**推荐 A（92）。**
- 端点：`GET /api/system/capacity`，仅管理员。
- 公式：`M_A(N) = M_shared + M_control_pg + N_hot_effective × M_scene_pg`。
- 常量与标定状态必须一起返回，禁止只给一个数字：
  - `M_shared_declared = 8192 MiB`：只等于 `embedding` 与 `reranker` 的 `mem_limit: 4g`；另有 8 个常驻服务未设上限，逐个列名返回，避免把“已声明上限”说成“总内存”。【源】`code/docker-compose.yml:86,117`、框架 F11。
  - `M_control_pg_lower = 128 MiB`：`shared_buffers` 默认值；运行时用 `current_setting('shared_buffers')` 覆盖并恒标 `calibrated=true`（值来自运行时实测），并标注“仅 shared_buffers 下界，不含连接/扩展开销”。【运行时实测】
  - `M_scene_pg = 300 MiB`：框架示例假设的**默认声明值**；运行时可由 `CAPACITY_M_SCENE_PG_MB` 覆盖，只有填写 `CAPACITY_CALIBRATION_REF` 后 `calibrated` 才为 true。【默认待标定】
- 返回 `result.postgres_side_lower_mb` 与 `result.calibrated`（仅有标定记录时为 true）；字段名带 `lower_bound`，禁止被当成承诺值。
- 实时观测：一次 `pg_stat_database` 查询返回已注册库名的 `size / blks_hit / blks_read / 命中率`；P95 从进程指标取，HTTP P95 明确标注 `scope=process-wide`，检索 P95 取 `rag.retrieve.total`（检索链路总耗时，不含 LLM 生成），不假装是按库延迟。
- 阈值四条逐条给 `evaluable` 与 `reason`，阈值本身可由 `CAPACITY_*` 配置：
  - 命中率 < `CAPACITY_DB_HIT_RATE_MIN`（默认 95%）：有读写样本即可评估。
  - 单库体积 / 实例内存 > `CAPACITY_DB_SIZE_RATIO_MAX`（默认 30%）：实例内存未配置（`CAPACITY_INSTANCE_MEMORY_MB=0`）→ `evaluable=false`。
  - 检索 P95 > `CAPACITY_RETRIEVAL_P95_RATIO_MAX`（默认 1.5）倍基线：基线未配置或无 `rag.retrieve.total` 样本 → `evaluable=false`。
  - 独立限流：未实现 → `evaluable=false`。
- 热档未实现：返回 `n_hot_declared`（DB 计数）与 `n_hot_effective`（本期恒 0），公式用后者，`hot_unsupported=true`。不把 hot 标签偷偷算成温档，也不把它隐藏。
- 不自动告警、不自动升降级：没有实测阈值前，自动动作等于用一个占位值改生产。

### D9 分级字段与 hot 可写性

| 方案 | 连续性 20 | 部署现实 25 | 可验证 20 | 改动面 15 | 可回滚 10 | 单一性 10 | 总分 |
|---|---|---|---|---|---|---|---|
| A 存 `tier` 列，CHECK 与 `db_binding` 一致，hot 本期拒绝 | 20 | 23 | 19 | 12 | 9 | 9 | **92** |
| B 不存 tier，全部推导 | 16 | 22 | 18 | 15 | 10 | 10 | 91 |
| C 允许 hot 标签但不校验 | 10 | 12 | 12 | 14 | 8 | 6 | 62 |

**推荐 A（92）。**
- 存 `tier` 的理由：框架 D5 要求“层级变更写审计”；推导值没有变更事件，无法审计。
- 防两份真源：DB CHECK 把 `tier` 与 `db_binding` 绑死；部署改 `db_binding` 必须在同一语句改 `tier`，否则 CHECK 拒绝，不会出现“库搬了、等级没搬”。
- `hot`：DB 枚举保留，但应用写接口返回 `400 TIER_HOT_UNSUPPORTED`（`db_router` 没有独立实例注册表）；若运维用 SQL 强写 hot，启动自检给 WARN，容量接口把 `n_hot_declared` 与 `n_hot_effective` 分开返回，不静默按温档算。
- 回滚：`UPDATE scenarios SET tier = CASE WHEN db_binding IS NULL THEN 'cold' ELSE 'warm' END`，回到“按库位推导”的基线。**注意**：框架写的回滚是“全部温”，但本部署的默认场景在控制库，实际基线是冷；这里按事实写，不照抄。
- 新增场景默认 `cold`（创建时没有 `db_binding`）；部署搬库时同语句置 `warm`。

**写入路径（实现期收窄，已落地）**：分级写入走专用端点
`PUT /admin/scenario-governance/{scenario_id}/tier`（搬库完成登记），通用 PUT
只收策略字段（`tier` 已从契约移除，传了即 422）。专用端点要求 `tier` 与
`db_binding` 同时提交并**同一句 UPDATE** 写入 —— CHECK 是行级即时校验，拆成
两条语句的中间态必然违约，这正是通用 PUT 无法完成分级转换的根因。该端点只登记
“数据已搬完”的事实，不搬数据；审计动作仍为 `scenario.tier.change`（同值重复
PUT 也写，detail 为 `{before: {tier, db_binding}, after: {tier, db_binding}}`）。

### 3.10 推荐方案汇总

| 裁决 | 推荐 | 分数 |
|---|---|---|
| D1 开关 | opt-in 默认 False | 98 |
| D2 存储 | 现有表加 7 个 nullable 列 | 95 |
| D3 钉定 | 场景级；published 可写；published/archived 可解析 | 95 |
| D4 清单 | 复用 `scenarios.status` + 治理清单接口 | 97 |
| D5 库上限 | `db_scope.resolve_kb_db_key` 校验 | 96 |
| D6 配额 | 沿用租户级聚合 + 回归测试 | 97 |
| D7 provenance | 管理端 + 日志 + 审计，不改 MCP 响应 | 97 |
| D8 容量 | 只读接口 + 下界 + 可评估性标记 | 92 |
| D9 分级 | tier 列 + CHECK 绑定 + hot 拒绝 | 92 |
| **package 平均** | | **95** |

全部 ≥90，按规则可进入开发；用户拍板后按第 5 节顺序执行。

---

## 4. 目标状态（拍板后的代码形状）

### 4.1 解析链

```
请求 → resolver（开关关：LOOKUP_SQL，published；开关开：LOOKUP_GOVERNED_SQL，pin-aware）
     → ScenarioSnapshot{policy, caps, kb_ids, version, version_source, fingerprint}
     → effective_retrieval(policy, platform_rerank_enabled, caps)
     → EffectiveRetrieval{recall_top_k, rerank_enabled, threshold_*, clamped, sources}
     → retrieve 拓扑（一行不改）
```

### 4.2 库映射链

```
kb_ids → db_scope.resolve_kb_db_key
       → 开关关：_MAP_SQL（published）
       → 开关开：_MAP_SQL_GOVERNED（published 或 pinned）
       → map 结果 → ensure_db_keys_allowed（开关开才读 tenants）
       → DefaultSessionFactory / data_session（不变）
```

### 4.3 provenance 的唯一来源

`effective_retrieval` 的 `sources` 是唯一来源。管理端预览、检索日志、审计 detail 都读它，不各自实现一套“谁覆盖了谁”。

折叠顺序与来源判定：
1. L0 平台硬上限先把 `recall_top_k` 收窄；若发生平台收窄，来源记 `platform`。
2. L2 场景值覆盖平台默认；未配置的键来源记 `platform`。
3. L1 租户 cap 最后收紧；只有它真正改变了值，来源才记 `tenant`。
4. 阈值同理，方向为“不低于”；`rerank_allowed=false` 才能在平台/场景已开启时把来源记为 `tenant`。

### 4.4 指纹语义

`ScenarioSnapshot.fingerprint` 保持现有算法；仅当 `caps` 非空时追加一段 `canonical(caps)`。
- caps 为空：指纹与 Step 6 逐字段相同，旧审计可对比。
- caps 非空：同一场景版本在收紧后得到不同指纹，审计能区分“原版”和“被租户收紧后实际跑的版本”。

---

## 5. 子项拆分与实施顺序

| # | 子项 | 主要落点 | 测试 | 完成判据 |
|---|---|---|---|---|
| S7-1 | 迁移 `0031` + 模型列 | `alembic/versions/0031_*.py`、`app/models/entities.py` | `tests/scenario/test_0031_migration.py` | 升级/降级可逆；约束拒绝非法 `tier`；回填正确 |
| S7-2 | 治理读取 + 快照 + 开关 | `settings.py`、`app/services/tenant/governance.py`（新）、`resolver.py`、`snapshot.py` | `tests/scenario/test_step7_governance.py` | 关闭态 equals 基线；开启态 caps/pin 进入快照；指纹空 caps 不变 |
| S7-3 | 钉定解析 + 库映射 | `resolver.py`、`db_scope.py` | `tests/scenario/test_step7_pin_pg.py`、`tests/scenario/test_step7_db_ceiling.py` | pin 旧版后仍返回旧版；发布新版不影响钉定客户端；映射认 pin |
| S7-4 | 三段折叠 + provenance | `effective.py`、`retrieve/__init__.py`、`chat.py`、`integrations.py`、`eval_service.py` | `tests/scenario/test_step7_effective_provenance.py` | 每个键来源正确；平台收窄优先于租户收紧；不改拓扑 |
| S7-5 | 治理面 API + 审计 | `app/api/tenant_governance.py`（新）、`main.py:459` 附近、`app/services/tenant/governance.py` | `tests/scenario/test_step7_api.py` | 越界/非法 pin/hot 均 400；治理变更落审计；关闭态写接口 400 |
| S7-6 | 容量看板 | `app/services/capacity.py`（新）、`api/system.py`、`db_router.py`、`placeholders.py` | `tests/scenario/test_step7_capacity.py` | 公式可复算；常量带来源与标定状态；未标定不伪装 |
| S7-7 | 配额与审计回归 | 无生产代码（缺审计才补） | `tests/scenario/test_step7_quota_aggregate.py`、`tests/scenario/test_step7_audit.py` | 跨场景租户聚合；四个版本动作 + 三个治理动作都有审计 |
| S7-8 | 全量回归 + 报告 | 无生产代码 | 全量 pytest | 失败集合仍为交接文档记录的 2 条环境失败；新增用例全绿 |

每项完成即本地 commit，不推送；commit message 用 `feat(step7): ...` / `test(step7): ...` / `docs(step7): ...`。

---

## 6. 验收口径（对应框架 §7 表）

| 框架验收 | 本设计的可执行判据 |
|---|---|
| 越界收紧被拒 400，非静默 | 库白名单越界 `DB_SCOPE_NOT_ALLOWED` 400；pin 非 published 400；tier 与 binding 不一致 400；hot 400；治理关闭态写 400 |
| provenance 能回答参数来源 | `GET /api/admin/scenario-governance/{id}` 的 `effective.<key>.source`；检索日志 `sources`；审计 detail `policy_sources` |
| checksum 并发保存被拦 | 既有 `If-Match` 428/409 用例纳入回归；不新增实现 |
| 旧钉定客户端行为不变 | 钉 v1 → 发布 v2 → 客户端仍得 v1 的 checksum/kb 集合；unpin 后得 v2 |
| 容量看板按 M_A(N) 算内存 | `GET /api/system/capacity` 返回公式、输入、`postgres_side_lower_mb`、`calibrated`（仅有标定记录时为 true）；单测用固定输入复算 |
| 关闭态零行为变化 | 同一场景在开关关/开但无治理配置时，`version/checksum/kb_ids/effective 值` 逐字段相等；关闭态不读 `tenants` |
| 回滚 | 开关置 false 行为回 Step 6；治理列全部可 NULL；tier 回到 CASE 推导；迁移 `0031 downgrade` 可执行 |

---

## 7. 回滚清单

1. `TENANT_GOVERNANCE_ENABLED=false`：忽略 pin/caps/白名单，行为回 Step 6；不需要改数据。
2. 清空治理值：`UPDATE scenarios SET pinned_version=NULL, recall_top_k_max=NULL, threshold_vector_min=NULL, threshold_rerank_min=NULL, rerank_allowed=NULL; UPDATE tenants SET allowed_db_keys=NULL;`。
3. tier 回基线：`UPDATE scenarios SET tier = CASE WHEN db_binding IS NULL THEN 'cold' ELSE 'warm' END;`。
4. 迁移回滚：`alembic downgrade 0030`；只删 Step 7 新增列与约束，不动 Step 6 资产。
5. 代码回滚：S7-1 到 S7-8 各自独立 commit，可单独 revert；S7-4 的调用点更新必须与 `effective.py` 同一 commit，避免部分回滚造成 caps 静默丢失。

---

## 8. 需要你拍板的事项

请逐项确认或改选（默认按推荐执行）：

| # | 事项 | 推荐 | 备选 | 我的分数 |
|---|---|---|---|---|
| 1 | 开关策略 | opt-in 默认 False | 始终启用 / 默认 True | 98 / 74 / 75 |
| 2 | 存储形态 | 现有表加 7 列 | 两张新表 / 单表 JSONB | 95 / 81 / 68 |
| 3 | 钉定落点 | 场景级 + published 可写 + archived 可解析 | 应用级 / 只认 published | 95 / 84 / 76 |
| 4 | 可用场景清单 | 复用 `scenarios.status` | 新增 grant.enabled / 平台目录 | 97 / 81 / 49 |
| 5 | 库可见性上限 | `db_scope` 请求层校验 | `db_router` 校验 / 只展示 | 96 / 69 / 69 |
| 6 | 配额口径 | 沿用租户级聚合 + 测试 | 新增 client_key / 按 created_by | 97 / 75 / 64 |
| 7 | provenance 暴露面 | 管理端 + 日志 + 审计 | 也进 MCP meta / 只进横幅 | 97 / 87 / 72 |
| 8 | 容量看板 | 只读接口 + 下界 + 可评估性 | 只算公式 / 完整告警 | 92 / 85 / 47 |
| 9 | 分级字段 | tier 列 + CHECK + hot 拒绝 | 不存只推导 / 允许 hot 不校验 | 92 / 91 / 62 |

拍板后我会把本文件状态改为“已拍板”，按 S7-1 到 S7-8 顺序开发；每步新增测试、本机 commit、不推送。
