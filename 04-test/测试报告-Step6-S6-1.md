# 测试报告 · Step 6 / S6-1（连接预算口径 B：预留 20，预算 80）

> 角色：测试工程师（P3，独立于编码者，不修改产品代码）
> 被测变更：`db_conn_reserved` 默认 10 → 20；`placeholders.DB_CONN_RESERVED` 同步 20；两个 Step 5 测试文件字面量重基线 + 回归/边界重设计；文档口径收口。
> 验收依据：`02-design/第六阶段-Step6-前置裁决.md` §2.3、§2.5（含验收段）
> 执行环境：Windows PowerShell 5，Python 3.13.14，工作目录 `code/apps/api`
> 执行时间：2026-11-23

---

## 1. 结论

**可以进入 commit。** 无阻断、无缺陷；独立验证的 18 项判据全部通过，回归 37 项全部通过。仅登记 4 条建议级残余风险（见 §5）。

---

## 2. 验证项清单与结论

| # | 验证项 | 判据 | 结论 |
|---|---|---|---|
| V1 | `Settings.model_fields["db_conn_reserved"].default` | 必须为 20 | PASS（实际 20） |
| V2 | `Settings()` 生效值（含 `.env`） | 必须为 20 | PASS（`code/.env` 无该变量覆盖） |
| V3 | `placeholders.value_of("DB_CONN_RESERVED")` | 必须 == Settings 默认值 == 20 | PASS |
| V4 | 默认四元组 `_arithmetic(Settings())` | 必须 `(40, 1, 50, 80)` | PASS |
| V5 | 闸门边界：needed == budget（50/50） | 必须放行（不 raise） | PASS |
| V6 | 闸门边界：needed == budget + 1（51/50） | 必须 raise，消息含 51 与 50 | PASS |
| V7 | 默认参数下 K=2（每桶 40 / N=1） | `(40,2,90,80)` 且 raise | PASS |
| V8 | 拒绝消息的三条可调项 | 含 `DB_POOL_SIZE`、`减库位`、`减副本` | PASS（另含"不要靠调大 max_connections"） |
| V9 | 启动横幅实际渲染 | 打印 `预算 50/80`，不得出现 `50/90` | PASS（实测输出见 §3.3） |
| V10 | 横幅站位值行 | 含 `DB_CONN_RESERVED=20` | PASS |
| V11 | AST：`app/` 内 `data_session` 调用点 | 必须为 0（S6-4 前） | PASS（`[]`） |
| V12 | AST：`data_session` 定义次数与位置 | 仅 1 次，位于 `app/infra/db_router.py` | PASS |
| V13 | 代码内 `db_conn_reserved` / `DB_CONN_RESERVED` 残留 10 | 不得存在 | PASS（见 §3.4） |
| V14 | `02-design` 与交接文档的口径残留 | 除前置裁决中"已否决 A/C 口径的裁决记录"外无残留 | PASS |
| V15 | 框架稿表格中 90 是否仍标为可行 | 必须标为超预算 80 | PASS |
| V16 | 手册表格中 90 是否仍标为可行 | 必须标为不可行 | PASS |
| V17 | 三个 Step 5 测试文件回归 | 全绿 | PASS（37 passed） |
| V18 | S6-1 范围外改动 | 无 `data_session` 调用点、无 T-5-7/T-5-8 守卫改动、无建库/迁移脚本改动 | PASS（见 §3.6） |

以上 V1–V12 由测试工程师自写脚本独立断言（不依赖编码者的测试用例），脚本路径：`C:\Users\86186\AppData\Local\Temp\s6_1_independent_check.py`（位于系统临时目录，未污染仓库）。

---

## 3. 执行证据

### 3.1 回归：三个 Step 5 测试文件

命令（工作目录 `code/apps/api`）：

```
python -m pytest tests/config/test_step5_settings.py tests/scenario/test_step5_db_router.py tests/scenario/test_step5_db_routing_report.py -q
```

真实输出：

```
.....................................                                    [100%]
37 passed in 0.80s
EXIT=0
```

分文件计数（同一命令逐文件执行）：

```
tests/config/test_step5_settings.py => 9 passed in 0.13s
tests/scenario/test_step5_db_router.py => 16 passed in 0.66s
tests/scenario/test_step5_db_routing_report.py => 12 passed in 0.12s
EXIT=0
```

附加旁证（池口径未受牵连）：

```
python -m pytest tests/config/test_db_pool.py
4 passed in 0.41s
EXIT=0
```

### 3.2 独立判据脚本

命令：

```
python "$env:TEMP\s6_1_independent_check.py"
```

输出（摘要，逐条 PASS，末行 `FAILED_ITEMS=0`，退出码 0）：

```
PASS Settings.model_fields 默认值 == 20 | 实际 20
PASS Settings() 生效值 == 20 | 实际 20
PASS placeholders 镜像 == Settings 默认值 | 镜像 20
PASS 默认四元组 == (40,1,50,80) | 实际 (40, 1, 50, 80)
PASS needed==budget (50/50) 算式
PASS needed==budget 必须放行
PASS needed==budget+1 (51/50) 算式
PASS needed==budget+1 必须拒绝 | 数据层连接预算超限，拒绝启动：40×1(副本)×1(库位)+11(worker) = 51 > 50；max_connections 预算 = 70 - 预留 20 = 50。…
PASS N=1/K=2/每桶40 算式 == (40,2,90,80)
PASS 90/80 必须拒绝启动 | 数据层连接预算超限，拒绝启动：40×1(副本)×2(库位)+10(worker) = 90 > 80；max_connections 预算 = 100 - 预留 20 = 80。处置：① 降 DB_POOL_SIZE/DB_MAX_OVERFLOW；② 减库位；③ 减副本。★ 不要靠调大 max_connections 解决
PASS 提示含池大小可调项 DB_POOL_SIZE
PASS 提示含减库位
PASS 提示含减副本
PASS 提示禁止调大 max_connections
PASS 横幅含 预算 50/80
PASS 横幅不含 50/90
PASS 横幅含站位值 DB_CONN_RESERVED=20
PASS app/ 内 data_session 零调用点（S6-4 前） | []
PASS data_session 只定义一次且在 db_router.py | ['…\\app\\infra\\db_router.py']
FAILED_ITEMS=0
EXIT=0
```

### 3.3 启动横幅实际输出（真实调用 `log_db_router_banner`）

用假会话工厂返回 `SHOW max_connections = 100`，捕获 `app.infra.db_router` 的日志记录：

```
DB 路由 : 关闭（全部落默认库） · 桶=1 · 每桶 40 · 总计 40 · 预算 50/80 · max_connections 实测=100
含站位值参数: DB_CONN_RESERVED=20（待实测标定 · 框架 §3A.4 明确它是「设定值不是实测」；默认 20 = 迁移 1 / pg_dump 1~2 / 监控 1~2 / psql 排障若干）, DB_WORKER_CONN_TOTAL=10（…）, DB_REPLICAS_EXPECTED=1（…）
```

结论：横幅打印的是 `50/80`；旧口径 `50/90` 未出现。

### 3.4 全仓一致性（代码）

命令：检索全仓 `db_conn_reserved|DB_CONN_RESERVED`。

真实结果要点：

- `app/config/settings.py:83` `db_conn_reserved: int = 20`
- `app/config/placeholders.py:74` `name="DB_CONN_RESERVED"`，`value=20`
- `app/infra/db_router.py` 仅消费该字段（`:157`、`:272`、`:307`、`:335`），无字面量 10
- `app/config/config_profile.py:158` 仅取 `s.db_conn_reserved` 入快照
- 测试文件中的期望值均为 20
- 唯一出现"10"的是 `02-design/第六阶段-Step6-前置裁决.md` 中的裁决记录原文（描述"10 → 20"这一变更本身），属允许项

命令：检索 `code/**/*.example` 中的 `DB_CONN_RESERVED|db_conn_reserved`：无匹配（编排侧无 10 残留默认值）。
另核实 `code/.env` 存在，但未设置 `DB_CONN_RESERVED` / `DB_MAX_CONNECTIONS_FOR_BUDGET` / `DB_POOL_SIZE` / `DB_MAX_OVERFLOW`，故 `Settings()` 生效值等于代码默认值。

### 3.5 文档一致性

命令：检索 `02-design` 中 `预留=10|预留\(10\)|预留 10|100 − 10|100-10|90/90|恰好触顶`。

真实结果：5 处命中，全部位于 `02-design/第六阶段-Step6-前置裁决.md`（`:98`、`:132`、`:160`、`:177`、`:412`），均为"描述已否决的 A/C 口径"的裁决记录，属允许项。其余设计文档 0 命中。

命令：检索全仓 `*.md` 中 `预留|50/90|预算 90|90/90`，确认除前置裁决外无旧口径。

逐份核对（git diff）：

- `第六阶段-Step5-详细设计.md`：`I-5-0s` / `F-7` 改为 `预留(20) = 80`；`K_max` 重算为 `floor(70/(P×N))`；表内 90 行由"⚠️ 恰好触顶"改为"❌ 超限 → 拒绝启动"；横幅示例改 `50/80`；附录源表改 `预留=20（预算 80）`。
- `统一RAG平台-场景化配置-框架设计-v1.md:341`：改为"预留取 20，预算 80"；表格中 `N=1,K=2=90` 与 `N=2,K=1=90` 均加注"超预算 80"；池上限公式改为 `(100 − 20 − 10)/(N×K) = 70/(2K)`。
- `部署标定手册.md`：`N=2,K=1` 的 90 由"⚠️ 顶满"改为"❌ 默认 100 下超限"；`N=2,K=2` 改为"❌ 超预算 80"；新增"应用以集群超级用户连接，superuser_reserved 不挤压应用"一句；池上限公式同步扣 worker 10。
- `交接文档-2026-11-22.md`：**本轮未改动**，但内容已与 20/80 一致（`:187-188` 写"`90 > 80`，拒绝启动"），无 90/90 残留，无需改动。

结论：框架稿与手册表格中 90 不再被标为可行配置。

### 3.6 范围外改动检查（`git diff` / `git status`）

真实 `git status --porcelain`：

```
 M 02-design/第六阶段-Step5-详细设计.md
 M 02-design/统一RAG平台-场景化配置-框架设计-v1.md
 M 02-design/部署标定手册.md
 M code/apps/api/app/config/placeholders.py
 M code/apps/api/app/config/settings.py
 M code/apps/api/tests/config/test_step5_settings.py
 M code/apps/api/tests/scenario/test_step5_db_router.py
?? 02-design/第六阶段-Step6-前置裁决.md
```

`git diff --stat`：7 个已跟踪文件，115 insertions / 49 deletions。

逐项核对：

- `app/infra/db_router.py` **未被修改**（算术与横幅逻辑保持原样，符合"只改常量"的裁决）。
- `api/kbs.py`、`api/chat.py`、`api/wiki.py`、`api/sync.py` 未改动。
- `alembic/` 与任何建库/迁移脚本未改动。
- `tests/scenario/test_step5_db_router.py` 的改动仅含：T-5-4 四组期望第四元 90→80、T-5-5 断言与边界用例重设计、新增 2 条 S6-1 回归；**T-5-7 / T-5-8 的 AST 守卫块逐字未动**（diff 中该段无 hunk）。
- AST 独立扫描：`app/` 内 `data_session` 调用点 = 0，定义 = 1（`db_router.py`），满足"S6-4 之前必须仍是零调用点"。

---

## 4. 未覆盖项（如实标注）

| 项 | 状态 | 原因 |
|---|---|---|
| 需要 Docker / PostgreSQL / Redis 的场景集成测试（如 `tests/scenario` 中连库用例、迁移往返、`db_routing_report` 真连库执行） | 受阻，未验证 | 本机容器状态未知/不稳定（前置裁决 §5.2 记录 `code-postgres-1` 为 Exited(255)、worker 因 Redis 不可达循环重启）；本任务要求的三份文件均属纯单测，不依赖 DB |
| 真实 `SHOW max_connections` 读取 | 以假会话工厂替代 | 同上无可用 PG；横幅逻辑本身不依赖真实 DB，`log_db_router_banner` 对读取失败只 WARN（不 raise） |
| 真实 `alembic upgrade` / 分库演练（S6-2 起的工作） | 不在 S6-1 范围 | S6-1 仅收敛常量与文档 |

以上未覆盖项均未用推断或伪造结果替代，明确记为"未验证"。

---

## 5. 缺陷与残余风险

**缺陷：0。阻断：0。**

| 编号 | 级别 | 内容 | 处置建议 |
|---|---|---|---|
| R-1 | 建议 | `tests/scenario/test_step5_db_routing_report.py:29` 夹具默认 `budget=90`，`:147-152` 用例断言 `needed=90 == budget=90 → PASS`。该夹具是独立入参、不读 Settings（前置裁决 §2.3 明文要求不改），生产路径的 `budget` 来自 `_arithmetic`（=80），因此不构成假绿；但文件内未注明"该 90 仅为本文件夹具、与 Settings 无关"，未来易被误抄回产品口径。 | 可选：在 `_report()` 的 docstring 补一句口径说明。不阻塞交付。 |
| R-2 | 建议 | `scripts/db_routing_report.py:152` 的⑤判定仍为 `needed <= budget`（等于放行）。这与启动闸门语义一致（前置裁决 §0.2 认可的"零余量放行"）；B 口径下 90 被拒靠的是 `budget=80`，而非把判定改成 `>`。若将来有人把预算改回 90，零余量放行会重新成立。 | 属"常量单点决定口径"的固有权衡，已由前置裁决承认；无需改动。 |
| R-3 | 建议 | 编码者声明的"4 处文档收口"中，`交接文档-2026-11-22.md` 实际未被修改（`git log` 显示其在 `88ab8e3` 提交时就已是 20/80 口径）。内容正确，无残留，但完成声明与 git 事实存在口径差异。 | 无需改动；建议后续完成声明以 `git diff` 为准。 |
| R-4 | 建议 | `02-design/第六阶段-Step6-前置裁决.md` 当前为未跟踪文件（P1 裁决产物）。若直接 `git add -A` 会一并纳入 S6-1 提交。 | commit 前明确该文件是随本次提交，还是单独提交；不影响 S6-1 本身。 |

---

## 6. 是否可以进入 commit

**可以。** 验收依据 §2.5 的四条验收信号全部实测达成：

1. 三个 Step 5 测试文件全绿（37 passed，退出码 0）。
2. 启动横幅打印 `50/80`（实测日志原文见 §3.3），非旧的 `50/90`。
3. `N=1/K=2/每桶 40`（needed 90 > 80）启动被拒，提示含池大小 / 减库位 / 减副本三项。
4. 边界两侧已验证：`needed == budget` 放行、`needed == budget + 1` 拒绝。

提交前唯一需确认的是 R-4（未跟踪的前置裁决文档是否随本次提交）。S6-1 产品代码与测试可原样提交。
