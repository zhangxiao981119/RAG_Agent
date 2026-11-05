# 第 3 步 wiki 生成 + 读接口 — 独立对抗验证报告

- **验证人**：agent-harness-tester（独立于编码者，未参与实现）
- **验证日期**：2026-11-05
- **验证对象**：第 3 步 wiki 交付 commit 链 `3b59d82` → `81c0040` → `a79426f` → `c2b525c` → `feca605` → `1dac4c1` → `2d3accf`
- **方法**：证伪不是确认。真库行为探针 + 三处变异测试（pytest 级 + 行为级）+ 迁移一次性重放 + 全量回归。全部证据为实际执行输出。
- **结论**：**通过**。P0=0，P1=0，P2=3（均不阻断交付）。

---

## 一、总评

| 维度 | 结论 |
|---|---|
| 404 三合一（存在性防探测） | ✅ 真库实证：三类 404 状态码、响应体逐字节、content-type 全一致 |
| 读接口 ACL（列表/详情双口径） | ✅ 真库实证 + 变异击杀（删 EXISTS → 越权 200 / 还原 → 404） |
| DDL 与 §8.3 一致性 | ✅ 逐字比对 + 一次性重放（up→down→up）+ 重放库/真库 pg_dump diff 为空 |
| purge_derived 同事务删页 | ✅ rollback DB 级实证 + 变异击杀 + 缺表守卫（to_regclass）测试钉住 |
| 幂等（同 hash 不调 LLM / 口径变更重建） | ✅ 桩 LLM 计数实证；单测走真实函数与真实 content_hash（无假覆盖） |
| LLM 失败留痕 | ✅ reasoning-only / 非法 JSON / LLMError 三态均抛 WikiBuildError 且落 failed 行 |
| 关闭态零痕迹 | ✅ 两端点 404 且响应体与其它 404 无差异；`grep retrieve/ 无 wiki import` |
| 全量回归 | ✅ 520 用例全过（exit=0），ruff All checks passed |
| commit 纯度 | ✅ 7 个 commit 共 29 个文件，全部落在 wiki 范围，无夹带 |

**缺陷计数：阻断 0 / 缺陷 0 / 建议（P2）3。**

---

## 二、逐检查点证据

### 检查点 1 — 越权（重点）：详情 404 三合一 + 列表 ACL ✅

**真库行为探针**（一次性容器，合成数据 + 结束清理，TestClient 走真实 SQLAlchemy 会话与真实 SQL）：

```
1. 详情 404 三合一 + 响应体无差异（真库 + TestClient）
  [✔] 全权主体看 ready 页 → 200
  [✔]   disclaimer 逐字返回
  status!=ready: status=404 body={"detail":"NOT_FOUND"}
  页不存在:      status=404 body={"detail":"NOT_FOUND"}
  无可见 chunk:  status=404 body={"detail":"NOT_FOUND"}
  [✔] 三类 404 状态码一致
  [✔] 三类 404 响应体逐字节一致
  [✔] 三类 404 与 200 的 content-type 一致（无类型差异可探测）

2. 列表 ACL（kb 成员 + 文档可见性过滤）
  [✔] 全权成员看到合成页
  [✔] public-only 成员看不到 dept-only 文档的页
  [✔] public-only 也看不到 fail 页
  [✔] 非 kb 成员 → 403（复用 get_kb 口径）
  [✔] 不存在的 kb → 403（与 get_kb 同序，无存在性泄漏）
```

合成数据构造：dept-only chunk（acl_tags=['dept:/总部/技术中心/后端组/']）的文档挂 ready 页；public chunk 的文档挂 failed 页。public-only 主体（subjects=['public']）对 dept-only 文档**零可见 chunk**。

**变异测试（三处，逐处击杀后 git checkout 还原，`git status` 干净自证）**：

| 变异 | 手法 | 结果 |
|---|---|---|
| M1（详情） | `_PAGE_VISIBLE_SQL` 删 `AND EXISTS (…{where_base}…)` | pytest 级：`FAILED test_detail_returns_page_with_disclaimer_and_caliber`（SQL 文本断言）；**行为级真库击杀**：无权用户（subjects=['public']）访问 dept-only 文档的页，变异下 `BEHAV_RESULT status=200`（越权成立），还原后 `BEHAV_RESULT status=404` |
| M2（列表） | 列表 SQL 删同款 EXISTS | `FAILED test_list_returns_only_visible_documents`，还原后绿 |
| M3（purge） | `DELETE FROM wiki_pages WHERE document_id=:id` → `SELECT 1` | `FAILED test_purge_deletes_wiki_pages_even_when_graph_disabled`，还原后绿 |

> M1 行为级击杀是本次验证的关键证据：EXISTS 四闸门过滤是**真实生效的访问控制**，不是摆设。变异期间未触任何真实页面（真库页 10538f14… 只读抽查）。

### 检查点 2 — DDL 逐字比对 + 迁移一次性重放 ✅

**静态比对**（0026_add_wiki_pages.py vs 设计文档 §8.3）：16 列名/类型/默认值、`ck_wiki_pages_scope CHECK (scope='doc')`、`ck_wiki_pages_status CHECK (status IN ('ready','stale','failed'))`、`uq_wiki_pages_document UNIQUE(document_id)`、`idx_wiki_pages_kb (tenant_id,kb_id,status)`、kb_id/document_id 双 CASCADE + tenant_id 无级联——全部一致。

**一次性容器重放**（临时 postgres:16 容器，alembic 0010→0026 全链）：

```
Running upgrade 0025 -> 0026, 新增 wiki_pages 一表（LLM wiki 导航页，第 3 步）。
0026 (head)
=== DOWNGRADE 0025 ===
AFTER_DOWNGRADE to_regclass(wiki_pages) = None      ← downgrade 真删表
=== UPGRADE head (round2) ===
AFTER_REUPGRADE to_regclass(wiki_pages) = wiki_pages ← 重升重建
```

**重放库 vs 真库 schema 比对**：两库 `pg_dump --schema-only --table=wiki_pages` 输出**逐字一致**（仅 owner 名不同，预期）。DDL 见附录。

### 检查点 3 — purge_derived 同事务删页 ✅

真库 rollback 实证（同一事务内查页数 → rollback → 再查）：

```
purge 返回 0（图关系数，关图=0）；事务内页数=0；rollback 后页数=1
  [✔] purge 在事务内删页（事务内 0）
  [✔] rollback 后页恢复（同一事务的 DB 级证据）
  [✔] commit 后页真的删除（行消失实证）
```

变异 M3（删 wiki DELETE 段）被 `test_purge_deletes_wiki_pages_even_when_graph_disabled` 击杀。缺表守卫（`to_regclass` 探测而非 wiki_enabled 开关）有独立测试钉住（`test_purge_skips_wiki_pages_when_table_missing`）。

### 检查点 4 — 幂等 ✅

```
4. 幂等：同 hash 不调 LLM；口径变更 → 重建（合成文档 + 桩 LLM 计数）
  [✔] 第一次生成 succeeded 且调了 1 次 LLM
  [✔] 第二次 skipped_idempotent 且不再调 LLM
  [✔] 页 generator_model = 桩口径
  [✔] 口径变更 → 重建（再调 LLM、页口径更新）   ← probe-model-v2/wprompt-v2 实际落库
```

**假覆盖抽查**：`test_idempotent_semantics` / `test_build_and_store_idempotent_hit_returns_distinguishable_value` 均走真实 `prepare_wiki_input` / `build_and_store`，content_hash 由真实函数计算（第一遍无页跑出真 hash、第二遍用它构造已存在页），非复制判定式。合格。

### 检查点 5 — LLM 失败留痕 ✅

```
5. LLM 失败留痕（reasoning-only / 非法 JSON / LLMError → failed 行）
  [✔] reasoning_only: 抛 WikiBuildError 且留 failed 行  raised=True row=failed
  [✔] bad_json:       抛 WikiBuildError 且留 failed 行  raised=True row=failed
  [✔] llm_error:      抛 WikiBuildError 且留 failed 行  raised=True row=failed
```

### 检查点 6 — 关闭态零痕迹 ✅

```
3. 关闭态：WIKI_ENABLED=false 两端点 404 且响应体与其它 404 无差异
  [✔] 关闭态两端点 404
  [✔] 关闭态 404 响应体与三类 404 逐字节一致
```

静态：`grep -rn "from app.services.wiki|import wiki" app/services/retrieve/` = 空（本步 wiki 不进检索）；worker `run_wiki_build` 开头 disabled 短路；`scripts/rebuild_wiki.py` WIKI_ENABLED=false 时 sys.exit fail-closed（`test_rebuild_wiki_script_*_fail_closed_when_disabled` 钉住）。

### 检查点 7 — 全量回归 + ruff ✅

一次性容器（工作区挂载）实际执行：

```
pytest tests/wiki tests/graph tests/chat tests/services tests/retrieve tests/eval tests/config
→ exit=0，输出 7×72+16 = 520 个通过点至 [100%]，零 F/E
ruff check . → exit=0，All checks passed!
```

### 检查点 8 — commit 纯度 + 测试无假覆盖 ✅

- 7 个 commit 共 29 文件（5/4/7/4/3/2/4），全部落在 alembic、wiki 模型/service/api/worker/脚本/tests 与 capabilities 范围，无夹带（逐 commit `--name-only` 核对）。
- 无假覆盖：端点测试用 TestClient + 依赖覆盖走真实 FastAPI 路由；幂等走真实函数；SQL 语义类断言的缺口见 P2-1。

### 检查点 9 — DEPLOY.md §3.5.1 过时行（记录，不改）✅

确认三处过时描述仍在（`code/docs/DEPLOY.md` §3.5.1）：
1. `WIKI_ENABLED` 行：「**本版未实现**：置 true 会因 wiki_pages 表不存在…第 2 步接入」；
2. 脚注：「wiki 的 4 个…本版未实现」；
3. `GRAPH_ENABLED` 行：「**本版只写不读**…（第 2 步）」。

本步已实现读接口，以上均为事实性过时；按分工由 team-lead 安排刷新，测试侧仅记录不改。

---

## 三、问题清单

**P0 / P1：无。**

**P2-1（建议）端点 ACL 的钉子是「SQL 文本断言」，不评估 SQL 语义。**
`tests/wiki/test_page_endpoints.py` 用 FakeSession，可见性判定靠 `assert "EXISTS" in last_sql()` 这类文本断言——SQL 换一种等价写法（如改 JOIN / 改名 CTE）会假红；反过来若有人把过滤逻辑挪出 SQL（Python 侧过滤），FakeSession 恒返空列表的行为级用例可能漏防（实测 `test_detail_404_when_no_visible_chunk` / `test_list_empty_when_no_visible_pages` 在 M1/M2 变异下不变红）。行为级防线本报告用真库探针补齐（变异击杀成立），建议后续在 tests/wiki 补 1~2 个真库集成用例（或把本报告探针脚本收编进仓库）。

**P2-2（建议）wiki 页重建会更换页 id（DELETE+INSERT），外部不得缓存 page id。**
实测佐证：coder 早前真跑产物页 `b242eed4…` 已不在库，同文档现页为 `10538f14-…`（ready/gpt-4o-mini/wprompt-v1）——内容/口径变更重建按设计删旧插新。这是设计使然（一文档一页 uq 兜底），但任何以 page id 为引用的外部系统（收藏、分享链接、审计引用）都会失效。建议对上游约定：**引用一律用 document_id**，page id 仅作瞬时主键。

**P2-3（建议）DEPLOY.md §3.5.1 三处「本版未实现」已过时**（见检查点 9），需随本步交付刷新，否则运维会因文档不敢开 WIKI_ENABLED。

---

## 四、真库已建页抽查（只读）

```
wiki_pages 唯一真页：10538f14-5be8-4e42-9d48-20de69df3a9b
  status=ready  generator_model=gpt-4o-mini  prompt_version=wprompt-v1
  来源文档：64888806-…（Vue3 Composition API 完整解析（面试版）.pdf）
  source_chunk_ids 6/6 存活（unnest LEFT JOIN chunks 全命中，无死 id）
  sections[0].chunk_ids=["4151be38-…"] 为有效 chunk
```

---

## 五、实际执行的动作清单

1. 静态：7 commit 纯度核对（`git show --name-only` 逐个）；设计文档 §8.3/§8.5/§9.6 权威基线提取；wiki.py / build.py / purge.py / graph_job.py / rebuild_wiki.py / 0026 迁移全文审阅；DEPLOY.md §3.5.1 过时行确认。
2. DDL：一次性 postgres:16 容器全链重放 0010→0026→downgrade 0025→re-upgrade；两库 pg_dump schema-only 比对。
3. 真库主探针（23 项断言全过）：404 三合一 / 列表 ACL / 关闭态 / 幂等（桩 LLM 计数）/ 失败留痕三态 / purge rollback+commit 实证 / 真页抽查。全程合成数据，结束清理（真库终态：1 真页、0 探针残留，SQL 验证）。
4. 变异×3：详情删 EXISTS（pytest 红 + 行为级真库击杀 200→404）、列表删 EXISTS（红）、purge 删 wiki DELETE（红）；逐处 `git checkout` 还原，`git status --porcelain` 干净自证。
5. 全量回归 520 用例 exit=0 + ruff All checks passed（一次性容器、工作区挂载、变异全部还原后执行）。
6. 现场清理：mutrun / mig-replay-pg 临时容器删除；探针泄漏数据（早期崩溃运行遗留 10 行）已从真库清除并复核。

## 附：探针关于自身构造错误的记录（透明化）

调试期间探针曾出现 5 次假失败，全部为探针自身构造问题并已修正（chunks 表 NOT NULL 列补齐、跨事件循环改 NullPool、依赖覆盖全局性导致身份串用、FakeLLM chunk 指向、purge 签名/返回值语义误读）——**均非被测代码缺陷**，最终 23 项断言在正确构造下全部通过。
