# 第七阶段 Step7 遗留修复 6 —— 测试报告（P3 独立对抗性验证）

- 测试角色：测试工程师（独立于编码者，**未修改任何业务代码**）
- 测试时间：2026-11-24
- 被测状态：P2 已完成、**未 commit** 的工作区改动
- 环境：Windows PowerShell；API 目录 `code/apps/api`；真库 `localhost:5433/kagent_test`（容器 `code-postgres-1`）
- 口令注入方式（报告中口令已替换为 `***`）：

```
$pw = ((Get-Content "...\code\.env" | Where-Object { $_ -match '^POSTGRES_PASSWORD=' }) -replace '^POSTGRES_PASSWORD=', '').Trim()
$env:SCENARIO_TEST_DB_URL = "postgresql+asyncpg://user:***@localhost:5433/kagent_test"
```

> 说明：`pyproject.toml` 的 `addopts = "-q --strict-markers"` 已带 `-q`，命令行再追加 `-q` 会变成 `-qq`，**计数行被吞掉**（本环境实测首轮输出只剩 dots 与 warnings summary）。因此本报告的三条命令均**不再追加 `-q`**，以保证拿到真实 passed/failed/skipped。

---

## 一、改动清单（git diff 审查）

`git diff --numstat` + 未跟踪文件实测，本次改动共 6 个文件（3 改 + 3 新增）：

| 文件 | 增/删 | 性质 |
|---|---|---|
| `code/apps/api/app/infra/schema_guard.py` | +99 / -0（新文件） | 新增启动闸门 `assert_schema_matches_code(*, context)` |
| `code/apps/api/app/main.py` | +10 / -36 | 删除 lifespan 末尾旧的 warning-only 检查块；新闸门前移到 `check_connection_budget()` 之后、`register_from_settings()` / `check_scenario_data` 之前 |
| `code/apps/api/app/workers/parse_job.py` | +6 / -0 | worker `on_startup` 在预算/注册之后 `await assert_schema_matches_code(context="worker")` |
| `code/apps/api/tests/test_schema_guard.py` | +249（新文件） | 6 条 NO-DB 行为用例 + 3 条 AST 接线守卫 = 9 条 |
| `code/apps/api/tests/scenario/test_step7_schema_guard_pg.py` | +57（新文件） | 1 条真库用例（head 放行 / 0030 拒绝 / finally 恢复 head） |
| `02-design/架构设计文档.md` | +1 / -1 | 「既有启动检查」表述改为按治理开关分档 |

★ `git status --porcelain` 共 11 行 = 8 个 `M` + 3 个 `??`。其中 5 个 `M` 是**历史脏文件（仅 stat 变化）**，已用 `git diff --quiet -- <5 个文件>` 验证**退出码 0**（内容与 HEAD 完全一致），**不属于本次改动、不应纳入提交**：

`app/api/sync.py`、`app/services/sync_service.py`、`scripts/rebuild_wiki.py`、`tests/chat/test_ask_with_image.py`、`tests/wiki/test_worker_and_script.py`

源码 diff 关键点核对：
- `main.py`：闸门调用位于 `check_connection_budget()`（153 行）之后、`register_from_settings()`（164 行）与 `async with SessionLocal() … check_scenario_data`（166-168 行）之前；旧块（`SELECT version_num …` + `Path / alembic.ini`）在 diff 中**整段删除（-35 行）**，`from pathlib import Path` 一并删除。
- `parse_job.py`：`on_startup` 顺序为 `check_connection_budget()` → `register_from_settings()` → `await assert_schema_matches_code(context="worker")` → `check_scenario_constants()` → `check_capabilities()`。
- `schema_guard.py`：只做一次 `SELECT version_num FROM alembic_version`；判定与后果解耦（`governed` 仅决定「拒绝 / 告警」）；校验异常统一 `raise … from exc`。
- `_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"` → `app/infra/schema_guard.py` 的 `parents[2]` 正是 `apps/api`（与旧块 `parents[1]` 在 `main.py` 下的指向等价，已核对目录层级）。

---

## 二、8 条设计承诺逐条证据

### 承诺 1：治理开启 + 库停在 0030 → 启动抛 `RuntimeError`（消息含 0030 / code heads / 升级提示），不是请求期 500

- 单测 `tests/test_schema_guard.py::test_governed_with_old_revision_refuses_startup` **PASS**；断言含 `0030`、`alembic upgrade head`、`0031`、`context="api"`，并断言「确实发出了查询」(`session.statements` 非空)，排除闸门退化成常量判断。
- 独立真库复现（自写脚本，非编码者用例）真实 `migrate("0030", downgrade=True)` 后调用 `assert_schema_matches_code(context="independent")`，抛 `RuntimeError`，原文：

```
启动检查失败（independent）：数据库 schema 版本与代码不一致。db_revision='0030'，
code_heads=['0031']。治理已开启（TENANT_GOVERNANCE_ENABLED=true），
先执行 alembic upgrade head（治理至少需要 0031）再启动。
```

- 语义佐证：`app/services/scenario/self_check.py` 的启动扫描 `SCAN_SQL` 显式读 `s.tier`（0031 新增列），治理开启时 `SCENARIO` 分支读 `allowed_db_keys/pinned_version` —— 缺列确会在启动/请求期炸，闸门前置成立。

### 承诺 2：治理开启 + 库在 head → 不抛，INFO

- 单测 `test_governed_with_head_passes_and_logs_info` **PASS**（断言 `schema 版本一致` 且 `record.revision == "0031"`）。
- 真库脚本：`migrate("head")` 后放行 PASS；恢复 head 后再放行 PASS。
- 真库单测 `test_guard_accepts_head_and_refuses_0030` 首段（head → 放行）**PASS**。

### 承诺 3：治理关闭 + 版本不匹配 → 不抛，WARNING 口径与改动前一致（db_revision / code_heads / hint）

- 单测 `test_switch_off_mismatch_only_warns` **PASS**：`db_revision == "0030"`、`code_heads == ["0031"]`、`hint` 非空。
- 独立真库关闭态脚本（`TENANT_GOVERNANCE_ENABLED=false`，库真实降到 0030）实测：

```
PASS 关闭态 + 0030 → 不抛
PASS 落下版本不一致 WARNING | 启动检查：数据库 schema 版本与代码不一致
PASS WARNING 含 db_revision | 0030
PASS WARNING 含 code_heads   | ['0031']
PASS WARNING 含 hint         | 执行 docker compose -p kagent -f docker-compose.prod.yml run --rm migrate alembic upgrade head
```

- 与改动前差异仅一处：`extra` 新增 `app` 字段（便于区分 api/worker 两侧日志），旧三个键与文案逐字保持。属日志 schema 的**增字段**，不改变告警可定位性。

### 承诺 4：治理开启 + 校验过程异常 → `RuntimeError` 且保留 `__cause__`；关闭态仅 warning

- 单测 `test_governed_probe_error_refuses_and_keeps_cause` **PASS**（`__cause__ is boom`）、`test_switch_off_probe_error_only_warns` **PASS**。
- 独立真库复现两种真实异常，均保留根因：
  - 连接被拒（端口 5999）：`RuntimeError`，`__cause__ = ConnectionRefusedError(22, '远程计算机拒绝网络连接。', ...)`；
  - 认证失败（故意错误口令）：`RuntimeError`，`__cause__ = InvalidPasswordError`。

### 承诺 5：闸门只读（不迁移、不写库）；关闭态仅一次 `SELECT version_num`

- 单测 `test_switch_off_is_read_only_single_probe` **PASS**：`len(session.statements) == 1` 且语句文本为 `SELECT version_num FROM alembic_version`；替身未实现 `commit()`，实现若私自写库会直接 `AttributeError`。
- 独立真库关闭态脚本：调用闸门**之后**再次读取 `alembic_version` 仍为 `0030`（`PASS 闸门只读：调用后仍是 0030`）；治理开启复现中闸门抛错时版本同样仍是 0030，未发生迁移，之后才由脚本显式 `migrate("head")`。

### 承诺 6：顺序（API 闸门在任何治理查询之前）+ 旧块已完全移除（无重复检查）+ worker 调用同一函数

- `test_api_guard_runs_before_scenario_data_check` **PASS**：lifespan 内 `{assert_schema_matches_code, check_scenario_data}` 按源码位置排序后首个即闸门，且闸门调用点恰好 1 处、`context` 字面量为 `"api"`。
- `test_api_has_no_leftover_schema_check` **PASS**：`app/main.py` 源码中已无 `SELECT version_num FROM alembic_version`；配合 `grep -rn "version_num|alembic_version|schema_guard" app/` 复核：`main.py` 仅剩注释与 import/调用，业务代码中 `alembic_version` 只出现在 `infra/schema_guard.py`、`infra/scene_db_migrate.py`（场景库合并校验，无关）、`config/self_check_ext.py`（仅用于提示文案，且在闸门之后执行）。
- `test_worker_startup_calls_guard_after_budget_check` **PASS**：`on_startup` 中闸门调用点 1 处、`context="worker"`、行号在 `check_connection_budget` 之后；源码目检为 `await assert_schema_matches_code(context="worker")`，非同步误用。
- 静态核对「任何治理查询之前」：`decisions.self_check()` / `check_scenario_constants()` 纯内存；`check_connection_budget()` 纯算术零 IO —— 闸门之前确实没有读治理列的 SQL。

### 承诺 7：关闭态启动行为与改动前一致

- 关闭态下函数路径为「查一次 + INFO（一致）/ WARNING（不一致）+ 异常吞掉 warning」，与旧块**语义等价**；WARNING 文案、三个 extra 键、`hint` 文案逐字一致。
- 差异（记录在案、不算行为回归）：旧块位于 lifespan 末尾（arq 池、重排探活、索引口径检查之后），新闸门前移 → 日志**先后顺序**变化；且关闭态若此刻 DB 未就绪，只提前打一条 warning（原本也会打，只是更晚）。
- 真库关闭态 0030 实测「不抛 + WARNING」已证（见承诺 3）。

### 承诺 8：文档描述与实现一致

- `02-design/架构设计文档.md` 的新句：「schema 版本检查按开关分档：`TENANT_GOVERNANCE_ENABLED=true` 时版本不匹配直接拒绝启动（治理查询读 0031 新列，缺列即请求期 500），关闭态只告警」—— 与 `schema_guard.py` 的判定/后果、`main.py` 与 `parse_job.py` 的接线一致，无夸大。

---

## 三、真实执行结果

### 命令 1：`python -m pytest tests/test_schema_guard.py`

```
9 passed, 7 warnings in 0.61s
```

### 命令 2：`python -m pytest tests/scenario/test_step7_schema_guard_pg.py tests/scenario/test_0031_migration.py tests/scenario/test_step7_governance.py tests/scenario/test_step4_guards.py`

```
54 passed, 2 warnings in 6.74s
```

（真库用例**未被 skip**：`SCENARIO_TEST_DB_URL` 已注入，0 skipped。）

### 命令 3：`python -m pytest`（全量，串行）

```
2 failed, 1673 passed, 15 skipped, 11 warnings in 153.62s (0:02:33)
```

失败集合（仅两条，与基线完全一致）：

```
FAILED tests/test_health.py::test_openapi_contains_health
FAILED tests/wiki/test_worker_and_script.py::test_rebuild_wiki_script_fails_closed_when_disabled
```

与基线的比对：

| 指标 | 基线（第 6 项完成时） | 本次 | 差异 |
|---|---|---|---|
| collected | 1680 | 1690 | **+10**（9 条 NO-DB + 1 条真库，全部新增用例） |
| passed | 1663 | 1673 | +10 |
| failed | 2 | 2 | **nodeid 集合完全相同，无新增失败** |
| skipped | 15 | 15 | **数量未变，无新增 skip 换绿** |

结论：失败 nodeid 集合未新增；无「新增 skip 换绿」。两条失败均为已知环境问题（`getaddrinfo failed` 本机 DNS 解析）。

---

## 四、独立复现（不照抄编码者的测试）

自写脚本：`%TEMP%\p3_independent_check.py`（仓库外临时文件，未入仓）。步骤与结果：

1. `migrate("head")` → `current_revision() == "0031"`；治理开启（进程内 `TENANT_GOVERNANCE_ENABLED=true`，在 import 前设置以绕过 `get_settings` 的 `lru_cache`）→ 放行 **PASS**；
2. `migrate("0030", downgrade=True)` → 真实 `current_revision() == "0030"` → `asyncio.run(assert_schema_matches_code(context="independent"))` 抛 `RuntimeError`，文案含 `0030` / `0031` / `alembic upgrade head` / `independent`，且**不含口令** **PASS**；
3. `migrate("head")` → 恢复 `0031` → 再次放行 **PASS**；
4. 附加：`SessionLocal` 指向 `localhost:5999`（连接被拒）→ `RuntimeError` 且 `__cause__` 保留，异常链文本不含口令 **PASS**；
5. 收尾 `finally: migrate("head")`，脚本退出码 0。

最终库版本（脚本内断言 + 事后独立复核）：

```
alembic_version = 0031
```

另跑关闭态真库脚本 `%TEMP%\p3_closed_state_check.py`：关闭治理 + 真实 0030 → 不抛、WARNING 四要素齐全、调用前后版本都停留 0030 → 恢复 0031，全部 **PASS**。

安全附加脚本 `%TEMP%\p3_security_check.py`：错误口令 → `RuntimeError`，`__cause__ = InvalidPasswordError`，`口令是否出现在异常链: False` → **PASS**。

> 三段脚本均与全量测试**串行**执行（全量结束后才跑），不存在并发改库冲突。

---

## 五、安全与一致性

- **不泄露 DSN / 口令**：版本不一致的 `RuntimeError` 文案只含 `db_revision` / `code_heads` / 固定迁移命令；连接被拒与认证失败两条真实异常链的 traceback 文本均经断言不含口令明文。三条日志（INFO 一致 / WARNING 不一致 / WARNING 跳过）均不含连接串。
- **异常链可控**：`raise … from exc` 保留根因，`RuntimeError` 自身文案固定，不拼接原始异常文本，避免把 SQLAlchemy 错误中的 URL 带入用户可见路径。
- **部署一致性（对抗性推演）**：`docker-compose.prod.yml` 中 `migrate` / `api` / `worker` 共用 `context: .` + `apps/api/Dockerfile`，Dockerfile 为 `COPY . .`，`code/.dockerignore` 未排除 `alembic.ini` → 治理开启时 worker 侧不会因缺 `alembic.ini` 被「校验异常」路径误拒启动。
- 闸门只读 `alembic_version`，不读 `information_schema`、不复制迁移列清单（与模块 docstring 的「单一真源」一致）。

---

## 六、发现的问题

- **阻断：无**
- **缺陷：无**
- **建议（不影响本次判定）**
  1. `test_worker_startup_calls_guard_after_budget_check` 只断言「存在调用点 + 行号顺序 + context 字面量」，**未断言 `ast.Await` 前缀**。若未来有人删掉 `await`，闸门会退化为「创建协程但不执行」，该用例仍会绿（承诺 6 的「worker 调用不是同步误用」缺少机器守卫）。当前源码为 `await …`，行为正确。
  2. `test_switch_off_probe_error_only_warns` 未断言该 WARNING 带 `exc_info`（即根因仍可定位）；实现已传 `exc_info=True`，但该性质未被锁死。
  3. `test_api_has_no_leftover_schema_check` 是大小写敏感的单句文本断言，属弱断言；仅靠它与 AST 顺序用例联合兜底。
  4. 关闭态 WARNING 的 `extra` 新增 `app` 字段属于日志 schema 微变，若有日志消费方按固定字段集解析，需知会。
  5. 真库用例依赖模块级 `schema` fixture 把共享库推到 head；若将来改用 xdist 并行，会与其他迁移类用例（同样降/升共享库）冲突。当前全量串行，无影响。

---

## 七、未覆盖 / 存疑项

1. **未真正启动 uvicorn 进程验证 lifespan**：`ASGITransport` 不跑 lifespan 是仓库既有约定（本机无 Redis/arq）。承诺 1/2 由「函数级真库用例 + AST 接线守卫 + 独立复现」三层覆盖，但「进程启动失败」这一端到端形态未实测。
2. **worker 真实进程启动未跑**：需 arq/Redis，仅做源码与 AST 级验证。
3. **`alembic_version` 表存在但为空（`None`）**的场景无单测；其代码路径与「不匹配」相同（`None not in heads` → 治理开启拒绝，关闭告警），风险低。
4. 全量 **skip 集合**未逐条与基线比对（基线只给了数量 15）；本次仅比对数量（15=15）与失败 nodeid（完全一致），且新增用例 0 skip。
5. 关闭态「启动行为与改动前一致」是语义等价判定，唯一可观测差异是日志时序前移（已在承诺 7 记录）。

---

## 八、结论

**PASS**

- 8 条设计承诺逐条有正向证据（单测 + 真库 + 独立复现）。
- 三条必跑命令全部真实执行：9 passed / 54 passed / 2 failed + 1673 passed + 15 skipped。
- 全量失败 nodeid 集合与基线**完全一致**，无新增失败、无新增 skip。
- 独立把测试库降到 0030 后治理开启确实拒绝启动，恢复 head 后放行，最终 `alembic_version = 0031`。
- 无阻断、无缺陷；5 条建议均为测试强度与日志 schema 层面的改进项。
