# 测试报告 · Step 6 / S6-2（场景库建库 + 全链迁移 + 跨库外键手术 + 白名单守卫 + 控制面禁写触发器）

> 角色：测试工程师（P3，独立于编码者，不修改产品代码）
> 轮次：**第二轮（复验返工）**。第一轮结论见 §1 下方的历史记录；本轮针对编码者修复的 D1/D2/D3 做独立复验，证据见 §7。
> 被测变更（3 个新增文件 + 1 处设计文档补强；无既有代码文件被修改）：
> - `code/apps/api/app/infra/scene_db_schema.py`
> - `code/apps/api/scripts/scene_db_surgery.py`
> - `code/apps/api/tests/scenario/test_step6_scene_db_schema.py`
> - `02-design/第六阶段-Step6-前置裁决.md`（§4.2 追加「实现补强（P3 实测发现）」3 条，5 行新增）
>
> 验收依据：`02-design/第六阶段-Step6-前置裁决.md` §4.1（搬迁表集与守卫）、§4.2（M-B 规则）、§5.3 D2–D5
> 执行环境：Windows PowerShell 5，Python 3.13.14，PostgreSQL 16.15（容器 `code-postgres-1`，宿主 5433 → 容器 5432，账号 `user`），工作目录 `code/apps/api`
> 执行时间：2026-11-23
> 口令来源：`code/.env` 的 `POSTGRES_PASSWORD`，仅注入进程环境变量，未写入本文件或任何仓库文件。

---

## 1. 结论

**第二轮复验：可以 commit。无阻断、无缺陷。**

编码者声称的 3 项修复全部**真修好**，且均由独立用例（不是复跑编码者的用例）在真库上验证：

| 编号 | 问题 | 修复内容 | 复验结论 |
|---|---|---|---|
| D1 | `verify()` 对「被禁用的触发器」假 PASS | `TRIGGER_SQL` 增加 `t.tgenabled IN ('O','A')` | 已修复：独立用 `ALTER TABLE "audit_logs" DISABLE TRIGGER "trg_scene_db_block_audit_logs"`（与编码者用例的 `DISABLE TRIGGER ALL`/`tenants` 不同）→ verify FAIL、`missing=['audit_logs']`、此时 DELETE 确实能执行；重跑手术后恢复 PASS |
| D2 | 守卫只覆盖三列，仅含 `tenant_id` 的新表逃逸 | `GUARD_SQL` 列集合加入 `tenant_id` | 已修复：临时库建 `guard_probe(tenant_id)` → 守卫列出该表、`ok=False`；删表后恢复 0 行、`ok=True` |
| D3 | 手术脚本不校验目标库身份，误指控制库会破坏控制库 | `apply_surgery` 前置「控制面表任一非空即 RuntimeError」 | 已修复：插入 1 行 `tenants` → 抛 `RuntimeError`，**指纹（71 FK / 0 触发器 / 0 函数）前后完全不变**（DDL 发生在校验之后）；错误信息不含任何 DSN 凭据 |

本轮新增结论：

- 现成用例 `8 passed`，真库用例实际执行 1.99s，无 skip；Step 5 回归 `37 passed`。
- 真实误操作演练：把脚本**默认模式**（会执行手术）指向控制库 `kagent_test` → 退出码 1、拒绝执行、控制库指纹不变（71 FK / 0 触发器）。
- 发现的 2 条新观察（不阻断，见 §5 R6/R7）：非空前置校验与「未来的迁移种子行」可能冲突；该报错无法区分「误指控制库」与「场景库被污染」。
- 第一轮登记的建议 S2（真库用例只对 `tenants` 覆盖 I/U/D）仍未解决，本轮由测试工程师独立补验了 5 张表 15/15 全通过。

### 1.1 第一轮历史结论（已被本轮取代，保留备查）

**可以 commit。** 无阻断项。

- 第一轮独立验证 21 项判据中 20 项 PASS，1 项为缺陷 D1（见上表）。
- 另有 3 条建议（§4.2）与 5 条残余风险（§5）。

---

## 2. 验证项清单与结论

| # | 验证项 | 判据 | 结论 |
|---|---|---|---|
| V1 | 含 `kb_id`/`document_id`/`chunk_id` 列的表全集（用 SQLAlchemy metadata 独立算出） | 必须全部落在 `MIGRATION_TABLES` 或 `CONTROL_PLANE_TABLES`；出现第三类即守卫漏洞 | PASS：命中 12 张，第三类 = `[]` |
| V2 | 两集合并集与 metadata 双向一致 | `MIGRATION ∪ CONTROL == metadata`（34 张），无多无少 | PASS |
| V3 | `MIGRATION_TABLES` 与设计 §4.1 的 10 张逐字比对 | 集合相等且无增减 | PASS（`mig==design_4_1: True`） |
| V4 | `CONTROL_PLANE_TABLES` 与搬迁集互斥、`WRITE_BLOCKED_TABLES ⊆ 控制面集` | 必须成立 | PASS（24 张，`blocked_all_control: True`） |
| V5 | 现成用例真库层是否真跑 | 不得 skip；必须 8 passed | PASS（`8 passed`，真库用例 1.66s） |
| V6 | 手术前跨库外键清单（自写 SQL，不调用被测模块的 CROSS_DB_FK_SQL） | 记录条数与约束名；确认指向 tenants/knowledge_bases/users | PASS：17 条，全部指向这 3 张表 |
| V7 | 手术后跨库外键 | 必须 0 条 | PASS |
| V8 | 搬迁集内部 FK 是否被误删 | `chunks.document_id→documents`、`graph_entity_mentions.entity_id→graph_entities` 等必须保留 | PASS：11 条内部 FK 前后完全一致 |
| V9 | 幂等：连续两次手术 | 第二次 `dropped == []`，verify 仍全 PASS | PASS（t1/t2 两块临时库均复现） |
| V10 | 幂等后状态：两块场景临时库 verify | 三项全 PASS | PASS（t1、t2 均 `ok=True / cross=0 / guard=0 / missing=[]`） |
| V11 | 触发器有效性：5 张表 × INSERT/UPDATE/DELETE | 全部被拒；UPDATE/DELETE 在 0 行命中时也拒（STATEMENT 级语义） | PASS（15/15 全部 `RaiseError`） |
| V12 | 触发器不影响 SELECT | 5 张表 `count(*)` 可用 | PASS |
| V13 | 5 张表以外的控制面表可写 | 设计与代码均只拦 5 张，`kb_members` 应可写 | PASS（确认：`kb_members` INSERT/DELETE 成功）——同时登记为残余风险 R1 |
| V14 | 负向对照：未手术库 / 空库 verify | 必须 FAIL、退出码 1 | PASS（控制库 17 条跨库 FK → FAIL；空库 0/5 触发器 → FAIL） |
| V15 | `--verify-only` 不改 schema | 前后 schema 指纹完全一致 | PASS（54 FK / 5 trigger / 1 函数 / 11 搬迁集 FK，前后相同） |
| V16 | 退出码正确性 | 全 PASS=0；FAIL=1；执行异常=1 | PASS（0 / 1 / 1 / 1 四组实测） |
| V17 | 输出脱敏 + `--json` 可解析 | 口令不得出现；JSON 可 `json.loads` | PASS（三组输出均无 `user:<口令>@` 片段、人读输出无 `@`；JSON 可解析） |
| V18 | S6-2 范围 | 只新增 3 个文件；不动 `api/kbs.py` / `db_router.py` / `backup.sh` / T-5-7、T-5-8；`data_session` 在 `app/` 内零调用点 | PASS |
| V19 | Step 5 回归 | 三个文件全绿 | PASS（37 passed） |
| V20 | 清理 | 临时库全部 DROP；`kagent_test` 无手术痕迹 | PASS（17 条跨库 FK 仍在、0 个非内部触发器、无拒绝函数） |
| V21 | 对抗探测：禁用触发器后 verify 表现 | 应能发现（否则假 PASS） | 第一轮 **FAIL → 缺陷 D1**；第二轮 **PASS**（已修复，见 §7.3） |
| V22 | D2 守卫覆盖 `tenant_id` | 只含 `tenant_id` 的探针表必须被守卫列出 | PASS（`guard_probe(tenant_id)` → `ok=False`；删表后 0 行） |
| V23 | D3 非空前置校验 | 控制面非空必须抛错；且在任何 DDL 之前中止 | PASS（指纹 71/0/0 前后一致；17 条跨库 FK 一条未删） |
| V24 | D3 误操作演练 | 脚本默认模式指向控制库必须拒绝 | PASS（退出码 1，控制库指纹不变） |
| V25 | 修复未破坏既有行为 | 术后 17 条外键删除、11 条内部 FK 保留、5 表 15/15 写操作被拒、幂等 | PASS（见 §7.5） |
| V26 | 设计文档补强 | §4.2 记录 3 条实现补强，且与代码一致 | PASS（`git diff` 5 行新增，逐条对应） |

---

## 3. 执行证据

> 本章是**第一轮**（修复前）的证据，保留备查；第二轮的复验证据见 §7。

### 3.1 现成用例（真库）

命令（工作目录 `code/apps/api`）：

```
$line = Get-Content "..\..\.env" | Where-Object { $_ -match '^POSTGRES_PASSWORD\s*=' }
$pw = ($line -split '=',2)[1].Trim().Trim('"').Trim("'")
$env:SCENARIO_TEST_DB_URL = "postgresql+asyncpg://user:$pw@localhost:5433/kagent_test"
python -m pytest tests/scenario/test_step6_scene_db_schema.py -q -rs --durations=10 --override-ini=addopts=
```

真实输出（节选）：

```
........                                                                 [100%]
============================ slowest 10 durations =============================
1.66s call     tests/scenario/test_step6_scene_db_schema.py::test_real_db_surgery_roundtrip
0.09s setup    tests/scenario/test_step6_scene_db_schema.py::test_migration_tables_are_exactly_ten_and_exist_in_metadata
8 passed in 1.79s
EXIT=0
```

结论：真库用例真实执行（1.66s 内完成建库、`alembic upgrade head`、手术、校验、幂等、禁写探测、DROP），无 skip。

### 3.2 表集正确性（独立于被测模块口径）

用 `entities.Base.metadata` 独立算「含三列的表全集」，不读被测模块的白名单：

```
metadata_tables_total= 34
tables_with_3_cols= 12
THIRD_CLASS: []
mig_count= 10  cp_count= 24
union==metadata: True
metadata_only_not_in_union: []
union_only_not_in_metadata: []
mig==design_4_1: True
mig_only: []  design_only: []
```

12 张命中表逐张归属（`MIGRATION` / `CONTROL`）：

```
  chunks                       MIGRATION  ['document_id', 'kb_id']
  documents                    MIGRATION  ['kb_id']
  eval_cases                   CONTROL    ['kb_id']
  governance_jobs              MIGRATION  ['document_id']
  graph_entities               MIGRATION  ['kb_id']
  graph_entity_mentions        MIGRATION  ['chunk_id', 'document_id', 'kb_id']
  graph_relations              MIGRATION  ['document_id', 'kb_id']
  kb_members                   CONTROL    ['kb_id']
  kb_terms                     MIGRATION  ['kb_id']
  parse_jobs                   MIGRATION  ['document_id']
  sync_sources                 CONTROL    ['kb_id']
  wiki_pages                   MIGRATION  ['document_id', 'kb_id']
```

结论：无第三类表，守卫不存在"结构性漏洞"；`MIGRATION_TABLES` 与 §4.1 的 10 张逐字一致。

### 3.3 手术真实性（临时库 `s6_verify_t1`，全链 0030）

手术前（自写 SQL 查 `pg_constraint`，未使用被测 SQL）：

```
alembic_version: 0030
base_tables: 35
cross_db_fk_count: 17
  CROSS chunks -> knowledge_bases  chunks_kb_id_fkey  ondelete=b'c'
  CROSS chunks -> tenants  chunks_tenant_id_fkey  ondelete=b'a'
  CROSS documents -> knowledge_bases  documents_kb_id_fkey  ondelete=b'c'
  CROSS documents -> tenants  documents_tenant_id_fkey  ondelete=b'a'
  CROSS documents -> users  documents_uploaded_by_fkey  ondelete=b'n'
  CROSS governance_jobs -> tenants  governance_jobs_tenant_id_fkey  ondelete=b'a'
  CROSS graph_entities -> knowledge_bases  graph_entities_kb_id_fkey  ondelete=b'c'
  CROSS graph_entities -> tenants  graph_entities_tenant_id_fkey  ondelete=b'a'
  CROSS graph_entity_mentions -> knowledge_bases  graph_entity_mentions_kb_id_fkey  ondelete=b'c'
  CROSS graph_entity_mentions -> tenants  graph_entity_mentions_tenant_id_fkey  ondelete=b'a'
  CROSS graph_relations -> knowledge_bases  graph_relations_kb_id_fkey  ondelete=b'c'
  CROSS graph_relations -> tenants  graph_relations_tenant_id_fkey  ondelete=b'a'
  CROSS kb_terms -> knowledge_bases  kb_terms_kb_id_fkey  ondelete=b'c'
  CROSS kb_terms -> tenants  kb_terms_tenant_id_fkey  ondelete=b'a'
  CROSS parse_jobs -> tenants  parse_jobs_tenant_id_fkey  ondelete=b'a'
  CROSS wiki_pages -> knowledge_bases  wiki_pages_kb_id_fkey  ondelete=b'c'
  CROSS wiki_pages -> tenants  wiki_pages_tenant_id_fkey  ondelete=b'a'
internal_fk_count: 11
  INTERNAL chunks -> documents  chunks_document_id_fkey
  INTERNAL governance_jobs -> documents  governance_jobs_document_id_fkey
  INTERNAL graph_entity_mentions -> chunks  graph_entity_mentions_chunk_id_fkey
  INTERNAL graph_entity_mentions -> documents  graph_entity_mentions_document_id_fkey
  INTERNAL graph_entity_mentions -> graph_entities  graph_entity_mentions_entity_id_fkey
  INTERNAL graph_relations -> documents  graph_relations_document_id_fkey
  INTERNAL graph_relations -> graph_entities  graph_relations_dst_entity_id_fkey
  INTERNAL graph_relations -> graph_entities  graph_relations_src_entity_id_fkey
  INTERNAL parse_jobs -> documents  parse_jobs_document_id_fkey
  INTERNAL parse_segments -> parse_jobs  parse_segments_job_id_fkey
  INTERNAL wiki_pages -> documents  wiki_pages_document_id_fkey
rowcounts: 控制面 5 张 + eval_cases/sync_sources/kb_members 全部为 0
```

> 补充核对：搬迁集指向"搬迁集以外"的全部 FK 恰好 17 条，目标集合 = `['knowledge_bases', 'tenants', 'users']`，无一遗漏（不存在指向 `scenarios`/`kb_members`/`tenant_quotas` 等其他控制面表的 FK）。另查「控制面 → 搬迁集」反向 FK 为 0 条。

手术后（`apply_surgery` 后立即复查）：

```
first_surgery_dropped_count: 17
  DROPPED chunks -> knowledge_bases  chunks_kb_id_fkey
  ...（17 条，逐条与手术前清单完全对应）
dropped_non_control_targets: []
first_surgery_triggers: ['trg_scene_db_block_tenants', 'trg_scene_db_block_knowledge_bases', 'trg_scene_db_block_users', 'trg_scene_db_block_scenarios', 'trg_scene_db_block_audit_logs']
post_surgery_cross_db_fk_count: 0
post_surgery_internal_fk_count: 11
chunks_document_fk_present: True
graph_mention_entity_fk_present: True
second_surgery_dropped: []
verify_ok: True cross: 0 guard: 0 missing: []
```

结论：被删的 17 条**全部**是「搬迁集 → tenants/knowledge_bases/users」方向；搬迁集内部的 11 条 FK（含 `chunks→documents`、`graph_entity_mentions→graph_entities`）一条未动；第二次手术无删除动作。

### 3.4 幂等与两库 verify

```
second_db_ready: s6_verify_t2
t2_first_dropped: 17
t2_second_dropped: []
t2_verify_ok: True cross: 0 guard: 0 missing: []
```

### 3.5 触发器有效性（5 张表 × 3 种写操作，含 0 行命中）

INSERT 用 `INSERT ... DEFAULT VALUES`、DELETE 用 `DELETE ... WHERE false`；UPDATE 用真实列名 + `WHERE false`（首次尝试写 `SET ctid = ctid` 被判非法 SQL，已改用真实列重测，下表为有效结果）：

```
  tenants            INSERT BLOCKED: 控制面表 tenants 在场景库恒为空且禁止写入（M-B 跨库外键手术）
  tenants            DELETE BLOCKED: 控制面表 tenants 在场景库恒为空且禁止写入（M-B 跨库外键手术）
  tenants            UPDATE(0 rows, col=id) BLOCKED: 控制面表 tenants 在场景库恒为空且禁止写入（M-B 跨库外键手术）
  knowledge_bases    INSERT/DELETE/UPDATE(0 rows) 全部 BLOCKED（同措辞，表名不同）
  users              INSERT/DELETE/UPDATE(0 rows) 全部 BLOCKED
  scenarios          INSERT/DELETE/UPDATE(0 rows, 表 0 行) 全部 BLOCKED
  audit_logs         INSERT/DELETE/UPDATE(0 rows, 表 0 行) 全部 BLOCKED
  tenants            SELECT ok  count=1
  knowledge_bases    SELECT ok  count=1
  users              SELECT ok  count=1
  scenarios          SELECT ok  count=0
  audit_logs         SELECT ok  count=0
```

`scenarios` / `audit_logs` 是空表，UPDATE/DELETE 命中 0 行仍被拒 —— 证实是 `FOR EACH STATEMENT` 语义，不是靠行命中触发。

未被设计列入 5 张表的控制面表可写（残余风险 R1 的实证据）：

```
kb_members_write: OK (not blocked) count= 1
kb_members_delete: OK
```

### 3.6 脚本行为

```
=== fingerprint BEFORE verify-only ===        === fingerprint AFTER verify-only ===
fk_total= 54                                   fk_total= 54
trig_total= 5                                  trig_total= 5
block_fn= 1                                    block_fn= 1
mig_fk= 11                                     mig_fk= 11

=== verify-only on s6_verify_t1 ===
场景库：localhost:5433/s6_verify_t1
手术：未执行（--verify-only）
1 跨库外键    PASS  remaining=0
2 白名单守卫  PASS  guard_tables=0
3 禁写触发器  PASS  5/5
EXIT=0

=== --json ===
EXIT=0
json_parsed=OK keys= ['surgery', 'target', 'verify']
target= localhost:5433/s6_verify_t1  surgery= None  verify.ok= True
cross= 0 guard= 0 missing= []

=== --help ===
usage: scene_db_surgery.py [-h] --dsn DSN [--verify-only] [--json]

=== 构造失败态：空库（未迁移/未手术） ===
场景库：localhost:5433/s6_verify_empty
1 跨库外键    PASS  remaining=0
2 白名单守卫  PASS  guard_tables=0
3 禁写触发器  FAIL  0/5
    - 缺少触发器：tenants / knowledge_bases / users / scenarios / audit_logs
EMPTY_EXIT=1

=== 负向对照：未手术的控制库 kagent_test ===
1 跨库外键    FAIL  remaining=17
2 白名单守卫  PASS  guard_tables=0
3 禁写触发器  FAIL  0/5
KAGENT_TEST_EXIT=1

=== 错误 DSN（口令错误） ===
BADDSN_EXIT=1
执行失败（InvalidPasswordError: password authentication failed for user "user"）
```

脱敏复核使用更可靠的口径 —— 检查是否出现 `user:<口令>@` 片段，而不是拿短口令整串做子串匹配（本机口令为 4 个字符，`Contains` 会在 `passed` 这类单词里误报）：

```
userinfo_in_human=False
userinfo_in_json=False
userinfo_in_error=False
at_sign_in_human=False
```

> `--help` 的 `HELP_EXIT=-1` 是 PowerShell 管道被 `Select-Object -First 8` 提前掐断造成的测量假象，不是脚本缺陷（脚本正常打印 usage）。

### 3.7 范围检查与回归

```
git status --short
?? code/apps/api/app/infra/scene_db_schema.py
?? code/apps/api/scripts/scene_db_surgery.py
?? code/apps/api/tests/scenario/test_step6_scene_db_schema.py
---
git diff --stat      （空）
git diff --name-only （空）
```

`data_session` 在 `app/` 内仍为 3 处命中：`db_router.py:10`（文档串）、`:64`（`__all__`）、`:247`（定义），**无调用点**。

```
python -m pytest tests/config/test_step5_settings.py tests/scenario/test_step5_db_router.py tests/scenario/test_step5_db_routing_report.py -q
.....................................                                    [100%]
37 passed in ...
EXIT=0
```

### 3.8 清理

```
=== 最终库清单 ===
 kagent
 kagent_test
 postgres
 template0
 template1

=== kagent_test 最终状态 ===
cross_fk = 17
non_internal_triggers = 0
（无 fn_scene_db_block_control_plane_write）
```

- pytest 真库用例自带 `finally` 清理：多轮运行后 `pg_database` 中**没有**任何 `scene_surgery_*` 残留。
- 本次人工建的 `s6_verify_t1` / `t2` / `t3` / `s6_verify_empty` 已全部 DROP。
- `kagent_test` 未被留下手术痕迹：17 条跨库 FK 仍在、触发器 0 个、拒绝函数不存在。

---

## 4. 缺陷与建议（只报告，不修改实现）

### 4.1 缺陷 D1：`verify()` 对「被禁用的触发器」假 PASS —— **第二轮已修复**

- 位置：`code/apps/api/app/infra/scene_db_schema.py` 的 `TRIGGER_SQL`（第一轮为第 141-155 行，现为第 144-164 行）。
- 原因（第一轮）：查询只按 `tgrelid` + 函数名 + schema 过滤，**没有 `t.tgenabled` 条件**，因此 `ALTER TABLE ... DISABLE TRIGGER` 后该触发器仍被计为"已安装"。
- 第一轮实测（临时库 `s6_verify_t3`）：

```
trigger disabled: trg_scene_db_block_tenants
verify_ok_after_disable: True missing: [] triggers.tenants: ['trg_scene_db_block_tenants']
write_after_disable: SUCCEEDED (bypass real) count= 1
```

- 影响：`verify` 是机器化门禁（§4.2「分叉可被机器检测」）。触发器被人为或运维禁用后，门禁会给出"防护仍在"的错误结论，而实际写入已成功。这属于"校验器假 PASS"。
- 修复（编码者已提交）：`TRIGGER_SQL` 增加 `AND t.tgenabled IN ('O', 'A')`；用例新增「禁用触发器 → verify FAIL → 重跑手术恢复 PASS」。
- **复验结论：真修好**。独立用例（§7.3）用 `ALTER TABLE "audit_logs" DISABLE TRIGGER "trg_scene_db_block_audit_logs"`（编码者用例用的是 `tenants` + `DISABLE TRIGGER ALL`），结果 `verify_ok=False, missing=['audit_logs']`，且此时 `DELETE FROM audit_logs WHERE false` 确实执行成功（证明触发器真的失效、verify 的 FAIL 不是误报）；重跑手术后 `dropped=[]`、`ok=True`。

### 4.2 建议

- S1（操作安全，优先级高）—— **第二轮已修复**：`apply_surgery` 增加前置校验：统计 `CONTROL_PLANE_TABLES` 全部 24 张表的行数，任一非空即抛 `RuntimeError`，且在所有 DDL 之前中止。复验见 §7.4（含一次「脚本默认模式误指控制库」的真实演练）。
- S2（用例覆盖，**仍未解决**）：真库用例的触发器 I/U/D 断言仍只覆盖 `tenants` 一张表，其余 4 张表无实库断言；也未断言「手术前后搬迁集内部 11 条 FK 不变」「`kb_members` 可写」。本轮由测试工程师独立补验全部通过（5 张表 15/15 被拒、内部 FK 11 条不变），建议补入用例以防后续回归。
- S3（空集 PASS，**仍未解决**）：空库/未迁移库上校验 1、2 是空集直接 PASS（只是被校验 3 兜住）。建议在 `verify()` 前置断言 `alembic_version` 或 `base table` 数量下限，避免"三项里两项空转"。
- S4（新增，可维护性）：`tests/scenario/test_step6_scene_db_schema.py` 模块 docstring（第 5-16 行）仍描述第一轮的用例序列，未提及新增的「非空拒绝」「禁用触发器恢复」两步；用例已改、文档串未同步，建议更新以免后人按 docstring 理解覆盖范围。

---

## 5. 残余风险（登记，不作为缺陷、也不作为通过项）

| 编号 | 内容 | 实证据 |
|---|---|---|
| R1 | 禁写触发器只覆盖设计原文的 5 张表；其余控制面表（`kb_members`、`departments`、`user_roles`、`tenant_quotas`、`sensitive_words` 等）在场景库仍可写 | `kb_members` INSERT/DELETE 实测成功 |
| R2 | 守卫列集合第二轮已扩到 `kb_id`/`document_id`/`chunk_id`/`tenant_id`（D2 已修复）。**剩余**：只含其他场景标识（如仅 `scenario_id`）的新表仍会逃逸 | 第一轮探针 `zz_probe_only_tenant` 曾逃逸；第二轮 `guard_probe(tenant_id)` 已被拦住 |
| R3 | 触发器与"场景库再次迁移"的相互作用：实测手术后将库降回 0027 再 `upgrade head` 可以成功（0028 的种子写进新建的 `scenarios` 表，未被拒），但 `scenarios` 表被重建导致其触发器丢失，`verify` 报 `missing: ['scenarios']` FAIL —— 必须重跑手术。真正会被拒绝的是"在已装触发器的库上，用迁移/补数脚本写这 5 张表" | 该路径的 `verify_after_reupgrade_ok: False, missing: ['scenarios']` |
| R4 | `TRUNCATE` 不在拒绝范围（设计只写 INSERT/UPDATE/DELETE）。场景库控制面表恒为空，TRUNCATE 影响有限，但它是绕过 STATEMENT 级触发器的唯一写路径 | 设计原文 §4.2 |
| R5 | 误把 `--dsn` 指向控制库会破坏控制库 —— **第二轮已由 D3 前置校验缓解**，但仅在控制库控制面表非空时生效；全新空控制库仍会被当成场景库处理 | 代码 `apply_surgery` 前置校验 + §7.4 实测 |
| R6 | 新增的非空前置校验与「未来的迁移种子行」可能冲突：场景库 Phase 1 的 `alembic upgrade head` 若某条迁移向任一控制面表写入种子行（当前 0028 只写 `scenarios` 且因 `tenants` 为空而插 0 行，故不触发），手术会被拒绝，需先人工清空。这是 fail-closed 的代价，与设计 §4.3 步骤 7「场景库控制面表行数为 0」一致，但迁移作者必须知道这条约束 | 第一轮实测：迁移后 `scenarios` 等控制面表行数全为 0；前置校验读的是这 24 张表的实时行数 |
| R7 | 非空前置校验的报错无法区分「误指控制库」与「场景库被污染」：两者都报「这不是空的场景库」。同时因为 19 张未被触发器覆盖的控制面表仍可写（R1），一旦场景库真的有行，重跑手术会一直失败 | 报错原文只打印非空表名与行数 |

---

## 6. 是否可 commit

**可以。**

- S6-2 的验收口径（§4.1 表集与守卫、§4.2 M-B 四项、§5.3 D2–D5）全部达成，且由独立证据支撑。
- 第一轮唯一的缺陷 D1（禁用触发器后 verify 假 PASS）与优先级最高的建议 S1（脚本不校验目标库身份）均已在第二轮修复并通过独立复验（§7.3、§7.4）。
- 范围：`git status --short` 只有 3 个 S6-2 新文件 + 设计文档 §4.2 的 5 行补强（`git diff --stat`：1 file changed, 5 insertions）；`data_session` 在 `app/` 内仍为零调用点；Step 5 回归 37 passed。
- 清理：临时库、探针表全部清除；`kagent_test` 保持 71 条外键 / 0 个非内部触发器 / 无拒绝函数。
- 未修复的 S2/S3/S4 均为建议级（用例覆盖、空集 PASS、docstring 未同步），不阻断交付；R6/R7 是设计层面的已知残余，已登记。

---

## 7. 第二轮复验记录（命令 + 真实输出）

环境与第一轮相同：容器 `code-postgres-1`，宿主 5433，库 `kagent_test`；口令从 `code/.env` 读入进程环境变量，未写入任何文件。

### 7.1 现成用例（无 skip，真库真跑）

```
$env:SCENARIO_TEST_DB_URL = "postgresql+asyncpg://user:<口令>@localhost:5433/kagent_test"
python -m pytest tests/scenario/test_step6_scene_db_schema.py -q -rs --durations=10 --override-ini=addopts=
```

```
........                                                                 [100%]
============================ slowest 10 durations =============================
1.99s call     tests/scenario/test_step6_scene_db_schema.py::test_real_db_surgery_roundtrip
0.09s setup    tests/scenario/test_step6_scene_db_schema.py::test_migration_tables_are_exactly_ten_and_exist_in_metadata

(8 durations < 0.005s hidden.  Use -vv to show these durations.)
8 passed in 2.11s
EXIT=0
```

### 7.2 Step 5 回归

```
python -m pytest tests/config/test_step5_settings.py tests/scenario/test_step5_db_router.py tests/scenario/test_step5_db_routing_report.py -q --override-ini=addopts=
```

```
.....................................                                    [100%]
37 passed in 0.81s
EXIT=0
```

### 7.3 D1 复验（临时库 `s6_recheck_t1`，独立禁用方式）

手术完成后，用**编码者用例未使用**的路径禁用：`ALTER TABLE "audit_logs" DISABLE TRIGGER "trg_scene_db_block_audit_logs"`（编码者用 `tenants` + `DISABLE TRIGGER ALL`）。

```
surgery_dropped: 17
verify_ok: True cross: 0 guard: 0 missing: []
D1_disabled: trg_scene_db_block_audit_logs
D1_verify_ok: False missing: ['audit_logs']
D1_write_bypass: DELETE 成功（触发器确实失效）
D1_rerun_dropped: []
D1_verify_after_recover_ok: True missing: []
```

判读：verify 在触发器失效时**确实报 FAIL**，且同一时刻 `DELETE FROM audit_logs WHERE false` 真的被执行（说明 FAIL 不是误报）；重跑手术重建触发器后恢复 PASS。

### 7.4 D3 复验（非空前置校验在 DDL 之前中止）

临时库先插入 1 行 `tenants`，再调用 `apply_surgery`：

```
pre_fp: (71, 0, 0) pre_cross: 17
probe_tenant_inserted, tenants= 1
D3_raised: RuntimeError
D3_msg: 这不是空的场景库，手术已中止：控制面表非空 -> tenants(1行)。请确认 --dsn 指向的是新建的场景库，而不是控制库。
D3_msg_has_userinfo: False  has_at: False  has_dsn_host: False
d3_fp_after: (71, 0, 0)
D3_no_ddl_before_abort: True
tenant_probe_deleted, tenants= 0
surgery_dropped: 17
verify_ok: True cross: 0 guard: 0 missing: []
```

判读：指纹（外键数 71 / 触发器 0 / 拒绝函数 0）在抛错前后完全一致，`pre_cross: 17` 的 17 条跨库外键一条未删 —— 校验确实发生在任何 DDL 之前；错误信息只含表名与行数，不含账号、口令、主机或库名。

真实误操作演练（脚本**默认模式**，会执行手术，指向控制库 `kagent_test`）：

```
python scripts/scene_db_surgery.py --dsn postgresql://user:<口令>@localhost:5433/kagent_test
```

```
执行失败（RuntimeError: 这不是空的场景库，手术已中止：控制面表非空 -> audit_logs(1685行)、conversations(42行)、integration_applications(78行)、integration_conversations(42行)、knowledge_bases(554行)、messages(84行)、scenario_versions(592行)、scenarios(592行)、tenants(591行)、users(540行)。请确认 --dsn 指向的是新建的场景库，而不是控制库。）
EXIT=1
has_userinfo=False has_at=False
```

演练后 `kagent_test` 指纹未变：`fk_total=71`、`non_internal_triggers=0`、拒绝函数 0 个。`--json` 模式下同样退出码 1 且不含凭据。

### 7.5 D2 复验 + 修复未破坏既有行为

```
CREATE TABLE guard_probe (tenant_id uuid)
D2_guard_tables: [('guard_probe', ['tenant_id'])] ok: False
D2_after_drop_guard: [] ok: True
```

同一临时库（已恢复全 PASS）上复查既有行为：

```
internal_fk_count: 11
  tenants            INSERT:BLOCKED UPDATE:BLOCKED DELETE:BLOCKED  select_count= 0
  knowledge_bases    INSERT:BLOCKED UPDATE:BLOCKED DELETE:BLOCKED  select_count= 0
  users              INSERT:BLOCKED UPDATE:BLOCKED DELETE:BLOCKED  select_count= 0
  scenarios          INSERT:BLOCKED UPDATE:BLOCKED DELETE:BLOCKED  select_count= 0
  audit_logs         INSERT:BLOCKED UPDATE:BLOCKED DELETE:BLOCKED  select_count= 0
```

判读：守卫新增 `tenant_id` 后，metadata 现有 34 张表仍全部落在两个表集内，未产生误报；5 张表 15/15 写操作仍被拒（含 0 行命中的 UPDATE/DELETE）；搬迁集内部 11 条 FK 未受影响。

### 7.6 范围与清理

```
git status --short
 M 02-design/第六阶段-Step6-前置裁决.md
?? 04-test/测试报告-Step6-S6-2.md
?? code/apps/api/app/infra/scene_db_schema.py
?? code/apps/api/scripts/scene_db_surgery.py
?? code/apps/api/tests/scenario/test_step6_scene_db_schema.py

git diff --stat
 02-design/第六阶段-Step6-前置裁决.md | 5 +++++
 1 file changed, 5 insertions(+)

=== 最终库清单 ===
 kagent / kagent_test / postgres / template0 / template1
=== kagent_test 最终状态 ===
fk_total: 71
cross_db_fk: 17
non_internal_triggers: 0
block_fn: 0
guard_probe_exists: 0
```

判读：`kagent_test` 未留手术痕迹 —— 17 条跨库外键仍在、触发器 0 个、拒绝函数不存在、探针表未落到该库。
`data_session` 在 `app/` 内仍只有 3 处命中：`db_router.py:10`（文档串）、`:64`（`__all__`）、`:247`（定义），无调用点。

