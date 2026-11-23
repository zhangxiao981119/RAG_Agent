# 测试报告：Step6 S6-6 场景库清理与双库孤儿检查（验收 commit e4c9666 / 复验 75c348d）

- 被测提交：`e4c9666`（S6-6 实现）；复验提交 `75c348d`（P1 修复）
- 仓库：`c:\Users\86186\Desktop\AI Agent学习\file_rag_agent`
- 后端工作目录：`code\apps\api`
- 测试角色：独立测试（不采信实现者结论，全部命令实际执行）
- 测试时间：初测 2026-11-23 18:20 - 18:50；复验 18:55 - 19:15（Asia/Shanghai）
- 被测文件：
  - `code/apps/api/app/infra/scene_db_cleanup.py`
  - `code/apps/api/scripts/scene_db_cleanup.py`
  - `code/apps/api/tests/scenario/test_step6_scene_db_cleanup.py`
  - 复用方：`code/apps/api/app/infra/scene_db_migrate.py`（`ensure_distinct_targets` 公开化）
- 设计口径：`02-design/第六阶段-Step6-前置裁决.md` §4.12、§4.7 规则 3

---

## 一、结论

**S6-6 验收通过，无未关闭问题。**

- 初测唯一 P1（归零复核在事务提交后执行、失败不回滚）**已由 `75c348d` 真实修复**：复核移入 `async with scene.transaction()` 内，残留时抛 `CleanupError` 由 `asyncpg` 回滚。我用独立探针（不复用实现者的用例）注入假残留复验：抛 `S6_CLEANUP_VERIFY_MISMATCH` 的同时，场景库 10 张表行数与清理前**逐表完全相等**。
- 复验命令全部符合预期：无真库 `6 passed, 2 skipped`；真库 `8 passed`；ruff `All checks passed!`。相邻回归（迁移/手术/库位/任务路由）本轮未改动，沿用初测 `65 passed`。
- 初测 P2 中「清理后未断言控制库 kb 仍在」「`CLEANUP_TARGET_HAS_CONTROL_ROWS` 零覆盖」两项**已补用例关闭**；「`CLEANUP_VERIFY_MISMATCH` 零覆盖」随真库回滚用例关闭。
- 仍未覆盖的仅为 CLI 展示层与两类孤儿检出方向的用例缺口（行为已由独立探针证明正确），不构成实现缺陷，列为残余风险。

---

## 二、环境

| 项 | 值 |
|---|---|
| OS | Windows（PowerShell 5） |
| Python | 3.13.14 |
| ruff | 0.16.1 |
| PostgreSQL | 容器 `code-postgres-1`（Up healthy），宿主 `localhost:5433` |
| 测试库 | `kagent_test`（`SCENARIO_TEST_DB_URL` 的账号口令取自 `code/.env`，全程未打印口令） |
| 临时库 | 用例内 `CREATE DATABASE ... TEMPLATE template0`、用后 DROP |
| 工作区状态 | 存在与本任务无关的 S6-7 改动；本次未修改代码、未提交 |

---

## 三、验收命令与真实输出

### 3.1 ruff（复验）

```
cd code\apps\api
python -m ruff check app/infra/scene_db_cleanup.py tests/scenario/test_step6_scene_db_cleanup.py
```
```
All checks passed!
```

### 3.2 无真库（复验）

```
python -m pytest tests/scenario/test_step6_scene_db_cleanup.py -q -o addopts=""
```
```
......ss                                                                 [100%]
6 passed, 2 skipped in 0.21s
```
符合预期：6 条 NO-DB 通过（新增 `test_scene_target_rejects_control_plane_rows`），2 条真库用例 skip 而非 error。初测为 `5 passed, 1 skipped`。

### 3.3 真库（复验）

```
$pw = ((Get-Content ..\..\.env | Select-String '^POSTGRES_PASSWORD=').Line -replace '^POSTGRES_PASSWORD=','').Trim().Trim('"').Trim("'")
$env:SCENARIO_TEST_DB_URL = "postgresql+asyncpg://user:$pw@localhost:5433/kagent_test"
python -m pytest tests/scenario/test_step6_scene_db_cleanup.py -q -o addopts=""
```
```
........                                                                 [100%]
8 passed in 11.08s
```
符合预期：原 9 步往返用例 + 新增 `test_real_db_cleanup_rolls_back_on_verify_mismatch`。初测为 `6 passed`。

### 3.4 相邻回归（初测，本轮未改相关代码）

```
python -m pytest tests/scenario/test_step6_scene_db_migrate.py tests/scenario/test_step6_scene_db_schema.py tests/scenario/test_step6_db_scope.py tests/scenario/test_step6_job_routing.py -q -o addopts=""
```
```
.................................................................        [100%]
65 passed in 24.86s
```

### 3.5 独立复验 P1（仓库外临时脚本，未改动被测代码）

脚本 `C:\Users\86186\AppData\Local\Temp\s6_6_p1_recheck.py`：自建临时双库 → alembic head → 造种子 → 手术 → 迁移 kb1 → 把模块内 `scene_kb_counts` 的第 2 次调用（事务内复核）改成伪造残留 → 调 `cleanup_scene_kb(apply=True)` → 用**新连接**回读 10 张表。真实输出：

```
PROBE raised_mismatch: True
PROBE counts_unchanged: True | documents: 1 chunks: 1
PROBE before: {'chunks': 1, 'documents': 1, 'governance_jobs': 1, 'graph_entities': 2, 'graph_entity_mentions': 1, 'graph_relations': 1, 'kb_terms': 1, 'parse_jobs': 1, 'parse_segments': 1, 'wiki_pages': 1}
PROBE after : {'chunks': 1, 'documents': 1, 'governance_jobs': 1, 'graph_entities': 2, 'graph_entity_mentions': 1, 'graph_relations': 1, 'kb_terms': 1, 'parse_jobs': 1, 'parse_segments': 1, 'wiki_pages': 1}
```

结论：抛 `CLEANUP_VERIFY_MISMATCH` 且**整笔回滚**（10 张表逐表与清理前相等），与初测「已提交、未回滚」形成对照。P1 关闭。

### 3.6 初测的其余独立探针（代码未变，结论沿用）

初测用同一套临时双库探针得到（`scene_orphans` / `control_orphans` 在 `75c348d` 中未改动）：

```
PROBE1 inspect_zero_write: True | kills_nothing: True        # apply=False 逐表 0 写入
PROBE2 cleanup_ok: True | control_kb_row: 1 | control_documents: 1   # 控制库一行未删
PROBE3b rollback_holds: True | documents: 1 | chunks: 1      # 中途 FK 失败整笔回滚
PROBE4 control_orphans: {'documents': 1}                     # 控制库陈旧副本可检出
PROBE5 parse_jobs_orphans: {'<missing_parent>': 1}           # 父实体缺失可检出
PROBE6 conn_refused/bad_port/bad_scheme: exit=1 leaked_password=False
```

---

## 四、逐条语义核验（复验版）

| # | 验收语义 | 实现位置 | 现有用例 | 独立核验 | 结论 |
|---|---|---|---|---|---|
| 1 | 删除按 `MIGRATION_ORDER` 严格逆序 | `scene_db_cleanup.py:93` | `test:61-67` | 静态 + 真库清理后 10 表全 0 | 通过 |
| 2 | 单事务完成删除 | `:198-199` | 无直接用例 | PROBE3b：中途失败行数不变 | 通过 |
| 3 | 删后复核 10 表归零 | `:202-203`（仍在事务内） | 真库往返 + 回滚用例 | 探针（§3.5） | 通过 |
| 4 | 不归零则 `S6_CLEANUP_VERIFY_MISMATCH` **且回滚** | `:204-208` | `test:345-422`（真库） | §3.5：10 表逐表相等 | **通过（P1 已关闭）** |
| 5 | 不带 `--apply` 只读、零写入 | `:195`、`:209-210` | `test:250-258`（仍为 `deleted==0` 的结构断言） | PROBE1 逐表相等 | 通过（断言仍偏弱） |
| 6 | 不删控制库任何行 | 全模块对 control 只读 | `test:270-280`（已补 `knowledge_bases` EXISTS） | PROBE2 | 通过（已覆盖） |
| 7 | 控制库缺 kb 默认拒绝 | `:183-193` | 真库第 8 步 | 实测抛错且无写入 | 通过 |
| 8 | 必须显式 `--allow-missing-kb` 才继续 | `:170`、`:186`；CLI `scripts:83-87` | 真库第 8 步（API 层）；CLI 接线仍无用例 | 代码直连 | 通过（CLI 接线未覆盖） |
| 9 | 场景库 7 张 kb_id 直连表按 kb 分组判孤儿 | `:253-257`、`:275-279` | 真库第 7 步 | 集合与谓词一致性断言 `:88-90` | 通过 |
| 10 | job 表经 documents、segments 经 jobs→documents | `:258-274` | 静态断言 `test:87-93` | PROBE5 命中 | 通过 |
| 11 | 父实体缺失记 `<missing_parent>` 且算孤儿 | `:95`、`:265`、`:273`、`:278` | 无 | PROBE5 | 通过（用例缺口） |
| 12 | 控制库陈旧副本 | `:285-296` | 真库第 9 步仅「干净为 0」 | PROBE4 | 通过（无检出用例） |
| 13 | 任一方向非零 → 退出码 1 | `check_orphans:314`；CLI `scripts:123` | `test:173-189` | 代码为 `not scene and not control` | 通过 |
| 14 | `KB_ID_TABLES` 一致性断言防回归 | `:71-73`、`:88-90` | `test:70-98` | 见 §六-1 | 部分（残余风险） |
| 15 | CLI 报错不回显口令 | `scripts:106-115`；`_describe` | `test:138-149` | PROBE6 三种真实失败 | 通过 |
| 16 | 身份校验复用搬迁侧 | `_assert_scene_target`（`:107` 起） | `test:226-238`、`test:101-124`（新增） | 实测同库/未手术/控制面非空三路均拒绝 | 通过（控制面行已补用例） |

---

## 五、问题清单（复验后状态）

### P1（中，已关闭）复核在事务提交之后，`S6_CLEANUP_VERIFY_MISMATCH` 不回滚

- 初测证据：`e4c9666` 的 `:197-206` 中 `after`/`residual` 在 `async with` 之外；注入残留实测「抛错时 documents 已为 0」。
- 修复：`75c348d` 把 `after` / `residual` / `raise` 整体缩进到 `async with scene.transaction():` 内（现 `:198-208`），并更新文档字符串。
- 复验证据：§3.5 探针 `raised_mismatch: True` 且 10 张表逐表相等；实现者新增的真库用例 `test_real_db_cleanup_rolls_back_on_verify_mismatch`（`test:345-422`）8 passed 中包含。
- 状态：**关闭**。

### P2（低）`apply=False` 的「零写入」断言仍是结构性断言

- 位置：`tests/scenario/test_step6_scene_db_cleanup.py:250-258`（未改动）。
- `deleted` 在 inspect 分支是硬编码空字典（`:196`），`item["deleted"] == 0` 恒真，不观察库。
- 行为已由 PROBE1（10 表前后逐表相等）证明正确；建议改为直接断言前后行数相等。
- 状态：**保留（非缺陷）**。

### P2（低，已关闭）清理后控制库 kb 未被断言

- `75c348d` 在真库用例第 6 步后新增 `knowledge_bases` EXISTS 断言（`test:270-280`）。PROBE2 亦独立确认。
- 状态：**关闭**。

### P2（低，已关闭）`CLEANUP_TARGET_HAS_CONTROL_ROWS` 与 `CLEANUP_VERIFY_MISMATCH` 零覆盖

- 前者：新增 NO-DB 用例 `test_scene_target_rejects_control_plane_rows`（`test:101-124`），monkeypatch `verify`/`control_plane_usage` 断言错误码。注意它直接调 `_assert_scene_target`，不经 `cleanup_scene_kb`，属单元级覆盖。
- 后者：真库回滚用例（`test:345-422`）覆盖抛错与回滚；其断言只查 `documents == 1`，未查其余 9 表（我的探针补了 10 表逐表相等）。
- 状态：**关闭**。

### P3（低）CLI 展示层与参数接线仍无用例

- `_print_kb_report`（`scripts:37-47`，cleanup/inspect 人类可读报告）无用例；
- `--json`（`scripts:117-118`）无用例；
- `--allow-missing-kb` 的 argparse 接线无 CLI 层用例（仅 API 层验证）；
- 通用 `except Exception` 与 `MigrateError` 分支无用例（初测 PROBE6 实测行为正确）。
- 状态：**保留（覆盖缺口）**。

### P3（低）孤儿检查「检出方向」仍只有一半用例

- 场景库：只有「kb 不在控制库」用例（`test:282-293`）；`<missing_parent>` 无用例（PROBE5 证明可检出）。
- 控制库：只有「干净=0」用例（`test:315-324`）；无「非零能检出」用例（PROBE4 证明可检出）。
- 状态：**保留（覆盖缺口）**。

### P3（信息）其它

- `_describe`、`_asyncpg_dsn` 是私有函数被跨模块复用（`scene_db_cleanup.py:27`、`scripts/scene_db_cleanup.py:34`）。
- `check_orphans` 要求场景库已完成手术且控制面为空，不能在未手术库巡检（与 §4.12 一致）。
- `apply=False` 时若 kb 不在控制库同样拒绝，只读统计也要显式放行（设计未区分，非缺陷）。

---

## 六、未覆盖与风险

1. **`KB_ID_TABLES` 断言的防回归边界**：`:71-73` 表名集合 + `:88-90` 谓词字面等价，能防「把 `parse_jobs` 误当 kb_id 直连表」与「新增表漏登记」；但 `ORPHAN_PREDICATES` 是手写 SQL 副本，其父实体语义与 `MIGRATION_ORDER` 谓词无自动比对，新增第 11 张表时测试只校验表名集合，不校验父实体。
2. **`<missing_parent>` 在正常库不可达**：手术保留搬迁集内部外键，正常库无法产生父实体缺失行；该分支属防御性代码，只有外键被破坏时才触发。
3. **并发窗口**：复核已在事务内，但事务仍是 READ COMMITTED；若在删除与复核之间有人写入同 kb，会得到 `CLEANUP_VERIFY_MISMATCH` 并回滚（此时回滚是设计选择，需停写窗口配合）。
4. **规模未测**：大 kb 的 `DELETE` 耗时、WAL 体积、锁持有时间不在功能验收内。
5. **权限降级**：`CREATE DATABASE` 无权限时用例 skip（`test:210-215`）；本次有权限，8 条全跑。

---

## 七、与设计一致性

| 设计条款（§4.12） | 实现（`75c348d`） | 一致性 |
|---|---|---|
| `--kb-id` + `--apply` 按 `MIGRATION_ORDER` 严格逆序、单事务删除 | `DELETE_ORDER = reversed(...)`；一个事务块 | 一致 |
| 删完立即复核各表行数必须全为 0，否则报 `S6_CLEANUP_VERIFY_MISMATCH` | 复核与抛错均在事务内，抛错即回滚 | 一致（且满足本次「且回滚」口径） |
| 不带 `--apply` 只做只读统计 | `:195`、`:209-210` | 一致 |
| 身份校验复用搬迁侧（非同库→手术 PASS→控制面空） | `_assert_scene_target` 复用三件套 | 一致 |
| 控制库不存在的 kb 默认拒绝，孤儿清理显式 `--allow-missing-kb` | `:183-193`、`:170` | 一致 |
| 控制库不删，kb 行留给 API | 对 control 只读 | 一致 |
| 孤儿检查两方向，任一非零退出码 1 | `check_orphans` + CLI | 一致 |
| 7 张 kb_id 表 / job 表经 documents / segments 经 jobs→documents / `<missing_parent>` | `scene_orphans` | 一致 |
| 控制库陈旧副本（父实体已不存在） | `control_orphans` + `ORPHAN_PREDICATES` | 一致 |

---

## 八、结语

`75c348d` 的修复真实有效：复核进入事务后，`S6_CLEANUP_VERIFY_MISMATCH` 与回滚成为同一件事，独立探针在 10 张表层面验证了「报错即未删除」。初测 P2/P3 中有三项随新用例关闭，剩余为断言强度与 CLI/孤儿检出方向的覆盖缺口，均已登记且不影响正确性。

**S6-6 验收通过，无未关闭问题，建议关闭 S6-6。**
