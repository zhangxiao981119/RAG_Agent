# 测试报告：Step6 S6-7 备份脚本覆盖 N 库（验收 commit b2b75a6 / 复验 949fe1a）

- 被测提交：`b2b75a6`（S6-7 实现）；复验提交 `949fe1a`（P1 修复）
- 仓库：`c:\Users\86186\Desktop\AI Agent学习\file_rag_agent`
- 测试角色：独立测试（不采信实现者结论，全部命令实际执行）
- 测试时间：初测 2026-11-23 18:15 - 18:35；复验 18:40 - 18:55（Asia/Shanghai）
- 被测文件：
  - `code/scripts/backup.sh`
  - `code/apps/api/tests/scenario/test_step6_backup_script.py`
  - `code/docs/DEPLOY.md` §5（备份与恢复）、`.gitignore`
  - 设计：`02-design/第六阶段-Step6-前置裁决.md` §4.8
- 关联：`code/scripts/release.sh:5`（`set -euo pipefail`）、`:11`（`bash scripts/backup.sh`）

---

## 一、结论

**S6-7 验收通过，建议关闭；初测的 P1 已由 `949fe1a` 关闭，无未关闭问题。**

- 测试真跑且**无 skip**：`8 passed in 2.38s`。本机 WSL 可用，3 条行为层用例全部真跑脚本（假 docker 替换外部命令），不是只做字符串断言。
- 核验点全部落地：库清单来自 `pg_database`、控制库与全部场景库各自一份 `-Fc` dump、任一库失败整次非零退出且不产出 manifest、manifest 字段齐全、RPO 政策值与设计/拍板一致。
- **P1（MinIO 恢复路径）已修复**：`DEPLOY.md` §5.2 现指向 `$PWD/backups/<时间戳>/minio-data.tar.gz`，与 `backup.sh` 在 `code/` 下的实际产物一致；并新增「有效备份判据 = `manifest.txt` 存在且 `database_count` 与实库一致」。
- 初测 P2/P3 中的三项已处理并实测确认：备份产物已进 `.gitignore`；窗口计时提前到枚举之前（实测 `window_seconds=2`）；同秒重跑显式失败（实测 `mkdir: File exists`、退出 1、不覆盖）。
- 残留风险：失败时残缺 dump 仍留在磁盘（判据已写进手册，降级为 P3）；MinIO 失败、特殊库名、RTO 演练仍无用例。

---

## 二、环境

| 项 | 值 |
|---|---|
| OS | Windows（PowerShell 5） |
| Python | 3.13.14 |
| ruff | 0.16.1 |
| bash | WSL（`wsl -e bash` 可执行，行为层未 skip） |
| PostgreSQL | 容器 `code-postgres-1`，PostgreSQL 16.15，宿主 `localhost:5433` |
| 测试库 | `kagent_test`（口令取自 `code/.env`，未打印） |
| 工作区状态 | 未修改代码、未提交；仅在系统临时目录写探针脚本 |

---

## 三、实测记录

### 3.1 用例（复验，真跑）

```
cd code\apps\api
python -m pytest tests/scenario/test_step6_backup_script.py -q -o addopts="" -rs
```
```
........                                                                 [100%]
8 passed in 2.38s
```
`-rs` 无任何 skip 行，说明 `bash_cmd` 夹具未降级：5 条契约层 + 3 条行为层全部执行。

### 3.2 ruff

```
python -m ruff check tests/scenario/test_step6_backup_script.py
```
```
All checks passed!
```
（`backup.sh` 是 bash，不归 ruff；脚本头 `set -euo pipefail`。）

### 3.3 独立探针：失败路径（初测，假 docker 真跑 `backup.sh`）

```
PROBE minio_fail: exit=1 files=['20261123-182037\\db_kagent.dump']
PROBE minio_fail output=minio tar failed
PROBE census_fail: exit=1 files=[]
PROBE census_fail output=psql: connection refused | [备份] 失败：未枚举到任何业务数据库（检查 postgres 容器是否在运行）
PROBE dump_fail_partial: exit=1 files=['20261123-182038\\db_kagent.dump']
PROBE dump_fail_partial output=dump failed | [备份] 失败：数据库 kagent 导出失败
```

- `minio_fail`：MinIO 打包失败 → 退出 1，目录里留下**已完成的 `db_kagent.dump`**，**没有 manifest**。MinIO 失败不会被掩盖，但产物不完整。
- `census_fail`：枚举 SQL 失败（psql 退出 1）→ 被 `|| true` 吞成空列表 → 统一报「未枚举到任何业务数据库」；真实原因保留在 stderr。
- `dump_fail_partial`：pg_dump 先吐了部分字节再失败 → 退出 1、无 manifest，但**残缺 `db_kagent.dump` 留在目录**。

### 3.4 独立探针：真实 `-Fc` dump → 空库 restore（初测）

```
PROBE restore_ok: triggers 1->1, documents_fks 0->0, tables=38
```

custom dump 可恢复到「先建空库」；手术状态（`tenants` 上 1 个禁写触发器、`documents` 上 0 个跨库 FK）完整保留。

### 3.5 复验 `949fe1a` 的三项修复（本次实测）

探针脚本在 WSL 里用带 `sleep 2` 的假 psql 真跑 `backup.sh`，并预建同名 stamp 目录模拟同秒重跑：

```
PROBE window_exit=0
window_seconds=2
database_count=2
PROBE same_second_attempt=1 exit=1
mkdir: cannot create directory ‘/tmp/tmp.UG3P1SpfUD/same/20261123-182352’: File exists
PROBE leftover_files=0
```

- 窗口：psql 故意睡 2 秒，`window_seconds=2` —— 计时已包含枚举阶段（修复前从枚举后开始，会接近 0）。
- 同秒重跑：第 1 次尝试即命中，退出 1，报 `File exists`，且该目录内**没有被写入任何文件**（`leftover_files=0`），确认不会覆盖上一次备份。
- `.gitignore`：
  ```
  .gitignore:43:code/backups/	code/backups/x.dump
  .gitignore:44:backup-pg-*.dump	backup-pg-x.dump
  .gitignore:45:backup-minio-*.tar.gz	backup-minio-x.tar.gz
  exit=0
  ```

### 3.6 DEPLOY §5.2 修复核对

```bash
# 2. 恢复 MinIO 文件（与 5.1 推荐脚本产物同名同目录）
docker run --rm -v kagent_minio_data:/data -v "$PWD":/backup alpine \
  sh -c "cd /data && tar xzf /backup/backups/XXXXXXXX-XXXXXX/minio-data.tar.gz"
```

`PWD` 为 `code/`，落到 `code/backups/<stamp>/minio-data.tar.gz`，与 `backup.sh:13`（`OUT_DIR="${BACKUP_DIR:-backups}/${STAMP}"`，脚本先 `cd "$(dirname "$0")/.."` = `code/`）及 `:63-64` 的产物完全一致。P1 关闭。

---

## 四、覆盖矩阵（核验点 × 代码 × 用例 × 独立核验）

| # | 核验点 | 实现 | 现有用例 | 独立核验 | 结论 |
|---|---|---|---|---|---|
| 1 | 库清单来自 `pg_database`，排除模板/不可连接/postgres，不依赖配置清单 | `backup.sh:30-34` | 契约 `test:112-122`（含反向断言 `-d "$POSTGRES_DB"` 不得出现）；行为 `test:206-231` | 假 docker 枚举同款 SQL 文本被脚本实际使用 | 通过 |
| 2 | 控制库与所有场景库都备份 | 逐行循环 `:45-59` | 行为 `test:217-221` 断言两份 dump 内容分别为 `PGDUMP:<db>`，无 `db_postgres.dump` | 探针：枚举 2 库 → 2 文件 | 通过 |
| 3 | 每库 custom dump（`-Fc`） | `:48` | 契约 `test:125-131` | §3.4 真实 `-Fc` 产物可 `pg_restore` | 通过 |
| 4 | 任一库失败整次失败且**不产出 manifest** | `:48-52`（`if ! ... ; then exit 1`）；manifest 最后写 | 行为 `test:234-245`（断言非零 + `rglob('manifest.txt')==[]`） | §3.3 `dump_fail_partial`：exit=1、无 manifest | 通过 |
| 5 | manifest 记录 `started_at/finished_at/window_seconds/database_count/rpo_policy` + 每库 `seconds/bytes/file` | `:71-86`（另有 `backup_stamp`、`minio_seconds`） | 契约 `test:143-157`；行为 `test:224-231` | §3.5 实测字段与值 | 通过 |
| 6 | RPO 政策值 = 每日一次全库 dump | `:19-20`、`:79` | 契约 `test:156`、行为 `test:226` | 与 §4.8 第 363 行拍板一致 | 通过 |
| 7 | 恢复命令能按 `db_<name>.dump` 恢复；场景库先建空库 | `DEPLOY.md:300-306` | 无用例 | §3.4 真演练通过 | 通过 |
| 8 | DEPLOY MinIO 恢复命令与脚本产物一致 | `DEPLOY.md:308-310` | 无用例 | §3.6 路径逐段核对 | **通过（P1 已关闭）** |
| 9 | 有效备份判据写入手册 | `DEPLOY.md:312`（新增引用块） | 无用例 | 与 manifest 落盘时机（全部成功后才写）一致 | 通过 |
| 10 | 同秒重跑不覆盖 | `:14-16`（父目录 `mkdir -p`，本体 `mkdir`） | 无用例 | §3.5 实测 exit=1 + `File exists` | 通过 |
| 11 | 窗口包含枚举 | `:22-24`（`START_EPOCH` 前移） | 无用例 | §3.5 实测 `window_seconds=2` | 通过 |
| 12 | 备份产物不入公开仓库 | `.gitignore:42-45` | 无用例 | §3.5 `git check-ignore` 三条全命中 | 通过 |
| 13 | 行为层真跑脚本、断言 dump 内容/manifest/失败中止/空枚举报错 | — | `test:206-231`、`test:234-245`、`test:248-284` | 我另跑三种失败路径复核 | 通过 |
| 14 | release 发布前备份 | `release.sh:5`、`:11` | 无用例（静态确认） | 静态阅读 | 通过（无测试） |

---

## 五、问题清单（复验后状态）

### P1（中，已关闭）DEPLOY §5.2 的 MinIO 恢复命令与推荐脚本产物不一致

- 初测证据：`DEPLOY.md:309-310` 指向 `backup-minio-<stamp>.tar.gz`，而推荐脚本产出 `backups/<stamp>/minio-data.tar.gz`。
- 修复：`949fe1a` 把命令改为 `-v "$PWD":/backup` + `tar xzf /backup/backups/<时间戳>/minio-data.tar.gz`，并新增「无 manifest 即无效」判据段（`DEPLOY.md:312`）。
- 复验：§3.6 路径逐段核对与 `backup.sh` 一致；§3.1 用例 8 passed。
- 状态：**关闭**。

### P2（中低，已关闭）备份产物未被 gitignore 忽略

- 初测证据：`git check-ignore code/backups/x.dump` 退出 1，公开仓库有误提交整库 dump 风险。
- 修复：`949fe1a` 在 `.gitignore:42-45` 增加 `code/backups/`、`backup-pg-*.dump`、`backup-minio-*.tar.gz`。
- 复验：§3.5 三条规则全部命中，`exit=0`。
- 状态：**关闭**。

### P3（低，保留）失败时残缺 dump 仍留在磁盘

- 证据：§3.3 `dump_fail_partial` / `minio_fail` 目录里都有 `db_kagent.dump`。
- 现状：`949fe1a` 已在 `DEPLOY.md:312` 明确「清单缺失或中途失败的目录一律视为无效，不得用于恢复」，判据问题解决；但脚本本身不清理残留文件。
- 建议（非阻塞）：失败分支 `rm -rf "$OUT_ABS"`，或在目录名后缀 `.failed`。
- 状态：**保留（降级）**。

### P3（已关闭）`window_seconds` 不含库枚举耗时

- 初测：`START_EPOCH` 在枚举之后。
- 修复：`949fe1a` 把 `START_EPOCH/START_UTC` 前移到枚举之前（`:22-24`）。
- 复验：§3.5 `window_seconds=2`（psql 人为睡 2 秒）。
- 状态：**关闭**。

### P3（已关闭）同秒重复运行会覆盖同一 stamp 目录

- 初测：`mkdir -p` + 秒级时间戳。
- 修复：父目录 `mkdir -p`，本体改 `mkdir "$OUT_DIR"`，已存在即失败（`set -e` 中止）。
- 复验：§3.5 第 1 次尝试即 `exit=1`，`File exists`，目录内 0 文件。
- 状态：**关闭**。

### P3（保留）枚举失败被 `|| true` 统一吞成「空枚举」

- 证据：`backup.sh:30-38`；§3.3 `census_fail`。容器未启动、账号无权限、SQL 写错都报同一条提示，真实原因只在 stderr。可接受，排障时需看 stderr。

### P3（保留）特殊库名会破坏 manifest 或输出路径

- `db_${db}.dump` 与 TSV 行直接拼库名；库名含 TAB 会打乱列，含 `/` 会导致重定向失败，含换行会被枚举拆成两个库名。无用例、无转义。

### P3（保留）手册要求「`database_count` 与实库一致」，但脚本不自动校验

- `DEPLOY.md:312` 把「`database_count` 与实库一致」作为有效判据，脚本只写成功计数、不回查实例库数（成功路径下两者必然相等；差异只可能来自备份后到校验前新建库）。属人工判据，建议手册说明「与执行 `pg_database` 枚举结果比对」。

### P3（信息）

- `mkdir` 失败时只输出 mkdir 原始错误（无「已有同名备份目录」中文提示），运维需自行理解；判定为可接受。
- `COMPOSE`/`$DOCKER` 未加引号（`:11`、`:31`、`:48`）：`BACKUP_DOCKER` 路径含空格会断（仅测试钩子受影响，生产 `docker` 无空格）。
- `minio_seconds` 不在验收字段清单里，但确实落盘，属有益补充。

---

## 六、未覆盖与风险

1. **MinIO 失败路径无用例**：实现正确（§3.3 exit=1、无 manifest），但测试套件只覆盖 pg_dump 失败。
2. **同秒失败、窗口含枚举、gitignore 均无自动化用例**：本报告用手工探针验证；建议把 §3.5 三条固化为用例，防止回归。
3. **真实规模与窗口未测**：设计把窗口标定列为 U13；当前串行实现，百万级库的耗时/体积、WAL 压力、MinIO 打包时长均无数据。
4. **RTO 未演练**：只验证了「场景库 dump → 空库 restore」的机制与手术状态保留，未计时、未做检索可用性校验。
5. **`release.sh` 调用关系无测试**：只有静态确认；`set -e` 保证 backup 失败即中止发布，但没有用例锁住。
6. **并行备份未实现**：设计提过串行/并行两种窗口，当前只实现串行，并行方案与锁未涉及。
7. **静态检查缺失**：`backup.sh` 无 shellcheck 类门禁。

---

## 七、与设计一致性

| 设计条款（§4.8） | 实现 | 判定 |
|---|---|---|
| 枚举口径：容器内 `pg_database`（`datistemplate=false AND datallowconn=true`），宿主排除 `postgres`，不读 `SCENE_DB_DSN` 清单 | `backup.sh:30-34` | 一致 |
| 每库 `db_<name>.dump`，`pg_dump -Fc` | `:48-49` | 一致 |
| 任一库导出失败即整次非零退出，`release.sh` 中止 | `:48-52`；`release.sh:5,11` | 一致 |
| `manifest.txt` 写 `backup_stamp/started_at/finished_at/window_seconds/database_count/rpo_policy` + 每库 `seconds/bytes/file` | `:71-86` | 一致（另有 `minio_seconds`） |
| RPO 政策值随清单落盘、不依赖口头约定 | `:19-20`、`:79` | 一致（与 §4.8 第 363 行拍板一致） |
| 恢复口径 `pg_restore` 按库，见 `DEPLOY.md` §5 | `DEPLOY.md:295-314` | 一致（MinIO 段已随 `949fe1a` 对齐） |
| 备份窗口待标定（U13），按每库 `seconds` 汇总 | 每库 `seconds/bytes` 已落盘；窗口含枚举 | 一致 |
| MinIO 原始文档必须一起备份 | `:61-66`（只读挂载 `kagent_minio_data:/data:ro`） | 一致 |

---

## 八、结语

`949fe1a` 把初测的 P1（MinIO 恢复路径）与三项运维风险（gitignore、窗口计时、同秒覆盖）全部闭合，且每项都有实测证据：恢复路径与脚本产物逐段一致、`window_seconds=2`、`mkdir: File exists` 且零覆盖、三条 gitignore 规则命中。

**S6-7 验收通过，建议关闭 S6-7。** 剩余 P3 均为明确的运维注意事项（失败残留、特殊库名、人工比对库数），不阻塞关闭。
