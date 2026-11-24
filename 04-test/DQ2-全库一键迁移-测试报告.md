# DQ2 全库一键迁移（alembic_all_dbs）测试报告

- 角色：测试工程师（P3），独立验证，不采信编码者完成声明
- 日期：2026-11-24
- 被测对象
  - `code/apps/api/scripts/alembic_all_dbs.py`（新增）
  - `code/apps/api/tests/scenario/test_alembic_all_dbs.py`（新增）
  - 关联生效通道：`code/apps/api/alembic/env.py`（既有，`config.attributes["sqlalchemy_url"]` 优先）
- 环境：Windows PowerShell 5，Python 3.13，PostgreSQL `localhost:5433/kagent_test`（真库）
- 结论：**通过**（满足「失败集合不新增」验收判据；发现 1 个缺陷，见第 5 节，需修复）

全部命令均在 `code/apps/api` 下执行，连接串由 `SCENARIO_TEST_DB_URL` 注入；输出中的口令已统一替换为 `***`。

## 1. 验收判据与基线

| 项 | 基线 | 本次 | 结论 |
| --- | --- | --- | --- |
| 收集用例数 | 1728 | 1740（+12，正好等于新增用例数） | 一致 |
| 失败集合 | `tests/test_health.py::test_openapi_contains_health`（DNS: postgres 不可解析）<br>`tests/wiki/test_worker_and_script.py::test_rebuild_wiki_script_fails_closed_when_disabled`（子进程 DNS 失败） | 同为上述 2 条 | **无新增失败** |
| 错误数 | 0 | 0 | 一致 |
| skipped | 15 | 15 | 一致 |
| 退出码 | 1（因 2 条环境失败） | 1 | 一致 |

全量执行：`python -m pytest -q --tb=line --junitxml=<temp>`，耗时 164.4s，结果 `tests=1740 failures=2 errors=0 skipped=15`。
JUnit 中两条失败逐字核对，均为宿主机解析不到 `postgres` 的环境问题，与本次改动无关。

新增文件 12 条用例在真实环境全部通过（含真库用例 `test_real_db_upgrade_reaches_head`，耗时 1.25s）。
`ruff check scripts/alembic_all_dbs.py tests/scenario/test_alembic_all_dbs.py`：All checks passed。

## 2. 代码核对（设计约束逐条）

| 约束 | 代码事实 | 判定 |
| --- | --- | --- |
| 必须显式给 `--db-key` 或 `--all-scenes` | `argparse` 互斥组 `required=True`；不给时退出码 2，不会隐式迁默认库 | 满足 |
| `--all-scenes` = 控制库 + 全部场景库位 | `build_targets()` 取 `[DEFAULT_DB_KEY, *sorted(settings.scene_db_dsn)]`，不读 `scene_db_split_enabled` | 满足（见 3.1） |
| 未知库位 fail-closed，不回落默认库 | `build_targets()` 抛 `TargetError`，`main()` 返回 1 且在任何迁移前退出 | 满足 |
| 逐库隔离失败 | `run()` 对每个目标单独 `try`，失败 `continue`，最终返回 1 | 满足（见 3.4） |
| 输出不打印口令/完整 DSN | 报告位只用 `_describe_dsn`（`host:port/dbname`），异常经 `safe_error` 脱敏 | 满足（见 3.5） |
| 只迁移不建库 | 只调用 `command.upgrade`，无建库代码 | 满足（见 3.6） |
| 生效通道为 `config.attributes["sqlalchemy_url"]` | 脚本写入 `attributes`；`env.py:29-30` 优先读它，仅缺失时回落 `get_settings().database_url` | 满足（正证见 3.7） |
| 交付物是否落地 | 设计文档 `02-design/第六阶段-Step6-前置裁决.md:283` 已引用该命令与两条显式声明要求 | 满足 |

## 3. 对抗性验证（临时探针，验证后已删除）

临时用例放在 `tests/scenario/test_tmp_dq2_probe.py`，运行后立即删除；未改动任何生产代码。探针全部通过，除第 5 节缺陷对应的断言以「确认缺陷」为目的。

### 3.1 分流关闭但已配 DSN 时 `--all-scenes` 仍覆盖场景库（运维需要）

构造 `scene_db_split_enabled=False` 且 `scene_db_dsn={"kb_fin": ...}`：目标为 `["default", "kb_fin"]`。
`--db-key kb_fin` 同样生效。设计文档 4.3 第 9 步明确要求「先保持 `SCENE_DB_SPLIT_ENABLED=false`」，因此这一行为是正确且必需的，不是缺陷。

### 3.2 无参数 / 互斥参数退出码

- `python -m scripts.alembic_all_dbs` → 退出码 **2**，stderr 为 argparse 必填提示。
- `--all-scenes --db-key default` 同时给 → 退出码 **2**（互斥组）。
- `--db-key kb_missing`（未配置）→ 退出码 **1**，输出「目标库配置错误：未知库位 'kb_missing'…已配置库位：（无）」，未发起任何迁移。

### 3.3 重复库位键去重

`db_keys=["kb_a","default","kb_a"]` → `["kb_a","default"]`；`["default","default"]` → `["default"]`。保留首次出现顺序，不重复执行。

### 3.4 失败隔离（真库，CLI 路径）

`run([Target("bad", 127.0.0.1:1/nope), Target("default", 空库)])`：
输出首库 `FAIL ConnectionRefusedError`，次库 `PASS revision=0031`，汇总「通过 1，失败 1」，退出码 1。
事后查空库 `alembic_version`，确为代码 head `0031`，证明失败库没有阻断后续库的真实迁移。

### 3.5 口令脱敏（真实认证失败路径）

对可达实例使用错误口令（`WrongPw123secret`）执行 `run()`：
- stdout 仅 `badpw FAIL localhost:5433/kagent_test InvalidPasswordError: password authentication failed for user "user"`；
- stderr 为空（alembic 未配置日志输出，未打印 URL）；
- 断言「口令子串」「完整 DSN」均不出现在 stdout/stderr。
`safe_error` 同时替换 `postgresql+asyncpg://` 与 `postgresql://` 两种形态，并额外替换 `make_url(dsn).password`（实测该属性返回**解码后**口令，可覆盖 percent-encoded 场景）。

### 3.6 只迁移、不建库

对不存在的库名执行 `upgrade_one`：抛 `InvalidCatalogNameError: database "…" does not exist"`；事后再查 `pg_database`，该库仍不存在。脚本未创建库。

### 3.7 attributes 通道正证（防假绿）

在测试实例上新建一个**全新空库**，用 `Target` 指向它执行 `upgrade_one`，再直连该库读 `alembic_version`，结果等于 `head_revision()`（`0031`）。这证明迁移确实经由 attributes 通道落到了指定库，而不是落到 `.env` 的库。

### 3.8 对交付测试的「假绿」检查

- `test_real_db_upgrade_reaches_head` 确实执行了真实升级（耗时 1.25s，非 mock），并且 `schema` 夹具已把测试库推到 head。
- 但其断言强度是**间接**的：该用例只断言「不抛错 + 版本==head」。若 attributes 通道失效，`env.py` 会回落到 `.env` 的 `postgres:5432`；本机该主机名不可解析（探针实测 `getaddrinfo failed`），升级会失败，用例才会变红。也就是说，它靠「回落地址不可达」间接兜住通道；一旦有人在 `.env` 里把 `DATABASE_URL` 指向一个可达库，即使通道被删，用例仍会绿（测试库已被夹具推到 head）。
- 结论：**当前不是假绿**，但属于弱证据。第 3.7 节的「新建空库」写法才是强正证，建议纳入交付用例（见 5.2）。
- 其余 11 条为纯单元用例，注入 `upgrade`/`version_reader`，只验证解析、报告与隔离逻辑，不声称验证 alembic 本身，分层合理。

## 4. 实际执行记录（关键命令）

```powershell
# 新增用例
python -m pytest -q tests/scenario/test_alembic_all_dbs.py
# 结果：12 passed

# 全量回归（保存完整日志与 JUnit）
python -m pytest -q --tb=line --junitxml=<temp>\dq2.xml *> <temp>\dq2_full2.log
# 结果：1740 tests, 2 failed, 0 errors, 15 skipped, 164.4s；失败集合 == 基线

# CLI 边界
python -m scripts.alembic_all_dbs                 # rc=2
python -m scripts.alembic_all_dbs --db-key kb_missing   # rc=1，配置错误，未迁移
python -m scripts.alembic_all_dbs --all-scenes          # 仅 default；本机 postgres 不可达 → FAIL，rc=1
```

## 5. 问题清单（按严重度）

### 5.1 缺陷（高）：口令含 percent-encoding 时全库迁移必然失败

- 现象：目标 DSN 口令含需 URL 编码的字符（`@`、`%`、`/` 等，编码后出现 `%`）时，`alembic_config()` 在 `config.set_main_option("sqlalchemy.url", dsn)` 处抛
  `ValueError: invalid interpolation syntax in 'postgresql+asyncpg://user:p%40ss@h:5433/db' at position 27`。
- 影响：该库位在连接数据库之前就失败，所有库位都会失败，脚本退出码 1。属于 fail-closed，无数据风险，但「一键迁移」在常见强口令下不可用；错误信息（插值语法）与真实原因（口令含 `%`）不符，误导排障。
- 同一路径：`alembic/env.py:30` 也用注入值调用 `set_main_option`，即使删除脚本里的同一行，问题依旧；`tests/scenario/pg_helpers.py:83` 有相同写法（既有问题，测试库口令无特殊字符故未暴露）。
- 已确认的边界：本缺陷下输出仍被脱敏（`h:5433/db`），且失败隔离仍成立（同批好库 PASS），即脱敏与隔离两条纪律没有被破坏。
- 修复建议：调用 `set_main_option` 前将 `%` 转义为 `%%`（`ConfigParser` 读取时会还原），即脚本与 `env.py` 两处都改为
  `config.set_main_option("sqlalchemy.url", dsn.replace("%", "%%"))`；`attributes` 仍保存原始 DSN。
  修复后需补一条「口令含 `@`/`%` 的 DSN 能进入真实升级」的用例（可用本地实例的临时库）。
- 证据：临时探针 `test_probe_encoded_password_breaks_alembic_config`、`test_probe_encoded_password_run_masks_and_isolates`（已删除）。
- 修复状态：已关闭（修复内容与独立复测见第 8 节）。

### 5.2 建议（中）：真库用例证据间接，建议补正证

- `test_real_db_upgrade_reaches_head` 只验证「升级不抛错 + 版本等于 head」，而 `schema` 夹具事先已把库推到 head；通道失效时依赖 `.env` 回落地不可达才变红。
- 建议改为强正证：新建一个空库（或把测试库降到上一个 revision），经 `Target` 升级后断言 `alembic_version` 从旧值变为 head；这样与 `.env` 是否可达解耦。

### 5.3 建议（低）：补齐运维与异常分支覆盖

- 未覆盖「`SCENE_DB_SPLIT_ENABLED=false` 但已配置 DSN」时 `--all-scenes` 覆盖场景库（本次已用探针验证行为正确，建议固化进交付用例，防止后人「顺手」加开关判断）。
- 未覆盖 `version_reader` 抛异常被计为失败且继续后续库（实现已正确，属覆盖缺口）。
- 未覆盖 `main()` 级 `--all-scenes` 传参路径（当前只测了 `build_targets`）。

### 5.4 观察（需产品确认，不计缺陷）

- 目标解析是「全有或全无」：`--db-key default --db-key kb_typo` 时 `default` 也不会迁移（先抛 `TargetError`）。
  这符合「未知库位 fail-closed」，但与「某库失败不阻断其他库」在执行阶段的口径不同。若期望解析阶段也逐库跳过，需要产品明确；当前实现未回落默认库，安全方向正确。
- `run()` 中 `head_revision()` 在逐库 try 之外，alembic 目录损坏时会抛裸 traceback（不含 DSN）。属极端环境，优先级低。
- 脱敏用 `str.replace(password, "***")`，当口令恰好是输出文本的普通子串时会误伤可读性（如本次环境口令使 `Password` 显示为 `***word`）。无泄露，纯观感问题。

## 6. 交付检查结论

- 新增脚本的 6 条设计约束逐条满足，且有真实运行证据；attributes 通道已用全新空库正证。
- 输出脱敏在「异常消息带完整 DSN」「真实认证失败」两条路径上均有效，未发现口令或完整 DSN 泄漏。
- 全量回归失败集合与基线完全一致，无新增失败、无新增 error，skipped 与基线一致。
- 唯一偏差是 5.1 的口令 percent-encoding 缺陷：不阻断本次验收判据，但会在真实强口令环境直接导致工具不可用，建议合并前修复并补用例。
- 未修改任何被测生产代码；临时探针文件与临时数据库均已清理（`pg_database` 中无 `kagent_dq2%` 残留）。

## 7. 附：本次运行的 head 与版本

- 代码 head：`0031`
- 真库用例升级后版本：`0031`
- 新建空库经脚本升级后版本：`0031`

## 8. 修复后复测（独立验证，2026-11-24）

复测结论：**通过**。5.1 的高危缺陷已关闭；转义未损坏真实口令；静态守卫能拦住两种退化；全库脚本的其余纪律未回退。本节不删除第 5.1 节的原始缺陷记录。

### 8.1 修复内容核对（读代码与 git diff）

| 文件 | 修复事实 |
| --- | --- |
| `alembic/env.py` | `_url = _injected_url or get_settings().database_url`；唯一一处 `set_main_option("sqlalchemy.url", _url.replace("%", "%%"))`。注入优先、配置兜底、attributes 通道三条均保留 |
| `scripts/alembic_all_dbs.py` | `alembic_config()` 只写 `config.attributes["sqlalchemy_url"]`，不再自行 `set_main_option`；`build_targets()` / `run()` / `safe_error()` 本次未改动 |
| `tests/scenario/pg_helpers.py` | `set_main_option("sqlalchemy.url", target.replace("%", "%%"))`，同时保留 attributes 通道 |
| `tests/scenario/test_0028_migration.py` | 守卫新增：值外层必须是 `.replace("%", "%%")`，且内部仍校验「attributes 注入 or settings 兜底」；`sqlalchemy.url` 赋值唯一性断言未变 |

### 8.2 原缺陷路径复现（同一输入 `postgresql+asyncpg://user:p%40ss@db-host:5433/kagent`）

- 旧写法 `Config().set_main_option("sqlalchemy.url", 原始 DSN)`：仍抛 `ValueError: invalid interpolation syntax ... at position 27`。说明原缺陷的触发点真实存在，不是测试构造问题。
- 新路径 `alembic_config(DSN)` + `command.upgrade(..., "head", sql=True)`：正常执行 env.py 并渲染 SQL（46901 字符），**未出现插值错误**。
- 结论：原缺陷消失。

### 8.3 转义未损坏口令（读回值与注入值逐字比较）

- `config.attributes["sqlalchemy_url"]` == 注入 DSN
- `config.get_main_option("sqlalchemy.url")` == 注入 DSN
- `config.get_section("alembic")["sqlalchemy.url"]` == 注入 DSN（这是 `async_engine_from_config` 实际读取的值，也是最终进入引擎的字符串）
- `pg_helpers.alembic_config()` 对同一 `%40` DSN 的读回值同样与注入值一致
- 说明 `%%` 只在 `ConfigParser` 存取期间存在，读回时已还原为单个 `%`，没有双重转义或口令被改坏。

### 8.4 静态守卫的退化验证（临时改文件，已还原）

在 `alembic/env.py` 上做两次临时退化，仅运行守卫用例 `test_env_py_honors_injected_url_before_app_settings`，每次运行后立即从内存原文还原：

| 退化写法 | 守卫结果 | 命中的断言 |
| --- | --- | --- |
| 去掉转义：`set_main_option("sqlalchemy.url", _url)` | rc=1（失败） | 外层必须是 `.replace("%", "%%")`（`ast.Call`） |
| 退回无条件覆盖：`set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))` | rc=1（失败） | 必须是 `注入值 or 兜底值`（`ast.BoolOp`） |
| 还原后 | 文本内容与改前一致；但临时写入时被平台转换为 CRLF，已归一化回 LF 并复核（`crlf=0`，与 HEAD 的 diff 仍为 5 增 1 删） | 还原脚本内存比对 `restored=True`，行尾另行修正 |

补充说明：守卫对「无条件覆盖」的拦截依赖外层转义断言先通过，因此退化写法必须同时带 `.replace("%", "%%")` 才走到 `ast.BoolOp` 断言；这是守卫的实现顺序，不影响拦截效果（两种退化都变红）。

### 8.5 `--all-scenes` / 失败隔离 / 脱敏抽查（真库）

- `--all-scenes`：`scene_db_split_enabled=False` 且配置 `{"kb_fin": ...}` 时目标为 `["default", "kb_fin"]`，覆盖场景库，符合运维要求。
- 失败隔离（新空库 + 不可达库）：`bad FAIL ConnectionRefusedError`、`good PASS revision=0031`，汇总「通过 1，失败 1」，退出码 1；事后直连该空库读到 `0031`，证明失败库未阻断好库的真实迁移。
- 脱敏：同上输出中无口令、无完整 DSN；stderr 为空。
- 结论：三项均未回退。

### 8.6 本轮实际执行记录

```powershell
# 定向用例
python -m pytest -q tests/scenario/test_alembic_all_dbs.py tests/scenario/test_0028_migration.py
# 结果：25 passed

# 全量回归（保存完整日志与 JUnit）
python -m pytest -q --tb=line --junitxml=<temp>\dq2_retest.xml *> <temp>\dq2_retest.log
# 结果：1730 tests, 2 failed, 0 errors, 15 skipped, 153.7s
```

- 收集数说明：`1730 = 基线 1728 + 本轮新增 2 条`（上一轮报告的 1740 含当时临时探针的 12 条，探针已删除）。
- 失败集合：仍为基线两条环境失败（`tests/test_health.py::test_openapi_contains_health`、`tests/wiki/test_worker_and_script.py::test_rebuild_wiki_script_fails_closed_when_disabled`），**无新增失败**。
- 新增用例 `test_config_accepts_percent_encoded_password`、`test_offline_render_accepts_percent_encoded_password` 在全量中通过；后者真正执行了 env.py 的 `set_main_option`，是本次修复的直接证据。

### 8.7 复测后的遗留项

- 5.2（真库用例证据间接）未在本轮修复，仍为建议项。
- 5.3（split 关闭时 `--all-scenes`、`version_reader` 抛错、`main()` 级路径的覆盖缺口）仍为建议项；其中 split 关闭行为已由探针再次确认正确。
- 5.4 观察项不变。
- 未改动任何生产代码（仅临时修改 env.py 做退化验证并已还原；还原后行尾由 CRLF 归一化回 LF，复核 `crlf=0` 且定向用例 25 passed）；临时探针脚本已删除。
- 自查披露：退化验证的临时写入经 Python 文本模式把 LF 变成了 CRLF，属测试操作引入、已修正；最终文件与 HEAD 的差异仍只有本次修复的 5 行新增与 1 行删除。
