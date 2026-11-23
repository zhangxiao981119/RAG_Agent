# 第五阶段 Step 4B：DNA 进场景 —— 详细设计

> **设计台账**：2026-11-22 · 统一RAG平台+场景化配置 · Step 4B · 1 个迁移（0029：1 函数 + 1 视图）+ 2 个只读入口 + 1 处发布期快照 + 脚本同源改造 · 目标真库用例 10 条 + 静态守卫 4 条
> **前置**：Step 4A（场景管理写接口 + T4 接线）已实现并独立验收。本文件**只设计、不改代码**。
> **本文件的两类内容严格分栏**：标【源】的逐条给 `file:line`（我打开文件读到的）；标【推导】的是我基于源码得出的结论或取舍（推出来的）。二者混在一句话里的，拆成两句。
> **与主理人给定口径的关系**：主理人给的 16 条事实我逐条复核，**14 条一致、2 条需修正**（F-11、F-13，见 §A.2）。范围裁决我**部分反对**（见 §A.5 DQ4B-2 与 DQ4B-5）——理由写在对应条目里，不是偏好，是可执行判据。

---

## 0. 边界与验收基数

### 0.1 输入：本步继承的既成事实

【源】以下各项是 4B 的输入，本设计不重新论证、不改动。逐条给行号，供实现时对照。

| # | 事实 | 出处 |
|---|---|---|
| I-4B-0a | 场景六条 CHECK + `uq_scenarios_tenant_key` + `uq_scenarios_one_builtin`；版本十四条 CHECK + `uq_scenario_versions_one_published` | `alembic/versions/0028_add_scenarios.py:131-149`、`:181-271` |
| I-4B-0b | 迁移最大号 = **0028**（目录内无 0029 及以上）；故 4B 的迁移号 = **0029**，`down_revision = "0028"` | `alembic/versions/` 目录实测（`0010`…`0028`） |
| I-4B-0c | `chunks` 三列口径均 `nullable=False`：`embedding_model` / `embedding_text_version` / `chunker_version` | `app/models/entities.py:263-265` |
| I-4B-0d | 检索期**只过滤两列**：向量 SQL 尾部 `AND c.embedding_model = :embedding_model AND c.embedding_text_version = :embedding_text_version` | `app/services/retrieve/__init__.py:114-115`；绑定值在 `:312-313` |
| I-4B-0e | 关键词两路（`~*` 正则 / `tsquery`）都只带 `_WHERE_BASE`（四闸门 ACL），**不带任何口径列** | `:131-145`、`:158-168`、`:92-97` |
| I-4B-0f | `chunker_version` 落行但从不参与检索；`IndexProfile.fingerprint` 只取 model + dim + text_version | `app/config/index_profile.py:17-21`、`:63-73` |
| I-4B-0g | 平台 DNA 归属上移：场景只能选策略，选不了向量空间（I-6）；`ScenarioSnapshot.dna` 是只读值拷贝 | `统一RAG平台-场景化配置-框架设计-v1.md:443`、`:449-458` |
| I-4B-0h | `dna_declared` 是声明镜像，**读路径永不读它**；真源是三个 `current_*_profile()` | `app/services/scenario/dna_declare.py:3-5`、`dna_declare.py:29-38`、`snapshot.py:313-322` |
| I-4B-0i | 发布期 T4 的唯一调用点是 `write_service.publish()`，位置在 If-Match 之后、归档旧 published 之前 | `write_service.py:577-611`；设计依据 `第四阶段-Step4-详细设计.md:516-519` |
| I-4B-0j | 4A 的 W4 **不跑 T4**，理由是"发布期的外部条件不该在校验期冻结" | `write_service.py:490-493`、`第四阶段…:198` |
| I-4B-0k | 分流纪律：代码常量错 → 启动 `RuntimeError`；数据错 → WARN + metric（永不 raise） | `app/main.py:123-125`、`self_check.py:614-626` |
| I-4B-0l | `/metrics`（Prometheus 文本，根路径，无鉴权）走 `metrics.render()`；`/api/admin/metrics` 走 `metrics.snapshot()` | `app/main.py:390-401`、`app/api/metrics.py:20-31` |
| I-4B-0m | 指标 API **只有** `incr` / `observe_ms`，底层是 Counter / Histogram，**没有 Gauge** | `app/infra/metrics.py:40-41`、`:74-90` |
| I-4B-0n | 4A 的静态守卫把写接口路由表**钉死在测试里**（少一条就红） | `tests/scenario/test_step4_guards.py:31-40`、`:44-60` |
| I-4B-0o | 真库约束用例的全部源文件里**只允许一处** `pytest.raises`（在 `expect_violation` 内） | `tests/scenario/test_0028_migration.py:225-264` |

### 0.2 本步做 / 不做

**先说清本步**不涉及**哪些框架维度**（避免下一轮以为漏了）：

| 维度 | 本步的结论 | 理由 |
|---|---|---|
| **架构范式 P** | **N/A**，沿用 4A：声明式配置 + 固定链路，只调参 | 4B 不改变链拓扑、不引入新阶段、不改任何编排（I-4B-4 用 golden SQL 哈希把这件事变成可断言的） |
| **模型组合 / 任务-模型分派表** | **N/A**，本步**零** LLM / embedding / rerank 调用 | 唯一涉及 embedding 的是 `reindex_embeddings.py`（既有路径，4B 只改它的谓词来源与一行赋值）；4B 的判定全部是 SQL + 纯函数比较 |
| **多 Agent 编排** | **N/A** | 平台侧无自主循环（`框架设计-v1.md:1235` 已定性），4B 与之无关 |
| **经济性三视角** | 成本 ≈ 0（离线扫一次表）、缓存零影响（不失效任何既有缓存）、体感零影响（不进请求路径） | 见 §H 必答项第 5 条 |

**4B-1（本期交付，且不改变任何检索期召回范围）**：

1. **`needs_reindex` 数据面**：迁移 0029 建 1 个参数化函数（口径可传参）+ 1 个无参分布视图；新增只读查询入口。分母口径与 `reindex_embeddings.py` **同源到"同一个函数据此判"**，不是"两处都写对"。
2. **场景 DNA 发布前快照**（接在 W5）：在发布那一刻把「该版本声明的 DNA 与平台当前 DNA 的差异」+「该场景 kb 集合的向量路可检比例」算出来，**非阻断地**写进响应与审计；只有一种**确定性数据损坏**形态才拒绝（§B.4.3）。
3. **可观测**：启动一行汇总 + 一个 admin JSON 端点 + 沿用既有 `incr` 口径的打点（**不新增 Gauge**，理由见 §B.5.1）。
4. **顺带修一处既有缺陷**：`reindex_embeddings.py:117` 在**没有重切**的情况下把 `chunker_version` 改写成当前值，使"建议重切"信号在跑一次重算后永久消失（§B.8）。

**4B-2（本期只设计，写成条件触发）**：检索期口径过滤扩到关键词两路。**我的裁决是"永久不做"**，并把框架稿 §7 的该项交付物替换为一条**可判定的不变量 + 一个触发器**（§C）。

**不做（逐条给理由）**：

| 不做的事 | 理由 | 归到哪里 |
|---|---|---|
| 把 `chunker_version` 升为硬 DNA（检索期过滤） | 【源】`_chunker_version()` 派生自三个 `CHUNK_*` 常量（`index_profile.py:45-51`）—— 调一次 overlap 就全库不可检。【源】`DNA与Policy口径对照表-v1.md:51` 已把这个误判单列为第 2 条 | 见 DQ4B-3（推荐"不升"，附翻转条件） |
| 改 `retrieve/__init__.py` 的任何 SQL 文本 | 4B-1 的承诺是"读路径召回范围零变化"。SQL 文本一旦变动，这个承诺就只能靠人记着 —— 所以用 §D.3 的 golden 哈希把它变成可断言的 | 4B-2（条件触发） |
| 给 `infra/metrics.py` 加 Gauge | 【源】加 Gauge 必然要进 `snapshot()`，而 `tests/test_metrics_and_rerank.py:80` 把 `snapshot()` 的键集**精确钉死**为 `{"counters","histograms"}`。为零收益去改一条已验收断言，违反本步"不碰既有断言"约束 | §B.5.1 给出替代（`incr` + 专用端点） |
| 在 W4 加 DNA 校验 | 【源】4A 已确立"发布期的外部条件不该在校验期冻结"（`write_service.py:490-493`）。DNA 可检率是**随时间变化的数据状态**，在 W4 冻结会产生"今天 validated、明天库变了、后天照发"的陈旧闸门 | 见 DQ4B-5（推荐放 W5 + 前置 preflight） |
| 让 `needs_reindex` 变成"覆盖率百分比" | 【源】`capabilities.py:66-72` 记录了一手教训：用了错误的分母，一篇失败文档让就绪度永远停在 20/21，"闸门永远红，运维很快就学会无视它" | §B.2 用"可加的对账恒等式"代替单一百分比 |
| 改 `step4_check.py` | 它已把 T4 的判据与 `PolicyValidationError` 的字段语义封好；4B 的判定对象（数据状态）与它（内容合法性）不同，混进去会让一个函数的返回值有两种含义 | 新增 `needs_reindex.py`，`step4_check.py` 一行不改 |

### 0.3 验收基数（不许放宽）

【源】当前测试总量：`tests/` 下 **1162 个 `def test_`**，其中 `tests/scenario/` **255 个**（含 4A 新增 63 个：`test_step4_api.py` 36 + `test_step4_guards.py` 10 + `test_step4_concurrency.py` 5 + `test_step4_constraints.py` 5 + `test_step4_archive_impact.py` 4 + `test_step4_read_path.py` 3）。【推导】`def test_` 数 ≠ collected 数（parametrize 会展开），故两个口径都写出来，验收时以 `--collect-only` 实际数为准 —— 口径同 `第四阶段-Step4-详细设计.md:46`。

【推导】4B 的硬约束：

1. **既有 1162 条一条都不许改语义**。4B **不产生任何事实变更**（见 §0.2 的四条"不做"，已刻意避开了 4A 那两处必须改断言的地方）。唯一可能触碰的是 `test_step4_guards.py:31-40` 的 `_EXPECTED_ROUTES` —— 若采纳 §B.3.2 的 preflight 端点，必须**追加一行**（这是加严：路由集合仍然精确相等）。**若判定不采纳 preflight，则该文件零改动**。
2. 新增用例不得用被测对象自身的常量做期望值（反自证），判据见 §D.4。

### 0.4 施工文件清单

| 动作 | 文件 | 内容 |
|---|---|---|
| 新增 | `alembic/versions/0029_add_chunk_reindex_views.py` | 函数 `fn_chunk_reindex_candidates` + 视图 `v_chunk_caliber_distribution`（DDL 见 §B.1）；**不含任何 CHECK/UNIQUE 约束**（理由见 §B.1.4） |
| 新增 | `app/services/scenario/needs_reindex.py` | `ReindexReport` / `collect()` / `scenario_kb_readability()` / `judge_publish_snapshot()` / `format_banner_line()`（纯读 + 一个纯判定函数） |
| 新增 | `app/schemas/needs_reindex.py` | 两个响应模型（`extra="forbid"` 只对请求；响应模型不加） |
| 新增 | `app/api/needs_reindex.py` | `GET /api/admin/needs-reindex`（R-4B-1）；可选 `GET /api/scenarios/{scenario_id}/versions/{version}/preflight`（R-4B-2） |
| 修改 | `app/services/scenario/write_service.py` | `publish()` 内插入口径快照（**约 12 行**）；`VersionActionOut` 的返回填充 |
| 修改 | `app/schemas/scenario.py` | `VersionActionOut` 追加 `dna_snapshot: dict \| None = None`（**加默认值，不动既有字段**） |
| 修改 | `app/services/scenario/write_errors.py` | 追加 1 个码 `SCENARIO_KB_EMPTY_BUT_INDEXED` + `_HTTP` 映射（**唯一新增码**，§B.6） |
| 修改 | `app/main.py` | `log_capability_banner` 之后追加一行 `await log_needs_reindex_banner()`（§B.5.2） |
| 修改 | `scripts/reindex_embeddings.py` | `_load_stale()` 改调同一函数（§B.7）；删 `:117` 的 `chunker_version=` 赋值（§B.8） |
| 修改 | `app/main.py` | 追加 `include_router(needs_reindex_router, prefix="/api")`（R-4B-1 若挂在 `/api/admin` 下则由既有 admin router 承接，实际是**新建 router + 一行 include**） |
| 新增 | `tests/scenario/test_0029_migration.py` | 迁移往返 + 函数/视图存在性 + 单头（**不新增约束名，故不动 `_PG_CONSTRAINT_SOURCES`**） |
| 新增 | `tests/scenario/test_step4b_reindex_pg.py` | §D.2 真库用例 T-4B-1…T-4B-7 |
| 新增 | `tests/scenario/test_step4b_guards.py` | §D.3 静态守卫 T-4B-8…T-4B-10（含 golden SQL 哈希） |
| 修改（**仅当采纳 preflight**） | `tests/scenario/test_step4_guards.py:31-40` | `_EXPECTED_ROUTES` 追加 1 条（加严，非放宽） |

### 0.5 术语

| 词 | 本文件里的确切含义 |
|---|---|
| **硬 DNA** | `embedding_model` / `embedding_dim` / `embedding_text_version` 三列 —— 不匹配则向量**不同构**，向量路必须排除。分界线出处：`DNA与Policy口径对照表-v1.md:30-35` |
| **软 DNA** | `chunker_version` 一列 —— 不匹配**照常可检**，只作"建议重切"。出处同上 `:32` |
| **分母（universe）** | `chunks` 中 `is_latest = true` 的行数。**只此一个条件**，理由见 §B.2.2 |
| **`hard_stale`** | universe 中硬 DNA 有一条不等的行数 —— 与 `reindex_embeddings.py --check` 的计数**逐条相等**（同源机制见 §B.7） |
| **`chunker_only`** | 硬 DNA 全等、仅 `chunker_version` 不等的行数。**独立分量，不与 `hard_stale` 合并**（这是 R7 的落点） |
| **对账恒等式** | `ok + hard_stale + chunker_only + embedding_missing == universe`，四桶互斥（优先级 hard > chunker > missing > ok） |
| **降级路径** | 向量路回 0 行、关键词路照常 —— 【源】`DNA与Policy口径对照表-v1.md:14` 明确把"换 model → 主检索退化为仅关键词"写作**预期行为**，不是故障 |
| **口径快照** | 4B 在 W5 算出并写进响应/审计的只读信息块（`dna_snapshot`），**不参与任何放行判定**（唯一例外见 §B.4.3） |

---

## A. 决策契约

### A.1 不变式（4B 新增，后续步骤不得违反）

| 编号 | 不变式 | 违反后的可观测症状 | 守卫 |
|---|---|---|---|
| **I-4B-1** | "当前口径"只有一个真源：`current_index_profile()`。SQL 里**不得**出现口径字面量（`'bge-m3'` / `'etext-v1'` / `'c400-800-60'` 之类）；口径一律作为**绑定参数**传进去 | 改 `.env` 后视图与检索各说各话，且没有任何报错 | T-4B-10（AST 守卫：`0029` 与 `needs_reindex.py` 内不得出现这些字面量） |
| **I-4B-2** | `needs_reindex` 的判定谓词**只有一份实现**（`fn_chunk_reindex_candidates`）。脚本与视图/端点都调它 | "视图说 0 条、脚本说 300 条" | T-4B-1（断言脚本数 == 报告数） |
| **I-4B-3** | `chunker_version` 不匹配**永不**导致检索排除，也**永不**进入任何"过期率/就绪度"分母 | 调一次 `CHUNK_OVERLAP` 就让全库检索塌掉 | T-4B-2（把 chunker 判进 `hard_stale` 必须让用例变红） |
| **I-4B-4** | 4B-1 不改变任何检索期 SQL 的**文本**（因此不改变召回范围） | 4B 的"零影响"承诺失效，且要靠人记 | T-4B-9（三段 SQL 的 sha256 golden） |
| **I-4B-5** | 发布期口径快照是**只读旁路**：它不写 `scenario_versions`、不参与 `checksum`、不触发 `resolver.invalidate` | 发布成了"改内容"的隐性通道，`If-Match` 语义被破坏 | T-4B-5（断言发布前后 `checksum` 与 `policy` 逐字节不变） |
| **I-4B-6** | 4B 新增的唯一阻断条件是**确定性数据损坏**（有 `status='indexed'` 文档却 0 条 chunk），不匹配本身**永不阻断** | 与"降级路径是预期行为"（`对照表:14`）冲突，会让换模型期间无法发布任何场景 | T-4B-4（三档各一条） |
| **I-4B-7** | 分母 = `chunks.is_latest = true`，**不得**再加第二个条件 | 分母随请求/A CL/时间变化 → 分母不是部署级常量 → 数字无法被解释 | T-4B-3（对账恒等式 + 分母为常量的断言） |

### A.2 事实锚定（逐条自己核过；与主理人给定口径的差异标出）

| # | 事实（【源】） | 出处（我打开文件核到的行号） | 与主理人给定是否一致 |
|---|---|---|---|
| **F-1** | Step 4 的框架定义 = 「DNA 进场景（分块/抽取口径）｜多 DNA 支持：chunk 行指纹过滤（既有）+ `needs_reindex` 视图 + 场景 DNA 发布前校验」；验收信号三条 | `框架设计-v1.md:799` | 一致 |
| **F-2** | Step 4 禁止「让旧 chunk 在 DNA 不匹配时返回低分片」；定性为"重算不可逆"；必须**先于** Step 6 | `:833`、`:824`、`:818` | 一致 |
| **F-3** | R7 已裁决 `chunker_version` 属"软 DNA"：不匹配不拒绝检索，但必须在 `needs_reindex` 视图显式暴露；"本设计保留该行为" | `:1199` | 一致 |
| **F-4** | R10 记录了 `needs_reindex` 变永久债务的机制；一手证据写在 `capabilities.py:69`「一篇失败文档（`status='failed'`，0 分片）让就绪度永远停在 20/21 —— 闸门永远红，运维很快就学会无视它」；`:1294` 至今把"分母口径 + 告警阈值"列为待定项 | `:1207`、`capabilities.py:66-72`、`:1294` | 一致 |
| **F-5** | 既有"过期 chunk"定义 = `is_latest` 且 (model 不等 OR text_version 不等)，**不算 chunker** | `scripts/reindex_embeddings.py:47-50` | 一致 |
| **F-6** | `--check` 已有 CI 门禁语义：有过期即非 0 退出码 | `:79-81` | 一致 |
| **F-7** | 检索期只过滤两列；参数在 `:312-313` 传入 | `retrieve/__init__.py:114-115`、`:312-313` | 一致 |
| **F-8** | 关键词两路只带 `_WHERE_BASE` | `:92-97`、`:131-145`、`:158-168` | 一致 |
| **F-9** | 注释「该过滤只加在向量路径：关键词路（ILIKE / tsquery）与向量空间无关，加会误伤」 | `:99-104`（关键句在 `:104`） | 一致 |
| **F-10** | `chunker_version` 影响分块粒度而非向量空间，故 fingerprint 不含它，只作"是否建议重切"的提示 | `index_profile.py:17-21`、`:63-73` | 一致 |
| **F-11** | **修正**：`IndexProfile` 有**四个**字段（`embedding_model` / `embedding_dim` / `embedding_text_version` / `chunker_version`），`fingerprint` 取前**三**个；主理人写的"只取 model+dim+text_version"准确，但"三要素过滤"在**检索 SQL 里只落了两列**（`embedding_dim` 没有列可比，维度写死在 `vector(1024)` 的 DDL 里）。**因此"硬 DNA 三列"在库内只有两列可判，第三列（dim）只能由"迁移是否跑过"间接保证** | `index_profile.py:54-73`、`entities.py:259`（`Vector(1024)`） | **需修正**：`embedding_dim` 在检索期**没有**列级过滤，它不是"被过滤的第三列" |
| **F-12** | `graph_profile` / `wiki_profile` 同款 fail-closed 纪律；图与 wiki 已有"口径不同则重算"的短路 | `graph_profile.py:13-16`、`wiki_profile.py:12-17`、`workers/graph_job.py:157-163`、`services/wiki/build.py:154-163` | 一致 |
| **F-13** | **修正**：主理人写"`dna_declared` 由 `build_dna_declared()` 生成，**读路径永不读它**；三个真源是 `current_index_profile()` / `current_graph_profile()` / `current_wiki_profile()`"。前半句成立；后半句的准确形态是：三个 profile 由 `snapshot.current_dna()` **组合**返回，且**存在一个例外读点**——S-9 漂移检测会读 `dna_declared`（`self_check.py:580-599`，仅对 `is_builtin` 行，只 WARN）。所以是"**请求路径**永不读它"，不是"全仓永不读" | `dna_declare.py:3-5`、`snapshot.py:313-322`、`self_check.py:569-611` | **需修正**：这是"请求路径"限定，不是全局 |
| **F-14** | 4A 遗留项 U4-10：`draft→validated` 目前只做 `validate_policy` 复算，"若将来加 DNA 校验，W4 会变成变更点" | `第四阶段-Step4-详细设计.md:968` | 一致（但我不采纳"加在 W4"，见 DQ4B-5） |
| **F-15** | `parse_job` 在**同一事务**里 `delete(Chunk).where(document_id == ...)` 后重建全部 chunk，`is_latest=True`，并把文档置 `status='indexed'` | `app/workers/parse_job.py:196`、`:218`、`:229`；第二处（分段聚合路径）`:525`、`:535` | **主理人未提，但这是本文件最关键的一条**（见 §A.3.3） |
| **F-16** | `reindex_embeddings.py` 在只重算向量的同时，把 `chunker_version` 一并改写为当前值 | `scripts/reindex_embeddings.py:113-118`（关键行 `:117`） | **主理人未提，属既有缺陷**（见 §B.8） |
| **F-17** | 软删文档只写 `documents.deleted_at`，**不删 chunk**，而召回 SQL 不 join `documents`（只有取 filename 时才过滤 `deleted_at IS NULL`） | `app/api/documents.py:433`、`retrieve/__init__.py:172-174`、`:92-97` | 主理人未提；影响分母（§B.2.4）与一条遗留项 |
| **F-18** | 关键词路回 0 行 / 向量路回 0 行**都没有任何指标**（`retrieve` 里 **33 处** `metrics.incr/observe_ms` 调用，逐一核过，无一条与"结果为空 / 某路回 0 行"有关） | `retrieve/__init__.py:279-999`（`grep -c` 得 34 行匹配，减掉 `:901` 那行仅提及 `metrics.incr` 的注释 = 33 处调用） | 主理人未提；这是"降级不可见"（§B.5.3） |

### A.3 核心裁决 R-1：关键词路加口径过滤到底是"误伤"还是"必要"

主理人要求这两句话**必须有一句被判定为错**，并给出判据与证据。我判定：

> **`:104`「加会误伤」的结论正确，但它的理由（"与向量空间无关"）是错的方向；主理人转述的第二句「两代 DNA 下同一文档会切成不同 chunk，混检会返回重复/错位内容」在当前写路径下不成立——它描述的是一个本表结构上还无法表示的状态。**

#### A.3.1 两句话各自断言了什么

- 句 A（`retrieve/__init__.py:104`）：关键词路与向量空间无关 → 加口径过滤会**误伤**（把本可正确返回的行排除掉）。
- 句 B（主理人转述的"必要"论）：同一文档在两代 DNA 下会产出**两组 chunk 行**，混检会返回重复/错位内容 → 必须过滤。

#### A.3.2 判据：三条硬 DNA 列，哪些会改变 chunk 的**正文**

这是唯一有判别力的判据，因为关键词路判的是**正文**（`c.content ~* :pattern`、`fts_vec @@ tsquery`），向量路判的是**向量**。

| 列 | 改变 `chunks.content` 吗 | 改变向量空间吗 | 关键词路命中它会不会给出错误内容 |
|---|---|---|---|
| `embedding_model` | **不改变** | 改变 | **不会**。正文是判定当时入库的真实正文，与新模型下重算出的正文**逐字节相同**（正文由解析+分块决定，与 embed 模型无关） |
| `embedding_text_version` | **不改变**（它只改 `to_embedding_text()` 的拼接公式） | 改变 | **不会**。同上 |
| `embedding_dim` | 不改变 | 改变 | 不会（且库里没有这一列可判，见 F-11） |
| `chunker_version` | **改变**（分块边界变了） | **不改变** | **不会"错"**：返回的是旧边界下的真实正文片段，不是伪造内容；只是粒度不同 |

【推导】结论：**三条硬 DNA 列里没有一条会让关键词路给出错误内容**。它们只影响向量。因此"过滤关键词路"在这三列上**只有损失、没有收益** —— 这正是句 A 的实质，只是句 A 把理由写成了"与向量空间无关"（这说法在**机制上**对，但没有落到"正文不变"这个可检验的点上，所以它读起来像一句教条）。

`chunker_version` 那一行需要单独说清：它**确实**改变正文边界，所以它不是"完全无关"。但它改变的是"边界"，不是"内容真伪"。而且它属于软 DNA（F-10 / R7），按定义不得据此排除。

#### A.3.3 证伪句 B：本表结构上还表示不了「两代并存」

【源】`parse_job.py:196`：重索引时**先 `delete(Chunk).where(document_id == ...)` 再重建**；重建时 `is_latest=True`（`:218`）。第二条路径（分段聚合）同样 `is_latest=True`（`:525`）。

【推导】三条推论，逐条与句 B 对照：

1. **在同一文档内，chunk 行集的代次恒为单一代次**（全表 `is_latest` 恒为 `true`，且旧行被硬删）。句 B 需要"同一文档存在两组 chunk 行"作为前提 → 前提不成立。
2. **`is_latest` 列今天不承载"代次"语义**。它只在"软删旧版本"这类场景有意义，而 parse_job 走的是硬删。要让"两代并存"可表示，必须有第二条区分维度（代次 id 或 `is_latest` 改为按代次判定）—— 那是**表结构变更**，4B 里不存在。
3. **跨文档的"代次混合"不会产生重复内容**：不同文档的 chunk 本来就是不同内容，混检不产生重复或错位。

所以句 B 描述的状态**在当前写路径下不可达**，它把"未来的双写代次灰度"当成了"今天的现状"。这不是"部分成立"，是**前提不成立**。

#### A.3.4 修正后的表述（这三句要替换掉 §7 与代码注释里的旧话）

1. **代码注释**（`retrieve/__init__.py:104`）建议改为：
   > 「该过滤只加在向量路径：三条硬 DNA 列只改变**向量**、不改变 `chunk.content`；关键词路判的是正文，命中它给出的是**正确内容**，加过滤只有召回损失。`chunker_version` 虽改变分块边界，但属软 DNA（`DNA与Policy口径对照表-v1.md:32`），按定义不得据此排除。」
2. **框架稿信号②**（`:799`）必须精确化为两段：
   > ②-1 伪造不匹配**硬 DNA** 的 chunk，**不进入向量路候选集**（空集，不是低分）；
   > ②-2 该 chunk **仍可被关键词路命中**，且必须被 `needs_reindex` 计入 `hard_stale`（可观测）。「关键词路照常」是**预期降级**（`对照表:14`），不是遗漏。
3. **框架稿 §7 Step 4 的交付物**里"chunk 行指纹过滤（既有）"应标注为"**已完成，且有意仅覆盖向量路**"，而不是列为一个待办。

#### A.3.5 那么 4B-2 到底该不该做

【推导】在 A.3.2/A.3.3 成立的前提下，4B-2 的收益是 **0**（没有一种当前可表示的状态需要它），代价是**消灭一条被文档明确规定为预期的降级路径**。因此：

- 若系统**永远保持单代次**（`Q3` 的推荐值是一期单代次，`框架设计-v1.md:848`）→ **4B-2 应永久不做**，且应从框架稿 §7 Step 4 的交付物里删除，替换为 §C.1 的不变量 + 触发器。
- 若未来采纳**同表多代次并存（双写）**，4B-2 才成立，且它的正确形态是"按代次 id 过滤"而**不是**"按口径列过滤"（后者会把"刚换模型但尚未重写"的合法旧代次一并排掉）。

详见 §C。

### A.4 待定项（现在定不了，须诚实标注）

| # | 项 | 为什么现在定不了 | 影响 | 临时处置 |
|---|---|---|---|---|
| **U-4B-1** | 软删文档的 chunk 是否进分母 | 【源】软删只写 `documents.deleted_at`、不删 chunk（F-17），且召回 SQL 不 join documents → 这些 chunk **仍可被检出**，所以算进分母在"能不能检出"这个意义上是对的；但跑重算它们是纯浪费 | 分母口径一旦改，`--check` 的 CI 语义同时变（一个开关改两件事） | **先不改**，把 `in_dead_documents` 作为**独立列**并列暴露（§B.2.4），下一轮拿真实数据再决定 |
| **U-4B-2** | 软删文档的 chunk 可被检索（F-17）—— 这是 4B 之外的安全/正确性缺口 | 修它要动召回 SQL（在 `_WHERE_BASE` 加 `documents` join 或 `c.document_id NOT IN (soft-deleted)`），直接改变召回范围，违反 4B-1 的 I-4B-4 | 已软删文档的内容仍可能进答案与引用 | **本步只登记**，作为独立缺陷单；4B 的 golden SQL 哈希（T-4B-9）会在修它时正当变红，届时必须显式更新 |
| **U-4B-3** | 告警阈值的真实标定（24h 是否为合理值） | 无真实运维数据；【源】`框架设计-v1.md:1294` 至今把"告警阈值"列为待定项 | 阈值过紧 → 换模型期间刷告警；过松 → 忘了重算无人知 | 给 24h 作**站位值**，按本项目纪律登记到 `placeholders`（见 §B.5.4） |
| **U-4B-4** | `embedding_dim` 是否需要一个列级守卫 | 维度写死在 `Vector(1024)` DDL（`entities.py:259`），改 dim 必须走迁移；迁移未跑时写入直接失败 → 目前"由 DDL 兜住" | 若将来支持多维度，`fingerprint` 里的 dim 就没有对应的列可比 | 本步**不加**，只在 `docstring` 里记一句"dim 由 DDL 兜底，不参与行级过滤" |
| **U-4B-5** | 多副本下启动横幅里的数字只代表启动那一刻 | 与 `U4-2` 同类（多副本无共享状态） | 横幅数字会过期 | 横幅明写"启动时快照"，并在 admin 端点给实时数 |

### A.5 待你裁决 DQ4B-x

> 格式：推荐值 + 两个备选 + 不裁决的后果。**加粗的是我的推荐**；标「反对」的是我不采纳主理人给定方向的地方，附理由。

#### DQ4B-1 关键词路口径过滤的最终裁决（对应 §A.3）

| 项 | 内容 |
|---|---|
| **推荐** | **4B-2 永久不做；从框架稿 §7 Step 4 的交付物里删除"检索期过滤扩到关键词两路"这一项**，替换为 §C.1 的可判定不变量 + 触发器 |
| 备选 1 | 做，但只在"同表多代次并存"开启时生效（需先做 §C.1 的不变量守卫） |
| 备选 2 | 做，且无条件生效（即现在就把口径列加进关键词两路） |
| 不裁决的后果 | 框架稿 §7 会一直挂着一个"未完成交付物"，而它按 A.3 是**不该完成**的 —— 下一轮的人会照做，然后把降级路径（`对照表:14`）关掉，症状是"换模型期间全库检索塌陷"，且没有任何指标能告诉他是这一步造成的 |
| 我为什么否掉备选 2 | 见 A.3.2 的表格：三条硬 DNA 列**不改变正文** → 过滤只损失召回、零收益；且它把 `对照表:14` 明确规定的预期降级路径变成"完全不可检" |

#### DQ4B-2 发布期 DNA 校验放在 W4 还是 W5（**反对主理人的"扩 W4"**）

| 项 | 内容 |
|---|---|
| **推荐** | **放 W5**（发布那一刻），W4 一行不改 |
| 备选 1 | 放 W4（主理人给定方向） |
| 备选 2 | 两处都放（W4 给"提前警告"，W5 给"最终判定"） |
| 不裁决的后果 | 若按备选 1 落地，会产生一个 4A 明确禁止的**陈旧闸门**：`validated` 时数据可检 → 期间有人删文档/跑重算 → 发布出去的场景处于与校验结论相反的状态 |
| **我反对的理由（三条，逐条有源）** | ① 【源】4A 已写明"发布期的外部条件不该在校验期冻结 —— 否则今天校验、明天平台关掉重排、后天发布就会发布一个与校验结论相反的东西"（`write_service.py:490-493`、`第四阶段…:198`）。DNA 可检率是**随时间变化的数据状态**，性质与"平台重排开关"完全同类。② 【源】`U4-10` 担心"加 DNA 校验会让 W4 变成变更点"（`第四阶段…:968`）—— 放 W5 就**没有这个变更点**，W4 保持"内容自洽"这一个语义。③ 【推导】W4 的语义是"内容冻结"，它对**版本行**负责；DNA 可检率是**语料状态**，与版本行无关。把语料状态塞进"内容冻结"这个动作，会让 `validated` 这个状态的含义变成"内容自洽 ∧ 当时语料恰好可用"，而后者随时会失效 |
| 若你仍选 W4 | 必须同时接受两件事：① `validated` 的语义要重写成"内容自洽 + 当时语料可检"，并在文档里明写"该状态随时可能变得与事实相反"；② 在 W5 再判一次（即落到备选 2），否则发布侧没有防线 |

#### DQ4B-3 `chunker_version` 是否升为硬 DNA

| 项 | 内容 |
|---|---|
| **推荐** | **不升**（保持软 DNA），但必须在 `needs_reindex` 里作为**独立分量**暴露（`chunker_only`） |
| 备选 1 | 升硬（检索期过滤 `c.chunker_version = :chunker_version`） |
| 备选 2 | 不升，且不暴露（现状的最省事延续） |
| 不裁决的后果 | 现状（备选 2）等于 R7 的"名为 DNA 实为 Policy"张力永远不解；而升硬会在下一次调 `CHUNK_OVERLAP_TOKENS` 时让全库检索塌陷 |
| 我为什么否掉备选 1（两条，可量化） | ① 【源】`_chunker_version()` 是三个常量的**派生值**（`index_profile.py:45-51`）—— 调 `CHUNK_TARGET_TOKENS` / `CHUNK_MAX_TOKENS` / `CHUNK_OVERLAP_TOKENS` 任一，`chunker_version` 就变。这三个是**常规调优旋钮**（Step 3 的 217 条 A/B 就是为了给它们定值），升硬等于"每调一次参数，全库不可检一次"。② 【源】`DNA与Policy口径对照表-v1.md:17` 与 `:51` 已把"加了过滤会让只调了分块参数就丢掉全部旧召回"写成**已知误判第 2 条**；`index_profile.py:19-21` 给出了机制依据（同一模型同一公式下，不同切法的向量仍在同一空间，余弦仍可比）—— 这个机制陈述我核过，**成立** |
| 升硬的**唯一翻转条件**（可判定） | 当且仅当"章节补全 / 表格回捞"的**边界错位**被证明会造成**用户可见的错误答案**（而不只是粒度差异）。判据：构造一组 `chunker_version` 混布的语料，跑评测集，若"表格类题"的错误率显著高于"全部同代次"的对照组 → 升硬。**在此之前不谈升硬** |

#### DQ4B-4 分母口径与告警阈值

| 项 | 内容 |
|---|---|
| **推荐** | 分母 = `chunks.is_latest = true`（**单条件**，I-4B-7）；**不产出任何"百分比就绪度"**，改为"四个互斥桶 + 一条对账恒等式 + 一条可判定告警式"（§B.2、§B.5.4） |
| 备选 1 | 沿用 `capabilities.py` 的做法：一个 `done/total` 百分比 + 覆盖率行 |
| 备选 2 | 只给"是否全绿"的布尔（`hard_stale == 0`） |
| 不裁决的后果 | 若照抄 `capabilities.py` 的**形态**而不看它的**教训**，`chunker_only` 会让百分比永远差几个点 → 就是 R10 记录的"闸门永远红 → 学会无视" |
| 我否决备选 1 的理由 | 【源】`capabilities.py:335-339` 的 `effective = total - skipped` 是一次**事后修补**：它把"明确不该抽"的从分母里剔除，才让百分比可收敛。而 4B 的 `chunker_only` **不收敛**（它的处置是全链重跑，不是跑个脚本），所以"减分母"这条路走不通 → 必须换形态：把不可收敛的分量从百分比里**拿出去**，作为独立计数 + 一次性提示 |
| 我否决备选 2 的理由 | 布尔会把 `hard_stale` 与 `chunker_only` 混成一个"是否全绿"，正是 R7 要避免的（软 DNA 不匹配不该让灯变红） |

#### DQ4B-5 发布期遇到「chunk 口径分布混合」时拒绝还是 WARN（**反对主理人的"不一致就拒绝"**）

| 项 | 内容 |
|---|---|
| **推荐** | **`hard_stale > 0` 一律不阻断，只 WARN + metric + 响应字段 + 审计留痕**。唯一阻断条件是**确定性数据损坏**：场景绑定的 kb 下**存在 `status='indexed'` 的文档**、但该 kb 的 `is_latest` chunk **总数为 0**（DDL 秒级可判，见 §B.4.3） |
| 备选 1 | 主理人方向：硬 DNA 不一致 → 拒绝发布 |
| 备选 2 | 全部只 WARN，连那一条阻断也不要 |
| 不裁决的后果 | 若按备选 1 落地，换模型期间**任何场景都无法发布**（包括新建的场景），而这恰好是"新场景自有数据"最需要上线的时刻；更糟的是它会**销毁它想保护的信息**（见下） |
| **我反对备选 1 的理由（三条）** | ① 【源】`dna_declared` 是声明镜像，请求路径永不读它（F-13）；读路径按 `current_index_profile()` 过滤（F-7）。所以"版本声明过期"**在行为上零后果**。② 【源】硬 DNA 不匹配的后果是"向量路拒检 → 主检索退化为仅关键词"（`对照表:14`），**不是**"全拒答" —— 因此"必然失效"这个前提为**假**；`retrieve()` 在向量路回 0 行时并不会早退，`_build_rrf_scores` 接受任意条路径（`retrieve/__init__.py:216-228`、`:258-259` 只在 `authorized_kb_ids` 为空时早退）。③ 【推导】**拒绝会销毁它想保护的信息**：被拒的处置是"W2 重建 draft（用当前 DNA 重新生成 `dna_declared`）"→ 旧声明被覆盖 → 恰好抹掉了"这个场景是在换模型之前设计的"这条审计事实。WARN 则把它留在 `scenario_versions.dna_declared` 与审计 `detail` 里 |
| 我否决备选 2 的理由 | "有已索引文档却 0 条 chunk"是一个**确定性**故障（parse_job 在 `status='indexed'` 的**同一事务**里写 chunks，`parse_job.py:228-232` —— 有文档、无 chunk 意味着数据被破坏或迁移残留），按 §0.2 的分流纪律它不属于"数据错 WARN"那一档，而属"必然失效"。这与 4A 的 W7 拒绝绑定"没有 published 的场景"（`write_service.py:811-817`）是同一类判断 |
| 补充：三档而不是二选 | 见 §B.4.2 的判定表：① 全无 chunk 但有已索引文档 → 拒；② 部分可检 → WARN（**这是"分布混合"的落点**）；③ 全可检 → 无提示 |

#### DQ4B-6 `chunker_version` 的"建议重切"信号被重算脚本抹掉，修不修

| 项 | 内容 |
|---|---|
| **推荐** | **修**：删除 `scripts/reindex_embeddings.py:117` 的 `chunker_version=profile.chunker_version`（详见 §B.8） |
| 备选 1 | 不修，接受"跑一次重算后 `chunker_only` 恒为 0" |
| 备选 2 | 不删赋值，改为在重算后**写一条独立的标记**（需要加列 → 0029 之外的迁移） |
| 不裁决的后果 | `needs_reindex` 视图会显示"分块参数已全部对齐"，而实际分块边界仍是旧的 —— 正是 R10 那类"数字对、事实错"的红灯 |
| 理由 | 【源】`:117` 在一个**只改 `embedding` 列**的 `update()` 里顺手改了 `chunker_version`（`:113-118`）。而该脚本**没有重切**（它用的 `row` 是重算前就读出来的 `content`/`token_count`，`:38-53`）。所以这是在写一个**假事实**：把"分块参数"标成新值，而正文边界是旧值。**这是既有缺陷，不是 4B 引入的**；但 4B 把它变得致命（因为 4B 让这个字段第一次有了消费方） |
| 选备选 1 的代价 | 4B 交付的 `chunker_only` 分量在第一次重算后就永久为 0 —— 等于交付了一个**永远不会说话**的观测项 |

#### DQ4B-7 是否新增 preflight 只读端点（R-4B-2）

| 项 | 内容 |
|---|---|
| **推荐** | **加**（它正是 DQ4B-2 选 W5 的**补偿**：W5 才判定会让"发布前无法预知"，preflight 把这个信息提前，但不冻结它） |
| 备选 1 | 不加，让运维去调 `GET /api/admin/needs-reindex` 自己判断 |
| 备选 2 | 加在 W4 里（返回 `changed=true` 同时附警告）—— 即主理人的方向 + 可观测 |
| 不裁决的后果 | 若不加且 W5 才判定，用户会在"点了发布"那一刻才第一次看到问题；若加在 W4（备选 2），又把警告挂在一个会过期的状态上 |
| 代价（必须承认） | 会触碰 `tests/scenario/test_step4_guards.py:31-40` 的 `_EXPECTED_ROUTES`（**追加一行**，是加严不是放宽） |

#### DQ4B-8 是否把 `rag.caliber.vector_empty`（降级可观测）纳入 4B-1

| 项 | 内容 |
|---|---|
| **推荐** | **纳入**，但**单独成一小步**（§F 第 5 步），可独立回滚 |
| 备选 1 | 不纳入，留给"可观测性专项" |
| 备选 2 | 纳入，但与 needs_reindex 同批交付（耦合，回滚时一起回滚） |
| 不裁决的后果 | 会漏掉本步**最真实**的可观测缺口：换模型后"主检索退化为仅关键词"（`对照表:14`）今天**完全不可见**（F-18：`retrieve` 的 27 处打点无一条与"结果为空"有关）。而 R10 的正解不是"再造一个百分比闸门"，是"让真实降级可见" |
| 代价 | 需要在 `retrieve/__init__.py` 加**一行** `metrics.incr`（无 SQL 变更 → 不违反 I-4B-4；T-4B-9 的 golden 哈希仍绿） |

---

## B. 实施规范：4B-1

### B.1 迁移 0029（DDL 全文）

#### B.1.1 文件头

```python
"""chunk 口径分布：可重算候选的统一判定函数 + 巡检视图

Revision ID: 0029
Revises: 0028
Create Date: 2026-11-22

★ 本迁移只建 1 个函数 + 1 个视图，不建表、不加列、不写数据、不建任何约束。
  零影响的数学根据：没有写入者、没有触发器、检索链路一行不改（I-4B-4）。

★ 为什么"判定"必须落在 SQL 函数里，而不是在 Python 里写 predicate：
  判定的消费方有两个 —— 应用（视图/端点）与重算脚本（--check 的 CI 门禁）。
  两处各写一遍 predicate 的第一次分叉就会产生「视图说 0 条、脚本说 300 条」，
  而这类分叉没有任何行为用例能同时覆盖两边。函数是唯一能保证"同一份实现"的载体。

★ 为什么函数要接收三个口径参数而不是在里面读 current_setting()：
  「当前口径」的唯一真源是 app/config/index_profile.py::current_index_profile()
  （它依赖 settings + decisions 两个 Python 层，DB 看不到）。让 DB 自己算等于
  造第二个真源 —— 那正是 index_profile.py 模块 docstring 反复警告的形态（I-4B-1）。
  故本函数是"数据形状的判据"，不是"当前口径的判据"。
"""
```

#### B.1.2 函数

```sql
CREATE OR REPLACE FUNCTION fn_chunk_reindex_candidates(
    p_embedding_model        text,
    p_embedding_text_version text,
    p_chunker_version        text
)
RETURNS TABLE (
    chunk_id              uuid,
    tenant_id             uuid,
    document_id           uuid,
    kb_id                 uuid,
    chunk_embedding_model text,
    chunk_text_version    text,
    chunk_chunker_version text,
    embedding_missing     boolean,
    stale_hard            boolean,
    stale_chunker         boolean,
    document_deleted      boolean,
    document_is_latest    boolean,
    chunk_created_at      timestamptz
)
LANGUAGE sql
STABLE
AS $$
SELECT
    c.id,
    c.tenant_id,
    c.document_id,
    c.kb_id,
    c.embedding_model,
    c.embedding_text_version,
    c.chunker_version,
    (c.embedding IS NULL)                                      AS embedding_missing,
    (c.embedding_model        <> p_embedding_model
     OR c.embedding_text_version <> p_embedding_text_version)   AS stale_hard,
    (c.chunker_version        <> p_chunker_version)            AS stale_chunker,
    COALESCE(d.deleted_at IS NOT NULL, false)                  AS document_deleted,
    COALESCE(d.is_latest, true)                                AS document_is_latest,
    c.created_at
FROM chunks c
LEFT JOIN documents d ON d.id = c.document_id
WHERE c.is_latest = true
$$;
```

【推导】四个实现细节，每个都有理由，不许"顺手"改：

1. **`WHERE c.is_latest = true` 是 universe 的唯一定义**（I-4B-7）。**不要**在这里加 `d.deleted_at IS NULL`、`d.status = 'indexed'`、`c.embedding IS NOT NULL` —— 这三个条件都是**列**（`document_deleted` / `document_is_latest` / `embedding_missing`），由调用方决定要不要排除。理由：分母一旦加条件就变成"判定的一部分"，而"排除谁"是需要独立裁决的（U-4B-1、DQ4B-4）。
2. **`LEFT JOIN` 而非 `INNER JOIN`**：`chunks.document_id` 是 `nullable=False` 外键（`entities.py:239`），理论上 inner join 不掉行；但用 LEFT + `COALESCE` 让"外键约束若将来被放松"这件事**不会改变行数**（分母不变是 I-4B-7 的要求）。
3. **`STABLE` 而非 `VOLATILE`**：同事务内多次调用结果一致，且让 planner 可以内联（SQL 函数在 STABLE 下可被内联，`WHERE f.stale_hard` 的下推才有效）。**不加 `SECURITY DEFINER`** —— 默认 `SECURITY INVOKER`，权限跟随调用方，与既有"四闸门把授权下推到 SQL"的纪律一致（`retrieve/__init__.py:88-97`）。
4. **三个 `IS NOT DISTINCT FROM` 语义**：这里用 `<>`。理由：三列都是 `nullable=False`（`entities.py:263-265`），`NULL` 不可能出现；用 `<>` 的好处是**若将来某列被改成 nullable，函数会静默漏判而不是抛错** —— 这是错的。因此 **`0029` 的同批必须加一条断言**：见 §B.1.4 的第 2 条（用 `DO` 块在迁移里显式检查三列的 `NOT NULL`，不满足则 `RAISE EXCEPTION`）。

#### B.1.3 巡检视图

```sql
CREATE OR REPLACE VIEW v_chunk_caliber_distribution AS
SELECT
    c.tenant_id,
    c.embedding_model,
    c.embedding_text_version,
    c.chunker_version,
    COALESCE(d.deleted_at IS NOT NULL, false) AS document_deleted,
    count(*)                                  AS chunk_count,
    count(*) FILTER (WHERE c.embedding IS NULL) AS embedding_missing,
    min(c.created_at)                         AS oldest_chunk_at,
    max(c.created_at)                         AS newest_chunk_at
FROM chunks c
LEFT JOIN documents d ON d.id = c.document_id
WHERE c.is_latest = true
GROUP BY 1, 2, 3, 4, 5;
```

【推导】这个视图**故意不含任何口径参数**：它回答的是"库里到底有几套口径"，而不是"哪些过期"。把两者分开的理由——**"哪些过期"需要当前口径（Python 侧），"有几套口径"不需要**。混在一起会让视图的列语义随部署变化，而视图是要给人 `psql` 里看的（列语义必须稳定）。

用法（运维第一句话）：
```sql
SELECT * FROM v_chunk_caliber_distribution ORDER BY chunk_count DESC;
-- 期望（单 DNA 稳态）：恰好 1 行，且 embedding_model / text_version / chunker_version
--                       与启动横幅打印的三元组一致。多于 1 行 = 正在重算或口径已分叉。
```

#### B.1.4 迁移里必须有的两条守卫（不是可选的）

1. **`alembic heads` 单头**：`down_revision = "0028"`，且 `0028` 之后无其它分支。由 `test_0029_migration.py` 的 `_render("up","0028:0029")` 断言存在 `-- Running upgrade 0028 -> 0029`。
2. **三列 `NOT NULL` 断言**（对应 §B.1.2 细节 4）：
```python
op.execute("""
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'chunks'
      AND column_name IN ('embedding_model','embedding_text_version','chunker_version')
      AND is_nullable <> 'NO'
  ) THEN
    RAISE EXCEPTION '0029: chunks 三列口径必须 NOT NULL，否则 stale_hard 的 <> 比较会漏判 NULL';
  END IF;
END $$;
""")
```
【推导】这是"函数正确性的前置条件"，而不是"约束"：函数里用 `<>`，NULL 会得到 NULL（既不 true 也不 false）→ 该行四个桶都不落 → **对账恒等式失配**。把前置条件写成迁移里的显式断言，比写在注释里强 —— 注释不会失败。

3. **不加任何 CHECK / UNIQUE 约束** → `tests/scenario/test_0028_migration.py::_pinned_constraint_names()` 与 `test_pg_tests_have_no_unnamed_violation_assertions`（`:225-264`）**均不受影响**，`_PG_CONSTRAINT_SOURCES` 不需要追加注册。这条要写进迁移 docstring，否则下一个人会以为"新迁移必须登记"。

#### B.1.5 `downgrade()`

```python
def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_chunk_caliber_distribution")
    op.execute("DROP FUNCTION IF EXISTS fn_chunk_reindex_candidates(text, text, text)")
```
【推导】函数与视图**无数据、无写入者** → 降级是纯 DDL，没有数据回滚问题。**但有一个顺序陷阱**：`scripts/reindex_embeddings.py` 会调这个函数（§B.7）→ 若先 downgrade 再 revert 脚本，脚本会 `UndefinedFunction`。因此回滚顺序**必须**是"先 revert 脚本、再 downgrade 迁移"（§E.1 把它写成可执行步骤）。

### B.2 分母口径的完整定义

#### B.2.1 定义（一句话 + 三个反例）

> **分母（universe）= `chunks` 表中 `is_latest = true` 的全部行。**

与 `scripts/reindex_embeddings.py:45-51` 的选取条件**逐字相同**（它只有 `is_latest` 这一个 `where` 条件 + 一个 `or_` 的口径判断；`or_` 部分是"分子判定"，不是"分母条件"）。

**三个必须排除的候选口径，及其排除理由**：

| 候选条件 | 排不排除 | 理由 |
|---|---|---|
| `c.embedding IS NOT NULL` | **不排除** | 排除它会让"口径匹配但 embedding 为空"的行**从分子分母同时消失** → 这类行向量路永远检不到（`retrieve:113` 的 `AND c.embedding IS NOT NULL`）且**没有任何数字**说明它存在。**必须独立成桶**（`embedding_missing`） |
| `d.deleted_at IS NULL` | **不排除**（但并列暴露） | 【源】软删不删 chunk、召回 SQL 不 join documents（F-17）→ 这些 chunk **仍可被检出**，所以"它们算不算要重算的对象"取决于"你要不要为可检出的数据保留可用性"。这是**运维决策**（U-4B-1），不是口径定义。故作为 `in_dead_documents` 独立列 |
| `d.status = 'indexed'` | **不排除** | 【源】`capabilities.py:66-72` 的教训在这里**不适用**，且必须说清为什么：图的输入是 `documents`（存在但未索引的文档会进分母 → 永远差几篇 → 永远红），而 chunk 的输入**已经是 chunks**（`parse_job.py:228-232` 在 `status='indexed'` 的同一事务里写 chunk，未索引文档**根本没有 chunk 行**）→ 该条件**自动成立**，加它是冗余的，而冗余条件最危险的地方是"将来它变得不冗余时没人知道" |
| ACL 条件（`level_rank` / `kb_id` / `deny_subjects` / `acl_tags`） | **绝不排除** | 这些是**请求相关**的（`_WHERE_BASE:92-97` 的绑定值来自 `CurrentUser`）→ 加进分母会让分母随请求变化，而分母必须是**部署级常量**（I-4B-7） |

#### B.2.2 为什么"与脚本同源"必须靠结构，不能靠纪律

【推导】若视图谓词与脚本谓词是**两处代码**，分叉只需要一次"改了一处忘了另一处"，而这类分叉有个恶劣性质：**它没有任何单一行为用例能同时覆盖两边**（视图用例绿、脚本用例也绿，两边各自内部自洽，只有"两边不一致"这件事没人测）。所以本设计把谓词放进 `fn_chunk_reindex_candidates`，脚本改为调它（§B.7）→ 分叉在**结构上不可能**（I-4B-2）。这与 4A 的 `PUSHDOWN_WHERE_SQL 是唯一真源，图/wiki 复用同一字符串`（`框架设计-v1.md:476`）是同一手法。

#### B.2.3 可直接跑的 SQL（对账恒等式）

```sql
-- 用法：三个 :param 从启动横幅抄（或从 python -c 'from app.config.index_profile import
--       current_index_profile as f; p=f(); print(p.embedding_model, p.embedding_text_version,
--       p.chunker_version)' 取）。这不是"第二真源"，只是把参数抄进去跑给人看。
WITH f AS (
    SELECT * FROM fn_chunk_reindex_candidates(:model, :text_version, :chunker_version)
)
SELECT
    count(*)                                                                    AS universe,
    count(*) FILTER (WHERE stale_hard)                                           AS hard_stale,
    count(*) FILTER (WHERE stale_chunker AND NOT stale_hard)                     AS chunker_only,
    count(*) FILTER (WHERE embedding_missing
                       AND NOT stale_hard AND NOT stale_chunker)                 AS embedding_missing,
    count(*) FILTER (WHERE NOT stale_hard AND NOT stale_chunker
                       AND NOT embedding_missing)                                AS ok,
    count(*) FILTER (WHERE document_deleted OR NOT document_is_latest)           AS in_dead_documents,
    max(chunk_created_at) FILTER (WHERE NOT stale_hard)                          AS newest_ok_at,
    max(chunk_created_at) FILTER (WHERE stale_hard)                              AS newest_stale_at,
    min(chunk_created_at) FILTER (WHERE stale_hard)                              AS oldest_stale_at
FROM f;
```

【推导】四桶互斥的优先级写在 `FILTER` 里（hard > chunker > missing > ok），且**这就是唯一正确的写法**：若写成 `count(*) FILTER (WHERE stale_chunker)` 而不加 `AND NOT stale_hard`，四桶会重叠，恒等式必失配（T-4B-3 的破坏方式正是这个）。

**对账恒等式（必须由代码断言，不靠肉眼）**：
```
ok + hard_stale + chunker_only + embedding_missing == universe
```
【推导】这条恒等式的价值：它是唯一能同时抓住"漏判"（某行四桶都不落）与"重判"（某行落两桶）的断言，而这两种错误在**单看任何一个桶**时都不可见。

#### B.2.4 `in_dead_documents` 与 `newest_ok_at` 的读法（写进端点文档，否则数字会被误读）

| 列 | 正确读法 | 常见误读 |
|---|---|---|
| `in_dead_documents` | 分母中"所属文档已软删或非最新版本"的行数。它们**仍可能被检出**（F-17） | 误读为"可以不重算" → 决定权在 U-4B-1 |
| `newest_ok_at` | 最新一条**合规** chunk 的入库时间 = "当前代次至少写到这个时刻" | 误读为"这是一次重算的时间" —— 它是 chunk 行的 `created_at`，不是重算时间 |
| `newest_stale_at` / `oldest_stale_at` | 不匹配行的时间范围。**它们回答的是"这些行什么时候入库的"，不是"它们已经过期多久"** —— 因为"口径切换发生在何时"这个事件**没有被记录**（这是 U-4B-6，见 §I） | 误读为"已过期 N 天"，进而把一个总是很大的数字当噪音 |

> 【推导】上表第 3 行是本步**最容易被写成假绿**的地方：若按"`oldest_stale_at` 距今多久"设告警阈值，阈值会在稳态下**恒为真**（旧数据本来就老），于是又一个"永远红的闸门"。所以告警**不用 stale 的时间**，而用 `newest_ok_at`（§B.5.4）。

### B.3 读路径

#### B.3.1 R-4B-1 `GET /api/admin/needs-reindex`

**为什么叫 `needs-reindex` 而不是 `reindex-status`**：与框架稿 `:799`、`:1294` 的用词一致，让人能按名字搜到设计依据。

请求：无参数（口径从 `current_index_profile()` 取，**绝不**从 query 取 —— 允许传口径等于允许"用别的口径问这个部署"）。
可选 `?scope=deployment`（默认 `tenant`）。

响应（200）：
```json
{
  "profile": {"embedding_model": "bge-m3", "embedding_text_version": "etext-v1",
              "chunker_version": "c400-800-60"},
  "scope": "tenant",
  "universe": 4213, "ok": 4100, "hard_stale": 108, "chunker_only": 5,
  "embedding_missing": 0, "in_dead_documents": 12,
  "newest_ok_at": "2026-11-22T03:11:04+00:00",
  "newest_stale_at": "2026-11-19T08:02:11+00:00",
  "reconciles": true,
  "verdict": {"level": "warn", "reason": "hard_stale_without_progress",
              "detail": "有 108 条 chunk 硬口径不匹配，且最新合规 chunk 已 19.2 小时未前进"},
  "note": "chunker_only 是软 DNA：不拒绝检索，只建议重切；它的处置是全链重跑，不进任何就绪度分母"
}
```

【推导】四处刻意的设计：

1. **`profile` 回显**：没有它，数字无法被解释（"108 条过期"相对哪个口径？）。回显的是**当前部署的值**，不是用户的输入。
2. **`reconciles` 布尔**：把 §B.2.3 的恒等式搬进响应。**它必须是响应里的字段**，不能只在日志里 —— 否则"对账失败"这件事只有翻日志的人知道。
3. **`verdict.level` ∈ `{ok, info, warn}`，没有 `error`**。理由：`hard_stale > 0` 在换模型窗口内是**合法**状态，用 `error` 会让合法状态产生 error 级信号 → R10 的"学会无视"从这个词开始。
4. **`note` 里明写 `chunker_only` 的性质**：这是唯一能拦住"运维看到 chunker_only 就去跑 reindex 脚本（跑完也没用，因为脚本不重切）"的一行文案。

**租户边界**（必须写死）：
- `scope=tenant`（默认）：`universe / hard_stale / …` 四个桶**只统计 `c.tenant_id = CurrentUser.tenant_id`**。
- `scope=deployment`：**只返回五个总数**（`universe / ok / hard_stale / chunker_only / embedding_missing`），**不返回** `newest_*_at`、不返回任何 `tenant_id` / `document_id` / `kb_id`。
- 【推导】理由：`--check` 的退出码是**全局**的（§B.7 的脚本无租户条件），运维必须能对上那个数；但**明细**跨租户是越权。所以"全局只给计数、明细只给本租户"是唯一同时满足"可对账"与"不越权"的切法。这与 `self_check.py:210-223` 的口径一致（"全仓唯一不带 `tenant_id` 的场景读取，三条约束让它安全：只在启动期执行 / 只用于计数与日志 / 日志只允许特定字段"）—— 本端点复用同一套三条约束。

**路由挂载**：新建 `app/api/needs_reindex.py`，`APIRouter(prefix="/admin", tags=["admin"])`，在 `app/main.py` 的 `main.py:403-…` 那一段加一行 `include_router`。认证：`Depends(require_admin)`（`deps.py:142-152`）。

#### B.3.2 R-4B-2 `GET /api/scenarios/{scenario_id}/versions/{version}/preflight`（DQ4B-7 采纳时）

作用：在**发布之前**回答"如果我現在点发布，会发生什么"，且**不冻结**结论。

响应（200）：
```json
{
  "version": 3, "state": "validated", "checksum": "9f2c1a…",
  "policy_publishable": true,
  "declaration": {
    "stale_keys": ["embedding_text_version"],
    "declared": {"embedding_text_version": "etext-v1", "chunker_version": "c400-800-60"},
    "current":  {"embedding_text_version": "etext-v2", "chunker_version": "c400-800-60"}
  },
  "kb_readability": {
    "kb_count": 2, "chunks_total": 431, "chunks_vector_readable": 431,
    "chunks_hard_stale": 0, "documents_indexed": 37
  },
  "publish_blockers": [],
  "publish_warnings": []
}
```

【推导】三个要点：
- `declaration.stale_keys` 只给**键名**，不给环境绑定项的值（`embedding_model` 属环境绑定，`dna_declared` 里只有字段名，比不了 —— 见 `dna_declare.py:20-26`）。
- 只读、无锁、无事务副作用：**不调** `_lock_scenario`（那是写路径的锁，读端点拿它会与写互斥，把一个查询变成阻塞点）。
- 跨租户 / 不存在 → **404**（同 4A 口径，`write_service.py:147-149`）。

**这个端点触碰的既有测试**：`tests/scenario/test_step4_guards.py:31-40` 的 `_EXPECTED_ROUTES` 必须追加
```python
"/api/scenarios/{scenario_id}/versions/{version}/preflight": {"get"},
```
【推导】这是 4A 那条守卫**设计好的**触发方式（它的 docstring 说"少一条就是『文档里有、线上 404』"）→ 加路由时它会红，逼你显式登记。**这是加严，不是放宽**：断言仍是"集合精确相等"。

### B.4 写路径 W-4B-1：W5 的口径快照

#### B.4.1 位置（一行代码都不能挪）

【源】`write_service.publish()` 的既有顺序（`write_service.py:560-657`）：
```
锁场景 → 锁版本 → 状态判定 → If-Match → T4 → checksum 复核 → 归档旧 → 发布 → 审计 → commit → invalidate
```
【推导】4B 的快照插在 **`checksum` 复核之后、归档旧 published 之前**，理由三条（与 4A 给 T4 定的理由同构）：

1. **必须在 If-Match 之后**：快照要针对"我看到的那份内容"。
2. **必须在归档之前**：若快照里的判定要阻断（§B.4.3 的那一档），阻断时旧 published **不能被归档** —— 否则事务回滚虽能复原，但"会失败的判定放在最前面"是零成本的正确做法（同 `第四阶段…:518`）。
3. **必须在 `commit` 之前**：快照要写进**同一条审计**，不能另开事务（否则又出现"主事务回滚、审计已提交"，即 4A 的 A-11）。

#### B.4.2 三档判定（这是 DQ4B-5 的落地）

```python
# needs_reindex.py —— 纯函数，零 IO，可单测
@dataclass(frozen=True)
class KbReadability:
    kb_count: int
    chunks_total: int
    chunks_hard_stale: int
    documents_indexed: int

@dataclass(frozen=True)
class PublishVerdict:
    blocker: str | None          # 唯一非空值：KB_EMPTY_BUT_INDEXED
    warnings: tuple[str, ...]    # 可空

def judge_publish_snapshot(r: KbReadability, *, declared_stale_keys: Sequence[str]) -> PublishVerdict:
    warnings: list[str] = []
    if declared_stale_keys:
        warnings.append("STALE_DECLARATION")
    if r.chunks_total and r.chunks_hard_stale:
        warnings.append("PARTIAL_UNREADABLE")
    if r.chunks_total == 0 and r.documents_indexed > 0:
        return PublishVerdict(blocker="KB_EMPTY_BUT_INDEXED", warnings=tuple(warnings))
    return PublishVerdict(blocker=None, warnings=tuple(warnings))
```

| 档 | 数据形状 | 判定 | 为什么 |
|---|---|---|---|
| **① 拒** | `chunks_total == 0` **且** `documents_indexed > 0` | 409 `SCENARIO_KB_EMPTY_BUT_INDEXED` | 确定性数据损坏：`parse_job.py:228-232` 在 `status='indexed'` 的**同一事务**里写 chunks → 有已索引文档却 0 条 chunk，只可能是数据被破坏/迁移残留。这与 4A 的 W7 拒绝"没有 published 的场景"（`write_service.py:811-817`）同类：**发布一个必然无数据的场景 = 制造一个必然全拒答的配置** |
| **② WARN** | `chunks_total > 0` 且 `chunks_hard_stale > 0` | 允许发布；`dna_snapshot` 带 `PARTIAL_UNREADABLE` + metric + 审计 | **这就是"分布混合"的落点**。混合是合法状态（正在重算 / 新文档刚入库未索引完），后果是向量路可检率下降、关键词路照常 → 属"数据错"（`main.py:123-125`），正确动作是 WARN |
| **③ 无提示** | `chunks_total > 0` 且 `chunks_hard_stale == 0` | 允许发布；`dna_snapshot` 仍回（值全 0 对齐） | —— |
| 附加 | `declared_stale_keys` 非空 | 一律只 WARN | 见 DQ4B-5 的反对理由（拒绝会销毁它想保护的信息） |

【推导】为什么 ② 一定要"允许"而不是"拒"：`retrieve()` 在向量路回 0 行时**不会早退**，只有 `authorized_kb_ids` 为空才早退（`retrieve/__init__.py:258-259`）；RRF 接受任意条路径（`:216-228`）。所以"硬 DNA 全不匹配"的场景**仍然可用**（关键词路），只是降级 —— 而这正是 `DNA与Policy口径对照表-v1.md:14` 写作**预期**的行为。拒绝它等于宣布"平台不支持自己文档里承诺的降级模式"。

#### B.4.3 `scenario_kb_readability()` 的 SQL（必须与召回 SQL 同源）

```python
_SQL_KB_READABILITY = """
WITH f AS (
    SELECT * FROM fn_chunk_reindex_candidates(:model, :text_version, :chunker_version)
)
SELECT
    count(DISTINCT f.kb_id)                          AS kb_count,
    count(*)                                         AS chunks_total,
    count(*) FILTER (WHERE f.stale_hard)              AS chunks_hard_stale
FROM f
WHERE f.kb_id = ANY(:kb_ids) AND f.tenant_id = :tenant_id
"""
_SQL_DOCS_INDEXED = """
SELECT count(*) AS n
FROM documents
WHERE tenant_id = :tenant_id
  AND kb_id = ANY(:kb_ids)
  AND is_latest = true AND deleted_at IS NULL AND status = 'indexed'
"""
```

【推导】三条纪律，每条都防一类假事实：
- **用同一个函数**（不是另写 predicate）：否则"发布时的可检比例"与"端点报的过期数"会是两个数（I-4B-2 的同一个理由）。
- **带 `tenant_id`**：`kb_ids` 来自版本行，版本行已按租户锁定（`_SQL_LOCK_VERSION` 带 `v.tenant_id`），但仍显式带 —— 4A 的文本守卫（`test_step4_guards.py` 的 SQL tenant 守卫）会检查这种"看起来多余"的条件存在于每条语句。
- **不用 ACL 条件**：ACL 是请求相关的（`_WHERE_BASE:92-97`），发布时**没有**一个"具体请求者"。用 `clearance=100` 之类假值会让数字变成另一个口径的谎言。因此这里的"可检"= "**口径维度上**可检"，不含授权维度 —— 这句话必须写进 `dna_snapshot` 的 `note`。

#### B.4.4 写进哪里（三个去处，一个都不能少）

| 去处 | 内容 | 为什么必须 |
|---|---|---|
| 响应 `VersionActionOut.dna_snapshot` | 完整块（§B.3.2 的 `declaration` + `kb_readability` + `warnings`） | 让"发布到了一个降级状态"这件事在调用方**当场可见**，而不是等下一次 GET |
| 审计 `detail` | `{"dna_snapshot": {…}, "dna_warnings": [...]}` | "半年后回答：这个场景发布时是不是在降级状态"（4A §B.7 的审计价值判据） |
| metric `.incr("rag.caliber.publish_with_stale_dna")` | 1 次（有 WARN 才打） | 让"累计有 N 次发布发生在降级状态"可查 |

【推导】`dna_snapshot` **不进** `checksum`（I-4B-5）：`compute_checksum` 的输入集是四个**内容**字段（`snapshot.py:302-309`），它是历史的、声明性的；快照是**当下观测的**。把观测塞进 checksum 会让同一个 `If-Match` 因为语料变化而失效 —— 那是把写接口的并发机制绑到语料状态上。

### B.5 可观测

#### B.5.1 为什么不加 Gauge（以及加了为什么是错的）

【源】`infra/metrics.py` 只有 Counter / Histogram（`:40-41`），API 只有 `incr` / `observe_ms`（`:74-90`）。要加 Gauge 必须动 `snapshot()`，而 `tests/test_metrics_and_rerank.py:80` 断言 `set(snap.keys()) == {"counters", "histograms"}`。

【推导】两条路，我选后者：
- 路 A：改 `snapshot()` 加 `"gauges"` 键 → 必须改 `:80`。这是**事实变更**（`== {"counters","histograms","gauges"}`），技术上允许；但它为"一个数字"付出"动一条已验收断言"的代价，而该数字有更好的载体（专用端点）。
- 路 B（**采用**）：**不加 Gauge**。"当前值"由 R-4B-1 提供（它就是为此存在的）；Prometheus 侧沿用既有 `incr(name, value=n)` 形态，并**在指标名与文档里明写它是"观测事件累计"而非当前值** → 告警规则必须写 `rate(...) > 0`，不能写 `> 0`。

> 【推导】路 B **不是妥协**：`self_check.py:620-622` 的 `_emit()` 已经是这个形态（`metrics.incr(metric_name, counts[key])`），4B 沿用它 = 零新基础设施、零既有断言改动。而"用 Gauge 表达当前值"在本项目里唯一缺的东西是**谁来定时 set 它** —— api 没有调度器，Prometheus 抓取时也不能异步查库 → Gauge 会变成一个"上次谁调端点时的值"，比没有更糟（它看起来实时，实则不是）。

#### B.5.2 启动横幅一行（人可读，每次部署必看）

```python
# app/config/capabilities.py 相邻位置新增，或放 needs_reindex.py 里由 main 调用
def format_banner_line(r: ReindexReport) -> str:
    return (
        f" chunk 口径               : {r.ok}/{r.universe} 条对齐"
        f" · 硬过期 {r.hard_stale}"
        f" · 建议重切 {r.chunker_only}（软 DNA，不拒绝检索）"
        f" · 空向量 {r.embedding_missing}"
        + (f" · 属已删/旧版文档 {r.in_dead_documents}" if r.in_dead_documents else "")
    )
```

调用点：`app/main.py` 的 `await log_capability_banner(role="api")`（`:171`）之后。**【推导】为什么用 `print` 而不是 `logger.info`**：沿用 `capabilities.py:443-445` 的理由 —— 横幅要出现在 `docker logs` 第一屏，不被 `LOG_LEVEL` 过滤。

【推导】为什么**只放 api 侧不放 worker**：与 T2 自检同一条理由（`main.py:126-130`）—— worker 不检索，跑全场景统计是纯加成本。

#### B.5.3 counter 打点（三处，命名与语义都要写死）

| 指标名 | 形态 | 语义（必须写进 docstring） | 告警规则写法 |
|---|---|---|---|
| `rag.caliber.hard_stale_observed` | `incr(name, value=n)` | **观测事件累计**：本次观测到 n 条硬过期。**不是当前值** | `rate(...) > 0` |
| `rag.caliber.chunker_mismatch_observed` | 同上 | 同上（软 DNA 分量） | **不告警**（DQ4B-4） |
| `rag.caliber.vector_empty` | `incr(name, keyword_hit="yes"/"no")` | 一次检索中向量路返回 0 行（即进入降级路径） | `rate(...{keyword_hit="yes"}) > 0` |

【推导】第三条是本步**最有价值**的一项（DQ4B-8）：它把 `对照表:14` 承诺的"退化为仅关键词"从**纸面**变成**可观测量**。而它正好是 R10 的正解形态 —— R10 的问题不是"缺指标"，是"指标的处置是'等它变绿'而它永远不会绿"；`vector_empty` 的处置是"看它是不是只出现在某段时间"，有明确的陡增/回落形状，不是一个只会亮不会灭的灯。

#### B.5.4 告警式（DQ4B-4 的落地，必须可判定）

```python
def verdict(r: ReindexReport, *, now: datetime, stale_progress_hours: float = 24.0) -> Verdict:
    """告警判定。★ 判据用 newest_ok_at（最新合规 chunk 的时间），不用 stale 的时间。"""
    if r.hard_stale == 0:
        return Verdict("ok", "all_aligned")
    if r.newest_ok_at is None:
        # 全库无一条合规 → 换完口径一条都没重算过（或全库都是旧口径）
        return Verdict("warn", "no_compliant_chunk")
    if (now - r.newest_ok_at) > timedelta(hours=stale_progress_hours):
        # 新一代次已 N 小时没有前进，而旧代次还在 → 重算卡住/忘了
        return Verdict("warn", "hard_stale_without_progress")
    return Verdict("info", "reindex_in_progress")
```

【推导】为什么这个判据**不会变成永久红灯**（R10 的正解）：

1. **重算进行中** → 每算完一条 `newest_ok_at` 就往前推 → `info`，不告警。
2. **重算卡住 / 忘了** → `newest_ok_at` 停住 → 超过 24h 变 `warn`。这个 warn 是**可处置**的（跑一次脚本），跑完就消失。
3. **全库无合规** → `newest_ok_at IS NULL` → 立刻 `warn`。这是唯一一个"数据缺失"档，且它的处置同样是跑脚本。
4. 与"`oldest_stale_at` 距今多久"的对比：后者在稳态下**恒大**（旧数据本来就老）→ 恒真 → R10。（§B.2.4 表第 3 行）

`stale_progress_hours = 24` 是**站位值**，必须按项目纪律登记到 `app/config/placeholders.py`（与 `RECALL_TOP_K_MAX` 同款，`policy_spec.py:44-48` 展示了登记断言的写法），否则它会变成"只写在 docstring 里、没人知道它没标定"的常量（`S-6` 的形态，`policy_spec.py` 文件头第 3 段）。

`chunker_only` **不进 `verdict()`**（I-4B-3）：它只出现在横幅与端点响应里。

### B.6 错误码

【源】4A 已建立 12 码表（`第四阶段-Step4-详细设计.md:230-243`），且有一条守卫强制"每个 `WriteCode` 必须有 `_HTTP` 映射"（`test_step4_guards.py` 的 docstring 第 4 条）。

| code | HTTP | 客户端该做什么 | 触发点 |
|---|---|---|---|
| `SCENARIO_KB_EMPTY_BUT_INDEXED` | **409** | 别重试；该场景绑定的知识库有已索引文档但没有任何 chunk，去查解析/迁移链路 | W5（§B.4.2 档①） |

【推导】三条判断：

1. **为什么是新码而不是复用 `SCENARIO_NOT_PUBLISHABLE`**：4A 的判据是"客户端的一种不同处置"（`:228`）。`NOT_PUBLISHABLE` 的处置是"改内容或改平台开关"（人还是这个人，改的是配置）；本码的处置是"去查数据管道"（**另一个责任方**，可能是运维/迁移）。合成一个码会让客户端的 `case` 分支写不出区别。
2. **为什么 409 不是 400**：这不是"请求体写错了"（请求体完全合法），而是"世界的状态与这个请求冲突"（同 4A 的 `STATE_CONFLICT` / `IN_USE`）。400 会让客户端以为改请求能解决。
3. **`context` 只放三个计数**：`{"kb_count": n, "documents_indexed": n, "chunks_total": 0}`，不回显 kb id 列表（id 已在版本行里，回显只增大日志）。

`write_errors.py` 的 `_HTTP` 映射必须同步追加（否则走到这条会 `KeyError` → 500，正是守卫要拦的那个形态）。

### B.7 脚本同源改造（`scripts/reindex_embeddings.py`）

```python
async def _load_stale() -> list:
    """取出所有硬口径与当前不一致的 is_latest chunk。

    ★ 谓词来自 fn_chunk_reindex_candidates（迁移 0029），**不在这里重写**：
      视图/端点与 --check 门禁必须同源，否则「视图说 0 条、脚本说 300 条」无从发现。
    ★ 本函数只投影重算所需的五列：判定的形状归函数，取数的列归调用方。
    """
    profile = current_index_profile()
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT c.id, c.content, c.heading_path, c.token_count, c.page_no "
                    "FROM chunks c "
                    "JOIN fn_chunk_reindex_candidates(:m, :tv, :cv) f ON f.chunk_id = c.id "
                    "WHERE f.stale_hard "
                    "ORDER BY f.chunk_created_at, f.chunk_id"
                ),
                {"m": profile.embedding_model,
                 "tv": profile.embedding_text_version,
                 "cv": profile.chunker_version},
            )
        ).all()
    return list(rows)
```

【推导】**加了 `ORDER BY` 是必要的，不是顺手**：原实现用 `select(...)` 无 `ORDER BY`，行序由堆表决定。分批重算时"哪 8 条先进一批"因此**不可复现**；一旦某批失败，重跑时的批次划分与上次不同 → 排障时"上次是哪几条失败"无法还原。加一个确定性排序是零成本的。

**`--check` 语义不变**：`return 1 if total else 0`（`:79-81`）一行不改 —— 这是 CI 门禁的契约，改了就是破坏既有验收。

**性能**：函数是 `STABLE` + `LANGUAGE sql` → PG 可内联；`WHERE f.stale_hard` 会下推成 `chunks` 上的过滤。与原 `or_` 条件同量级（都是对 `is_latest` 行的扫）。**本步不做索引优化**，理由：`chunks` 已有 `idx_chunk_filter (tenant_id, kb_id, is_latest, level_rank)`（`entities.py:277`），口径三列无索引，但"过期扫描"是离线动作、非请求路径；若实测慢，下一轮加 `(is_latest, embedding_model)` 组合索引（登记为 U-4B-7）。

### B.8 修既有缺陷：`reindex_embeddings.py:117` 篡改 `chunker_version`

#### B.8.1 事实

【源】`:94-118` 的循环里：
```python
await session.execute(
    update(Chunk)
    .where(Chunk.id == row[0])
    .values(
        embedding=vec,
        embedding_model=profile.embedding_model,
        embedding_text_version=profile.embedding_text_version,
        chunker_version=profile.chunker_version,        # ← :117
    )
)
```
【源】`row` 来自 `_load_stale()`（`:33-54`），投影 `(id, content, heading_path, token_count, page_no)` —— **没有**重新分块。`_row_text()`（`:57-67`）只是把**原有** `content` 拼成 embedding 输入。

#### B.8.2 后果（三步推演）

1. `chunker_version` 被写成当前值，但 `content` / `token_count` / 边界**仍是旧分块**的产物。
2. 于是"建议重切"信号（`index_profile.py:19-21` 定义的唯一用途）**在跑一次重算后永久消失**。
3. 4B 的 `chunker_only` 分量因此会在第一次重算后恒为 0 —— **交付了一个永远不会说话的观测项**。

【推导】这不是"顺手多写了一列"的洁癖问题：`chunker_version` 的**全部语义**就是"这行正文是按哪套分块参数切出来的"。写了新值就撒了一个谎，而这个谎恰好是 4B 唯一的观测对象。

#### B.8.3 修法

删除 `.values(...)` 里的 `chunker_version=profile.chunker_version` 一行（其余三列保留：它们是真实的，因为向量确实按新口径重算了）。

【推导】影响面：
- **对重算本身零影响**（重算只关心 `embedding` 与两个硬口径列）。
- **对 `--check` 零影响**（它的 `where` 不含 `chunker_version`）。
- **对 4B 的 `chunker_only` 是决定性影响**：修后它才有意义。
- **对既有测试**：`tests/` 里无任何用例断言"重算脚本会写 chunker_version"（我按 `grep -rn chunker_version tests/` 逐条核过：命中的都是夹具字面量与 `0028` 种子断言，无一处断言此行为）。

【推导】是否该给出"重切完成后写 chunker_version"的正路？**是**，但那是"真正重切"的责任——即 `parse_job`（`:223`、`:530` 已经正确地写当前值）。所以修法与既有架构一致：**只有真正按新参数切了，才写新参数**。

---

## C. 4B-2 条件设计（本期只设计，不实现）

### C.1 触发条件必须是可判定的（这是主理人要求的核心）

主理人给的方向是"存在多 DNA 代次灰度时"。**这个方向不可判定** —— "代次灰度"是一个部署流程的名字，不是一个能查的状态。我把它替换成**两条可判定的谓词**，任一为真即触发：

```sql
-- 谓词 P1：同一 (document_id, chunk_index) 出现多行 is_latest 数据
--          = 表里已经存在"两代并存"，关键词路确实会返回重复/错位内容
SELECT count(*) FROM (
    SELECT document_id, chunk_index
    FROM chunks WHERE is_latest = true
    GROUP BY 1, 2 HAVING count(*) > 1
) t;
```
```sql
-- 谓词 P2：同一 (document_id, chunk_index) 在一行里存在两套口径
--          = 双写已经发生（不同代次的行共用同一个 chunk_index）
SELECT count(*) FROM (
    SELECT document_id, chunk_index
    FROM chunks WHERE is_latest = true
    GROUP BY 1, 2
    HAVING count(DISTINCT embedding_model || '|' || embedding_text_version) > 1
) t;
```

【推导】P1/P2 为 0 **今天必然成立**（因为 `parse_job.py:196` 硬删旧行 + `is_latest` 恒 `true`，见 §A.3.3）。它们不是"检查有没有问题"，而是**把"4B-2 需要不需要"这件事从判断题变成查询题** —— 这才是"条件触发"的正确形态。

**触发器（我在 4B-1 里就把它落地，成本 ≈ 0）**：把 P1 作为 `needs_reindex` 响应里的一个字段 `generation_conflicts`（默认 0）。于是：

```
若 generation_conflicts > 0 → 4B-2 必须立即开工（且这是缺陷，不是功能）
若恒为 0                  → 4B-2 永不做，从框架稿 §7 删除该交付物
```

### C.2 若触发，最小改动面（预置设计，免得到时候重新想）

| 改动 | 位置 | 内容 |
|---|---|---|
| 1 | `retrieve/__init__.py:131-145`、`:158-168` | 两条关键词 SQL 各加一行 `AND c.embedding_model = :embedding_model AND c.embedding_text_version = :embedding_text_version` |
| 2 | 同上 `:405-424`、`:447-455` | 两处 `session.execute` 的参数字典各加两个键 |
| 3 | `_TABLE_RECALL_SQL`（`:180-188`）、`_SECTION_RECALL_SQL`（`:199-210`） | 同样要加 —— **主理人的范围裁决漏了这两条**：表格回捞与章节补全也是"按 `document_id`/`table_group_id` 取 chunk 行"的路径，两代并存时它们**同样**会捞到旧代次的行。若只改关键词两路而漏掉这两条，4B-2 会交付一个"看起来做了"的半成品 |
| 4 | `tests/scenario/test_step4b_guards.py::GOLDEN_SQL` | 必须显式更新哈希（T-4B-9 会红，**这是设计意图**） |

【推导】上表第 3 条是我对主理人范围裁决的第二处修正：**"关键词两路"不是全部的"非向量召回路径"**。4 条路径（向量 / ILIKE / tsquery / 表格回捞 / 章节补全 —— 严格说是 4 条非向量路径）里，主理人只点了 2 条。若真做 4B-2，必须 4 条全覆盖，否则"混检重复"会从另两条路回来。

### C.3 我的裁决

> **4B-2 应永久不做**（在 `Q3` 保持"一期单代次"的前提下），且**框架稿 §7 Step 4 的交付物应从三项改为两项 + 一个触发器**：
>
> ① chunk 行指纹过滤（**既有，且有意仅覆盖向量路**，`retrieve:99-104` 的注释按 §A.3.4 改写）；
> ② `needs_reindex` 视图（4B-1 交付）；
> ③ 场景 DNA 发布前快照（4B-1 交付，形态按 DQ4B-2/DQ4B-5 修正）；
> ④ **新增触发器**：`generation_conflicts`（C.1 的 P1），恒 0 则 4B-2 永久不做。

**理由（不是偏好，是可执行判据）**：
1. 收益 0：A.3.2 证明三条硬 DNA 列不改变正文，过滤只损失召回；A.3.3 证明"混检重复"的前提在当前写路径下不可达。
2. 代价确定：它会关闭 `对照表:14` 明确规定的降级路径 → 换模型窗口内**完全不可检**，比"仅关键词"更糟。
3. 可判定：P1/P2 让"要不要做"变成一条 SQL，而不是每次开会重新吵。

---

## D. 测试规格（有牙）

### D.1 环境与反假绿纪律

环境同一 4A（§D.1.1 原文照做）：`SCENARIO_TEST_DB_URL` + `pgvector/pgvector:pg16` 容器；迁移注入必须走 `config.attributes["sqlalchemy_url"]` 通道（`test_0028_pg_constraints.py:53-61` 记录的未遂事故）。

【推导】**本项目已抓到四种假绿**，4B 的每条用例必须能对号入座地避开它们：

| # | 假绿形态 | 4B 的具体规避 |
|---|---|---|
| ① | 断言过宽（`raises(CheckViolationError)` 接住另一个约束） | 4B **不新增任何约束**，也不写任何裸 `pytest.raises`（受 `test_0028_migration.py:225-264` 硬约束：跨源文件只允许**一处** `pytest.raises`）。所有负向用例**走 HTTP 层**断言 `status_code + detail.code`，不需要 `raises` |
| ② | 常量相等导致不可观测（两个阈值出厂都是 0.50） | 4B 的期望值**一律来自独立 SQL**（同一容器的另一个连接跑对账查询），**不来自** `ReindexReport` 自身；并且造数据时**让四桶都非 0**（若四桶里有两个恒为 0，"互斥"这件事测不出来） |
| ③ | 接缝被 monkeypatch 掉（测的是"我传了"不是"链路收到了"） | 发布期快照的用例**不断言 `judge_publish_snapshot()` 的返回值**，而断言"响应体里的 `dna_snapshot.kb_readability.chunks_hard_stale` == 独立 SQL 数出来的数" |
| ④ | 并发测试其实没并发 | 4B **没有并发用例**（读多写少，且唯一的写点是一行审计）。**这一项诚实标注为 N/A**，不凑数 |

### D.2 真库用例（`tests/scenario/test_step4b_reindex_pg.py`）

> 每条给：断言 / **把哪一行代码改坏它必须变红**。第二列是"有牙"的唯一判据 —— 没有第二列的用例是本步不允许的。

| # | 用例 | 断言 | **破坏方式（必须变红）** |
|---|---|---|---|
| **T-4B-1** | 视图/端点与 `--check` 同源 | 造 3 类 chunk（干净 2 条 / 硬过期 3 条 / 只 chunker 过期 4 条）→ 断言 `await _load_stale()` 的**长度 == 3**，且 `report.hard_stale == 3`，且 `report.universe == 9` | 把 `0029` 函数的 `stale_hard` 里删掉 `OR c.embedding_text_version <> p_embedding_text_version`（只比 model）→ 若造的第 3 条是"仅 text_version 不同"，长度变 2 → 红 |
| **T-4B-1b** | `--check` 退出码与报告一致 | 直接 `await reindex_embeddings.main(batch_size=8, dry_run=False, check=True) == 1`；清空过期后再跑 `== 0` | 把 `:81` 的 `return 1 if total else 0` 改成 `return 0` → 红（这条拦"CI 门禁被静默关掉"） |
| **T-4B-2** | 软 DNA 不污染硬 DNA（**I-4B-3 的守卫**） | 只造 4 条 `chunker_version` 不同的 chunk → `hard_stale == 0`、`chunker_only == 4`，且 `--check` 退出码 **0** | 把 `stale_hard` 的谓词加上 `OR c.chunker_version <> p_chunker_version`（即把软 DNA 升硬）→ `hard_stale` 变 4、退出码变 1 → 红。**这条用例就是 DQ4B-3 裁决的可执行形态**：将来谁想升硬，它先红 |
| **T-4B-3** | 对账恒等式（四桶互斥） | 造一批混合数据，断言 `ok + hard_stale + chunker_only + embedding_missing == universe`；**并且**断言四个桶里**至少三个非 0**（②号假绿的规避） | 把 `chunker_only` 的 SQL 条件从 `stale_chunker AND NOT stale_hard` 改成 `stale_chunker` → 造一条"既硬过期又 chunker 过期"的行 → 恒等式右侧多 1 → 红 |
| **T-4B-4** | 发布期三档判定（**I-4B-6 的守卫**） | ①kb 下 1 篇 `status='indexed'` 文档 + 0 条 chunk → W5 **409 `SCENARIO_KB_EMPTY_BUT_INDEXED`**，且**旧 published 未被归档**（独立会话回读 `state`）；② 造 5 条 chunk、全硬过期 → W5 **200**，响应 `dna_snapshot.kb_readability.chunks_hard_stale == 5`（**该 5 来自独立 SQL**，③号假绿的规避），`warnings` 含 `PARTIAL_UNREADABLE`；③ 全对齐 → W5 200 且 `warnings == []` | ① 把 `if verdict.blocker` 改成 `if False` → 第一格红；② 把 `scenario_kb_readability` 的 `kb_ids` 参数写成 `[]` → 第一格变"0 chunk → 但 documents_indexed 也查成 0 → 走放行" → 红；③ 把 `stale_hard` 的 `WHERE` 去掉 → 第二格的 5 变 9（跨租户/跨 kb 计数）→ 红 |
| **T-4B-5** | 快照是只读旁路（**I-4B-5 的守卫**） | 发布前后：`scenario_versions` 该行的 `checksum` / `policy` / `kb_ids` / `dna_declared` **逐字节不变**（`SELECT md5(row_to_json(v)::text)` 前后相等）；且 `resolver` 缓存在提交后被失效（把 `CACHE_TTL_SECONDS` monkeypatch 成 3600 后立刻读，读到新版本号） | 在快照里顺手写一列（例如把 `dna_declared` 重新 `build_dna_declared()` 后写回）→ 行的 md5 变 → 红。这是**最容易滑进去的一步**（"既然都算出来了，顺手更新一下声明吧"） |
| **T-4B-6** | `reindex` 不再篡改 `chunker_version`（DQ4B-6 的守卫） | 造 1 条"仅 chunker 过期"的 chunk（`chunker_version='c300-600-40'`）→ monkeypatch `get_embedding_service` 返回确定向量 → 跑 `main(check=False, dry_run=False)` → 断言该行 `chunker_version` **仍是 `'c300-600-40'`**，而 `embedding_model` / `embedding_text_version` 已变成当前值 | 把 `:117` 的 `chunker_version=profile.chunker_version` 加回去 → 红 |
| **T-4B-7** | 端点租户边界 | A 租户的会话调 `GET /api/admin/needs-reindex`：`universe` 只含 A 的行；`?scope=deployment` 时只返回五个总数且响应体**不含** `newest_` / 任何 uuid（正则断言无 `[0-9a-f]{8}-` 形态） | 把 `scope=tenant` 分支的 `WHERE f.tenant_id = :tenant_id` 删掉 → 第一格红；把 `scope=deployment` 分支也返回 `newest_ok_at` → 第二格红 |

### D.3 静态守卫（`tests/scenario/test_step4b_guards.py`，不连库，任何机器必须跑）

| # | 用例 | 断言 | **破坏方式（必须变红）** |
|---|---|---|---|
| **T-4B-8** | `0029` 不含口径字面量（**I-4B-1**） | AST 扫 `0029_*.py` 与 `needs_reindex.py` 的所有字符串常量：**不得**出现 `"bge-m3"` / `"etext-v"` / `"c400-800-60"` / `"wprompt-v"` / `"gprompt-v"` 这些口径值 | 在 `0029` 里把函数写成 `WHERE c.embedding_model = 'bge-m3'`（"反正默认值就是它"）→ 红。这是**第二真源**最自然的入侵方式 |
| **T-4B-9** | **三段检索 SQL 的 golden 哈希**（**I-4B-4**，4B-1 的核心承诺） | 取 `retrieve/__init__.py` 的 `_VECTOR_SQL_TEMPLATE` / `_KEYWORD_SQL.text` / `_KEYWORD_FTS_SQL.text` 三个常量，各算 sha256，与文件里钉死的字面量逐字相等 | 任何人给关键词路加 `AND c.embedding_model = :embedding_model`（哪怕只加一行注释也行）→ 哈希变 → 红。**这是"我没改读路径"的唯一可执行证据**（"没改"无法用行为断言证明）。4B-2 若实施，第一条动作就是显式更新这三个哈希并在 commit message 里引用 DQ4B-1 |
| **T-4B-10** | 谓词只有一份（**I-4B-2**） | 断言 `scripts/reindex_embeddings.py` 的源码里**不出现** `Chunk.embedding_model !=` / `Chunk.embedding_text_version !=` 这类 ORM 比较（即谓词不在脚本里重写）；且出现 `fn_chunk_reindex_candidates` 字面量 | 把 `_load_stale()` 改回 `or_(Chunk.embedding_model != ..., ...)` → 红 |
| **T-4B-11** | `_HTTP` 映射完整 | 遍历 `WriteCode` 成员，断言每个都在 `_HTTP` 里（与 4A 的守卫同款，但**只针对新增码**，不改既有那条） | 加 `SCENARIO_KB_EMPTY_BUT_INDEXED` 到枚举但忘了 `_HTTP` → 红 |

【推导】为什么 T-4B-9 是 golden 而不是行为断言：行为断言只能证明"某组特定输入得到的召回集没变"，而召回范围取决于 SQL 的**每一个字**（索引选择、LIMIT 下推、谓词顺序都会影响结果与执行计划）。golden 是唯一能覆盖"全部输入"的断言。代价是它会在**正当改动**时变红 —— 而这个"红"正是要的：它强制改动者显式声明"我动了读路径"。

### D.4 反自证判据（写进 `conftest` 或文件头）

1. **期望值来源必须是独立 SQL**：任何断言里的数字，若它的来源是被测函数（`collect()` / `judge_publish_snapshot()`），改用同一容器里另一条连接直接查 `fn_chunk_reindex_candidates` 得到。判据：把被测函数改成 `return ReindexReport(universe=0, ...)`，用例**必须**红。
2. **不许用被测代码的常量做期望**：期望值一律写字面量（`"c400-800-60"` 只在**造数据**时出现，在**断言**里出现时必须能解释为什么它在这里是事实而不是副本）。
3. **禁止 `assert resp.status_code == 200` 了事**：每条至少再断一个库里的状态或响应体的具体字段。
4. **`--collect-only` 的条目数与 "0 skipped" 必须给证据**：`SCENARIO_TEST_DB_URL` 未设时跑的 `pytest -q` **不算验收**（4A §D.7 同款）。

### D.5 必须逐条保持不变的既有测试

| 文件 | 条数 | 4B 允许的改动 |
|---|---|---|
| `tests/scenario/test_0028_pg_constraints.py` | 18 | **无** |
| `tests/scenario/test_0028_migration.py` | 11 | **无**（`_render("up","0027:0028")` 是显式区间，加 0029 不影响） |
| `tests/scenario/test_0028_seed.py` | 9 | **无** |
| `tests/scenario/test_effective.py` + `test_resolver_*.py`（3 个：policy / snapshot / truth_table） | 20 + 48 | **无** |
| `tests/scenario/test_policy_wiring.py` / `test_self_check.py` / `test_scenario_wiring.py` | 18 / 49 / 16 | **无** |
| `tests/scenario/test_step4_*.py`（6 个） | 63 | **仅** `test_step4_guards.py` 的 `_EXPECTED_ROUTES` 追加 1 条（**且仅当采纳 preflight**） |
| `tests/test_metrics_and_rerank.py` | 全部 | **无**（这是 §0.2"不加 Gauge"的直接收益：`:80` 的键集断言不用动） |
| `tests/test_integrations.py` | 12 | **无** |

---

## E. 回滚点（逐项，顺序不可调换）

| # | 动作 | 撤回方式 | 可撤回性 | **顺序约束** |
|---|---|---|---|---|
| **E-1** | 迁移 0029 的函数 + 视图 | `alembic downgrade 0028` → `DROP FUNCTION` / `DROP VIEW` | **无损**（无数据、无写入者） | **必须先 revert `reindex_embeddings.py` 再 downgrade**。反过来脚本会 `UndefinedFunction` → 重算脚本不可用（而它可能是唯一的重算手段） |
| **E-2** | 脚本同源改造（§B.7） | `git revert` 该文件 | **无损**（回到 ORM 版 `or_` 条件，语义等价） | 与 E-1 同批 revert；单独 revert 脚本而留着函数是安全的（函数成了孤儿，无害） |
| **E-3** | `:117` 删除（§B.8） | `git revert` 那一行 | **无损**，但会让 `chunker_only` 重新变成永远 0 | 独立 revert 即可，与 E-1/E-2 无耦合 |
| **E-4** | W5 的口径快照 + 唯一阻断码 | **代码 revert `publish()` 的那 12 行 + `write_errors.py` 的那个码** | 无损；revert 后 W5 行为与 4A 逐字节等价 | **不推荐用配置开关做回滚**（理由见下） |
| **E-5** | `include_router` + preflight 路由 | revert 两行 | 无损 | 若只 revert 路由而留 service，`test_step4_guards.py` 的 `_EXPECTED_ROUTES` 会红 —— 那是**好事**（防止"路由删了、断言还留着"） |
| **E-6** | 启动横幅一行 | revert 一行 | 无损 | 独立 |
| **E-7** | `rag.caliber.vector_empty`（DQ4B-8 采纳时） | revert 一行 `metrics.incr` | 无损（指标消失，无消费方依赖） | 独立；它是唯一"回滚后无任何行为差异"的一项 |

【推导】**为什么 E-4 不提供配置开关（而主理人问的正是"如何降级回只校 policy"）**：本项目已有 `SCENARIO_POLICY_ENABLED` / `GRAPH_RECALL_SELECTIVE` 这类开关，它们的共同前提是"关闭态与未实现逐字节等价，且开启是**增量能力**"。而 4B 的快照**不是增量能力**：它在正常路径上只产出信息（不改变任何放行结果），唯一的阻断档是"确定性数据损坏"。所以：

- 加开关的收益 ≈ 0（正常部署永远不会想关它）；
- 加开关的代价是确定的：它造出一个**运行期可变的第二状态**（"这台机器的发布校验开没开"），而这个状态**不在任何横幅、不在任何 config_fingerprint 里** → 出问题时无人能回答它当时是开还是关。
- 因此回滚 = **revert 代码**（显式、走评审、留 commit 痕迹），**不是**翻一个配置。

若你仍要开关，必须同时做两件事（否则比不加更糟）：① 把它加进 `runtime_config_snapshot()` / `config_fingerprint()`（`app/config/config_profile.py`），让"这台机器开没开"可被比对；② 在启动横幅打一行。

---

## F. 分步开发路线（每步给硬验收信号）

> 【推导】每一步的"验收信号"必须是**可跑的命令**或**可断言的测试**，不接受"看起来对"。且每一步都独立可回滚（§E 对应项）。

| 步 | 交付 | **硬验收信号（可执行）** | 回滚 |
|---|---|---|---|
| **1** | 迁移 0029（函数 + 视图 + 两条守卫） | ① `alembic heads` 输出**恰好一行** `0029 (head)`；② `alembic upgrade head` → `alembic downgrade 0028` → `alembic upgrade head` 往返无错；③ `psql -c "SELECT * FROM v_chunk_caliber_distribution"` 在空库返回 0 行、在种子库返回 **1 行**；④ `pytest tests/scenario/test_0029_migration.py -q` 全绿 | E-1 |
| **2** | `needs_reindex.py`（`collect` / `ReindexReport` / 恒等式断言） | ① `pytest tests/scenario/test_step4b_reindex_pg.py -q -k "T_4B_1 or T_4B_2 or T_4B_3"` 全绿；② 造混合数据后跑 §B.2.3 的 SQL 与 `collect()`，**四个数字逐字相等**（脚本比对，不是眼看） | E-1（无独立回滚点，随 1 起） |
| **3** | 脚本同源改造 + `:117` 删除 | ① `pytest ... -k "T_4B_6"` 全绿；② `python -m scripts.reindex_embeddings --check; echo $?` 在"只有 chunker 过期"的库上 **输出 0**。在"有硬过期"的库上**输出 1**；③ `python -m scripts.reindex_embeddings --dry-run` 的输出条数 == `collect().hard_stale` | E-2 / E-3 |
| **4** | R-4B-1 端点 + 启动横幅 | ① `pytest ... -k "T_4B_7"` 全绿；② `curl -H "Authorization: …" /api/admin/needs-reindex | jq .reconciles` == `true`；③ 重启 api 后 `docker logs` 第一屏出现 ` chunk 口径 : ` 那一行 | E-5 / E-6 |
| **5** | `rag.caliber.vector_empty`（DQ4B-8 采纳时，独立可省） | ① `pytest ... -k "vector_empty"` 全绿；② `curl /metrics | grep vector_empty_total` 有输出；③ **T-4B-9 的 golden 哈希仍全绿**（证明这一行 `incr` 没碰 SQL） | E-7 |
| **6** | W5 口径快照 + 唯一阻断码（**依赖步 2 的 `scenario_kb_readability`**） | ① `pytest ... -k "T_4B_4 or T_4B_5"` 全绿；② **全量回归**：`pytest -q` 的 collected 数 ≥ 4A 基线且 **0 failed / 0 skipped**；③ `pytest tests/scenario/test_step4_guards.py -q` 全绿（若加 preflight，则需 `_EXPECTED_ROUTES` 已更新） | E-4 |
| **7** | 文档与注释收口 | ① 按 §A.3.4 改写 `retrieve/__init__.py:104` 的注释后，**T-4B-9 仍须显式更新哈希**（注释在 SQL 常量**外**，故哈希不变 —— 若变了说明改到了 SQL 本体，必须停下来核）；② 框架稿 §7 的 Step 4 按 §C.3 改为"2 项 + 1 触发器" | —— |

【推导】**步序为什么是这个**：1 → 2 是"先有判据再有消费"；3 必须在 2 之后（同源改造依赖函数语义已在测试里钉住）；4/5 是纯观测（可省、可后补）；**6 必须最后**（它是唯一改变写路径行为的步骤，且它依赖 2 的 `scenario_kb_readability`）。把 6 放前面的风险：一旦 6 出问题，你会同时怀疑判据与判定 —— 两个变量一起动，正是框架稿 `:818` 反复强调要避免的形态。

---

## G. 魔鬼代言人：反例（每条指明成立条件）

| # | 反例 | 判定 |
|---|---|---|
| **R-1** | **`needs_reindex` 必然成为 R10 的第二个永久债务**：单 DNA 稳态下 `hard_stale` 恒为 0、`chunker_only` 在修完 §B.8 后单调增长且**永不归零**（处置是全链重跑，没人会为一个"建议"跑全链） | **成立且是最大风险**。缓解（两条，缺一不可）：① `chunker_only` **不进任何百分比、不进 `verdict()`、不告警**（I-4B-3），只出现在横幅与端点的一行提示里 —— 一个不进闸门的数字不会被养成"学会无视"的习惯（R10 的病因是"闸门永远红"，不是"数字存在"）；② `hard_stale` 的判据用 `newest_ok_at` 的**推进**而不是**绝对值**（§B.5.4）→ 正常重算期间它是 `info`，只有卡住才 `warn`。**若你要求 `chunker_only > 0` 就告警，请先回答：谁会跑那条全链重跑？** 若答案是"没人"，那么这个告警的唯一效果是训练运维忽略它 |
| **R-2** | **`--check` 与视图同源改造把 CI 门禁的语义绑到了函数上**：某次迁移把函数改坏 → 门禁静默变宽松（返回 0 表示"没有过期"） | **成立**。缓解：T-4B-1b 断言"有过期时 `main(check=True)` 返回 1"，且 T-4B-1 断言"函数判出的数 == 已知构造的过期数"。两者合起来，函数改坏必然让其中一条红。**不接受"门禁逻辑写两遍互相校验"**（那是 I-4B-2 的直接违反） |
| **R-3** | **W5 的快照给了"我们在关注 DNA"的**错觉**，但真正会让检索塌掉的动作（改 `.env` 的 `EMBEDDING_MODEL`）与它无关**：改 `.env` 不需要发布任何场景，所以 W5 的快照永远不会在"最该出现的时刻"出现 | **成立，且必须承认**。这是 4B 的结构性盲区：**DNA 变更是一个部署动作，而 4B 的所有交付物都挂在场景写接口上**。缓解：本步把最强的观测放在**启动横幅**（每次改 `.env` 都要重启，横幅必打）与 `--check`（可进 CI/cron），W5 快照只是"顺带在场景侧也留一份"。**U-4B-6 记录了这个盲区**：真正的正解是给 DNA 变更加一条**部署审计**（谁在什么时候把 `.env` 改了），那是 Step 5+ 的事，4B 做不到 |
| **R-4** | **`SCENARIO_KB_EMPTY_BUT_INDEXED` 的判据会误伤"刚建 kb、刚传文档、解析还在跑"的合法场景**：文档 `status='pending'` 时不满足 `status='indexed'` → `documents_indexed == 0` → 走放行档，**看起来没事**；但反过来，若一篇文档解析完成（`indexed`）而 chunk 写入被后续事务回滚（同一事务，不可能）—— 所以这个档只在数据被外部破坏时触发 | **不成立**（我核过：`parse_job.py:228-232` 把 `document.status='indexed'` 与 `session.add(Chunk(...))` 放在**同一个 `commit()`** 里 → `indexed` 与 chunk 行**原子**成立或原子不成立）。因此"有 indexed 文档、0 chunk"确实是数据损坏。**但承认残余**：若将来有人把 chunk 写入拆成两个事务（比如为了流式进度），这条判据就会变成误伤 —— 届时 §B.4.2 的档① 必须改成 WARN。这个前提必须写进代码注释 |
| **R-5** | **T-4B-9 的 golden 哈希会阻断正当的读路径优化**：未来任何人想调 `_KEYWORD_SQL` 的 `ORDER BY`（注释里已记录它是为了"评测可复现"加的）都必须先改测试 | **成立，且是设计意图**。缓解：哈希旁边写清"改它的正确流程 = 改 SQL → 跑哈希 → 在 commit message 里引用 DQ4B-1 与本文件名 → 说明召回范围变化"。**判据**：读路径的改动**必须**有对应的 A/B 证据（`框架设计-v1.md:798` 的 217 条基线是唯一那道门），golden 哈希只是"提醒你有这道门" |
| **R-6** | **§B.8 的修改会让 `chunker_only` 变成单调增长的数字，而"重切"的代价是全链重跑 → 事实上永不执行 → 又一个不动的事实** | **成立**。已在 R-1 里接受了这个代价，但必须补一句：**"永不执行"是正当的**——分块参数调优的真实验收信号是"下一次全量重建时用新参数"，不是"为了对齐数字跑一次昂贵重建"。所以 `chunker_only` 的正确读法是**"下次重建的待办清单规模"**，不是"当前缺陷数"。这句话必须出现在端点响应的 `note` 与横幅文案里，否则数字会被误读 |
| **R-7** | **4B 一分钱没省、一次 A/B 没跑，却新增了 3 张表之外的 1 个函数 + 1 个视图 + 1 个端点 + 1 个错误码 → 复杂度增加而无可度量收益** | **部分成立**。可度量的收益只有一条：**"换模型/改文本公式"这个部署动作从"无观测"变成"有观测 + 有 CI 门禁"**（今天它是 `--check` 一个裸脚本，没人跑；改后它在启动横幅第一屏 + 一个 admin 端点 + 一条可判定的告警式里）。第二条：**§B.8 修的是一个会撒谎的列**。若你认为这两条不值这个复杂度，**最小可交付版本**是：只做 §B.1 的函数 + §B.7 的脚本同源 + §B.8 的修复（即"让 `--check` 有一个可解释的分量分解"），把端点与 W5 快照整体推后 —— 这个裁剪我认可，且 §E 的回滚点按步边界切好了 |

---

## H. 生产级必答项（只答与本步相关的；其余标 N/A + 理由）

| # | 必答项 | 结论 |
|---|---|---|
| **1** | **执行环境 / 权限**：新增的 SQL 对象在哪跑、谁能读？凭证是否进执行环境？ | 函数与视图在 `kagent` 库内，`SECURITY INVOKER`（不提升权限，跟随调用方）。`GET /api/admin/needs-reindex` 走 `Depends(require_admin)`（`deps.py:142-152`，`clearance < 40` → 403）。**没有新增任何凭证**：口径参数来自 `current_index_profile()`（读 `settings`/`decisions`），不入库、不回显 `.env` 的其它项。**写操作影响半径**：4B 唯一的写是 `audit_logs` 一行（同事务）；`scenario_versions` 的内容**逐字节不变**（T-4B-5 断言）。函数是 `STABLE` + 只读 → **不可能被用来写**。 |
| **2** | **错误回喂**：失败如何回到调用方，如何分类？ | 三分类：① `SCENARIO_KB_EMPTY_BUT_INDEXED`（409）→ **不可重试**，去查数据管道；② `SCENARIO_CHECKSUM_MISMATCH` / `PRECONDITION_REQUIRED`（409/428）→ 重新 GET 后重试；③ 端点侧 `needs-reindex` 的统计失败 → **不抛**（沿用 `capabilities.py:419-422` 的口径："就绪度统计失败不该阻断启动；横幅照打，数据字段留空"），响应里 `reconciles: false` + `error` 字段。**禁止**把统计失败变成 500：那会让"查不了过期数"变成"管理端页面挂了"。 |
| **3** | **幂等** | 4B 的全部读端点天然幂等。W5 的快照是**纯计算 + 一行审计**：重放 W5（已 published）在 4A 里就是 `changed:false` 且**不写审计**（`write_service.py:565-568` 直接 return）→ 所以快照也不会重算、不会重复落审计。**这一点必须保持不变**（若为了"每次都记快照"去改那段 return，会破坏 4A 的幂等语义）。 |
| **4** | **证据版本绑定** | 这是本步的主线。四层绑定里 4B 负责第 ① ② 层的**可观测化**：① 向量（`chunks` 三列，检索按前两列过滤）；② 分块（`chunker_version`，软 DNA）。**评估结论的版本绑定**：4B **不触碰** `eval_runs` / `caliber` → 跨场景 A/B 的可比性问题（`框架设计-v1.md:1238`）**不在本步**。 |
| **5** | **成本 / 缓存 / 体感（经济性三视角）** | **成本**：4B **不新增任何 LLM / embedding / rerank 调用**（唯一涉及 embedding 的是脚本，而那是既有路径）→ 模型组合与任务-模型分派 **N/A（本步无模型调用）**。可量化的成本只有一条：`collect()` 是对 `chunks` 的一次扫（`is_latest` 行数级），启动时一次、端点按需 → 在 10 万 chunk 量级是百毫秒级；**不进任何请求路径**。**缓存**：4B 不引入新缓存，也**不失效**任何既有缓存（发布快照不改内容 → `resolver.invalidate` 的行为与 4A 完全一致）。**体感**：零影响 —— 4B-1 不改检索 SQL（I-4B-4 / T-4B-9），请求墙钟不变。 |
| **6** | **可观测性的"谁看"** | 三处按**读者**分工：① 启动横幅 → **部署者**（改 `.env` 后必然看第一屏）；② admin 端点 → **运维排障时**（有明确问题时才查）；③ `--check` 退出码 → **CI / cron**（唯一能"自动发现"的通道）。**没有一处依赖"有人主动盯着"** —— 这是 R10 的正面回答。 |

---

## I. 遗留问题与未知区

| # | 项 | 为什么现在定不了 | 影响 | 建议 |
|---|---|---|---|---|
| **U-4B-1** | 软删文档的 chunk 是否进分母 | 见 §A.4 | 分母口径与 `--check` 语义强耦合 | 先并列暴露 `in_dead_documents`，拿真实数据再定 |
| **U-4B-2** | 软删文档的 chunk **仍可被检索**（F-17） | 修它要改召回 SQL（`_WHERE_BASE`），违反 4B-1 的 I-4B-4 | 已软删内容可能进答案与引用 | 独立缺陷单；修时 T-4B-9 会正当变红，必须显式更新 |
| **U-4B-3** | 告警阈值的真实标定（`stale_progress_hours = 24`） | 无运维数据 | 阈值错 → 刷告警或漏告警 | 站位值 + `placeholders` 登记（§B.5.4） |
| **U-4B-4** | `embedding_dim` 无行级守卫 | 维度写死在 `Vector(1024)` DDL | 将来多维度时 `fingerprint` 的 dim 无列可比 | 本步不加，docstring 记一句 |
| **U-4B-5** | 多副本下横幅数字只代表启动那一刻 | 无共享状态（同 4A 的 U4-2） | 横幅会过期 | 明写"启动时快照"，实时数走端点 |
| **U-4B-6** | **DNA 变更本身没有部署审计**（R-3 的盲区） | 改 `.env` 不经过任何代码路径 | "谁在什么时候换了模型"不可查；这是 4B 结构性做不到的事 | Step 5+ 给 `config_fingerprint` 的变更加一条部署审计（谁/何时/从什么到什么） |
| **U-4B-7** | 口径三列无索引，过期扫描是全表扫 | 离线动作，未实测 | 十万级 chunk 上 `collect()` 可能数百毫秒 | 实测后加 `(is_latest, embedding_model)` 组合索引 |
| **U-4B-8** | `generation_conflicts`（C.1 的 P1）在 4B-1 是否落地 | 落地成本 ≈ 1 条 SQL 子查询；但不落地则"4B-2 要不要做"仍是会议题 | 4B-2 的触发条件不可判定 | **推荐落地**（成本极低、消除一个无限期悬空项） |
| **U-4B-9** | R-4 的残余：若 chunk 写入被拆成两个事务，档① 判据变误伤 | 依赖未来的实现选择 | 会误拒合法发布 | 已写进 §B.4.2 的代码注释，且 T-4B-4 的破坏方式 ② 会暴露 |

---

## 附录 A：本文件与框架稿 §7 的偏移清单

> 格式对齐 `第四阶段-Step4-详细设计.md` 的附录 A。

| # | 位置 | 前文怎么说 | 本文件怎么做 | 性质与理由 |
|---|---|---|---|---|
| **A-1** | 框架稿 §7 Step 4（`:799`） | Step 4 = DNA 进场景：chunk 行指纹过滤（既有）+ `needs_reindex` 视图 + 场景 DNA 发布前校验，三项并列 | **4A 已把 Step 4 的"载体"交付完**（写接口 + T4）；本文件交付另三项，但**形态被修正**：① 指纹过滤标注为"既有，且有意仅覆盖向量路"；② `needs_reindex` 从"视图"扩为"函数 + 视图 + 端点 + 脚本同源"；③ "发布前校验"从"拒绝"改为"非阻断快照 + 一个确定性阻断档" | **口径细化 + 一处实质修正（③）**。理由见 DQ4B-5 |
| **A-2** | 框架稿 §7 Step 4 验收信号②（`:799`） | "伪造不匹配指纹 → 拒绝检索（不是低分返回）" | 拆成 ②-1（向量路空集）+ ②-2（关键词路照常，但必须被 `hard_stale` 计入） | **实质修正**。理由见 §A.3.4：原文的"拒绝检索"会把 `对照表:14` 明确规定的降级路径一并关掉 |
| **A-3** | 框架稿 §7 Step 4 验收信号①（`:799`） | "单 DNA 部署：旧 chunk 100% 仍可检（指纹匹配数 = 总数）" | 落地为 `ok / universe` + 明确 **`chunker_only` 不参与该比值** | 细化。原文的"指纹"若含 `chunker_version`（`IndexProfile.fingerprint` 不含，F-10），这条信号会在调分块参数后恒不成立 |
| **A-4** | 框架稿 §7 Step 4 回滚点（`:799`） | "DNA 过滤仅在『存在多 DNA』时启用；单 DNA 时等价现状" | 改为**可判定谓词 P1/P2**（§C.1）+ 永久不做的裁决（DQ4B-1） | 细化。"存在多 DNA"不可判定（§C.1） |
| **A-5** | 框架稿 §7.2 禁止事项（`:833`） | 「禁止让旧 chunk 在 DNA 不匹配时返回低分片（必须空集 + 拒答）」 | **不变**，但明确其适用范围 = **向量路**；关键词路的命中是"正文命中"，不是"低分片" | 细化。这条禁令针对的是"把不同构向量混进来算分数"，关键词路不算向量 |
| **A-6** | `retrieve/__init__.py:104` 的注释 | 「关键词路（ILIKE / tsquery）与向量空间无关，加会误伤」 | 注释按 §A.3.4 改写为"三条硬 DNA 列只改变向量、不改变正文"的版本 | **结论不变、理由修正**。原措辞是教条，改后是可检验的机制陈述 |
| **A-7** | `DNA与Policy口径对照表-v1.md:32` | 「仅建议重切 …… 只在 `needs_reindex` 视图里暴露」 | 本文件给出"暴露"的**具体形态**：独立分量 `chunker_only` + 不进百分比 + 不告警 | 落地细化。原表只说了"要暴露"，没说"暴露成什么形状"，而形状决定它会不会变成 R10 |
| **A-8** | `reindex_embeddings.py:117` | 重算时把 `chunker_version` 一并写成当前值 | **删除该赋值** | **既有缺陷修正**（§B.8）。不修则 4B 的 `chunker_only` 在第一次重算后永久为 0 |
| **A-9** | 框架稿 §7.1 的"Step 4 必须先于 Step 6"（`:818`） | 理由是"口径变了 vs 库变了无法区分" | **不变**，并补一条 4B 做不到的事：DNA 变更本身没有部署审计（U-4B-6）→ 所以"口径变了"这件事即使在 4B 之后仍不能从审计回答，只能从横幅/端点回答 | 补充说明 |
| **A-10** | 主理人的范围裁决："关键词两路" | 4B-2 的改动面 = 关键词两路 | 若实施，**必须覆盖 4 条非向量路径**（ILIKE / tsquery / 表格回捞 / 章节补全） | **范围修正**（§C.2 第 3 条）。漏掉后两条会交付半成品 |
| **A-11** | 主理人的范围裁决："发布校验扩 W4" | W4 加 DNA 校验 | **放 W5**；W4 一行不改 | **反对**，见 DQ4B-2（三条理由，全部有源） |
| **A-12** | 主理人的范围裁决："不一致 → 拒绝发布" | 硬 DNA 不一致即拒 | **只 WARN**；唯一阻断档 = "有 `status='indexed'` 文档但 0 条 chunk" | **反对**，见 DQ4B-5（拒绝会销毁它想保护的信息） |
| **A-13** | 主理人要求"`/metrics` 或启动自检暴露计数" | 二选一 | **两处都做**，且**不加 Gauge** | 见 §B.5.1：加 Gauge 会强制改动 `tests/test_metrics_and_rerank.py:80` |
| **A-14** | 4A 的 `test_step4_guards.py:31-40` | 路由表精确钉死 | 若采纳 preflight 则**追加一行** | 加严（集合仍精确相等），且这正是守卫的设计意图 |

---

## 附录 B：源核实 / 设计推导 分栏

| 类别 | 条目 | 出处 |
|---|---|---|
| **【源】读到的** | chunks 三列口径 `nullable=False`；`embedding` 是 `Vector(1024)` | `app/models/entities.py:259`、`:263-265` |
| 【源】 | 向量路只过滤两列；参数绑定在 `:312-313` | `retrieve/__init__.py:114-115`、`:312-313` |
| 【源】 | 关键词两路只带 `_WHERE_BASE`；注释「加会误伤」 | `:92-97`、`:131-145`、`:158-168`、`:99-104` |
| 【源】 | RRF 接受任意条路径；只在校验 kb 为空时早退 | `:216-228`、`:258-259` |
| 【源】 | 表格回捞 / 章节补全也是非向量召回路径 | `:180-188`、`:199-210` |
| 【源】 | `chunker_version` 不影响向量同构；`fingerprint` 不含它 | `index_profile.py:17-21`、`:63-73`、`:45-51` |
| 【源】 | 过期定义只含 model + text_version；`--check` 退出码；`:117` 写 chunker_version | `scripts/reindex_embeddings.py:47-50`、`:79-81`、`:113-118` |
| 【源】 | `parse_job` 先硬删旧 chunk 再重建、`is_latest=True`、与 `status='indexed'` 同事务 | `app/workers/parse_job.py:196`、`:218`、`:228-232`、`:525`、`:535` |
| 【源】 | 软删只写 `documents.deleted_at`；召回 SQL 不 join documents | `app/api/documents.py:433`、`retrieve/__init__.py:92-97`、`:172-174` |
| 【源】 | `capabilities.py` 的分母教训与"覆盖率 = (total-skipped)"事后修补 | `capabilities.py:66-72`、`:335-339`、`:419-422` |
| 【源】 | 图 / wiki 的 fail-closed 与幂等短路 | `graph_profile.py:13-16`、`wiki_profile.py:12-17`、`workers/graph_job.py:157-163`、`wiki/build.py:154-163` |
| 【源】 | T1/T2 分流纪律（代码错 raise / 数据错 WARN） | `app/main.py:123-125`、`:137-140`、`self_check.py:614-626` |
| 【源】 | `dna_declared` 是声明镜像；S-9 只在 `is_builtin` 行上读它 | `dna_declare.py:3-5`、`:29-38`、`self_check.py:569-611` |
| 【源】 | T4 调用点与顺序；W4 不跑 T4；W7 拒绝"无 published" | `write_service.py:560-657`、`:490-493`、`:811-817` |
| 【源】 | `checksum` 只含四个内容字段 | `snapshot.py:291-310` |
| 【源】 | metrics 只有 Counter/Histogram；`snapshot()` 键集被精确断言；`/metrics` 走 `render()` | `infra/metrics.py:40-41`、`:74-90`、`tests/test_metrics_and_rerank.py:80`、`main.py:390-401` |
| 【源】 | 4A 的路由表守卫与 SQL tenant 守卫；约束名守卫只允许一处 `raises` | `test_step4_guards.py:31-60`、`test_0028_migration.py:225-264` |
| 【源】 | 框架稿 Step 4 的三项交付与三条验收信号；R7；R10；待定项 7 | `框架设计-v1.md:799`、`:1199`、`:1207`、`:1294` |
| 【源】 | DNA/Policy 对照表：硬/软 DNA 分界、"主检索退化为仅关键词"、"误判第 2/4 条" | `DNA与Policy口径对照表-v1.md:14`、`:17`、`:30-35`、`:51`、`:53` |
| **【推导】我判的** | 三条硬 DNA 列不改变 `chunk.content` → 关键词路过它们只有损失 | §A.3.2 |
| 【推导】 | "混检重复/错位"的前提在当前写路径下不可达（硬删 + `is_latest` 恒 true） | §A.3.3 |
| 【推导】 | 4B-2 应永久不做；触发条件改为可判定谓词 P1/P2 | §C.1、DQ4B-1 |
| 【推导】 | 分母 = `is_latest` 单条件；四桶互斥 + 对账恒等式 | §B.2 |
| 【推导】 | 发布期校验放 W5 而非 W4（陈旧闸门） | DQ4B-2 |
| 【推导】 | 硬 DNA 不匹配不阻断（拒绝会销毁证据） | DQ4B-5 |
| 【推导】 | 告警用 `newest_ok_at` 的推进而不是 stale 的绝对值 | §B.5.4 |
| 【推导】 | 不加 Gauge（代价是改一条已验收断言，收益可由端点替代） | §B.5.1 |
| 【推导】 | `:117` 是既有缺陷，且 4B 让它变得致命 | §B.8 |
| 【推导】 | golden SQL 哈希是"没改读路径"的唯一可执行证据 | §D.3 T-4B-9 |
| 【推导】 | 回滚用 revert 而非配置开关（开关会造一个不在指纹里的运行期状态） | §E-4 |
| **本机限制** | **我没有跑任何命令**（无 pytest / 无 PG 可连、且约束禁止跑迁移）；本文件所有"已核"均指**读源码核实**，所有数字（1162 / 255 / 63 个 `def test_`）均来自 `grep -c` 的静态计数，未执行测试 | —— |
| **未核实** | ① `chunks` 在真实库中的行数量级（U-4B-7 的性能估计无实测）；② `alembic heads` 的实际输出（我只能确认目录内最大号是 0028 且无分支文件）；③ `0029` 函数被 PG 内联的实际情况（`EXPLAIN` 未跑） | —— |

---

## 附录 C：待你裁决项汇总（推荐值一览）

| # | 决策 | 我的推荐 | 与主理人方向 |
|---|---|---|---|
| **DQ4B-1** | 关键词路口径过滤的最终裁决 | **永久不做**；框架稿 §7 删除该交付物，替换为触发器 | 主理人给的方向是"存在多 DNA 代次灰度时做"，我判定该方向不可判定且收益为 0 |
| **DQ4B-2** | DNA 校验放 W4 还是 W5 | **W5**（W4 一行不改）；补偿手段 = preflight 端点 | **反对**主理人的"扩 W4" |
| **DQ4B-3** | `chunker_version` 是否升硬 | **不升**，但作为独立分量 `chunker_only` 暴露 | 与 4A/R7 一致（主理人未表态，我明确给出翻转条件） |
| **DQ4B-4** | 告警阈值与分母 | 分母 = `is_latest` 单条件；**不产出百分比**；告警判据 = `hard_stale > 0` 且（`newest_ok_at IS NULL` 或 已停推进 > 24h）；`chunker_only` **永不告警** | 主理人要求"与脚本同源 + chunker 独立分量"，**一致** |
| **DQ4B-5** | 口径分布混合时拒绝还是 WARN | **WARN**；唯一阻断档 = 有 `status='indexed'` 文档但 0 条 chunk | **反对**主理人的"不一致即拒绝" |
| **DQ4B-6** | `:117` 的缺陷修不修 | **修**（删除该赋值） | 主理人未提（我发现的） |
| **DQ4B-7** | 是否新增 preflight 端点 | **加**（它是 DQ4B-2 选 W5 的补偿） | 主理人未提；代价是改一行 `_EXPECTED_ROUTES`（加严） |
| **DQ4B-8** | `rag.caliber.vector_empty` 是否纳入 | **纳入**，独立成步、可独立回滚 | 主理人未提（我发现的，且是 R10 的正面回答） |
