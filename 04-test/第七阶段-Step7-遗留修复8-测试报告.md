# 第七阶段 Step7 遗留修复 8（G-8 / G-9）测试报告

> 阶段：P3 独立对抗性验证
> 角色：测试工程师（独立于编码者，不修改业务代码）
> 日期：2026-11-24
> 环境：Windows / PowerShell 5 / 系统 `python`；真库 `localhost:5433` → `code-postgres-1`（`kagent_test`）
> 被测状态：P2 已完成、未 commit（工作区）
> 结论：**PASS**（0 阻断 / 0 缺陷 / 2 建议）

---

## 一、改动清单（工作区，未提交）

### 1. 生产代码（3 个文件）

| 文件 | 改动 |
| --- | --- |
| `app/config/decisions.py` | 新增 `TABLE_RECALL_ENABLED=True`、`SECTION_RECALL_ENABLED=True`（含中文说明，L810-813 / L824-827） |
| `app/services/retrieve/__init__.py` | G-8：tsquery 路计算并上报 `rag.lexicon.fts_alias_dropped`（L460-477）；G-9：表/段召回分别用常量门控（L761 / L834） |
| `app/services/scenario/policy_spec.py` | 两键 `default_semantics` 改为「常开…写入闭集为 3，不可按场景覆盖（wired_in_step=4）」；`wired_in_step` 与闭集未变 |

### 2. 测试（3 改 + 1 新）

| 文件 | 改动 |
| --- | --- |
| `tests/retrieve/test_fts_alias_dropped.py` | 新增（原 4 条；P3 补 2 条 → 6 条） |
| `tests/retrieve/test_table_recall.py` | 新增 4 条 G-9 用例（常量默认值、表关、段不受影响、双关） |
| `tests/retrieve/test_section_recall.py` | 新增 2 条 G-9 用例（段关、表不受影响） |
| `tests/scenario/test_resolver_policy.py` | 更新语义断言；P3 补 2 条参数化「写入被拒」用例 |

### 3. 文档（2 个文件 / 3 处）

| 文件 | 改动 |
| --- | --- |
| `02-design/C4-别名静默截断影响面量化.md` | 头部追加第 7 步遗留修复关闭行（1 行） |
| `02-design/第二阶段-Step1-2-详细设计.md` | E 表 #15/#16 追加「默认常开」；§G 表 G-8/G-9 追加关闭说明（8 行） |

> 备注：任务描述为「3 份文档」，实际改动为 **2 个文件、3 处**（C4 头部 + Step1-2 的 E 表与 G 表）。历史测试报告（`04-test/` 既有文件）未被修改。

### 4. 明确排除的历史脏文件（内容与 HEAD 一致，仅 stat；不计入、不提交）

`app/api/sync.py`、`app/services/sync_service.py`、`scripts/rebuild_wiki.py`、`tests/chat/test_ask_with_image.py`、`tests/wiki/test_worker_and_script.py`。
验证：`git diff --numstat -- <上述 5 文件>` 输出为空（无内容差异）。

---

## 二、必做验证 A：git diff 审查结论

- 3 个生产文件差异与任务描述逐字一致，无越界改动。
- 4 个测试文件只做新增/语义更新，未删除、未弱化既有断言。
- 2 个文档仅追加关闭说明，未改写历史结论主体。
- `app/api/*`、`scripts/*` 等 5 个脏文件无内容差异，已排除。
- 迁移目录无任何变更（`git status --porcelain code/apps/api/migrations` 为空）。

---

## 三、9 条设计承诺逐条证据

| # | 承诺 | 证据 | 判定 |
| --- | --- | --- | --- |
| 1 | 默认配置下检索行为与改动前等价 | 常量默认 True；`if decisions.TABLE_RECALL_ENABLED and hit_group_ids` / `if decisions.SECTION_RECALL_ENABLED and hit_sections` 在 True 时与原 `if hit_group_ids` / `if hit_sections` 短路结果一致；tsquery 路在 `KEYWORD_TSQUERY_ENABLED=True` 时仍调用同一 `build_fts_query(query, alias_tokens, extra_index_tokens=wiki_fts_tokens)`，`alias_map` 为空时 `alias_tokens=[]`（旧实现 `alias_index_tokens({})==[]` 等价）。既有召回/预算/排序用例全绿（命令 1/2） | 通过 |
| 2 | G-8 计数口径准确 | 8 个去重别名、预算 6 → 1 次打点、值 2（`test_alias_over_budget_reports_dropped`）；恰好 6 个 → 0（`test_alias_exactly_at_budget_silent`）；`ALIAS_EXPANSION_ENABLED=False` → 0（`test_alias_expansion_disabled_silent`）；`KEYWORD_TSQUERY_ENABLED=False` → 0（P3 补测 `test_tsquery_disabled_silent`）；无别名 → 0（`test_no_alias_silent`）；重复项按去重计（P3 补测，原始 12 → 去重 8 → 值 2） | 通过 |
| 3 | 指标从检索入口发出 | 打点在 `RetrievalService.retrieve` 内（`retrieve/__init__.py:474`）；测试统一调用 `RetrievalService().retrieve(...)` 并截获 `retrieve_mod.metrics.incr`，未在测试内复制公式 | 通过 |
| 4 | 表开关只关表回捞 | `test_table_recall_switch_off_skips_sql`：`recall_calls == []`、`table_recall_chunks == []`、主 `chunks == [hit]`；`test_section_recall_unaffected_when_table_off`：`section_calls` 非空、表回捞为空 | 通过 |
| 5 | 段开关只关章节补全 | `test_section_recall_switch_off_skips_sql`：`section_calls == []`、`section_recall_chunks == []`、主召回不变；`test_table_recall_unaffected_when_section_off`：`recall_calls` 非空、段补全为空 | 通过 |
| 6 | 两开关同关 | `test_both_recall_switches_off`：两处为空、两路 SQL 均零调用、主 `chunks` 与默认路径一致 | 通过 |
| 7 | 未扩写入闭集 | `WIRED_KEYS` 仍由 `wired_in_step <= 3` 推导；两键 `wired_in_step=4`；`test_policy_keyset_matches_migration` 钉 4 键；P3 补测 `test_validate_policy_rejects_recall_switches` 断言 `PolicyValidationError`（"尚未接线"/Step 4）；迁移零变更 | 通过 |
| 8 | 语义文本与实现一致 | 文本为「常开：decisions.X=True；当前写入闭集为 3，不可按场景覆盖（wired_in_step=4）」，与常量、闭集、生效步骤逐一对应；`test_undefined_parameters_do_not_invent_defaults` 断言前缀/常量名/闭集措辞/生效步骤 | 通过 |
| 9 | 文档关闭说明属实 | `keyword_text.py:124-194` 确有别名独立预算（`alias_max_terms=LEXICON_FTS_ALIAS_MAX_TERMS`，默认 6）；`tests/retrieve/test_fts_alias_cap.py` 两条交叉用例存在且绿；C4 头部关闭行与代码/用例对得上；历史测试报告未被改写 | 通过 |

---

## 四、必做验证 C：真实执行（退出码与统计）

前置（每次均注入，报告中口令以 *** 代替）：

```powershell
$pw = ((Get-Content "c:\Users\86186\Desktop\AI Agent学习\file_rag_agent\code\.env" | Where-Object { $_ -match '^POSTGRES_PASSWORD=' }) -replace '^POSTGRES_PASSWORD=', '').Trim()
$env:SCENARIO_TEST_DB_URL = "postgresql+asyncpg://user:$***@localhost:5433/kagent_test"
```

> 说明：`pyproject.toml` 的 `addopts = "-q --strict-markers"` 与命令行 `-q` 叠加成 `-qq`，会隐藏末尾统计行。为取得可核对的计数，实际执行统一加 `-o addopts="--strict-markers" -q`；测试集合不变。

### 命令 1（定向）

```text
python -m pytest tests/retrieve/test_fts_alias_dropped.py tests/retrieve/test_fts_alias_cap.py tests/retrieve/test_table_recall.py tests/retrieve/test_section_recall.py -o addopts="--strict-markers" -q
EXIT=0
29 passed in 0.57s
```
（P3 补测前为 27 passed；补 2 条后 29 passed。）

### 命令 2（模块集）

```text
python -m pytest tests/retrieve tests/scenario tests/config -o addopts="--strict-markers" -q
EXIT=0
830 passed, 2 warnings in 113.08s (0:01:53)
```

### 命令 3（全量）

```text
python -m pytest -o addopts="--strict-markers" -q
EXIT=1
2 failed, 1699 passed, 15 skipped, 11 warnings in 154.28s (0:02:34)
```

---

## 五、失败集合与基线比对

基线（第 8 项完成时）：collected 1702 / 1685 passed / 2 failed / 15 skipped。

最终：collected = 1699 + 2 + 15 = **1716**（= 基线 1702 + 10 条 P2 新增 + 4 条 P3 补测）。

失败 nodeid 集合（与基线逐字相同，无新增）：

1. `tests/test_health.py::test_openapi_contains_health`（`socket.gaierror: [Errno 11001] getaddrinfo failed`）
2. `tests/wiki/test_worker_and_script.py::test_rebuild_wiki_script_fails_closed_when_disabled`（子进程同样因 DNS 解析失败，属已知环境失败）

skip 数：15 → 15，**未用新增 skip 换绿**。结论：失败集合零新增，通过。

> 旁证：P2 首次全量（补测前）为 `2 failed, 1695 passed, 15 skipped`，collected 1712，与「基线 +10」吻合。

---

## 六、必做验证 B：敏感度抽查（改坏即红，逐项恢复并核对 SHA256）

恢复基准（改动完成后、变异前）：

- `app/services/retrieve/__init__.py` = `B1F4D739E51E7959EBF9F2BE669DCBBD321419D839BEED7B99C6B487CC0BBB12`
- `app/config/decisions.py` = `04BC8F40C31A0356BEC4FE1197B7058A7FC6C66327396B4C2EFD7B93BB68692A`

| 编号 | 变异 | 执行 | 结果 | 恢复后 SHA256 |
| --- | --- | --- | --- | --- |
| M1 | 删除 `rag.lexicon.fts_alias_dropped` 打点 | `test_fts_alias_dropped.py` | **1 failed, 3 passed**；`test_alias_over_budget_reports_dropped`：`应恰好打点 1 次，实际 []` | 与基准一致 |
| M2 | `dropped` 公式改为 `len(alias_tokens)`（不去重、不减预算） | `test_fts_alias_dropped.py` | **2 failed, 2 passed**（超预算用例得 8；恰好 6 个用例错误打点 6） | 与基准一致 |
| M3 | 表召回门控短路为 `if False and hit_group_ids` | `test_table_recall.py` | **4 failed, 7 passed** | 与基准一致 |
| M4 | 段召回门控短路为 `if True or hit_sections` | `test_section_recall.py` | **5 failed, 5 passed**（关开关仍发补全 SQL） | 与基准一致 |
| M5 | 两常量默认改 `False` | `test_table_recall.py` + `test_section_recall.py` + `test_resolver_policy.py` | **11 failed, 46 passed** | 与基准一致 |

5 处变异全部被现有/补测用例捕获；恢复后两文件 SHA256 与基准逐字相同，生产代码未被残留修改（`git diff --stat` 仅 3 个生产文件，行数与改动前一致）。

---

## 七、必做验证 D：补测记录（只增不减）

P3 新增 4 条（均已绿，计入最终 1716）：

1. `tests/retrieve/test_fts_alias_dropped.py::test_alias_drop_counts_deduped_terms_not_raw`
   - 四个 4 字别名原始切出 12 个 bigram，去重后 8 个 → 断言恰好 1 次打点、值 = 2。
   - 目的：证明计数按**去重**口径，而非按原始长度（原始口径会错报 6）。
2. `tests/retrieve/test_fts_alias_dropped.py::test_tsquery_disabled_silent`
   - `KEYWORD_TSQUERY_ENABLED=False` → 零打点。
3. `tests/scenario/test_resolver_policy.py::test_validate_policy_rejects_recall_switches[table_recall_enabled]`
4. `tests/scenario/test_resolver_policy.py::test_validate_policy_rejects_recall_switches[section_recall_enabled]`
   - 均断言 `PolicyValidationError`、`field` 正确、文案含「尚未接线」与「Step 4」。

**SQL 零执行验证方式**（不只是结果为空）：表/段用例的 `FakeSession.execute` 分别记录 `recall_calls` / `section_calls`，开关关闭时断言记录列表为空；开关关闭的对照用例同时断言「另一路 SQL 仍被调用」，排除「两路都坏」的假绿。

**场景写入拒绝的现有口径**：`write_service._validated_policy` 将 `PolicyValidationError` 映射为 `400 WRITE_INVALID`（`app/services/scenario/write_service.py:309-316`）；本次未改该映射，补测在 `validate_policy` 主校验层证明两键被拒。

---

## 八、必做验证 E：文档核对

- C4 头部新增行：「F6 独立预算与两条交叉用例已在 N6/第 4 步段 A 落地…本次补 `rag.lexicon.fts_alias_dropped` 打点，G-8 关闭」——与 `keyword_text.py:124-194`、`tests/retrieve/test_fts_alias_cap.py` 现状一致。
- Step1-2 E 表 #15/#16：追加「待定义 —— 默认常开：`decisions.X=True`，当前不开放场景覆盖」，与 `policy_spec.default_semantics` 一致。
- Step1-2 §G：G-8/G-9 行追加「2026-11-24 已关闭」及实现要点；其余历史结论未被改动。
- 历史测试报告（`04-test/` 既有文件）无改动。

---

## 九、发现的问题

### 阻断
无。

### 缺陷
无。

### 建议
1. **Step1-2 E 表 #15/#16 存在同格自相矛盾文本（文档级，不影响运行）**：单元格前半仍是「源码无此开关」，追加句又说「默认常开：`decisions.X=True`」；表下注释仍写「源码里只有上限/预算常量，**没有开关常量**」。按任务要求保留历史结论，但建议在追加句前加「（2026-11-24 更新）」或改写该注释，避免读者误判。
2. **`dropped` 计算存在冗余去重**：`alias_index_tokens()` 已返回去重列表，`len(dict.fromkeys(alias_tokens))` 再包一层虽无行为影响，但可读性上容易让人误以为原始列表含重复。若追求最小表达，可直接 `len(alias_tokens)`；保留现状亦正确（无缺陷）。

---

## 十、未覆盖 / 存疑项

1. **API 层 HTTP 状态码未新增用例**：本次仅验证 `validate_policy` 主校验拒绝；HTTP 映射沿用既有 `400 WRITE_INVALID`，未新增端到端 HTTP 用例（现有 API 测试覆盖通用非法 policy 路径）。
2. **默认等价性为「代码审查 + 既有用例全绿」证据**，未对本次改动做「改动前/后 tsquery 与 SQL 序列」的字节级 Golden 快照；`tests/retrieve/test_golden_wiki_nav.py` 等既有 Golden 用例在命令 2 中通过，可作为间接锚点。
3. **敏感度抽查的 M2 只验证了「错误公式会红」**，未穷举所有错误公式形态（如只减预算不去重）；已覆盖任务点名的两类偏差。

---

## 十一、结论

**PASS**。默认路径行为等价、G-8 打点口径准确且作用于真实检索入口、G-9 两个开关门控隔离正确、写入闭集未扩大；全量失败集合与基线完全一致（2 个已知环境失败），skip 未增加；5 处敏感度变异全部转红并已无损恢复。
