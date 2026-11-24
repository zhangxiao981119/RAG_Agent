# 第七阶段 Step7 遗留修复 5 —— 测试报告（P3 独立对抗性验证）

- 测试角色：测试工程师（独立于编码者，未修改任何业务代码）
- 测试时间：2026-11-24
- 被测状态：P2 已完成、**未 commit** 的工作区改动
- 环境：Windows PowerShell；API 目录 `code/apps/api`；真库 `localhost:5433/kagent_test`（容器 `code-postgres-1`）
- 口令注入方式（报告中口令已替换为 `***`）：

```
$pw = ((Get-Content "...\code\.env" | Where-Object { $_ -match '^POSTGRES_PASSWORD=' }) -replace '^POSTGRES_PASSWORD=', '').Trim()
$env:SCENARIO_TEST_DB_URL = "postgresql+asyncpg://user:***@localhost:5433/kagent_test"
```

> 说明：`pyproject.toml` 的 `addopts = "-q --strict-markers"` 已带 `-q`，命令行再追加 `-q` 会变成 `-qq` 并吞掉计数行。因此本报告的三条命令均**不再追加 `-q`**，以保证拿到真实 passed/failed/skipped。

---

## 一、改动清单（git diff 审查）

`git diff --numstat` 实测 7 个文件（与任务描述一致）：

| 文件 | 增/删 | 性质 |
|---|---|---|
| `app/services/db_scope.py` | +39 / -17 | 探测白名单化 + 两个 resolve 入口增加必填 keyword-only `tenant_id` |
| `app/api/documents.py` | +14 / -8 | `_locate_doc_db_key` 增加 tenant_id，6 个调用点透传 `user.tenant_id` |
| `app/api/wiki.py` | +3 / -1 | `GET /api/wiki/pages/{id}` 调用点透传 `user.tenant_id` |
| `tests/scenario/test_step6_job_routing.py` | +251 / -16 | 新增探测白名单用例（P2） |
| `tests/scenario/test_step6_db_scope.py` | +15 / -4 | 机械迁移：补 `tenant_id=` |
| `02-design/第六阶段-Step6-前置裁决.md` | +1 / -1 | D-S6-3-2 表述改为「探测白名单内已注册场景库」 |
| `02-design/第七阶段-Step7-前置裁决.md` | +1 / -1 | 校验点表述补「探测路径同样按 allowed_db_keys 过滤」 |

★ 另有 5 个文件在 `git status` 中显示 `M`，但 `git diff` 无任何内容差异（`git diff --quiet` 退出码 0，内容与 HEAD 一致），属于 mtime / 行尾归一化产生的 stat 变化，**不属于本次改动，不纳入提交**：

- `app/api/sync.py`
- `app/services/sync_service.py`
- `scripts/rebuild_wiki.py`
- `tests/chat/test_ask_with_image.py`
- `tests/wiki/test_worker_and_script.py`

> 与任务描述的「6 个无关文件」相差 1 个：实测 stat-only 文件为 5 个（`git status --porcelain` 共 12 行 = 7 内容改动 + 5 stat-only）。不影响结论，但记录在案。

源码 diff 关键点核对：
- `_probe_scene_db_key` 新增 `*, allowed_db_keys: frozenset[str] | None`；循环内 `if allowed_db_keys is not None and db_key not in allowed_db_keys: continue` **位于 `data_session(db_key)` 之前** —— 被拒库位不可能被打开；
- 两个 resolve 入口签名均为 `*, tenant_id: uuid.UUID`（**无默认值**，无 `| None`）；
- 关闭态分支仍为 `return DEFAULT_DB_KEY`，且位于所有 `get_allowed_db_keys` 调用之前；
- 控制副本命中分支代码未变（仍 `resolve_kb_db_key(...)`）。

---

## 二、静态核对（承诺 7）

命令：`grep -rn "resolve_document_db_key\|resolve_wiki_page_db_key" app/`

| 调用点 | 传参 |
|---|---|
| `app/api/documents.py:161`（`_locate_doc_db_key` 内） | `resolve_document_db_key(session, doc_id, tenant_id=tenant_id)` |
| `app/api/wiki.py:192` | `resolve_wiki_page_db_key(session, page_id, tenant_id=user.tenant_id)` |

- app 内除上述两处外**无其他调用点**（worker / script / service 均未直接调用）。
- `_locate_doc_db_key` 的 6 个调用点全部为 `(session, doc_id, user.tenant_id)`：`get_document:180`、`get_document_governance:241`、`rerun_document_governance:276`、`get_document_raw:338`、`get_document_parse_preview:414`、`delete_document:477`，无遗漏、无参数错位。
- 参数为 keyword-only（`*` 之后），无默认值；已由新增用例 `test_resolve_signatures_require_keyword_only_tenant_id` 用 `inspect.signature` 固化（防回退）。

---

## 三、真实执行结果

### 命令 1：定向用例

```
python -m pytest tests/scenario/test_step6_job_routing.py tests/scenario/test_step6_db_scope.py
```

真实输出：`61 passed in 8.30s`（0 failed / 0 skipped；含本轮补测 4 条）
（补测前同一命令为 `57 passed`，P2 自带的用例全部通过。）

### 命令 2：场景 + 知识库

```
python -m pytest tests/scenario tests/kbs
```

真实输出：`634 passed, 1 warning in 125.49s (0:02:05)`（0 failed / 0 skipped；1 warning 为既有 `HTTP_413_REQUEST_ENTITY_TOO_LARGE` DeprecationWarning）

### 命令 3：全量

```
python -m pytest
```

真实输出：`2 failed, 1663 passed, 15 skipped, 1 warning in 162.41s`

失败集合（与基线逐字一致，无新增）：

```
FAILED tests/test_health.py::test_openapi_contains_health
FAILED tests/wiki/test_worker_and_script.py::test_rebuild_wiki_script_fails_closed_when_disabled
```

两者均为已知环境失败（`socket.gaierror [Errno 11001] getaddrinfo failed`，本机 DNS 不可解析）。

### 基线比对

| 项 | 基线（第 5 项完成时） | 本次全量 | 差异判定 |
|---|---|---|---|
| collected | 1667 | 1680（1663+2+15） | +13 = P2 新增 9 条 + 本轮补测 4 条 |
| passed | 1650 | 1663 | +13，与新增用例数吻合 |
| failed | 2 | 2 | **失败 nodeid 集合完全相同，无新增** |
| skipped | 15 | 15 | **无新增 skip，未用 skip 换绿** |

结论：不存在「新增失败」或「新增跳过」；通过数增长全部来自新增用例。

---

## 四、9 条设计承诺逐条证据

### 承诺 1：被拒库位零打开（`data_session` 从未以被拒键调用）

实现证据：`_probe_scene_db_key` 的白名单 `continue` 在 `data_session(db_key)` 之前（`db_scope.py:280-284`）。

测试证据（探测替身记录每一次 `data_session` 调用）：
- `test_probe_opens_only_allowed_scene_db`：注册 `kb_probe` + `kb_other`，白名单仅 `kb_probe` → `probed == ["kb_probe"]`，`kb_other` 零打开；
- `test_probe_denied_when_allowlist_only_default`：白名单 `["default"]` → `probed == []`；
- `test_wiki_probe_page_missing_respects_allowlist` / `test_wiki_probe_owner_document_missing_respects_allowlist`：wiki 两条探测路径均 `probed == []`；
- `test_probe_empty_allowlist_denies_all_and_opens_nothing`（本轮补测）：白名单 `{}` → `probed == []`；
- API 级 `test_document_by_id_probe_respects_allowlist[allowed=default]`：`probed == []` 且 `opened == []`。

### 承诺 2：全部被拒 → None → 调用方 404；不回落 default

- 单测：`test_probe_denied_when_allowlist_only_default`、`test_probe_empty_allowlist_denies_all_and_opens_nothing` → 返回 `None`（不是 `"default"`）；
- API 级：`test_document_by_id_probe_respects_allowlist`（allowed=`["default"]`，文档仅在被拒场景库）→ `resp.status_code == 404`，且数据面 `opened == []`；
- 代码核对：`documents._locate_doc_db_key` 对 `None` 抛 404；`wiki.get_wiki_page:203-204` 对 `None` 抛 404；两条路径均无 default 回落。

### 承诺 3：白名单内多库都有同一主键 → 仍报 `DB_SCOPE_AMBIGUOUS`

- `test_resolve_document_control_miss_two_scene_hits_is_ambiguous`（治理关闭，等价于白名单 NULL）→ `DB_SCOPE_AMBIGUOUS`，`detail["db_keys"] == ["kb_a","kb_b"]`；
- `test_probe_allowlist_covering_two_hit_keys_still_ambiguous`（**本轮补测**）：治理开启 + 白名单 `{kb_a, kb_b}` 两库都命中 → 仍 `DB_SCOPE_AMBIGUOUS`，`db_keys == ["kb_a","kb_b"]`，两个库位都被探测（都属允许范围）。
- 真库：`tests/scenario/test_step6_db_scope.py::test_real_db_scene_only_probe_and_ambiguous` 用两个真实场景库同主键验证歧义（治理关闭口径）。

### 承诺 4：同一主键在「被允许库」与「被拒库」各一份 → 只返回被允许库、不误报歧义、被拒库零打开

- `test_probe_allowed_copy_wins_over_denied_copy_without_ambiguity`：`kb_probe`（允许）与 `kb_hidden`（被拒）同命中 → 返回 `kb_probe`，`probed == ["kb_probe"]`；
- `test_probe_allowed_key_after_denied_key_is_not_ambiguous`（**本轮补测**）：把被拒命中库位排在探测顺序**前面**（`kb_hidden` 先于 `kb_probe`）→ 仍只打开 `kb_probe`，返回 `kb_probe`，不误报歧义。覆盖了「顺序敏感」这一对抗场景。

### 承诺 5：白名单 NULL / 治理关闭 → 探测全部已注册场景库，行为与改动前一致；治理关闭零额外 SQL

- `test_probe_without_governance_opens_all_and_queries_nothing_extra`：控制会话无 execute 脚本（一旦查白名单即 IndexError）→ 通过；`session.calls` 仅 1 次 `get`，`probed == ["kb_a","kb_b"]`（全探测）；
- `test_probe_null_allowlist_opens_all_scene_dbs`：治理开启 + 白名单 NULL → `probed == ["kb_a","kb_b"]`，且 `execute` 恰好 1 次（只多出那一条白名单查询）；
- 关闭态零 SQL：`test_resolve_document_closed_state_returns_default_without_sql`、`test_resolve_wiki_page_closed_state_returns_default_without_sql` 使用「发 SQL 即抛」的 `_NoSqlSession`，返回 `"default"`；
- 代码核对：关闭态 `return DEFAULT_DB_KEY` 在 `get_allowed_db_keys` 之前，`get_allowed_db_keys` 关闭开关时也直接返回 `None` 不发查询。

### 承诺 6：wiki 两条探测路径同样遵守白名单

- 路径一「页副本缺失」：`test_wiki_probe_page_missing_respects_allowlist` → 白名单 `["default"]`、页在 `kb_probe` → `None`、`probed == []`；
- 路径二「页副本在、归属文档副本缺失」：`test_wiki_probe_owner_document_missing_respects_allowlist` → 同上，按 `document_id` 探测前先取白名单，`probed == []`；
- 对照（允许时正常返回）：`test_resolve_wiki_page_control_miss_probes_scene`、`test_resolve_wiki_page_owner_missing_probes_scene_by_document`。

### 承诺 7：`tenant_id` 必填、无默认值；所有 app 调用点显式传递

见「二、静态核对」；另有新增用例 `test_resolve_signatures_require_keyword_only_tenant_id` 断言 `KEYWORD_ONLY` 且 `default is Parameter.empty`。

### 承诺 8：控制库有副本分支行为不变（仍由 `resolve_kb_db_key` 校验白名单）

- diff 显示该分支代码未变（仍调用 `resolve_kb_db_key(control_session, document.tenant_id, [document.kb_id])`）；
- `test_resolve_document_control_copy_wins_without_probing`、`test_resolve_wiki_page_control_copy_derives_from_owner_document`、真库 `_run_control_copy_same_id` 均通过（`probed == []`，不做全库扫描）；
- 白名单拒绝语义仍由既有 `tests/scenario/test_step7_db_ceiling.py`（`DB_SCOPE_NOT_ALLOWED`，拒绝点是 `resolve_kb_db_key`）与 `tests/kbs/test_kb_scope_wiring.py` 覆盖，本次全量运行全部通过。

### 承诺 9：文档失效表述已改，且未改出与实现不符的新描述

- Step6：原文「探测已注册场景库」→「探测白名单内已注册场景库（被拒库位不打开；全部被拒→404）」。与实现一致（白名单过滤 + None→404）。
- Step7：补明控制副本命中路径由 `resolve_kb_db_key` 派生，探测路径同样先按 `allowed_db_keys` 过滤已注册库位（被拒不打开、全拒 404）。与实现一致。
- 两份文档其余行未动（各 1 行修改），未引入与实现不符的新表述。

---

## 五、本轮补测清单（仅改测试，未改业务代码）

写入 `tests/scenario/test_step6_job_routing.py` 第 4b 节末尾（4 条）：

1. `test_probe_allowlist_covering_two_hit_keys_still_ambiguous` —— 允许集合内含两个命中库位仍报歧义（补承诺 3 的治理开启缺口）；
2. `test_probe_allowed_key_after_denied_key_is_not_ambiguous` —— 被拒命中库位排在探测顺序前面时不误报歧义（补承诺 4 的顺序对抗缺口）；
3. `test_probe_empty_allowlist_denies_all_and_opens_nothing` —— 白名单 `{}` 全禁、零打开、返回 None；
4. `test_resolve_signatures_require_keyword_only_tenant_id` —— `tenant_id` 必填 keyword-only，防兼容默认值回退（补承诺 7 的机读证据）。

全部通过；未修改、未放宽任何既有断言。

---

## 六、发现的问题

**无阻断级、无缺陷级问题。**

建议级（不影响本次验收）：

1. `test_probe_without_governance_opens_all_and_queries_nothing_extra` 未显式把 `tenant_governance_enabled` 置为 `False`，而是依赖 `Settings` 类默认值与本机 `.env` 不含该开关。当前环境（`.env` 无 `TENANT_GOVERNANCE_ENABLED`）稳定通过；若某机器在 `.env` 打开治理，该用例会因「多查一次白名单」而失败。建议显式 `monkeypatch.setattr(get_settings(), "tenant_governance_enabled", False)`，与 `_governance_on` 对称。
2. 探测路径新增对 `tenants` 的读取：治理开启但租户行缺失时会抛 `TenantNotFoundError`（500）。这与 Step7 既有口径一致（历史报告「已知边界 4」），非本次引入的新语义，仅提示知情。

---

## 七、未覆盖 / 存疑项

1. wiki 的「全部被拒 → 404」目前由单测（resolve 返回 None）+ `wiki.py` 的 `db_key is None → 404` 代码路径共同保证，**未在同一用例里做 wiki API 级 404 断言**（documents API 级 404 已覆盖）。判断为可接受：wiki 的 None→404 是一行显式分支且未被本次改动触碰。
2. 「控制副本命中 + 白名单拒绝 → 400 `DB_SCOPE_NOT_ALLOWED`」未在 `documents.py`/`wiki.py` 端点层新增用例；该语义由 `resolve_kb_db_key` 既有测试（`test_step7_db_ceiling.py`）覆盖，本次改动未触碰该分支。
3. 真实多库探测的真库用例（`test_real_db_scene_only_probe_and_ambiguous`）在治理关闭口径下运行，未构造「真库场景库 + 真实 tenants 白名单」的组合；白名单过滤由假会话单测覆盖。若后续需要端到端证据，可加真库白名单用例（非本次缺口）。

---

## 八、历史报告过期说明

`04-test/第七阶段-Step7-测试报告.md:128` 曾登记：

> 「Step 6 遗留边界（本期未改）：`db_scope._probe_scene_db_key` 的探测路径不经过 `resolve_kb_db_key`，因此不受库白名单约束。本期任务未要求改，登记为已知边界。」

该「已知边界」**已在本次遗留修复 5 中失效**：探测路径现在先取 `allowed_db_keys` 并只打开白名单内已注册场景库，全部被拒返回 `None`（404），不再绕过白名单。该历史报告属于当时事实的记录，**按要求未修改**；以本报告为准。

---

## 九、结论

- 改动与设计承诺一致，9 条承诺均有可复现证据；
- 三条命令真实结果：`61 passed` / `634 passed` / `2 failed, 1663 passed, 15 skipped`；
- 全量失败集合与基线完全相同（2 条已知环境失败），无新增失败、无新增 skip；
- 未发现业务缺陷；仅有 2 条建议级提示；

**PASS**
