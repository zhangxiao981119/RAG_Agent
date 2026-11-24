# 第七阶段 Step7 遗留修复4 —— P3 独立对抗性验证报告

- 验证对象：`GET /api/kbs` 列表统计补上租户库白名单校验（P2 已完成，未 commit）
- 验证环境：Windows / PowerShell 5 / Python 3.13.14 / `code\apps\api`；真库 `localhost:5433` → 容器 `code-postgres-1`
- 真库 DSN：`postgresql+asyncpg://user:***@localhost:5433/kagent_test`（口令已替换为 `***`）
- 验证角色：测试工程师（P3），独立于编码者；**未修改任何业务代码**，仅在 `tests/kbs/test_kb_scope_wiring.py` 追加 1 条补测
- 结论：**PASS**（无阻断、无缺陷；无建议需立即处理；4 条未覆盖/存疑；全量失败集合与基线完全一致，skip 数不变）

---

## 一、改动清单（`git diff --numstat` 实查）

| 文件 | 增 / 删 | 内容 |
| --- | --- | --- |
| `code/apps/api/app/api/kbs.py` | +5 / -0 | import `ensure_db_keys_allowed`；在分桶之后、统计之前插入 2 行中文注释 + 1 行调用 |
| `code/apps/api/tests/kbs/test_kb_scope_wiring.py` | +395 / -0 | 纯追加：7 条 S7-3 用例（编码者）+ 1 条补测（本次）；无任何删除行（`git diff` 中 `^-` 行数 = 0） |

`app/api/kbs.py` 的 diff 全貌（仅此 5 行）：

```diff
+    ensure_db_keys_allowed,
@@
+    # 白名单必须先于数据面：被拒库位一律不打开，避免列表统计绕过 allowed_db_keys。
+    # 治理关闭时该函数零 SQL；拒绝交给全局 DbScopeError 处理器映射为 400。
+    await ensure_db_keys_allowed(session, user.tenant_id, sorted(buckets))
```

无关文件核对：`git status` 另有 6 个 M 文件（`app/api/sync.py`、`app/services/sync_service.py`、`scripts/rebuild_wiki.py`、`tests/chat/test_ask_with_image.py`、`tests/wiki/test_worker_and_script.py`、以及 `app/api/kbs.py` 之外的其余项）。对前 5 个执行
`git diff --numstat -- <这 5 个文件>` **输出为空**（仅 stat 变化），与本次变更无关，未计入改动清单，也不应加入提交。

静态审查：
- `python -m ruff check app/api/kbs.py tests/kbs/test_kb_scope_wiring.py` → `All checks passed!`
- 无 fallback：新增调用无 try/except、无「解析失败就跳过校验」分支；拒绝路径一律 `raise DbScopeError`。
- 注释全中文、UTF-8；仅使用仓库既有的 `★`（U+2605）符号，无 emoji。

---

## 二、必做验证结果

### A. diff 审查

- 只改两处（`app/api/kbs.py` +5 行、测试文件纯追加），无额外逻辑。
- 新增行位置正确：`buckets` 构建完成（`kbs.py:77-79`）之后、`open_data_session` 统计循环（`:85-97`）之前。
- 控制面无写操作：`list_kbs` 全程只读，不存在需要 rollback 的未提交写。

### B. 测试敏感度（注释掉新增行 → 是否转红）

临时把 `kbs.py:83` 的调用替换为注释，执行：

```
python -m pytest tests/kbs/test_kb_scope_wiring.py -o addopts="" -q -rf --tb=line
```

结果：`6 failed, 6 passed in 2.43s`。失败为：

```
FAILED ...::test_kbs_list_denied_location_returns_400_and_never_opens_it
FAILED ...::test_kbs_list_allowed_both_buckets_returns_real_counts     (assert 2 == 3，缺白名单语句)
FAILED ...::test_kbs_list_empty_allowlist_denies_default_too
FAILED ...::test_kbs_list_partial_grant_user_still_denied_on_subset
FAILED ...::test_kbs_list_empty_kb_set_with_empty_allowlist_returns_200 (assert 1 == 2，缺白名单语句)
FAILED ...::test_kbs_list_whitelist_rejection_precedes_data_plane
```

剩余 2 条通过的是**正向语义用例**（`NULL 白名单=不限`、`治理关闭=行为不变`），本就不应因删掉校验而失败，符合预期。

> 说明：新增的「空 kb 集 + 白名单 []」用例在删除校验后仍期望 200，其失败来自「控制面语句数少了白名单这一条」（`assert 1 == 2`），这也是它在删除校验时转红的正确原因。

随后 `Copy-Item` 还原并用 SHA256 校验：
`F64D6DF29FAACFB4FD7EA8D4709412A6529B2B797D5BA62CB03593F29D0DCB8C`（与改动前一致），`git diff --stat` 仍为 `5 +++++`。

### C. 真实执行（`code\apps\api`，注入真库地址）

```
1) python -m pytest tests/kbs/test_kb_scope_wiring.py -o addopts="" -q -rf
   → 12 passed in 2.26s

2) python -m pytest tests/kbs tests/scenario -o addopts="" -q -rf
   → 621 passed, 1 warning in 105.98s (0:01:45)

3) python -m pytest -o addopts="" -q -rf
   → 2 failed, 1650 passed, 15 skipped, 1 warning in 149.42s (0:02:29)
```

> `-o addopts=""` 只是为了避开 `pyproject.toml` 的 `addopts="-q"` 与命令行 `-q` 叠加成 `-qq`（会吞掉汇总行），不影响任何用例的选择与执行。

### D. 全量失败集合与基线比对

| 项 | 基线 | 本次 | 差异 |
| --- | --- | --- | --- |
| collected | 1659 | 1667 | +8（恰好 = 新增 8 条用例） |
| passed | 1642 | 1650 | +8 |
| failed | 2 | 2 | 0 |
| skipped | 15 | 15 | 0 |

失败 nodeid 集合（与基线**完全一致**，无新增）：

```
tests/test_health.py::test_openapi_contains_health
tests/wiki/test_worker_and_script.py::test_rebuild_wiki_script_fails_closed_when_disabled
```

两条均为已知环境失败：
- 前者 `socket.gaierror: [Errno 11001] getaddrinfo failed`（沙箱域名解析）；
- 后者脚本子进程同样因 `socket.gaierror` 提前崩溃，未走到 `WIKI_ENABLED=false` 分支。

pass 增量（+8）与用例增量（+8）严格相等、skip 数与失败集合均未变 —— **不存在用 skip / 删用例换绿**。

---

## 三、8 条设计承诺逐条对账

### 承诺 1：被拒库位绝不打开（400 时没有任何 `open_data_session`，包括 default；无 count）

- 代码证据：`kbs.py:83` 的调用位于 `:85-97` 统计循环之前；`ensure_db_keys_allowed`（`db_scope.py:151-169`）在 `denied` 非空时 `raise`，控制流直接离开路由函数。
- 用例证据（全部真实执行通过）：
  - `test_kbs_list_denied_location_returns_400_and_never_opens_it`：`opened == []`、`count_calls == []`；且测试替身在 `default`/`kb_x` 被打开时主动 `AssertionError("被拒库位 ... MUST NOT 被打开")`（敏感度实验已验证该守卫会触发）。
  - `test_kbs_list_empty_allowlist_denies_default_too`：`opened == []`、`count_calls == []`（`deny_open=("default",)`）。
  - `test_kbs_list_partial_grant_user_still_denied_on_subset`：同上。

### 承诺 2：白名单放行 `["default","kb_x"]` → 两个桶分别统计，计数不串桶

- 用例证据：`test_kbs_list_allowed_both_buckets_returns_real_counts` → 200；`counts == {kb_default: 2, kb_scene: 3}`（逐 kb 精确值，非「碰巧为 0」）；`opened == ["default","kb_x"]`、`count_calls == ["default","kb_x"]`（按库位排序、每桶恰好一次 count）；控制面语句数 == 3（kb 列表 + 库位映射 + 白名单）。

### 承诺 3：白名单 `[]`（全禁）→ 400，即使全部 kb 都在 default

- 用例证据：`test_kbs_list_empty_allowlist_denies_default_too` → 400，`detail.denied == ["default"]`，`opened == []`。
- 语义依据：`governance.py:159` `allowed = None if raw is None else frozenset(raw)`；`ensure_db_keys_allowed` 中 `set(db_keys) - allowed` 对空集 `allowed=frozenset()` 会把 `"default"` 判为 denied。

### 承诺 4：白名单 NULL → 不限，200

- 用例证据：`test_kbs_list_null_allowlist_is_unrestricted` → 200，`{kb_default: 5, kb_scene: 7}`，`opened == ["default","kb_x"]`。
- 代码证据：`governance.py:159` `raw is None → allowed=None`；`db_scope.py:162-163` `if allowed is None: return`。

### 承诺 5：治理关闭 → 200，结果与改动前一致，且不新增任何 SQL（尤其不查 tenants）

- 用例证据：`test_kbs_list_governance_off_keeps_behavior_and_adds_no_sql` → 200，计数 `{1, 4}` 正常；`len(control.statements) == 2`（kb 列表 + 库位映射），`not any("allowed_db_keys" in sql ...)`。
- 代码证据：`governance.py:146-147` 在开关关闭时直接 `return None`，位于任何 `session.execute` 之前 —— 零 SQL。

### 承诺 6：普通用户仅部分 kb 授权时也走同一校验，不因子集漏检

- 用例证据：`test_kbs_list_partial_grant_user_still_denied_on_subset`（`clearance=20`、`authorized=(kb_denied,)`）→ 400，`denied == ["kb_x"]`，`opened == []`。
- 代码证据：白名单校验位于权限过滤（`:63-69`）与分桶（`:77-79`）之后，作用于**过滤后的实际库位集合**，因此只能更严，不会因授权子集漏检。

### 承诺 7：顺序 —— 白名单在数据面前；失败时控制面无未提交写（`open_data_session` 的 commit 分支未触发）

- 用例证据：`test_kbs_list_whitelist_rejection_precedes_data_plane` → 语句顺序 `statements[0]` 含 `knowledge_bases`、`statements[1]` 含 `scenarios`、`statements[2]` 含 `allowed_db_keys`；`opened == []`、`count_calls == []`；`control.commits == 0`；全部语句均以 `SELECT` 开头。
- 代码证据：非 default 分支的 `await control_session.commit()` 在 `open_data_session`（`db_scope.py:235`）内，晚于白名单校验，拒绝路径不会触达。

### 承诺 8：不把被拒 KB 静默记为 0，不从列表隐藏

- 用例证据：混合场景（`kb_default` 在 default、`kb_denied` 在 `kb_x`、白名单仅 `default`）返回 **400 `DB_SCOPE_NOT_ALLOWED`**，`detail = {code, denied:["kb_x"], tenant_id}`，而不是 200 + 部分列表 + `doc_count=0`。
- 代码证据：`raise DbScopeError` 后路由函数不构造 `result`，不存在「被拒 KB 补 0 或过滤掉」的代码路径。

---

## 四、补测（本次新增，位于 `tests/kbs/test_kb_scope_wiring.py:529-551`）

`test_kbs_list_empty_kb_set_with_empty_allowlist_returns_200`
- 场景：admin、`kbs=[]`（控制面无 kb 行）、`allowed=[]`（全禁）。
- 断言：200 + `[]`；`opened == []`、`count_calls == []`；控制面语句数 == 2 且不含 `scenarios`（空 kb 集不发出库位映射查询）。
- 意义：固化任务 D 点名的「空 rows + 白名单 [] 不误报 400」——没有数据面读取时，全禁白名单不应把空列表变成错误。
- 已验证它对该实现敏感（删除白名单调用后因语句数变为 1 而转红）。

任务 D 点名的另一项「混合 default + 被拒键必须 400 而非部分读取」已由既有 `test_kbs_list_denied_location_returns_400_and_never_opens_it` 与 `test_kbs_list_whitelist_rejection_precedes_data_plane` 覆盖，未重复新写。

---

## 五、发现的问题

**无阻断、无缺陷。**

两条非阻断观察（不要求本次处理，不建议阻断提交）：

1. 治理开启且 `tenants` 行缺失时：`get_allowed_db_keys` 抛 `TenantNotFoundError`（`governance.py:156`），仓库内无对应全局处理器（`main.py` 只有 `DbScopeError` / `ValueError` / `Exception`），最终落到通用 500。这是既有的 fail-closed 设计（「tenant 行缺失 = 数据损坏」），本次改动把它带到了 `/api/kbs` 这一读多写少的列表接口上；不泄露细节，但可用性影响面变大。
2. 白名单读取有 60s 本地缓存（`governance.py:27` `CACHE_TTL_SECONDS`），改白名单后列表统计的最长滞后为 60s。既有设计，非本次引入。

---

## 六、未覆盖 / 存疑项

1. **非 admin 且 `authorized_kb_ids` 为空**时，`list_kbs` 在 `kbs.py:63-65` 提前 `return []`，**不读白名单**；因此治理开启 + `allowed=[]`（全禁）时该路径返回 200 `[]` 而不是 400。没有数据面读取，不构成越权，但与承诺 3 的字面口径存在差异。当前未加用例固化行为，需产品确认期望语义（建议：如认为「全禁即拒绝」，应把校验前移到提前返回之前；如认可现状，可补一条用例把语义写死）。
2. 列表路径下 `map_kb_db_keys` 命中同一 kb 多库位（`DB_SCOPE_MULTI_KEY`）时，全局 `DbScopeError` 处理器只在 `DB_SCOPE_NOT_ALLOWED` 时映射 400，其余返回 500。属本次改动之前就存在的行为，未写用例覆盖。
3. 白名单缓存 TTL 内的读写滞后（见五.2），未做端到端计时用例。
4. 真实 `tenants` 行缺失导致 500 的路径未写用例（构造成本高、且属既有数据事故语义）。

---

## 七、附：证据日志

| 文件 | 内容 |
| --- | --- |
| `04-test/_step7r4_run1_final.log` | `12 passed in 2.26s` |
| `04-test/_step7r4_run2_final.log` | `621 passed, 1 warning in 105.98s` |
| `04-test/_step7r4_all.log` | `2 failed, 1650 passed, 15 skipped, 1 warning in 149.42s` + 完整失败堆栈 |
| `04-test/_step7r4_run1v.log` | 补测前 11 项用例名清单（`-v`，含 `11 passed in 2.26s`） |
