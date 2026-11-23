# 测试报告：Step6 S6-5 存量搬迁（验收 commit 26c1856）

- 被测提交：`26c1856`（S6-5 验收轮；前序 `4a67586`、`b63bf07`、基线 `8b3415a`）
- 仓库：`c:\Users\86186\Desktop\AI Agent学习\file_rag_agent`
- 后端工作目录：`code\apps\api`
- 测试角色：独立测试（不采信实现者结论，全部命令实际执行）
- 测试时间：首测 2026-11-23 17:30 - 17:53；复验 17:55 - 18:05；终验 18:03 - 18:12；**验收 18:05 - 18:14**（Asia/Shanghai）
- 被测文件：
  - `code/apps/api/app/infra/scene_db_migrate.py`
  - `code/apps/api/scripts/scene_db_migrate.py`
  - `code/apps/api/app/infra/scene_db_schema.py`
  - `code/apps/api/tests/scenario/test_step6_scene_db_migrate.py`
- 设计口径：`02-design/第六阶段-Step6-前置裁决.md` §4.2 / §4.3 Phase 3 / §4.5 / §4.11

---

## 一、结论

**S6-5 验收通过，无未关闭问题。**

- 上轮唯一低风险项（SOURCE_CHANGED 用例不具区分力）**已关闭**：`26c1856` 把注入改成「先向源库插 `race.txt`，再拷 documents」，未回滚时场景库会变成 2 篇且含 race.txt，回滚成立时仍是 1 篇，断言（`documents == 1` 且 `race.txt == 0`）现已能区分两种结果。
- 上轮两项信息项（设计 §4.11 未写反向也强制版本一致、CLI 使用说明未提）**已一并关闭**：设计 §4.11 回滚行与 `scripts/scene_db_migrate.py:10` 均已注明「两侧版本必须一致」。
- 本轮命令全部符合预期：ruff 0 告警、真库 `9 passed`、无真库 `8 passed 1 skipped`。
- 剩余仅有实现固有、已被分层检查兜住的残余风险（§六），不属于未关闭缺陷。

---

## 二、环境

| 项 | 值 |
|---|---|
| OS | Windows（PowerShell 5） |
| Python | 3.13.14 |
| ruff | 0.16.1 |
| PostgreSQL | 容器 `code-postgres-1`（pgvector/pgvector:pg16），宿主 `localhost:5433` |
| 测试库 | `kagent_test`（`SCENARIO_TEST_DB_URL` 的账号口令取自 `code/.env`，全程不打印口令） |
| 临时库 | 用例内创建、用后 DROP |

计数口径：`pyproject.toml` 的 `addopts = "-q --strict-markers"` 与命令行 `-q` 叠加为 `-qq` 会隐藏汇总行。两种口径都跑了：原样 `-q` 得到 `........s` / `.........`，`-o addopts="" -q` 得到明确计数，结果一致。

---

## 三、验收命令与真实输出

### 3.1 ruff（本轮改动的两个文件）

```
cd code\apps\api
python -m ruff check tests/scenario/test_step6_scene_db_migrate.py scripts/scene_db_migrate.py
```
```
All checks passed!
EXIT=0
```

### 3.2 无真库

```
$env:SCENARIO_TEST_DB_URL=$null
python -m pytest tests/scenario/test_step6_scene_db_migrate.py -q
```
```
........s                                                                [100%]
8 passed, 1 skipped in 0.14s
```
符合预期：8 个 NO-DB 通过，真库用例 skip 而非 error。

### 3.3 真库

```
$env:SCENARIO_TEST_DB_URL = "postgresql+asyncpg://<user>:<pw>@localhost:5433/kagent_test"
python -m pytest tests/scenario/test_step6_scene_db_migrate.py -q
```
```
.........                                                                [100%]
9 passed in 12.17s
```
符合预期。单条真库用例 `test_real_db_migration_roundtrip` 串行覆盖 13 个步骤：同库拒绝（正/反向）→ 手术前拒绝 → 控制面非空拒绝 → 源库空拒绝 → 正向 + 关键词探测 → 幂等重跑 → SCHEMA_DRIFT → VERIFY_MISMATCH 回滚 → SOURCE_CHANGED 回滚 → 别家 kb 拒绝 → reverse 增量/重跑 → reverse 不覆盖 → 版本不一致。

---

## 四、本轮唯一改动的复核（SOURCE_CHANGED 用例区分力）

改动位置：`tests/scenario/test_step6_scene_db_migrate.py:612-619`

```python
async def _race_write(source, target, spec, kb_id, columns):
    if spec.name == "documents" and not injected:
        injected.append(
            await _insert_extra_document(source, ids, ids["kb1"], "race.txt")
        )
    return await original_copy(source, target, spec, kb_id, columns)
```

执行顺序变成：注入 `race.txt`（源库变为 2 篇）→ 复制 documents（此时会拷到 2 篇）→ 其余表复制 → 前后摘要比对不等 → 抛 `S6_MIGRATE_SOURCE_CHANGED` → 目标事务回滚。

断言（`:628-631`）：`scene.documents == 1` 且 `scene` 中 `filename='race.txt'` 为 0。

区分力判定：**成立**。
- 回滚成立 → 场景库保持上一次完整搬迁的 1 篇、无 race.txt → 断言通过；
- 若实现漏了回滚 → 场景库会留下本次拷贝的 2 篇并含 race.txt → 断言必失败。
- 同时，注入行在用例末尾从控制库删除（`:634-638`），不污染后续 10-13 步。

真库执行通过，说明当前实现确实回滚；该用例今后可以捕获回滚缺失的回归。

---

## 五、问题关闭总表（三轮累计）

| 轮次 | 编号 | 问题 | 状态 |
|---|---|---|---|
| 首测 | F1 | CLI 文档用法本机不可执行 | 关闭（改 `-m`，实测退出 0） |
| 首测 | F2 | TRUNCATE 无 CASCADE 与设计文字不一致 | 关闭（§4.11 与代码注释均写明「有意收窄」） |
| 首测 | F3 | 同库判定只看 DSN 字符串 | 关闭（改服务端视角 `server_identity`） |
| 首测 | F4 | 未校验 `alembic_version` | 关闭（新增 `VERSION_MISMATCH` + 用例） |
| 首测 | F5 | 「检索等价」口径偏乐观 | 关闭（统一改为「关键词探测等价」） |
| 复验 | 计数 | 用例条数与预期不符 | 已澄清（9 条 = 8 NO-DB + 1 真库） |
| 复验 | N1 | VERIFY_MISMATCH 断言无法区分回滚 | 关闭（改断言被跳过的 `kb_terms`） |
| 复验 | N2 | 同库检查被前置检查遮蔽 | 关闭（`_ensure_distinct_targets` 提到 verify 之前，正/反向用例覆盖） |
| 复验 | N3 | 三个守卫错误码无用例 | 关闭（SAME_DATABASE 正反向、HAS_CONTROL_ROWS、SOURCE_CHANGED 均有用例） |
| 复验 | N5 | `alembic_version` 缺失抛非结构错误 | 关闭（转 `VERSION_MISMATCH`） |
| 复验 | N6 | `--probe-term` 帮助措辞不同步 | 关闭 |
| 终验 | N1 | SOURCE_CHANGED 断言不具区分力 | **关闭（本轮 26c1856）** |
| 终验 | N2 | 设计 §4.11 未写反向版本一致 | 关闭（设计 + CLI 使用说明已注明） |
| 终验 | N3 | CLI 使用说明未提反向版本约束 | 关闭（`scripts/scene_db_migrate.py:10`） |

---

## 六、残余风险（非缺陷，不阻塞验收）

1. **服务端身份判定的固有边界**：同一实例若一侧经宿主端口映射、另一侧直连容器端口，或 TCP 与 Unix socket 混用，`server_identity` 会判为不同库（漏报）。该场景仍由 `verify()`（手术三项）与 `control_plane_usage()`（控制面非空）拦截，不会走到 TRUNCATE。
2. **规模与资源未测**：大表经临时文件的磁盘占用、COPY 文本格式耗时、目标库 HNSW 索引重建时间。属容量验证范畴，不在 S6-5 功能验收内。
3. **写暂停检测的覆盖边界**：只覆盖 COPY 窗口内的源库变化；停写窗口外的跨库半状态需真实例演练（§4.9 已声明单库演练测不出）。
4. **守卫用例集中在单条真库用例内**：失败时定位依赖步骤注释，不影响正确性。

---

## 七、结论

`26c1856` 的修正真实且有效：SOURCE_CHANGED 的注入时序前移后，用例能区分「回滚 / 未回滚」，上轮唯一低风险项关闭；设计 §4.11 与 CLI 使用说明同步了反向版本约束，两项信息项一并关闭。本次验收命令全部通过（ruff 0 告警、真库 9 passed、无真库 8 passed 1 skipped），已覆盖 §4.3 Phase 3 / §4.5 / §4.11 的全部硬性要求，未发现新的实现缺陷。

**S6-5 验收通过，无未关闭问题。**
