# 第 4 步 · wiki 术语扩展进检索 · 可编码增量设计

> 增量设计台账：2026-11-04 · file_rag_agent · v1.0（待管家节点审核）· 基线 = `02-design/架构设计文档.md` **v1.2 冻结版**（commit `894e137`）· 第 3 步已交付（迁移 head `0026`，测试报告 `04-test/测试报告-第3步-wiki.md`）
>
> **范围纪律**：本文只补第 4 步（主文档 §13 行 1570-1577）的**可编码细节**。不改主文档、不改代码、不 commit。主文档 v1.2 与本增量文档冲突时，**以主文档为准**；本文只在 §6 把冲突显式登记，不单方面改判据。
>
> **决策标注约定**（贯穿全文）：`[细化]` = 已确认契约的细化，不需重新裁决；`[新决策]` = 本文新引入、需录入设计台账的决策；`[待裁决]` = 必须上管家节点桌面的选择题，本文只给建议与依据，**不替项目拍板**。
>
> 术语约定：英文术语首次出现带中文解释。**Wiki**（维基式导航页，本项目中特指按文档生成的 LLM 导航摘要页）、**Query Expansion**（查询扩展：往检索词元集合里补词）、**FTS**（Full-Text Search，PostgreSQL 全文检索）、**ILIKE / 正则子串路**（本项目的关键词召回支路）、**RRF**（Reciprocal Rank Fusion，倒数排名融合）、**GIN**（Generalized Inverted Index，PostgreSQL 通用倒排索引）、**JSONB**（PostgreSQL 二进制 JSON 类型）、**ACL**（Access Control List，访问控制）、**SLO**（Service Level Objective，服务等级目标）、**Golden Test**（金标准测试：把既有输出存成快照逐字节比对）、**Counter / Histogram**（Prometheus 的计数型 / 直方图型指标）、**Fixture**（测试夹具：为评测临时构造、跑完清理的数据）、**Fail-open / Fail-closed**（在线失败时「该路退化为 0、主链路照常」/ 启动期依赖缺失时「拒绝启动」）、**Side channel**（旁路：不进主评分体系的独立通道）。
>
> 撞限协议：若本轮轮次耗尽或被截断，已在头标注「轮次耗尽，产出不完整」并上报主理人裁决，禁止静默截断。本文为完整产出。

---

## 0. 决策摘要（一页速览）

| # | 问题 | 结论 | 类别 |
|---|---|---|---|
| 1 | 「命中 wiki 页」用什么输入 | 用 `retrieve()` 当轮的 `query` 参数，词元来源 = `build_ilike_terms(query)`（**唯一真源**，不新建切分器）；链式多轮时每轮子问题各自判定 | `[细化]` |
| 2 | 匹配规则 | **归一化后字面子串包含**（`strpos(lower(kt), needle) > 0`），**不是** `jsonb ?\|` 精确包含 —— 第 3 步落库的 `key_terms` 是 LLM 原文（未 NFKC / 未小写），精确匹配结构性失效 | `[新决策]` |
| 3 | 四闸门 | 导航 SQL 内联 `_WHERE_BASE` 的 `EXISTS`（页 → 文档 → 可见 chunk），复用 `pushdown` 四个绑定值；额外下推 tenant / kb / status / scope / 生成口径 | `[细化]` |
| 4 | 索引 | **不新增 GIN、不新增迁移**。现有 `idx_wiki_pages_kb (tenant_id, kb_id, status)` 够用；写侧归一列 + GIN 升级登记为规模触发项 | `[新决策]` |
| 5 | 选词与限流冲突 | **独立预算**：原 `ilike_terms`（3 个）一个字节不改，wiki 词元走 `extra_*` 关键字参数作附加 OR 分支 / OR 项；**既不追加后截断，也不预留名额** | `[新决策]` |
| 6 | 两路接入 | 一次导航 SQL 产出 `WikiNavPage`，选词纯函数的结果两路复用；`build_ilike_pattern` / `build_fts_query` 各加一个**带默认值的关键字参数**，既有调用点零改动 | `[细化]` |
| 7 | 失败语义 | 在线 **fail-open**：超时 / 异常 / 无页 → 不扩展，主链路照常；超时必须 `rollback()` + 还原 `statement_timeout`；**不新增 `RetrievalResult` 顶层字段**（与图召回的理由不同，见 §2.4） | `[细化]` + `[新决策]` |
| 8 | 性能预算 | 业务查询 **1 条** SELECT（+ 2 条 `set_config` 控制语句）；p95 ≤ 10ms；端到端 P95 增幅 ≤ 1%；关闭态 **0 条** SQL | `[新决策]` |
| 9 | 验收最危险处 | dev 库**只有 1 页 wiki**，「217 条不劣化」极可能是扩展一次都没触发的**假绿** → 加 `hit_cases ≥ 20` 前置门槛，否则本轮评测无效 | `[新决策]` |
| 10 | 回滚 | `WIKI_ENABLED=false` 逐字节等价（三层证据）；本步**零迁移、零索引、零新列**，代码回退即完整回滚 | `[细化]` |

**需管家节点裁决**：`N1`（归一落点：读侧子串 vs 写侧归一列 + 迁移）、`N2`（超时值进 `settings` 还是 `decisions.Final`）、`N3`（一组未标定的阈值常量）、`N4`（是否允许为评测构造合成 wiki 页）、`N5`（是否新增独立开关）—— 全部集中在 §5。

---

## 1. 现状锚点（逐条已核实，不是推测）

| # | 事实 | 证据位置 |
|---|---|---|
| F1 | `wiki_pages.key_terms` 是 JSONB 数组，页级 `sorted` 去重；`sections[].key_terms` 同源 | 迁移 `0026_add_wiki_pages.py`；`services/wiki/build.py:301` |
| F2 | **`key_terms` 未做 NFKC / 小写归一**：`_clean_terms()` 只做 `.strip()`、截长（`WIKI_TERM_MAX_CHARS=40`）、保序去重，页级再 `sorted` | `services/wiki/build.py:183-195, 300-301` |
| F3 | 查询侧 `build_ilike_terms(query)` 输出是 **NFKC + lower + 疑问词切段 + 长度降序 + 截前 3** | `services/keyword_text.py:158-195` |
| F4 | 已存在一条「查询扩展」通道：`load_alias_map()` → `alias_map: dict[str, list[str]]`，ILIKE 走 `build_ilike_pattern(terms, alias_map)`（别名作为**同一逻辑词元的并列 OR 分支，不占 `ILIKE_MAX_TERMS` 名额**），FTS 走 `build_fts_query(query, alias_index_tokens(alias_map))` | `services/lexicon.py`；`services/retrieve/__init__.py:247-288` |
| F5 | `alias_index_tokens()` 用 `to_fts_text(alias).split()` —— 一个 4 字中文别名会展开成 **3 个 2-gram**，即「1 个顶层词元 → N 个索引词元」 | `services/lexicon.py:123-130` |
| F6 | `build_fts_query` 的别名路径存在**静默截断**：`windows` 先 `[:24]`，再 `_dedupe(windows + cjk_aliases)[:24]`；当 query 窗口已达 `FTS_QUERY_MAX_TERMS=24` 时，别名全部被丢弃且不报错 | `services/keyword_text.py:128, 142-146` |
| F7 | `_WHERE_BASE` 含 `c.is_latest = true` 与 G1~G4，占位符恰为 `tenant_id / clearance / authorized_kb_ids / user_subjects` | `services/retrieve/__init__.py:47-52`；`services/acl/visibility.py:27-33` |
| F8 | 第 3 步 wiki 读接口的可见性就是「`EXISTS` + 内联 `_WHERE_BASE` + `c.document_id = p.document_id`」形状 | `api/wiki.py:36-47, 117-129` |
| F9 | `retrieve_chain()` 拆解后把**子问题**作为 `query` 逐轮传给 `retrieve()`；`chat.py` 传的是改写后的 `retrieve_query` | `services/retrieve/__init__.py:789-796`；`api/chat.py:421` |
| F10 | `WIKI_NAV_MAX_TERMS=2` **尚未落地**（全仓 `grep WIKI_NAV` 为空），主文档 §9.5 只写了它应进 `decisions.py` | 主文档 §9.5 行 936；`code/apps/api/app/config/decisions.py` |
| F11 | 第 3 步把生成侧 wiki 常量放在 `services/wiki/prompts.py`（`WIKI_MAX_SECTIONS=8` / `WIKI_MAX_TERMS_PER_SECTION=12` / `WIKI_TERM_MAX_CHARS=40`），**不在 `decisions.py`** | `services/wiki/prompts.py:76-81` |
| F12 | 启动自检 C5（wiki 依赖 = LLM 凭据 + `wiki_pages` 表存在）已覆盖「开了但表没建」；C6 拒绝 `WIKI_SCOPE=kb` | `config/self_check_ext.py:219-242, 342-368` |
| F13 | `config_profile` 跨角色白名单已含 `wiki_enabled` 等 5 项 → 开关变化会自动改 `config_fingerprint` | `config/config_profile.py:70-74, 135-139` |
| F14 | `scripts/run_eval.py::_warn_caliber_drift` 只比对 `graph_enabled` / `context_caliber` 两个键 | `scripts/run_eval.py:185-205` |
| F15 | **dev 真库只有 1 页 wiki**（`10538f14-…`，来源文档 = Vue3 Composition API 面试版 PDF，口径 `gpt-4o-mini` / `wprompt-v1`） | `04-test/测试报告-第3步-wiki.md` §四 行 167-175 |
| F16 | `set_config('statement_timeout', :ms, true)` 是事务级，会溢出到同事务后续 SQL；第 2 步已因此修过一次（`e6b6a94`） | 主文档 §9.7 偏离 2；`services/graph/recall.py` |
| F17 | `path_stats: dict[str, int]`，`merge_rounds` 对它**按轮求和**；`chat.py` 用 `**result.path_stats` 自动带进 SSE / 审计 | `services/retrieve/base.py:63`；`services/retrieve/chain.py:149-150`；`api/chat.py:754, 808` |

> **F2 + F3 是本文最关键的一对事实**：写入侧是「LLM 原文短语」（如 `"Next.js"` / `"App Router"`），查询侧是「归一化切段词元」（如 `router` / `pages` / `next`）。二者既不相等、也不是同一粒度 —— 任何「精确包含匹配」的 SQL 都注定 0 命中，且现象与「库里没有相关页」完全一样（静默失效）。

---

## 2. 决策契约与实施规范（逐条结论 + 数据 / SQL / 接口 / 失败语义 / 验收 / 回滚）

### 2.1 「命中 wiki 页」的判定

#### 2.1.1 输入：`[细化]` 用当轮 `query` 的 `build_ilike_terms` 输出

```python
# 在 RetrievalService.retrieve() 内，位置：现有第 247 行之后、load_alias_map 之前
ilike_terms = build_ilike_terms(query)          # 一个字节都不改
needles = list(ilike_terms)                    # 导航查询的唯一输入
```

**不用「用户原始 question」/「改写前 query」的三条理由**：

1. **同源**：`build_ilike_terms` 已经是 ILIKE 路与 FTS 路共同的词元真源（F3/F4）。另起一套切分 = 第二个真源，与 `keyword_text.py` 模块 docstring 的硬约束（「入库与查询各写一份 bigram 逻辑 → 静默 0 召回」）冲突。
2. **不放大**：若用「扩展后的词元」去命中页面，页面再产出扩展词元，会形成一次请求内的自我放大链；用未扩展的 `ilike_terms` 让判定与扩展严格单向。
3. **改动面**：`retrieve()` 的 `query` 在链式多轮时已是子问题（F9）。取原始 question 需要给 `retrieve()` / `retrieve_chain()` / `merge_rounds()` 全链加参数，且多轮共用一个原始问句的 wiki 页会把 A 轮的主题术语灌进 B 轮 —— 这是噪声，不是召回。**每轮各自判定**与既有「每轮各自跑三路召回 + RRF」的语义一致。

> 已知的边界（诚实标注）：`chat.py` 传的是**改写后** `retrieve_query`（F9），因此导航判定跟的是改写词元而不是用户逐字输入。这与三路召回的现状完全一致，本文不引入新的偏差；若将来要求「必须用原始问句命中页」，那是一次独立的口径变更。

#### 2.1.2 匹配规则：`[新决策]` 归一化后**字面子串包含**，不是 `jsonb ?|`

| 候选 | 判定 | 理由 |
|---|---|---|
| `key_terms ?\| :needles`（JSONB 精确包含 + GIN） | **否决** | F2/F3：`?\|` 要求数组元素与词元**逐字相等**。实测形状：问句 `Next.js App Router vs Pages` → `needles = ['router','pages','next']`；页 `key_terms = ["Next.js","App Router","Pages Router"]` → **0 命中**（大小写 + 短语 vs 词元）。除非写侧归一，否则这是结构性失效 |
| `lower(kt) LIKE '%' \|\| :needle \|\| '%'` | **否决** | `LIKE` 的 `%` / `_` 是通配符，而 `needles` 里天然含 `_`（ASCII 标识符如 `get_retrieval`）与 `-`（型号 `ax-3000`）→ 会错配，且需转义 |
| **`strpos(lower(kt.term), n.term) > 0`** | **采纳** | `strpos` 是**字面量**子串比较，无通配语义、无注入面；`lower()` 补上 ASCII 大小写差；子串语义正好覆盖「短语含词元」。与既有 `_KEYWORD_SQL` 的 `strpos(lower(c.content), :needle)` **同源同款**（`retrieve/__init__.py:94-97`） |

**方向**：`needle ⊆ key_term`（查询词元是针，页术语是草堆），不做反向。理由：反向会让一个 2 字查询词元把「包含它的长术语」全部点亮，命中页数不可控；正向时命中数由「页里有多少术语含这个词」决定，收敛得多。

**针的过滤在 Python 侧做**（不进 SQL）：`needles` 已经是 `build_ilike_terms` 的输出（中文段 ≥2 字、ASCII ≥4 字或含 `_`），天然满足最小长度；再剔除纯数字项。SQL 只做比较，不重复一套长度规则。

**已知缺口（登记为 U-D）**：PG 的 `lower()` 不做 NFKC，全角字符（如 `ＡＸ３０００`）仍匹配不上 NFKC 归一后的 needle。当前语料未见全角术语，接受为残余，触发条件见 §7。

#### 2.1.3 SQL 形状（完整，可直接编码）

```sql
-- app/services/wiki/navigate.py :: _WIKI_NAV_SQL
-- {where_base} 由 app.services.retrieve._WHERE_BASE 内联（延迟 import，单一真源）
WITH hit AS (
    SELECT p.document_id,
           p.key_terms,
           max(length(n.term))     AS best_len,
           count(DISTINCT n.term)  AS hit_terms
    FROM wiki_pages p
    CROSS JOIN LATERAL unnest(CAST(:needles AS text[])) AS n(term)
    JOIN LATERAL jsonb_array_elements_text(p.key_terms) AS kt(term) ON TRUE
    WHERE p.tenant_id = :tenant_id
      AND p.kb_id = ANY(:authorized_kb_ids)
      AND p.scope = 'doc'
      AND p.status = 'ready'
      AND p.generator_model = :gm            -- 口径 fail-closed（§2.1.4）
      AND p.prompt_version  = :pv
      AND EXISTS (
            SELECT 1 FROM chunks c
            {where_base}                     -- ← 四闸门逐字内联，别名 c 不变
              AND c.document_id = p.document_id
      )
      AND strpos(lower(kt.term), n.term) > 0
    GROUP BY p.document_id, p.key_terms
)
SELECT document_id, key_terms
FROM hit
ORDER BY best_len DESC, hit_terms DESC, document_id ASC
LIMIT :max_pages                            -- = 1（WIKI_NAV_MAX_PAGES）
```

**逐条设计说明**（不是装饰，都是可写错的地方）：

- `p.tenant_id = :tenant_id` 与 `_WHERE_BASE` 的 `c.tenant_id = :tenant_id` **并存不冲突**：前者让 `idx_wiki_pages_kb` 立刻收敛，后者保证 EXISTS 内的 chunk 属于本租户；`c.document_id = p.document_id` 把两者绑定。第 3 步 `api/wiki.py` 的详情 SQL 没有显式 `p.tenant_id`（它靠 `page_id` 定位），本步按 kb 集合扫描，**必须**显式加。
- **不重算 `authorized_kb_ids`**：`api/wiki.py` 为 admin（`clearance >= 40`）额外查了一次 `_tenant_kb_ids()`；而 `retrieve()` 收到的 `pushdown["authorized_kb_ids"]` 已经是 `chat.py` 算好的 `effective_kb_ids`（含 admin 豁免 + 前端选库交集，见 `api/chat.py:421` 注释）。**直接 `**pushdown` 展开，禁止再查一次库或再合并一次**，否则会重蹈「admin 豁免失效 / 检索范围被扩大」的既有坑。
- **口径双过滤**（`gm` / `pv` 来自 `current_wiki_profile()`）：旧口径页在重建窗口期内会被**排除**而非混算，与图召回的 fail-closed 同纪律。第 3 步 `capabilities.collect_wiki_data_stats()` 已有 `pages_caliber_mismatch` 统计，运维可观测。
- **排序全序**：`best_len DESC`（命中最长的查询词元 = 最具体）→ `hit_terms DESC`（命中词元多）→ `document_id ASC`。**不用 `p.id`**：第 3 步测试报告 P2-2 实证「页重建 = DELETE+INSERT 换 id」，`p.id` 不稳定；`document_id` 才是稳定键。全序保证同一输入同一结果（主文档 §17 必答项 8 的「排序键全序」纪律）。
- `LIMIT :max_pages = 1`：`[新决策]` **只取一页**。理由：① `WIKI_NAV_MAX_TERMS=2` 的容量天然只够一页，多页混合会把不同主题的术语拼进同一个查询（噪声叠加）；② 一页的语义清晰 —— 「这句话提到了某篇文档的 wiki 主题，用该页的其它术语补词」；③ 多页需要跨页打分，把一个未标定过的相关性口径引进检索，风险与收益不成比例。
- `GROUP BY p.document_id, p.key_terms`：JSONB 可分组；`p.key_terms` 随行带出，避免二次查询。
- **`:needles` 的绑定形状**：传 Python `list[str]`，SQL 里显式 `CAST(:needles AS text[])`（项目既有 `= ANY(:authorized_kb_ids)` 已证明数组绑定可用；显式 cast 是给 asyncpg 的类型推断兜底）。**不要**把词元拼成字符串再 `string_to_array` —— 词元里可能含逗号 / 空格，拼接会静默拆错。
- **口径不一致会静默 0 命中（运维须知）**：`gm` 由 `current_wiki_profile()` 从 `settings.wiki_generator_model`（空则回落 `llm_model`）解析。若 api 侧 `LLM_MODEL` 与生成 worker 侧不同，导航查询会**排除全部页**且不报错，现象与「语料里没有相关主题」一样。验收前 MUST 在管理端「系统能力」页核对 `pages_caliber_mismatch = 0`；这也是 §7.4 跨角色 `config_fingerprint` 比对的用武之地。

#### 2.1.4 四闸门保证：`[细化]`

| 要求 | 落点 |
|---|---|
| G1 密级 / G2 kb / G3 deny / G4 acl / `is_latest` | `_WHERE_BASE` 内联进 `EXISTS`，**不新增、不减少、不重排**任何条件（红线 B 的逐字纪律） |
| 绑定参数 | `**pushdown` 原样展开，占位符与 `_WHERE_BASE` 一一对应；额外只加 `needles` / `gm` / `pv` / `max_pages` |
| 页 → 文档 → 可见 chunk | `c.document_id = p.document_id`（与第 3 步详情/列表端点同形状，F8） |
| 「关了就跳过权限」 | 禁止。权限判定在 SQL 内，开关只决定「发不发这条 SQL」；且开关关闭时不存在任何 wiki 数据被读 |
| 测试 | `tests/wiki/test_nav_acl.py` 四类越权反例 + **真库探针**（第 3 步 P2-1 指出纯 SQL 文本断言会假红，本步必须补语义级用例） |

#### 2.1.5 索引结论：`[新决策]` 不新增 GIN，现有索引够用

- 本 SQL 用的是 `strpos(lower(...))`，**本来就用不上** `jsonb_ops` GIN；`?|` + GIN 只在「写侧归一 + 精确匹配」方案下才有意义（见 `N1`）。
- 真正起过滤作用的是 `idx_wiki_pages_kb (tenant_id, kb_id, status)`（F1），把候选收敛到「本租户 + 授权库 + ready 页」；然后 `EXISTS` 走 `chunks` 主键/G1~G4 索引；`jsonb_array_elements_text` 只在通过 ACL 的页上展开。
- 成本量级：页数 ≈ 文档数（一文档一页，`uq_wiki_pages_document`）。按 1k 文档 × ≤24 术语 × ≤3 针 ≈ 7.2 万次短字符串比较，PG 内毫秒级；`EXISTS` 的索引探测是主导成本，与术语数无关。
- **升级触发条件（写进契约，不是「以后再说」）**：`pages_ready > 5_000` **或** `rag.retrieve.wiki_nav` p95 > 10ms 连续两个观测窗 → 启动 `N1` 的写侧归一列 + GIN 方案（需迁移 0027 + `--recaliber` 回填）。在此之前不建索引 = 少一份写入侧维护、少一次部署变更。

---

### 2.2 词元选择算法与限流冲突

#### 2.2.1 结论先行：`[新决策]` **独立预算，原词元集合冻结**

| 方案 | 判定 | 理由 |
|---|---|---|
| 追加后重排截断（把 wiki 词元塞进 `ilike_terms` 再 `sorted(...)[:3]`） | **否决（致命）** | `build_ilike_terms` 按**长度降序**取前 3。一个 8 字 wiki 术语会把原 query 的第 3 个词元挤出集合 → **改动了主路输入本身**，217 条基线直接劣化，且失败形态是「命中率掉了但没人知道为什么」 |
| 预留名额（query 只取 `ILIKE_MAX_TERMS - WIKI_NAV_MAX_TERMS = 1` 个） | **否决** | 等于主动把已标定的 3 词元削成 1（`keyword_text.py:169-171` 实测 3 处饱和、精度 66%）；且只能「有 wiki 词元时才降」，造成开/关两态走不同代码路径 → 评测不可复现 |
| **独立预算**（采纳） | 原 `ilike_terms` 输出**逐字节不变**；wiki 词元通过新增的 `extra_terms` / `extra_index_tokens` 关键字参数作为**附加 OR 分支 / OR 项**注入，不占 `ILIKE_MAX_TERMS` 名额，也不参与原 `[:FTS_QUERY_MAX_TERMS]` 截断 | 与既有 lexicon 别名的语义完全一致（F4：别名不占名额），只是「扩展来源」不同；主路输入被冻结 → 关闭态与今天逐字节等价成为可证事实 |

#### 2.2.2 为什么 `WIKI_NAV_MAX_TERMS=2` 还不够：二级 FTS 预算 `[新决策]`

`alias_index_tokens()` 证明了一个容易漏的放大（F5）：中文顶层词元经 `to_fts_text` 会展开成 **(len-1) 个 2-gram**。若把两个 8 字中文 wiki 术语直接塞进 tsquery，会多出 14 个 OR 项 —— `FTS_QUERY_MAX_TERMS=24` 的 tsquery 膨胀 58%，且这些 2-gram 区分度低，正是 `keyword_text.py:106-107` 明确要避免的「命中全库」噪声。

因此新增 **`WIKI_NAV_MAX_FTS_TOKENS: Final[int] = 6`**，规则：

- 预算按**整个顶层词元**申请，装不下就**整个跳过该词元**（不是截半截 2-gram —— 半截词元在 FTS 里是静默的伪匹配）；
- ILIKE 与 FTS **共用同一份被选中的顶层词元**（一次查询两路复用，回答 team-lead 的「是否共用同一份扩展结果」）；跳过就是两路一起跳过，不出现「ILIKE 扩了、FTS 没扩」的口径分裂；
- FTS 的有效上限 = `FTS_QUERY_MAX_TERMS + WIKI_NAV_MAX_FTS_TOKENS`，**原 query 窗口一个不少**（这正是 F6 的既有缺陷在 wiki 路径上不得复刻）。

#### 2.2.3 选词伪码（纯函数，可单测，无 IO）

```python
# app/services/wiki/navigate.py
def select_nav_terms(
    page_terms: list[str],            # 命中页的 key_terms（页级已 sorted 去重）
    query_terms: list[str],           # = build_ilike_terms(query)，未扩展
    query_norm: str,                  # keyword_text.normalize_query_text(query)
    alias_forms: set[str],            # lexicon 已扩出的写法，避免重复
) -> tuple[list[str], list[str]]:
    """→ (顶层词元, FTS 索引词元)。全序、限量、不修改任何入参。"""
    out: list[str] = []
    fts: list[str] = []
    used = 0
    # 全序：长度降序（长词更具体，与 build_ilike_terms 同源），字典序破平局
    for raw in sorted((normalize_key_term(t) for t in page_terms),
                      key=lambda t: (-len(t), t)):
        term = raw
        if not _acceptable(term):                                  # 长度 / 停用 / 纯数字
            continue
        if term in out or term in alias_forms:
            continue
        if any(term in qt or qt in term for qt in query_terms):    # 与 query 词元去重（双向包含）
            continue
        if term in query_norm:                                     # 原问句里已有 → 扩了等于没扩
            continue
        tokens = to_fts_text(term).split()                         # 1 → N 放大点
        if used + len(tokens) > decisions.WIKI_NAV_MAX_FTS_TOKENS:
            continue                                               # 整个词元跳过，不截半截
        out.append(term)
        fts.extend(tokens)
        used += len(tokens)
        if len(out) >= decisions.WIKI_NAV_MAX_TERMS:
            break
    return out, list(dict.fromkeys(fts))
```

`_acceptable(term)` 的契约（全部可编码、无需模型判断）：

| 规则 | 值 | 依据 |
|---|---|---|
| 中文最短 | ≥ 2 字符 | 与 `build_ilike_terms` 的 `len(segment) >= 2` 同款 |
| ASCII 主导最短 | ≥ 4 字符（或含 `_`） | 与 `_ASCII_RE` 路同款，挡住 `vs` / `js` 类噪声 |
| 最长 | ≤ 12 字符 | 超过后 FTS 展开必然撞 6 token 预算；ILIKE 上长短语也几乎不可能在原文里原样出现 |
| 停用词 | 复用 `keyword_text.ILIKE_STOPWORDS`（把现有 `_ILIKE_SPLIT_WORDS` 提升为模块公开常量） | **不新造第二套停用词表**；它本来就是「疑问/功能词」集合 |
| 纯数字 / 纯标点 | 丢弃 | 与 `_ASCII_RE` 的噪声口径一致 |

```python
def _acceptable(term: str) -> bool:
    """确定性过滤；规则表见上。不含任何模型判断、无 IO。"""
    if not term or len(term) > decisions.WIKI_NAV_MAX_TERM_CHARS:
        return False
    if term in keyword_text.ILIKE_STOPWORDS or not term.strip("_- 0123456789"):
        return False
    is_cjk = bool(keyword_text.CJK_RE.fullmatch(term))   # _CJK_RE 提升为公开 CJK_RE
    minimum = (decisions.WIKI_NAV_MIN_CJK_CHARS if is_cjk
               else decisions.WIKI_NAV_MIN_ASCII_CHARS)
    return len(term) >= minimum or (not is_cjk and "_" in term)
```

> **注意**：页级 `key_terms` 是 `sorted` 的，但 `select_nav_terms` **重新按长度排序** —— 这是刻意的：`sorted` 是字典序，与「区分度」无关；长度降序才与 `build_ilike_terms` 的长词优先同源。重排后仍是全序（字典序破平局），结果可复现。

#### 2.2.4 `keyword_text.py` 的 API 增量（向后兼容）

```python
# ① 新增公开函数（与私有 _normalize 同源，禁止复制第二份归一逻辑）
def normalize_query_text(text: str) -> str: ...      # _normalize(text[:400])，供 query_norm
def normalize_key_term(term: str) -> str: ...        # NFKC + lower + strip + 折叠空白

# ② 两个既有私有常量提升为公开（**只改名字，不改值、不改正则**）：
CJK_RE = _CJK_RE                                     # 供 _acceptable 判中文/ASCII
ILIKE_STOPWORDS = _ILIKE_SPLIT_WORDS                 # 供 _acceptable 判停用词

# 两个既有函数各加一个 keyword-only 且带默认值的参数：
def build_ilike_pattern(
    terms: list[str],
    aliases: dict[str, list[str]] | None = None,
    *,
    extra_terms: Sequence[str] = (),               # ← 新增，默认空
) -> str: ...

def build_fts_query(
    query: str,
    alias_tokens: list[str] | None = None,
    *,
    extra_index_tokens: Sequence[str] = (),        # ← 新增，默认空
) -> str | None: ...
```

**契约（写进 docstring，Golden Test 钉住）**：`extra_*` 为空时，两个函数的返回值 MUST 与改动前**逐字节相同**；所有既有调用点（含 `keyword_text._find_probe_hit` 的 `build_ilike_pattern([term])`、`hit_snippet`）**一个实参都不改**。这是回滚等价性的第 2 层证据（§2.7）。

`build_fts_query` 内部改动位置（精确到行）：

```python
# 现状 keyword_text.py:128  windows = _dedupe(windows)[:FTS_QUERY_MAX_TERMS]
# 现状 keyword_text.py:145  windows = _dedupe(windows + cjk_aliases)[:FTS_QUERY_MAX_TERMS]
# 新增（在 groups 拼装之前，:148 之前）：
if extra_index_tokens:
    cap = decisions.FTS_QUERY_MAX_TERMS + decisions.WIKI_NAV_MAX_FTS_TOKENS
    cjk_extra = [t for t in extra_index_tokens if _CJK_RE.fullmatch(t)]
    ascii_extra = [t for t in extra_index_tokens if not _CJK_RE.fullmatch(t)]
    windows = _dedupe(windows + cjk_extra)[:cap]      # 不截断原窗口
    ascii_items = _dedupe(ascii_items + ascii_extra)[:cap]
```

> **不得复用 F6 的别名追加行**（`:145-146`）—— 那行会把原窗口重截到 24，wiki 词元会被静默吞掉。wiki 走独立的 `if extra_index_tokens` 分支；F6 的既有缺陷**本步随 N6 一起修**（lexicon 别名也纳入独立预算，与 `WIKI_NAV_MAX_FTS_TOKENS` 范式统一，见 §6-C4 / §7-U-C），别名行不再重截原窗口。

---

### 2.3 两路接入点与接口签名

#### 2.3.1 新增模块 `app/services/wiki/navigate.py`：查页 / 选词两层

```python
# app/services/wiki/navigate.py
from __future__ import annotations
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.config.wiki_profile import WikiProfile


@dataclass(frozen=True)
class WikiNavPage:
    """**一次导航 SQL** 的产物。所有字段带默认值 → `WikiNavPage()` 即「不扩展」哨兵。

    不把选词结果放进来：选词需要 `alias_forms`（词表别名），而 `alias_map` 在
    `retrieve()` 里晚于导航查询才取得。拆成「查页（IO）/ 选词（纯函数）」两层，
    SQL 仍只有一条，两路共用同一份选词结果。
    """
    document_id: uuid.UUID | None = None       # 稳定键，不用 page_id（§2.1.3 / P2-2）
    key_terms: tuple[str, ...] = ()            # 命中页的 LLM 原文术语，原样带回
    degraded_reason: str = ""                  # "" | "timeout" | "error" | "no_match"
    elapsed_ms: float = 0.0


async def load_wiki_nav_page(
    session: AsyncSession,
    *,
    needles: list[str],                        # = build_ilike_terms(query)，未扩展
    pushdown: dict[str, object],               # 直接 ** 展开，不重算、不改名
    profile: WikiProfile,                      # gm / pv 的唯一来源
    timeout_ms: int,
) -> WikiNavPage:
    """发 1 条 SELECT 找命中页；任何失败都返回「不扩展」，不抛给主链路。"""


def select_nav_terms(
    page_terms: list[str],
    query_terms: list[str],
    query_norm: str,
    alias_forms: set[str],
) -> tuple[list[str], list[str]]:
    """纯函数：→ (顶层词元, FTS 索引词元)。全序、限量、不改入参。见 §2.2.3。"""
```

**为什么是 dataclass 而不是 `list[str]`**：降级原因、命中页、耗时都要进观测；`retrieve()` 还必须能区分「有页但没选出一个词元」（选词过滤生效）与「根本没命中页」（数据未就绪）—— 这两者处置完全不同，塞进一个列表会丢信息。

**为什么查页与选词拆两层（易被写错的顺序依赖）**：`select_nav_terms` 的入参 `alias_forms` 来自 `load_alias_map()`，而后者在 `retrieve()` 中位于导航查询之后。若把选词塞进 `load_wiki_nav_page()`，要么得把 lexicon 查询提前（改动既有顺序、扩大改动面），要么得放弃「与词表别名去重」（重复扩词）。拆层后依赖方向是单向的：`查页 → 取别名 → 选词 → 造 pattern / tsquery`，且**只有「查页」碰数据库**（一次查询两路复用）。

#### 2.3.2 `retrieve()` 内的接入顺序（精确到现有行）

```python
# ① 位置：services/retrieve/__init__.py 第 247 行之后、第 250 行 load_alias_map 之前
#    ★ 关闭态不能出现任何 wiki 模块符号（连 dataclass 都不实例化）——否则「零加载」
#      会退化成「零 SQL 但已 import」。故用原生哨兵 + 函数句柄，全部在 if 内赋值。
ilike_terms = build_ilike_terms(query)                     # 现状，一行不改
nav_key_terms: tuple[str, ...] = ()
nav_hit = False
nav_select = None                                          # type: Callable | None
nav_stats: dict[str, int] = {}                             # ★ 见下方「局部变量时机」
wiki_terms: list[str] = []
wiki_fts_tokens: list[str] = []
wiki_on = get_settings().wiki_enabled

if wiki_on and ilike_terms:
    from app.config.wiki_profile import current_wiki_profile
    from app.services.wiki.navigate import load_wiki_nav_page, select_nav_terms
    nav_select = select_nav_terms
    page = await load_wiki_nav_page(
        session, needles=ilike_terms, pushdown=pushdown,
        profile=current_wiki_profile(), timeout_ms=...,
    )
    nav_key_terms = page.key_terms
    nav_hit = page.document_id is not None
    stages["wiki_nav"] = int(page.elapsed_ms)
    metrics.observe_ms("rag.retrieve.wiki_nav", page.elapsed_ms)
    nav_stats["wiki_nav_hits"] = int(nav_hit)

# ② 既有词表扩展（位置不变；nav 查询在前，只是因为选词要等 alias_map）
alias_map: dict[str, list[str]] = {}
if ilike_terms and decisions.ALIAS_EXPANSION_ENABLED:
    alias_map = await load_alias_map(session, authorized_kb_ids, ilike_terms)
    ...

# ③ 选词（纯函数；仅在开关开启且查页成功时可能产出非空）
if nav_select is not None and nav_key_terms:
    alias_forms = {f for forms in alias_map.values() for f in forms}
    wiki_terms, wiki_fts_tokens = nav_select(
        list(nav_key_terms), ilike_terms,
        normalize_query_text(query[:400]), alias_forms,   # ← 加入既有 keyword_text import 列表
    )
    nav_stats["wiki_nav_terms"] = len(wiki_terms)

# ③-b 观测：★ 每个开启态请求都打桶（含 0 词元），否则分布缺 0 桶、分位数失真
if wiki_on:
    metrics.incr("rag.wiki.nav.terms_bucket", bucket=str(min(len(wiki_terms), 3)))
    if wiki_terms:
        metrics.incr("rag.wiki.nav.expand", len(wiki_terms))

# ④ ILIKE 路（现状第 271 行）
"pattern": build_ilike_pattern(ilike_terms, alias_map, extra_terms=wiki_terms),
"needle":  ilike_terms[0].lower(),        # ★ 不变：仍是最长的**原 query 词元**

# ⑤ FTS 路（现状第 285 行）
build_fts_query(query, alias_index_tokens(alias_map),
                extra_index_tokens=wiki_fts_tokens)

# ⑥ 位置：现状第 698 行 `path_stats: dict[str, int] = {}` 之后、return 之前
path_stats.update(nav_stats)               # 关闭态 nav_stats 为空 → 不写任何 wiki_* 键
```

**六处关键决定（逐条都是可写错的点）**：

1. **`path_stats` 的局部变量时机**：`retrieve()` 里 `path_stats` 直到**第 698 行**才初始化（`__init__.py:698`）。在第 247 行直接写 `path_stats[...]` 是 `NameError`。因此先攒在 `nav_stats`，到第 698 行之后 `update()` —— 与图召回的第 8 步打点同位置。这一条不写清，coder 照抄伪码会直接崩。
2. **`:needle` 保持不变**：`_KEYWORD_SQL` 的 `ORDER BY strpos(lower(c.content), :needle)` 用最长词元定序。若换成 wiki 词元，排序键会变成「跟本次问答无关的词」，命中集顺序漂移 → RRF 顺序漂移 → 217 条基线风险。**写进契约 + 单测断言**。
3. **关闭态零 SQL**：`if get_settings().wiki_enabled` 包住 + **延迟 import**（与第 2 步图召回同款）。关闭时 wiki 模块不加载、导航 SQL 一条不发、`nav_stats` 为空 → `path_stats` 不出现 `wiki_*` 键（与基线逐键可字节比对）。
4. **一次查询两路复用**：`load_wiki_nav_page()` 只在 ① 调用一次；`wiki_terms` / `wiki_fts_tokens` 是同一次 `select_nav_terms` 的两个返回值，ILIKE 与 FTS 共用，不存在「两路各自查一次库」或「两路扩了不同的词」。
5. **降级不改变两路实参形状**：`nav_key_terms` 为空 / 超时 / 选不出词时，`wiki_terms = []` / `wiki_fts_tokens = []` → `extra_*` 传空序列，两个构造函数走原分支（Golden Test 保证逐字节相同）。
6. **`path_stats` 两键是 int**：`wiki_nav_hits`（0/1）、`wiki_nav_terms`（0..2）。`chat.py` 的 `**result.path_stats` 自动带进 SSE 与审计（F17），无需改 `chat.py`；多轮 `merge_rounds` 会自动按轮求和 —— 与 §10.1-R2 同型，见 §7-U-E。**不放原因字符串**（`path_stats` 是 `dict[str,int]` 且按轮求和，原因不可加）。

> 桶标签 `min(len(terms), 3)` 把 `{0,1,2,3+}` 映射成 `"0"/"1"/"2"/"3"`，是 4 个低基数枚举值（符合 `metrics.py` 对标签的约束）；报告侧把 `"3"` 显示为 `gt2`。

---

### 2.4 失败 / 超时语义：`[细化]` fail-open + `[新决策]` 不新增顶层字段

三档失败语义（团队 wiki 底座已沉淀的模式，与主文档 §4.1 一致）：

| 档 | 触发 | 行为 |
|---|---|---|
| 启动 fail-closed | `WIKI_ENABLED=true` 但 `wiki_pages` 表不存在 / LLM 凭据缺失 / `WIKI_SCOPE≠doc` | 已有 C5 / C6 覆盖（F12），**本步不新增自检规则** |
| 在线 fail-open | 导航 SQL 超时 / 异常 / 无命中页 / 页有但选不出词 | `WikiNavPage(degraded_reason=...)`，不扩展，三路召回照常出答案 |
| 离线 fail-visible | 页生成失败 | 第 3 步 `status='failed'` + `pages_failed` 统计，本步不动 |

**超时机制（必须照抄第 2 步踩过的坑，F16）**：

```python
async def load_wiki_nav_page(session, *, needles, pushdown, profile, timeout_ms):
    t0 = time.perf_counter()
    if not needles:
        return WikiNavPage(degraded_reason="no_match")

    # ① 读原值（不是假设 0）—— 上游可能给整个事务设过 statement_timeout
    original = (await session.execute(
        text("SELECT current_setting('statement_timeout')")
    )).scalar_one()
    try:
        # ② 设本语句的墙钟上限。is_local=true 的作用域是**整个事务**，故必须还原
        await session.execute(
            text("SELECT set_config('statement_timeout', :ms, true)"),
            {"ms": str(int(timeout_ms))},
        )
        row = (await session.execute(
            text(_WIKI_NAV_SQL.format(where_base=_WHERE_BASE)),
            {**pushdown, "needles": needles,
             "gm": profile.generator_model, "pv": profile.prompt_version,
             "max_pages": decisions.WIKI_NAV_MAX_PAGES},
        )).first()
    except Exception as exc:
        # ③ QueryCanceled 会让事务进入 aborted 状态 → MUST rollback，
        #    否则紧接着的 ILIKE / FTS 两条 SQL 全部报 "current transaction is aborted"
        await session.rollback()
        reason = "timeout" if isinstance(exc, (QueryCanceled, TimeoutError)) else "error"
        logger.info("wiki 导航查询失败，本次不扩展词元: %s", reason, exc_info=exc)
        metrics.incr("rag.wiki.nav.degraded", reason=reason)
        return WikiNavPage(degraded_reason=reason,
                           elapsed_ms=(time.perf_counter() - t0) * 1000)
    else:
        # ④ 成功路径 MUST 还原：不还原会溢出到紧接着的 ILIKE（顺序扫描，最易
        #    超时）/ FTS 两条 SQL —— e6b6a94 修过的同款缺陷。还原失败不许拖垮主链路。
        try:
            await session.execute(
                text("SELECT set_config('statement_timeout', :v, true)"),
                {"v": original},
            )
        except Exception:
            await session.rollback()          # 事务已不可用 → 回到干净状态
            logger.warning("wiki 导航还原 statement_timeout 失败", exc_info=True)
            metrics.incr("rag.wiki.nav.degraded", reason="error")
            return WikiNavPage(degraded_reason="error",
                               elapsed_ms=(time.perf_counter() - t0) * 1000)
        elapsed = (time.perf_counter() - t0) * 1000
        if row is None:
            metrics.incr("rag.wiki.nav.no_match")
            return WikiNavPage(degraded_reason="no_match", elapsed_ms=elapsed)
        metrics.incr("rag.wiki.nav.hit")
        return WikiNavPage(
            document_id=row.document_id,
            key_terms=tuple(row.key_terms or ()),
            elapsed_ms=elapsed,
        )
```

**五条契约（逐条对应上面编号）**：

- **① 读原值，不硬编码 `'0'`**：若将来上游给整个事务设过超时，写死 0 会静默取消人家的护栏（`e6b6a94` 的修法即读原值）。
- **② 位置特殊性**：导航查询在**三路关键词召回之前**，比图召回（在闸门之后）更靠前。溢出影响面更大 —— ILIKE 是顺序扫描，最容易撞超时。这是本步比第 2 步更需要还原语句的理由。
- **③ `rollback()` 不是可选项**：`QueryCanceled` 之后事务已 abort；降级路径事务级设置随事务消失，**不还原**（与 `services/graph/recall.py` 同款）。`rollback()` 自身用第 2 步 `_rollback_quietly()` 的同款静默包装 —— 回滚再抛异常不能变成「问答失败」。
- **④ 还原失败也必须 fail-open**：单独 try 包住；失败就 `rollback()` 并返回「不扩展」，绝不把异常抛回 `retrieve()`。
- **⑤ `no_match` 是正常态**：只打 `rag.wiki.nav.no_match`，**不计 degraded**；`degraded_reason` 里保留它仅供单测与本地排障区分「空 needles / 无页 / 有页但选不出词」。

> `_classify` 里 `QueryCanceled` 需从 `sqlalchemy.exc` / `asyncpg` 的具体异常类型导入，实现时按项目既有 `graph/recall.py` 的异常面照抄，**不要新造一套异常分类**。

**为什么不新增 `RetrievalResult.wiki_degraded_reason`（与图召回裁决 A 的对照）**：

| 维度 | 图召回（§9.7 裁决 A） | wiki 导航（本步） |
|---|---|---|
| 失败影响 | 影响「这条回答为什么没有图证据」—— 属于**证据链排障**，必须能从单条审计行直接反查 | 只影响「这次有没有多扩两个词」，**不产生任何证据、不进上下文、不改引用** |
| 消费方 | 运维要区分「超时 / 口径不符 / 没邻居」，三者处置不同 | 运维只需聚合趋势（「今天多少次导航超时」），不需要逐请求反查 |
| 合并代价 | `merge_rounds` 要新增一条「取首个非空」规则 | 若加字段，`merge_rounds` 又多一条规则、`RetrievalResult` 又多一个状态位 —— 为一条**只进指标**的信息付出跨层改动 |

→ **走 `metrics.incr("rag.wiki.nav.degraded", reason=...)` + 聚合日志即可**。`path_stats` 只放 int 计数（它是 `dict[str,int]` 且按轮求和，原因字符串不可加 —— 与 §9.7 裁决 A 的理由 ① 完全相同，结论不同是因为**信息用途不同**）。

**观测清单**：

| 指标 | 类型 | 说明 |
|---|---|---|
| `rag.wiki.nav.expand` | Counter（`incr(name, value=len(terms))`，仅 `terms>0` 时打） | 主文档 §13 第 4 步点名的指标 |
| `rag.wiki.nav.hit` | Counter | 命中页数（每次至多 1） |
| `rag.wiki.nav.no_match` | Counter | 正常空态，与 degraded 分开计数 |
| `rag.wiki.nav.degraded` | Counter，标签 `reason ∈ {timeout, error}` | 降级可见性 |
| `rag.wiki.nav.terms_bucket` | Counter，标签 `bucket ∈ {"0","1","2","3"}`（报告里 `"3"` 显示为 `gt2`） | **低基数分桶**，用于计算词元数分布与 p95（见 §2.6 ③） |
| `rag.retrieve.wiki_nav` | Histogram（`observe_ms`） | 导航耗时，SLO p95 ≤ 10ms |

---

### 2.5 性能预算：`[新决策]`

| 项 | 契约 | 理由 |
|---|---|---|
| 业务 SQL 上限 | **1 条** SELECT | 一次查询两路复用；不做「先 count 再查」的两段式（count 护栏会多一条 SQL，且 count 本身也 O(n)） |
| 控制语句 | **2 条** `set_config`（设 + 还原）；成功路径合计 3 条 statement，降级路径 2 条（含 rollback） | 事务级 statement_timeout 没有单语句等价写法；**关闭态 0 条**（硬要求，单测可证） |
| 硬超时 | `WIKI_NAV_TIMEOUT_MS = 50`（来源见 `N2`） | 远小于 `GRAPH_RETRIEVE_TIMEOUT_MS=800`；导航查询只是「找一页」，50ms 已属异常 |
| p95 | `rag.retrieve.wiki_nav` **≤ 10ms**（含控制语句） | 超过说明 `strpos × jsonb_array_elements` 规模该上护栏了（§2.1.5 触发条件） |
| 端到端 P95 增幅 | **≤ 1%**（比第 2 步的 3% 更严） | 本步不调 LLM、不进上下文、不加重排，只有 1~3 条轻量 SQL；给 3% 等于放任劣化 |
| 下推参数 | `**pushdown` 原样复用 4 个绑定值，**不重算、不改名** | 与 `_WHERE_BASE` 占位符一一对应（F7） |
| 连接池 | **不新增连接**：复用 `retrieve()` 的 `session`，串行执行 | 团队 wiki 底座「并发可行性必须先过两道容量校验」：`AsyncSession` 非并发安全；本步串行，每在途请求连接数不变 |
| 前缀缓存 | 影响 = 0 | wiki 词元只进 SQL，不进 system prompt、不进 `current_chunks`（红线 A） |

---

### 2.6 验收口径（可测化）

#### ① 关闭态逐字节等价 `[细化]`

| 层 | 断言 |
|---|---|
| 代码路径 | `WIKI_ENABLED=false` → `session.execute` 计数中导航 SQL = **0**；`app.services.wiki.navigate` 未被 import（延迟导入可证） |
| 纯函数 | 对 217 条问句 + 20 条代表问句（含型号 `ax-3000`、含 `_` 的 `get_retrieval`、纯中文切段）：`build_ilike_terms` / `build_ilike_pattern` / `build_fts_query` 输出与**冻结 Golden 快照逐字节相同** |
| 结构 | `pattern` 里 `extra_terms=()` 不产生任何分支；`tsquery` 里 `extra_index_tokens=()` 不产生任何 OR 项；`:needle` 仍等于 `ilike_terms[0].lower()` |
| 行为 | 217 条四项指标（可答通过率 / 漏答率 / 拒答正确率 / 命中率）与基线**逐项相同**；`path_stats` **无** `wiki_*` 键（与基线逐键可字节比对） |
| 纪律 | 团队 wiki 底座条目 5：探针只能定位原因，**不能替代评测**（「少一条召回、改一个排序键」探针都看不见） |

#### ② 开启态 217 条：用哪条链路、与哪份基线比 `[细化]` + `[新决策]`

| 项 | 决定 |
|---|---|
| 主验收链路 | **`scripts/run_eval.py`**（走 `/api/chat/ask` 生产链路，`context_caliber = "production_chat_path"`）—— 217 条历史基线由它产生，同链路才可比 |
| 辅助链路 | admin 门禁 `POST /api/admin/eval/run?with_recall_chunks=true`（`EvalService`，单趟 `retrieve()`）—— 检索层变化更直接，用于交叉验证；**两条链路的分数禁止横向比较**（主文档 §14.4 / v1.2 §9.8 双口径禁令） |
| 比对对象 | 同 `case_fingerprint`、同 `context_caliber`、**同 `config_fingerprint` 家族**里最近一次 `WIKI_ENABLED=false` 的记录；`_compare_baseline()` 仍只比三项既有指标，`wiki_*` 计数**只记录、不进方向表**（主文档 §14.1 纪律） |
| 判定 | 命中率不降、漏答率不升、可答通过率与拒答正确率不劣化；越权回归全绿；`rag.retrieve.wiki_nav` p95 ≤ 10ms；端到端 P95 增幅 ≤ 1% |
| **caliber 留痕** | `caliber.capabilities.wiki.enabled`（已有）、`caliber.context_caliber`（已有）、`config_fingerprint`（自动含 `wiki_enabled`，F13）；**新增** `eval_runs.caliber["wiki_nav"] = {hit_cases, expanded_cases, expanded_terms, degraded_reasons}`（只记录）；**新增** `_warn_caliber_drift` 的 `wiki_enabled` 漂移键（F14 —— 不加就会拿开/关 wiki 两条基线互比，正是双口径禁令要防的） |

#### ②-b **假绿防线（本步最重要的一条，`[新决策]`）**

F15：dev 真库**只有 1 页 wiki**（Vue3 文档）。第 4 步「只在命中 wiki 页时扩」+ 现有数据 ⇒ 217 条评测里扩展**几乎一次都不会触发** ⇒ 「命中率不降、漏答率不升」会以 100% 通过率通过，但测的是**关闭态**。这是与 §14.6「没测到 ≠ 没影响」完全同型的循环论证。

**MUST 写进验收单**：

```
前置门槛：wiki_nav_hit_cases ≥ 20（可答类 111 条里至少 20 条命中页）
     且  wiki_nav_expanded_cases > 0（至少一次真的扩出了词元）
不满足 → 本轮评测结论无效，禁止宣称「不劣化通过」。
报告 MUST 打印：hit_cases / expanded_cases / expanded_terms 三个数。
```

满足门槛的两条路径（`N4` 需裁决）：① 先跑 `python -m scripts.rebuild_wiki --all` 把评测语料的页补齐；② 构造合成 wiki 页 fixture（单独 kb 或事务内，跑完清理，与第 3 步真库探针同纪律）。**无论哪条，报告 MUST 标注「含合成页」或「页覆盖率」**。

#### ③ 扩展词元数 p95 ≤ 2 的打点方式 `[细化]`

- `WIKI_NAV_MAX_TERMS=2` 是**代码硬上限**，所以「p95 ≤ 2」在结构上恒成立 —— 把它当门禁等于设了一个不可能失败的判据（与 §9.8 裁决 1 推翻 `graph_unique` 的教训同类）。
- 真正该测的是「**上限没被绕过**」+「**扩展确实发生过**」：
  - `rag.wiki.nav.terms_bucket{bucket="3"}`（即 `gt2`）计数 MUST = **0**；
  - `bucket="2"` 计数 MUST > 0（证明跑到过上限，而不是恒 0 的空转）；
  - 报告里的 p95 由 4 个桶的累计分布计算，标注为「硬上限的推论值」，**不作为独立门禁**。
- 为什么用分桶 Counter 而不是新加 Histogram：`metrics.py` 只暴露 `incr` / `observe_ms`，后者固定按毫秒→秒换算且桶是 5ms~10s，装不下「0/1/2」；4 个低基数标签即可算分位数，零新增指标 API。

#### ④ 越权与失败注入（回归集）

| 用例 | 断言 |
|---|---|
| 四类越权（跨 kb / 高密级 / deny / acl 不匹配） | 导航查询**不返回该页**、扩展词元为空；真库探针（非纯 SQL 文本断言，补第 3 步 P2-1） |
| 页存在但 `status='stale'/'failed'` / 口径不匹配 | 不命中 |
| 命中页但 key_terms 全在 query 里 | `terms=()`，`no_match` 之外的空态可见 |
| 导航 SQL 抛 `QueryCanceled` | `session.rollback()` 被调用；后续 ILIKE / FTS 正常返回；答案不失败；`rag.wiki.nav.degraded{reason=timeout}` +1；`current_setting('statement_timeout')` 在成功路径复原为原值 |
| 关闭态 | 见 ① |

---

### 2.7 回滚：`[细化]`

**`WIKI_ENABLED=false` 逐字节等价的三层证据**（与第 2 步同款结构）：

1. **代码路径层**：`if settings.wiki_enabled` 包住 + 延迟 import → 导航 SQL 0 条、wiki 模块 0 加载；`build_ilike_terms` **一行未改**；两个 `keyword_text` 函数新增的是 keyword-only 默认参数，所有既有调用点实参不变。
2. **结构层**：`extra_*` 为空时走原分支（Golden Test 钉住）；`:needle` 不变；`pattern` / `tsquery` 与今天逐字节相同；`path_stats` 不写 `wiki_*` 键。
3. **行为层**：217 条四项指标逐项相同 + `path_stats` 逐键相同（可字节比对）。

**数据不删**：页留库，重新开启立即生效（主文档 §7.3 第 8 行已写「关 wiki → 什么都不用重跑；页留库，检索不扩展词元」—— 这句话在**第 3 步是空承诺，本步才真正兑现**，登记为 §6-C5）。

**代码级回滚**：本步 **零迁移、零索引、零新列、零新表**（§2.1.5）。回退镜像即完整回滚，不需要 `alembic downgrade`，不影响 `0026` 与第 3 步端点。这是本步明确的设计目标。

**不新增独立开关**：与 `WIKI_ENABLED` 共用。理由：① 主文档 §13 第 4 步的回滚就是 `WIKI_ENABLED=false`；② 三处强提示（启动横幅 / 管理端能力页 / DEPLOY.md §3.5）已把 `WIKI_ENABLED` 描述为整体能力，拆开关会让文案与配置组合同时失真；③ 与 §13 第 5 步「不新增开关，回滚 = 回到上一镜像」同纪律。若管家节点要求「只回退第 4 步、保留导航页」，见 `N5`。

---

## 3. 常量契约：`decisions.py` 投影与启动自检断言

> 判断标准沿用主文档 §9.5：**影响判定语义 / 口径 / 会让评测基线失去可比性的值 → `Final`；随部署环境取不同值是正常预期的值 → `settings`**。本步所有检索侧上限都是前者（改它 = 基线不可比，必须走代码评审）。

| 常量 | 类型 / 默认 | 类别 | 注释要点（可直接抄进 `decisions.py`） |
|---|---|---|---|
| `WIKI_NAV_MAX_TERMS` | `Final[int] = 2` | `[细化]` | 主文档 §9.5 已定。与 `LEXICON_MAX_ALIASES_PER_KEY=2` 同量级：扩词从宽会精度崩（项目既有教训）。**改它 = 217 条基线不可比** |
| `WIKI_NAV_MAX_FTS_TOKENS` | `Final[int] = 6` | `[新决策]` | 二级预算：中文顶层词元经 `to_fts_text` 会 1→N 展开成 2-gram。上限按「2 个词元 × 典型 3 gram」取 6。**MUST ≥ WIKI_NAV_MAX_TERMS** |
| `WIKI_NAV_MAX_PAGES` | `Final[int] = 1` | `[新决策]` | 一次只取一页；多页混合不同主题术语 = 噪声叠加 |
| `WIKI_NAV_MIN_CJK_CHARS` | `Final[int] = 2` | `[新决策]` | 与 `build_ilike_terms` 的 `len(segment)>=2` 同源 |
| `WIKI_NAV_MIN_ASCII_CHARS` | `Final[int] = 4` | `[新决策]` | 与 `_ASCII_RE` 路同源（挡 `vs` / `js`）；含 `_` 的标识符例外 |
| `WIKI_NAV_MAX_TERM_CHARS` | `Final[int] = 12` | `[新决策]` | 超过后 FTS 展开必撞 6 token 预算；且长短语在原文里几乎不可能原样命中。**MUST ≤ 第 3 步 `WIKI_TERM_MAX_CHARS=40`** |
| `WIKI_NAV_CANDIDATE_PAGE_GUARD` | `Final[int] = 5_000` | `[新决策]` | **观测触发阈值**，不是 SQL 里的 LIMIT（LIMIT 放在 ACL 之前会系统性漏掉命中的页）。`pages_ready` 超它 → 启动 §2.1.5 的 GIN 升级评估 |
| `WIKI_NAV_TIMEOUT_MS` | `settings` 或 `Final[int] = 50` | `[待裁决 N2]` | 见 §5 |

**`decisions.self_check()` 增量**（只断言本步常量的内部一致性，**禁止 import `app.services.*`** —— 配置层反向依赖服务层会在启动期拉出检索模块，破坏「关闭态零加载」）：

```python
if not (1 <= WIKI_NAV_MAX_TERMS <= 2):
    raise RuntimeError("WIKI_NAV_MAX_TERMS MUST ∈ [1,2]：放开会让扩词精度崩（项目既有教训）")
if WIKI_NAV_MAX_FTS_TOKENS < WIKI_NAV_MAX_TERMS:
    raise RuntimeError("FTS token 预算 MUST >= 顶层词元上限，否则每个中文词元都装不下")
if WIKI_NAV_MAX_PAGES != 1:
    raise RuntimeError("本步只支持单页扩展；多页需先标定跨页相关性口径")
if not (WIKI_NAV_MIN_CJK_CHARS <= WIKI_NAV_MAX_TERM_CHARS
        and WIKI_NAV_MIN_ASCII_CHARS <= WIKI_NAV_MAX_TERM_CHARS):
    raise RuntimeError("词元长度下限 MUST <= 上限，否则恒选不出词")
```

**跨模块常量一致性**（`WIKI_NAV_MAX_TERM_CHARS ≤ services/wiki/prompts.py::WIKI_TERM_MAX_CHARS`）用 `tests/wiki/test_nav_constants.py` 单测钉住，不进 `self_check`。

**`settings` 增量（仅当 `N2` 选 settings 方案）**：`wiki_nav_timeout_ms: int = 50`，取值范围自检 `[10, 200]`；同步加入 `config_profile` 的跨角色白名单（F13）—— 这是「改一个配置项要同步四处」的典型，见 `N2`。

---

## 4. 分步实现路线（三段式，每段可独立验收 / 独立回滚）

### 段 A · 纯函数与契约（零 DB、零线上风险）

**改动面**：`services/keyword_text.py`（`normalize_query_text` / `normalize_key_term` 两个公开函数、`ILIKE_STOPWORDS` 公开化、两个函数加 keyword-only 默认参）；`config/decisions.py`（8 个 `Final` + `self_check` 断言）；`services/wiki/navigate.py` 的 `WikiNavPage` / `select_nav_terms`（纯函数部分）。

**验收**：
- Golden Test：改动前后对 217 条问句 + 20 条代表问句，`build_ilike_terms` / `build_ilike_pattern` / `build_fts_query` 输出逐字节相同（**先存快照再改代码**，否则是循环论证）；
- `select_nav_terms` 表驱动 ≥8 例：与 query 双向包含去重、已在原问句、alias 重复、超 FTS 预算整词跳过、长词优先、全序可复现、停用词、空页；
- 常量一致性单测（含跨模块上限）。

**回滚**：`git revert` 段 A 即可，无数据面。

### 段 B · 导航 SQL 与两路接入

**改动面**：`services/wiki/navigate.py`（`_WIKI_NAV_SQL` + set_config 三段式 + rollback + `_classify`）；`services/retrieve/__init__.py`（第 247 行后插入，第 271 / 285 行各加一个关键字实参，`path_stats` 两键）；`infra/metrics` 无改动（复用 `incr` / `observe_ms`）。

**验收**：
- 真库探针：命中 / 不命中 / 四类越权 / `status` 三态 / 口径不匹配 / 超时注入 / 成功路径 `current_setting('statement_timeout')` 复原；
- 单测：关闭态 `session.execute` 计数中导航 SQL = 0、wiki 模块未 import；`:needle` 仍等于 `ilike_terms[0].lower()`；
- `EXPLAIN` 记录一次：证明 `idx_wiki_pages_kb` 参与过滤、`chunks` 侧走索引，而不是 `Seq Scan` 全表。

**回滚**：`WIKI_ENABLED=false`（秒级）；或回退段 B 代码。

### 段 C · 评测与回归

**改动面**：`scripts/run_eval.py`（`_warn_caliber_drift` 加 `wiki_enabled`；`wiki_nav` 三数打印 + `eval_runs.caliber["wiki_nav"]`；顺手修 §6-C3 的陈旧 docstring）；`services/eval_service.py`（`EvalReport` 加 `wiki_nav_*` 三个只记录字段）；`tests/wiki/test_nav_acl.py`（真库用例）；fixture 页脚本（`N4` 批准后）；全量 217 条 A/B + `scripts/loadtest_chat.py` P95 对照。

**验收**：§2.6 全表 + 假绿前置门槛 + 越权回归全绿 + 压测增幅 ≤ 1%。

**回滚**：段 C 只改观测，回退不影响线上行为。

> **三段之间的硬约束**：段 B 完成前**不得**开启 `WIKI_ENABLED=true` 跑评测（页还没被读，跑了也是假绿）；段 C 的 fixture 页**不得**残留进生产库（第 3 步探针已建立「合成数据跑完清理 + SQL 复核」纪律）。

---

## 5. 需管家节点裁决的新决策清单

| # | 选择题 | 本文建议 | 依据 / 代价 |
|---|---|---|---|
| **N1** `[待裁决]` | **`key_terms` 归一落点**：① 读侧 `lower + strpos` 字面子串（不动第 3 步、零迁移、零重建）vs ② 写侧新增 `key_terms_norm JSONB` 列 + GIN + `--recaliber` 回填，改用 `jsonb ?\|` 精确匹配（迁移 `0027`） | **选 ①**，把 ② 登记为 §2.1.5 的规模触发升级 | ① 零部署变更、零重生成，且**语义上更贴合**（查询词元本来就是短语的子串）；② 的收益是索引加速，但当前页数 ≈ 文档数（≤10³，F15 实测 1 页），收益看不到；代价是迁移 + 全量重建页 + §7.3 变更后果矩阵新增一行 + 三处提示文案同步。真要精确匹配，必须先解决 F2 的归一，否则 `?\|` 结构性 0 命中 |
| **N2** `[待裁决]` | **超时值放哪**：`settings.wiki_nav_timeout_ms`（按 §9.5 判据：超时属部署环境值）vs `decisions.WIKI_NAV_TIMEOUT_MS: Final[int]`（不动配置指纹） | **选 settings**（守 §9.5 判据），但必须一次性同步 4 处：`settings.py` / `config_profile` 跨角色白名单 / DEPLOY.md §3.5 变更后果矩阵 / 启动横幅与管理端能力页的「改了要重做什么」 | 选 `Final` 的代价是与 §9.5 判据公开偏离（需在台账写明理由），好处是零配置面变更；选 settings 的代价是 `config_fingerprint` 变化 → **历史评测基线的 `config_fingerprint` 不同**（`_compare_baseline` 按 `case_fingerprint` 比对，不受影响，但报告要能解释） |
| **N3** `[待裁决]` | 一组**未标定的阈值**：`WIKI_NAV_MAX_FTS_TOKENS=6` / `MAX_TERM_CHARS=12` / `MAX_PAGES=1` / `hit_cases ≥ 20` | 按本文默认值落地，标注为「先验值，首轮实测后重设」；**MUST NOT 因评测不达标临时下调/上调**（同主文档 §20-U10 的纪律） | 这四个值都没有历史分布可依（F15：现在只有 1 页）。改它们 = 基线不可比 → 走代码评审 + 重跑 217 条 |
| **N4** `[待裁决]` | 是否允许为 217 条评测**构造合成 wiki 页 fixture** | **允许**，但限「单独 kb 或事务内、跑完清理、报告标注含合成页」；优先路径仍是 `rebuild_wiki --all` 补真实页 | 不构造则 §2.6 ②-b 的 `hit_cases ≥ 20` 极可能达不到，验收无法给出有效结论。第 3 步 tester 已有「合成数据 + 清理 + SQL 复核」的完整先例 |
| **N5** `[待裁决]` | 是否新增 `WIKI_NAV_ENABLED` 独立开关（只回退第 4 步、保留导航页） | **不新增**（本文默认假设，与主文档 §13 第 4 步 / 第 5 步同纪律）；若需要细粒度回滚，用「回退段 B 代码」而非开关 | 多一个开关 = 多一份配置组合、多一处 `config_fingerprint` 字段、三处提示文案要重写；而主文档已把 `WIKI_ENABLED` 定义为整体能力 |

**哪些是「已确认契约的细化」**（不需裁决，coder 可据本文直接编码）：用当轮 `query` 的 `build_ilike_terms` 作输入、`_WHERE_BASE` EXISTS 形状、`pushdown` 复用、fail-open 三档、`path_stats` 两键、关闭态零 SQL、两路 keyword-only 默认参、零迁移零索引、§2.7 回滚三层证据、§2.6 关闭态与越权回归。

**哪些是「新决策」**（录入台账即可）：字面子串匹配规则、独立预算 + 二级 FTS 预算、单页、不新增顶层降级字段、指标与分桶口径、假绿前置门槛、8 个 `Final` 常量、p95/增幅预算。

---

## 6. 与主文档 v1.2 / 第 3 步已实现代码的冲突与待同步项（单列）

| # | 冲突 / 缺口 | 性质 | 处置 |
|---|---|---|---|
| **C1** | 主文档 §9.5 把 `WIKI_KEY_TERMS_MAX` / `WIKI_SECTION_MAX_CHARS` 列为 `decisions.py` 新增 `Final`；第 3 步实际把生成侧常量放在 `services/wiki/prompts.py`（F11），`decisions.py` 里没有它们 | 已交付实现与主文档常量投影**不一致** | 本步不追改第 3 步。新常量仍放 `decisions.py`（它们影响检索口径，符合 §9.5 判据）；跨模块一致性用单测钉，禁止 `self_check` 反向 import 服务层。建议管家节点在阶段收尾统一修主文档 §9.5 表格 |
| **C2** | 主文档 §8.3 注「`key_terms` 已 sorted 去重，供查询扩展」，但**未定义归一与匹配口径**；实际落库是 LLM 原文（F2），查询侧是 NFKC+lower（F3） | **设计缺口**（不是代码 bug） | 本文 §2.1.2 补齐；`N1` 决定长期形态。**这是第 4 步能不能工作的前提，不是优化项** |
| **C3** | `scripts/run_eval.py` 模块 docstring 第 33-34 行仍写「`graph_unique` 的分母口径仍在裁决中（当前取三路候选池）」，与 v1.2 + `d6ff0f4` 已落地的「交付上下文口径」矛盾 | 文档陈旧，与本步验收链路直接相关 | 段 C 顺手改 docstring（**不改任何逻辑**）；在改之前，验收报告 MUST 声明「以主文档 v1.2 §9.3 为准」。**不改主文档** |
| **C4** | `build_fts_query` 别名路径的静默截断（F6）：query 窗口达 24 时 lexicon 别名全部被吞且不报错 | **既有缺陷**；N6 裁决**本步随 N6 一起修** | 本文要求 wiki tokens **不得复用该行**（§2.2.4），走独立预算分支；F6 本身随 N6 一并修复 —— lexicon 别名也纳入独立预算，与 `WIKI_NAV_MAX_FTS_TOKENS` 范式统一（见 §2.2.4 / §7-U-C），须带 Golden 快照 + 两条「达上限 × 传别名」红断言转绿 + 全量复跑 |
| **C5** | 主文档 §7.3 第 8 行「关 wiki → 检索不扩展词元」在第 3 步是**空承诺**（检索根本不读 wiki），第 4 步才真正兑现 | 文案无需改，义务是新的 | §2.7 把它列为本步验收义务；DEPLOY.md §3.5 的「开 wiki 要重建页、不用重跑解析」结论**依然成立**（本步不新增数据准备步骤），无需改三处提示 |
| **C6** | dev 真库仅 1 页 wiki（F15），217 条用例几乎不会触发扩展 | 数据现状 | §2.6 ②-b 假绿防线 + `N4` |
| **C7** | 主文档 §13 第 4 步写「`ilike_terms` / `tsquery` 构造处各加一处」；本文决定**原 `ilike_terms` 集合冻结**，只在 `build_ilike_pattern` / `build_fts_query` 的**输出构造处**加参数 | 细化，非冲突 | 主文档措辞是「构造处」的概称；冻结原集合是 §2.2.1 的硬约束（防基线劣化）。若把「各加一处」理解成「往 `ilike_terms` 列表里 append」，会踩 C7 的致命方案 |

---

## 7. 魔鬼代言人（反例 ≥3，逐条给缓解）与未知区

**反例 1 · 「额外 OR 分支会把 ILIKE 路变成噪声源，命中率反而降」**
ILIKE 是顺序扫描（F4 注释），多两个 OR 分支就多一批命中行 → RRF 候选池变大 → 可能把原本 top-8 的好片挤出去 → 命中率下降。**缓解**：只在命中页时扩、≤2 词元、长词优先、与 query/alias 双向去重、ASCII≥4 / CJK≥2 的过滤；关键是 **`:needle` 与原 3 个词元一个不改**（排序键冻结）；217 条命中率不降是**硬门禁**，不达标即 `WIKI_ENABLED=false` 回滚。若仍不达标，正确处置是砍掉第 4 步，不是调参硬撑（同 §14.6 归因表第二行的纪律）。

**反例 2 · 「`strpos(lower(...))` 走不了索引，大库会拖垮在线检索」**
**缓解**：候选先由 `idx_wiki_pages_kb` + `EXISTS` 四闸门收敛到「可见页」，`jsonb_array_elements_text` 只在这些页上展开；硬超时 50ms + 超时 rollback 保证单次请求不被拖死；SLO p95 ≤ 10ms + `WIKI_NAV_CANDIDATE_PAGE_GUARD=5_000` 触发 GIN 升级评估（§2.1.5）。**诚实边界**：本步没有给出 1 万页以上的实测 p95（U-A），所以升级条件是观测触发式而非拍胸脯保证。

**反例 3 · 「只在命中页时扩 + dev 只有 1 页 = 评测必然假绿」**
**缓解**：§2.6 ②-b 的 `hit_cases ≥ 20 且 expanded_cases > 0` 前置门槛，不满足则本轮评测**结论无效**；报告强制打印三个数；`N4` 提供补页/合成页两条路径。这是全文最容易被「217 条全绿」掩盖的一条。

**反例 4 · 「`set_config` 溢出到后续 ILIKE/FTS，重演 `e6b6a94`」**
导航查询的位置**比图召回更靠前**（在 ILIKE/FTS 之前），溢出影响面更大 —— 而 ILIKE 恰恰是顺序扫描、最容易撞超时。**缓解**：读原值 → 设置 → 查询 → `finally` 语义还原原值；降级路径 `rollback()` 后设置随事务消失；单测断言还原值 + 后续 SQL 正常返回（§2.4）。

**反例 5 · 「wiki 是 LLM 产物，把它的术语灌进检索 = 幻觉影响召回」**
红线 A 的结构性保证：wiki 只产出**词元**进 `build_ilike_pattern` / `build_fts_query`；召回结果仍是 `chunks` 表的**原文行**，`current_chunks` 的四类来源里没有任何 wiki 文本；`summary` / `sections` 连读都没被读（SQL 只 SELECT `document_id, key_terms`）。术语错的最坏后果是「多召回几片无关原文」，会被既有 L1 阈值与 RRF 排序自然淘汰，**不会变成「模型被告知了一个假事实」**。口径双过滤（gm/pv）再挡一层旧页。

### 未知区（诚实标注，不脑补）

| # | 未知 | 需要什么才能决 |
|---|---|---|
| U-A | 页数 > 5_000 时导航 SQL 的 p95 未实测（dev 只有 1 页，F15） | 造 1k / 5k / 2 万页合成数据，测 `rag.retrieve.wiki_nav` p95 与 `EXPLAIN` 计划 |
| U-B | 「长度降序」对 wiki key_terms 是否优于「按章节顺序 / 按出现频次」未验证 | 补齐页后，用可答类 111 条对比两种排序的命中率与 `expanded_terms` 分布 |
| U-C | F6 的 lexicon 别名静默截断对现网影响面未完全量化（C4 报告已量化「达上限」占比，但「剩余容量 < 扩展词元数」的**部分截断**子集大小仍属未知区） | 影响面已由 `02-design/C4-别名静默截断影响面量化.md` 评估；**本步随 N6 一起修**（独立预算范式统一），残余未知区需真库精确统计与全量 A/B 复跑关闭 |
| U-D | `lower()` 不做 NFKC，全角术语 / 型号分隔符写法的失配率未知（针 `ax-3000` 打不中页里的 `AX3000`） | 扫一遍已生成页的 `key_terms`，统计含全角、异体字、无连字符型号的比例；非 0 则评估 `normalize(text, NFKC)`（需确认部署 PG 版本支持）或写侧归一；型号分隔符可考虑走 `build_ilike_pattern` 的 `[-_ ]?` 同款展开，但本步不做 |
| U-E | 多轮链式检索下 `wiki_nav_hits` / `wiki_nav_terms` 是**按轮累加值**（F17），与逐请求值不同 | 与 §10.1-R2 同型：审计看趋势，精确值用逐请求统计；验收报告需注明口径 |
| U-F | `WIKI_NAV_MAX_TERM_CHARS=12` 会丢掉长英文术语（如 `Vue3 Composition API`，19 字符）的比例未知 | 补齐页后统计被长度规则丢弃的术语占比；若 >20%，重新标定该常量并**重跑 217 条基线** |

---

## 8. 拟入库知识条目（随摘要回传主理人，由 wiki-creator 编译进 `.wiki-creator/`）

> 三条均为**可复用模式**（机制层，不硬编码本项目框架）；按 `references/knowledge-policy.md` 的防污染标准整理，附「来历」与「何时不用」。架构师不直接写包内 / 底座。

**条目 A · 「条件触发型能力」的评测假绿防线**
当一项可选能力只在「某条件命中时才生效」（如只在命中 wiki 页时才扩词），全量评测的「不劣化」结论**必须附带触发次数前置门槛**，否则测的是关闭态。判据形态：`hit_cases ≥ N` **且** `expanded_cases > 0`，不满足即判本轮评测**无效**（不是「通过」也不是「失败」）；报告必须打印触发数、生效数、产出量三个数。
*何时不用*：能力在每次请求都无条件生效时（如固定追加一个 SQL），无需此门槛。
*来历*：本项目第 4 步 dev 库仅 1 页 wiki、217 条用例几乎不触发扩展；与 v1.2 §9.8 推翻 `graph_unique`「结构恒 0 的判据」属同一类错误（没测到 ≠ 没影响）。

**条目 B · 查询扩展必须独立预算，不能以截断主查询词元为代价**
在已有标定过的词元上限（如 `ILIKE_MAX_TERMS=3`）上追加扩展词时，三种做法只有一种成立：① 追加后重排截断 → 会挤掉原 query 词元（长词优先排序下尤甚），直接劣化基线；② 预留名额 → 主动削弱主路，且造成开/关两态行为不一致（评测不可复现）；③ **主词元集合冻结 + 扩展走独立配额**（正确）。
二级约束：若扩展词元会被展开成索引词元（中文 2-gram 使 1→N），必须再设**索引词元预算**，且按「整个顶层词元」申请、装不下就整词跳过 —— 截半截索引词元会产生静默伪匹配。
*何时不用*：扩展本身就是替换主查询（而非补充）时。
*来历*：本项目 `ILIKE_MAX_TERMS=3` 的实测标定（3→4 只多 1 条、精度 66%→54%）与 `LEXICON_MAX_ALIASES_PER_KEY=2` 的教训；`build_fts_query` 别名路径已有「追加后被 `[:24]` 静默吞掉」的既存反例。

**条目 C · LLM 产出的标签类字段必须先定义归一与匹配口径**
写入侧是「LLM 原文短语」（大小写/全角/粒度不受控）、查询侧是「归一化切分词元」时，`jsonb ?|` 一类**精确包含**匹配会结构性失效，且失效形态是静默 0 命中 —— 与「库里没有相关数据」完全不可区分。可选解：读侧字面子串（`strpos(lower(...))`，无通配语义、无注入面，代价是无法走索引）或写侧归一列 + GIN（代价是迁移 + 回填 + 口径重建）。
选择判据是**规模**：候选行数 ≈ 文档数（≤10³）用读侧；上万且 p95 超预算才升级写侧。
*何时不用*：标签由确定性规则生成、两侧共用同一归一函数时。
*来历*：第 3 步 `wiki_pages.key_terms`（`_clean_terms` 只 `.strip()`/截长/去重）与 `build_ilike_terms`（NFKC + lower + 疑问词切段）的形状差异；第 2 步 `graph_entities.name_key` 走的是「写入与链接同源归一」，才可以用精确/前缀匹配。

---

## 9. 给管家节点的一句话结论

**第 4 步能编码，但它的验收比实现更危险：dev 库只有 1 页 wiki，「217 条不劣化」在扩展一次都没触发时会全绿 —— 所以本文把 `hit_cases ≥ 20` 设成前置门槛，并把「`key_terms` 未归一」这个会让精确匹配结构性失效的设计缺口（C2）连同 5 个待裁决项一起摆到桌面上，请裁决后再让 coder 开工。**
