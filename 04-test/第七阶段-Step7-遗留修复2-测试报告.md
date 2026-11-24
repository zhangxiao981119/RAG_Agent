# 第七阶段 Step7 遗留修复2 —— P3 独立对抗性验证报告

- 验证范围：启动自检扫描口径（治理开关分两套 SQL）、`ScenarioRow.tier`、S-10（tier=hot 声明不生效）
- 验证环境：Windows / PowerShell 5 / Python 3.13.14 / `code\apps\api`；真库 `localhost:5433` → 容器 `code-postgres-1`（healthy）
- 真库 DSN：`postgresql+asyncpg://user:***@localhost:5433/kagent_test`（口令已替换为 `***`）
- 验证角色：测试工程师（P3），未修改任何业务代码；仅按授权**追加 1 条 NO-DB 用例**
- 结论：**PASS**（无阻断、无缺陷；3 条建议、3 条未覆盖/存疑；全量失败集合与基线完全一致）

---

## 一、改动清单（`git diff` 实查）

`git diff --stat`（仅 4 个文件，另有 1 个 untracked 新文件）：

| 文件 | 变更 |
| --- | --- |
| `code/apps/api/app/services/scenario/snapshot.py` | `ScenarioRow` 新增 `tier: str = "cold"`（+2） |
| `code/apps/api/app/services/scenario/resolver.py` | `_row_from` 无条件读取 `tier`（+2，不受 `governed` 影响） |
| `code/apps/api/app/services/scenario/self_check.py` | `SCAN_SQL` 增加 `s.tier`；新增 `SCAN_GOVERNED_SQL`；`_load_all_published` → `_load_all_for_scan` 按开关选 SQL；计数键改名；新增 `_check_tier_hot`（S-10）+ metric 登记（133 行变动） |
| `code/apps/api/tests/scenario/test_self_check.py` | 更新既有断言 + 新增 8 条 NO-DB 用例（337 行变动）；本次补测再 +1 条 → 62 例 |
| `code/apps/api/tests/scenario/test_self_check_pg.py` | **新增**（untracked，267 行，3 条真库用例） |

`git status` 另有 6 个 M 文件（`app/api/kbs.py`、`app/api/sync.py`、`app/services/sync_service.py`、`scripts/rebuild_wiki.py`、`tests/chat/test_ask_with_image.py`、`tests/wiki/test_worker_and_script.py`）：
`git diff --stat -- <这 6 个文件>` 输出为空（仅 stat 变化），与本次变更无关，已确认不计入改动清单。

静态审查：

- `python -m ruff check`（5 个被测文件）：`All checks passed!`
- 无 fallback：新增分支只有「按开关选 SQL」与「tier != 'hot' 直接 return」，没有任何「查不到就造默认行 / 回退旧口径」的分支。
- 无 emoji：改动行中的 `★` 为仓库既有注释符号（U+2605），非 emoji。
- 注释与 docstring 全部中文，UTF-8 读取正常。
- 无超范围改动：diff 未触及 `app/api/*`、路由注册、检索链路；`ScenarioRow` 仅在 `resolver._row_from` 一处构造（grep 确认）；`ScenarioSnapshot.fingerprint` 由 `key|version|index.fingerprint|canonical_policy` 计算，不含 `tier`，指纹不受影响；`to_policy()` 不读 `tier`。

---

## 二、7 条设计承诺逐条对账

### 承诺 1：关闭治理时扫描只含 published，与请求期 `LOOKUP_SQL` 同口径

- 代码证据：`self_check.py:389-394` `governed = get_settings().tenant_governance_enabled`；`sql = SCAN_GOVERNED_SQL if governed else SCAN_SQL`；`resolver._row_from(..., governed=governed)`。`SCAN_SQL` 的版本条件仍是 `AND v.state = 'published'`。
- NO-DB 证据：
  - `test_governance_off_scan_sql_is_published_only`：实际执行 SQL 文本中 `'pinned_version' not in sql`、`"'archived'" not in sql`、`"s.tier" in sql`。
  - `test_governance_off_ignores_governance_columns_even_if_fed`：关闭态**故意喂入** `pinned_version=3 / version_state='archived' / 四项 caps`，折叠结果仍为 `pinned_version is None`、`version_state == "published"`、`to_caps()` 全 `None`。这条证明开关语义由 `_row_from` 兜住，不依赖「SQL 恰好没选列」。
- 真库证据：`test_closed_scan_excludes_pinned_archived_scenario` —— 同一「v1 archived + 钉定 v1 + v2 published」场景，关闭态扫描 `ours` 非空且 `all(version == 2)`，且该租户零 WARN。

### 承诺 2：开启治理时版本选择与 `LOOKUP_GOVERNED_SQL` 逐字同规则，钉定 archived 必须被扫到

- 代码证据：`SCAN_GOVERNED_SQL` 的版本块与 `LOOKUP_GOVERNED_SQL` 完全相同：
  `(s.pinned_version IS NULL AND v.state = 'published') OR (s.pinned_version IS NOT NULL AND v.version = s.pinned_version AND v.state IN ('published','archived'))`
- 用例证据：
  - `test_scan_sql_columns_match_migration_and_lookup`（三层对账）：`scan_closed == {**lookup_closed, "tier": "tier"}`、`scan_governed == {**lookup_governed, "tier": "tier"}`，并对两条 SQL **各自**断言上述版本规则子串存在。
  - `test_governance_on_scan_sql_uses_pinned_rule`：开启态 SQL 含 `pinned_version`、`'archived'` 与四项 caps 列。
  - `test_governance_on_folds_archived_pinned_row`：钉定归档行折叠后 `version_state == "archived"`、`pinned_version == 1`、`to_caps()` 四项值保持，`selfcheck_unparsable == 0`。
  - 真库 `test_governed_scan_includes_pinned_archived_scenario`：`len(ours) == 1`、`version == 1`（**不是** v2）、`version_state == "archived"`，且判据确实处理了该行（S-2b WARN 中 `recall_top_k == 3`，v1 policy 的标记值）。
- 见「八、问题 → 建议 1」：JOIN 内少 `v.tenant_id = s.tenant_id`（详见下文，非阻断）。

### 承诺 3：`tier` 是物理列，不受 governed 影响；hot 必 WARN，warm/cold 不得 WARN

- 代码证据：`resolver.py:211-212` 无条件读取（注释已写明「不属于治理开关管辖」）；`_check_tier_hot` 仅在 `row.tier == "hot"` 时计数 + WARN。
- NO-DB 证据：
  - `test_s10_counts_hot_declared_but_unsupported[hot/warm/cold]`：计数分别为 `1/0/0`；hot 时 WARN 带 `scenario_key`、含「不会生效」与「n_hot_declared」；warm/cold 时断言 `not hot_records`。
  - **本次补测** `test_s10_reads_tier_even_when_governance_on`：`governance=True` 时 `tier_hot_unsupported == 1`、`scanned_versions == 1`。
  - **变异实测（证明补测不空转）**：把 `resolver._row_from` 临时改成 `tier=("cold" if governed else (mapping.get("tier") or "cold"))` 后执行 `-k "s10"`：
    ```
    1 failed, 4 passed, 57 deselected
    FAILED tests/scenario/test_self_check.py::test_s10_reads_tier_even_when_governance_on - assert 0 == 1
    ```
    即：关闭态用例对该回归仍绿，只有新补用例能抓住；随后已精确还原，`git diff` 哈希未变（`index 48719dc..e65de95`，与还原前一致）。
- 真库证据：`test_scan_counts_hot_tier_declared` —— `INSERT ... tier='hot', db_binding='kb_hot01'` 成功提交（证明满足 `ck_scenarios_tier` 与 `ck_scenarios_tier_binding`），WARN 带本租户 id / `scenario_key` / `tier="hot"`。

### 承诺 4：S-10 在 `to_policy()` 之前触发（policy 坏行也报 hot）

- 代码证据：`self_check.py:404-410`，`_scan_one` 第一行即 `_check_tier_hot(row, counts)`，之后才进入 `try: policy = row.to_policy()`。
- 用例证据：`test_s10_fires_even_when_policy_is_broken`（`policy={"recall_top_k": "abc"}`）→ `tier_hot_unsupported == 1` 且 `selfcheck_unparsable == 1`。把调用点下移到 `to_policy()` 之后时坏行会先 return，此断言必失败（有效守位）。

### 承诺 5：计数键改名彻底；`scanned_versions=0` 仍能区分「全绿」与「没扫到」

- 全仓 grep `published_versions_scanned`：**0 命中**（含源码、测试、docs）。
- `scanned_versions` 共 17 处命中：`self_check.py` 3 处（基线字典 / docstring / 赋值）+ 测试 14 处。
- 用例证据：
  - `test_data_check_counts_scan_size`：`empty["scanned_versions"] == 0` 且 `set(empty) == set(_EMPTY_COUNTS)`；两行时 `== 2`。
  - `test_data_check_query_failure_is_not_green`：查询失败时 `selfcheck_query_failed == 1`、`scanned_versions == 0`（0 不被当成全绿的解释依据）。
  - 更新后的既有用例全部改用新键（无一处保留旧键）。

### 承诺 6：自检契约不变 —— 数据类检查永不 raise，查询失败仍计入 `selfcheck_query_failed`

- 代码证据：`check_scenario_data` 的查询 `try/except Exception` → `selfcheck_query_failed` + ERROR 日志；逐行 `try/except Exception` → `selfcheck_unparsable`。
- 用例证据：`test_data_check_never_raises_across_all_violations`（9 类命中集合精确相等）、`test_bad_dna_declared_never_raises`、`test_bad_dna_declared_does_not_stop_scanning`、`test_unexpected_check_exception_is_contained`、`test_data_check_query_failure_is_not_green`。全部在本次重跑中通过。

### 承诺 7：不新增请求路径读取、不改路由集合、不触碰检索链路

- diff 未出现 `app/api/`、`app/main.py`、检索/重排/ACL 相关文件。
- `ScenarioRow.tier` 仅在 `resolver._row_from` 构造（`grep "ScenarioRow("` 仅 1 处），请求期 `LOOKUP_SQL` 不含 `tier` 列 → `.get` 返回 `None` → 常量 `"cold"`，不产生额外查询也不改变任何 snapshot 字段。
- 指纹（`snapshot.py:247-258`）不参与 `tier`；`to_policy()` 不读 `tier`，因此请求期行为与改造前等价（全量 1621 passed 亦可作为回归佐证）。

---

## 三、测试是否被削弱（重点核对）

原断言 `assert scan == lookup`（两层对账）已改为**三层、且更强**：

1. `scan_closed == {**lookup_closed, "tier": "tier"}` —— 精确相等，只允许 `tier` 这一列差异，其它任何多列/少列/别名不同都会失败；
2. `scan_governed == {**lookup_governed, "tier": "tier"}` —— 同理覆盖开启态；
3. 版本选择块在两条 SQL 中分别做**归一化空白后的子串断言**，防止自检自创第三种口径；
4. 列来源从只解析 `0028` 扩到 `0028 + 0031`（正则 `ALTER TABLE (\w+) ADD COLUMN (\w+)` 与 0031 的实际写法匹配；若解析不到 `tier`，该用例会直接失败——本次通过即证明解析有效）。

结论：**没有放宽**，属于等价改造 + 强约束增强。唯一「多出来的一列」是设计要求的 `tier`，并被精确断言为唯一例外。

---

## 四、真实执行结果（命令 / 输出 / 退出码）

均在 `code\apps\api` 下执行；真库命令注入 `SCENARIO_TEST_DB_URL`（口令 `***`）。为取得摘要行，显式覆盖 `-o addopts="--strict-markers"`（仓库默认 `addopts` 含 `-q`，再叠加命令行 `-q` 会变成 `-qq` 而**吞掉摘要行**，这是第一次运行时看不到 `N passed` 的原因）。

| # | 命令 | 结果 |
| --- | --- | --- |
| 1 | `python -m pytest tests/scenario/test_self_check.py -q -o addopts="--strict-markers"` | **61 passed** in 0.42s（补测前）→ 补测后 **62 passed** in 0.31s，exit 0 |
| 2 | `python -m pytest tests/scenario/test_self_check_pg.py -q -o addopts="--strict-markers"`（注入真库） | **3 passed** in 1.70s，exit 0 |
| 3 | `python -m pytest tests/scenario/test_resolver_truth_table.py tests/scenario/test_resolver_snapshot.py tests/scenario/test_step7_governance.py tests/scenario/test_step7_capacity.py tests/scenario/test_step7_pin_pg.py -q -o addopts="--strict-markers"`（注入真库） | **63 passed, 0 skipped** in 5.79s；顺序重跑 **63 passed** in 5.82s，exit 0 |
| 4a | 全量（补测前）`python -m pytest -q -o addopts="--strict-markers" --tb=short`（注入真库） | `2 failed, 1620 passed, 15 skipped, 1 warning in 146.73s`；收集 1637 |
| 4b | 全量（补测后）同上 `--tb=no` | `2 failed, 1621 passed, 15 skipped, 1 warning in 135.77s`；收集 1638 |

补充说明（环境噪声，非被测变更）：第一次把第 2、3 项**并行**执行时，第 3 项出现 10 条 skip（输出 `................................................s.....sssssssss`）。原因是两个进程同时对同一个测试库调用 `pg_helpers.prepare_schema()`（`downgrade 0027 → upgrade head`）互相干扰。改为**串行**后连续两次均为 63 passed / 0 skipped。真库用例应串行执行。

---

## 五、真库用例质量（是否靠「无法区分场景的总数」碰巧通过）

- 三个用例各造**独立租户**（`uuid4`），断言按 `tenant_id` / `scenario_key` 过滤，**完全不用**「总数恰好等于 N」（测试库里有种子行与其它用例残留）。用例 docstring 明确写了这条纪律。
- 治理开启用例（`test_governed_scan_includes_pinned_archived_scenario`）：
  - 若漏扫归档行 → `len(ours) == 1` 失败（并打印实际 `scenario_key` 列表）；
  - 若错扫到 v2 → `ours[0].version == 1` 失败；
  - 若同时扫到 v1+v2 → `len(ours) == 1` 失败；
  - 再以「S-2b WARN 的 `recall_top_k == 3`（该值只写在 v1 的 policy 里）」证明这一行**确实被判据处理过**，而不只是被 SQL 取回。可区分「钉定归档被扫」「没扫」「扫成 published」三态。
- 治理关闭用例（`test_closed_scan_excludes_pinned_archived_scenario`）：
  - 先证「确实扫到了东西且是 v2」（`ours` 非空、`all(version == 2)`），排除「一行都没扫到」；
  - 再证「该租户零 WARN」，排除「v1 被扫到」；
  - 两个方向同时成立，才是有效证明。
- hot 用例：真实 `INSERT ... tier='hot', db_binding='kb_hot01'` 并 `commit`（CHECK 拒绝则插入即失败），WARN 断言带本租户 id、`scenario_key`、`tier="hot"` 与「不会生效」文案。
- 清理：三例均在 `finally` 中按外键逆序 `DELETE`（versions → scenarios → tenants），失败路径也不留脏数据。

---

## 六、本次补测（仅追加，不改既有断言）

新增 `tests/scenario/test_self_check.py::test_s10_reads_tier_even_when_governance_on`：

- 动机：承诺 3 要求 `tier` **双向**不受 `governed` 影响；原有用例只覆盖关闭态（`default_settings` 默认关闭），「仅在 governed=False 时读 tier」这类回归无法被抓住。
- 做法：`monkeypatch` 让 `get_settings()` 返回 `tenant_governance_enabled=True`，喂入 `tier="hot"` 的行，断言 `tier_hot_unsupported == 1`、`scanned_versions == 1`。
- 有效性已验证（见承诺 3 的变异实测）。
- 副作用：全量用例数 1637 → 1638；不影响任何既有断言。

---

## 七、全量失败集合与基线比对

基线（P2 完成时）：`1626` 收集 / `2` failed / `15` skipped（即 1609 passed）。

本次（补测后）：`1638` 收集 / `2` failed / `1621` passed / `15` skipped。

| 项 | 基线 | 本次 | 差异 |
| --- | --- | --- | --- |
| 收集数 | 1626 | 1638 | +12 = P2 新增 11（8 条 NO-DB + 3 条 PG）+ 本次补测 1 |
| passed | 1609 | 1621 | +12（全部为新增用例，无既有用例由红转绿） |
| failed | 2 | 2 | 集合完全一致，无新增 |
| skipped | 15 | 15 | 无新增 skip 换绿 |

失败集合（逐字）：

1. `tests/test_health.py::test_openapi_contains_health` —— `socket.gaierror: [Errno 11001] getaddrinfo failed`（环境 DNS）
2. `tests/wiki/test_worker_and_script.py::test_rebuild_wiki_script_fails_closed_when_disabled` —— 子进程同样死于 `socket.gaierror`（环境 DNS）

与基线**完全相同**，无新增失败项，无新增 skip。

---

## 八、发现的问题

**阻断：无。缺陷：无。** 以下为建议级：

1. **（建议）`SCAN_GOVERNED_SQL` 的版本有效性语义与 `LOOKUP_GOVERNED_SQL` 有 1 处窄差异**：
   `LOOKUP_GOVERNED_SQL` 在 JOIN 内有 `AND v.tenant_id = :tenant_id`（配合 WHERE 等价于 `v.tenant_id = s.tenant_id`），即「版本行租户与场景不一致 ⇒ 视为查不到」；`SCAN_GOVERNED_SQL` 只按 `v.scenario_id = s.id` 关联，没有这条。只有在「版本行的 `tenant_id` 被脏数据改成别的租户」时才会出现偏差：自检会扫到、请求期看不见。后果是**多一条 WARN 噪音**（诊断偏差），不会造成假绿，也不会误报通过。
   建议：JOIN 内补 `AND v.tenant_id = s.tenant_id`（无需参数，保持跨租户全量扫描），使 fail-closed 语义与请求期逐字一致；并把 docstring 里「唯一差别是不带 `:tenant_id`」补上这层说明。
   注：`SCAN_SQL`（关闭态）同样缺这条，属于改造前既有形态，不是本次引入。
2. **（建议）钉定到「不存在/已删除版本」的场景在两套口径下都不产出任何行** → 自检完全看不到它，而请求期会以 R1 500 暴露。这是「按行扫描」的固有盲区（与本次设计目标一致：扫描口径与请求期同源）。建议登记为已知盲区，由巡检 SQL 或容量接口侧发现，不新增 S 判据。
3. **（建议）真库用例串行执行**：并行跑两个真库 pytest 进程会因 `prepare_schema()` 的 `downgrade/upgrade` 互相干扰产生 skip（本次实测 10 条），容易被误读为「新增 skip」。建议在 CI/脚本中串行化真库用例。

---

## 九、未覆盖 / 存疑项

1. 开启治理时，四项 caps 在**真库**上的读取值未直接断言（NO-DB 的 `test_governance_on_folds_archived_pinned_row` 已覆盖折叠逻辑；真库只验证了「选中哪一行」）。风险低：caps 读取路径未改，仅多选列。
2. `tier` 取 CHECK 之外的脏值（如 `'HOT'`、`NULL`、非字符串）没有专门用例：`_row_from` 会把 `None` 归为 `"cold"`，`_check_tier_hot` 用精确 `!= "hot"` 判断，不会 raise；行为可接受，但「脏 tier 不炸」未被显式测试。
3. 全量的 2 条失败是既有环境问题（DNS `getaddrinfo`），与本次变更无关；已确认基线同样存在。
4. 本次未对「1000+ 行的真实场景规模」做性能验证（扫描为启动期一次性全表 JOIN，场景量级为个位数到十几个，设计上不要求）。
