# 测试报告 · 第 4 步「wiki 导航参与检索」P3 独立验证

> **验证者**：agent-harness-tester-3（P3 独立验证者）
> **日期**：2026-11-06（含「真库前提更新」补充轮）
> **项目根**：`C:\Users\86186\Desktop\AI Agent学习\file_rag_agent`
> **工作模式**：**只读实现代码**。全程未修改任何实现文件 / `docker-compose.yml` / `Dockerfile`（变异自检在临时副本上、真库诊断补丁在临时脚本上，原文件 SHA256 前后一致）；未 `git commit`、未 `git push`。补充轮按 N4 向**真库**写入过合成页（独立租户，跑完由交付文件自带的 `_cleanup` 清理、并经独立只读 SQL 复核 count=0）。
> **独立性声明**：不采信实现者「已完成」声明，**也不采信下发前提**。补充轮亲自复核了 Docker / PG 环境与真库元数据；以下每条结论均来自本轮实际执行的命令与原始输出；无「已核对」「存在」一类空话。
> **补充轮说明**：首轮下发「Docker daemon 未启动、无 PostgreSQL 实例」在本轮开始前已过时。本轮按实测重写 §0.2 环境、§0.3、§0.4、H6、H8、H10、I 全节、问题清单与放行建议，并**真的把真库探针跑起来**；A~G 七项的结论与证据不变。
> **增量复验轮**：coder-3 提交 `3c5d209` 声称修复 P1-1 / P2-7 后，本轮再次独立复核提交范围、重建镜像、注入真库 URL 复跑探针（**10 passed / 0 failed / 0 skipped**）、跑全量回归、做提交态钉子变异自检，并如实记录**工作区存在未提交改动、与提交态不一致**的版本漂移（P2-8）。完整证据见 §0.5、H6 / H10、I4 与问题清单。
> **中间形态复核轮（§0.6，已被撤销，历史记录）**：§0.5 落盘后 HEAD 一度被 amend 为 `cbd7b0d`（303 行 / 10 用例 / **删除回归钉子**）。本轮独立核对该形态三方 hash 一致（`8299e309…`）、真库 `10/10` 复跑通过，并做对抗性实证（删 3 列复现 P1-1 → 宿主 `sssssssss.` 全绿 exit=0 / 容器真库 `FFFFFFFFF.` 9 FAILED exit=1），据此登记 P2-8「回归护栏缺失」。**随后 reflog 显示 `reset: moving to 3c5d209`，该形态已被撤销**；§0.6 保留为过程审计，不再是最终结论。
> **最终形态复核轮（§0.7，当前口径）**：HEAD 已 reset 回 `3c5d209`（363 行 / **11 用例 / 含回归钉子**）；工作区为 CRLF、HEAD blob 为 LF，`core.autocrlf=true` 归一化后逐字节一致。本轮再重建镜像（`BUILD_EXIT=0`，镜像内 363 行 / `e9e0c01a…` / 含钉子）、重跑宿主探针（`sssssssss..` = 9 skip + **2** pass，钉子在宿主可跑）、真库探针（junit `tests=11 failures=0 errors=0 skipped=0`、exit=0）、容器内全量（`942 / 930 passed / 1 failed / 0 errors / 11 skipped`，唯一失败仍是 redis DNS 环境问题）、钉子变异自检（删列名 → `AssertionError: 列清单漏了必填列 embedding_model` → 还原转绿）。**P2-8 随钉子回归而消解**；新增 P2-9 登记 HEAD 三次改写与行尾差异。**本报告的最终放行口径以 §0.7 与文末「放行建议」为准。**

---

## 0. 元信息、环境与结论速览

### 0.1 提交链现状（与下发口径的差异，须先对齐）

下发口径称「段 C 是工作区未提交态，3 改 1 新增」。本轮开验时实测已变化：

```
$ git -C <root> status --porcelain=v1
(clean / empty)

$ git -C <root> log --oneline -8
86f5fc7 feat(step4-C): 评测观测、假绿防线与真库 ACL 探针（只记录，不进方向表）
6717a85 feat(step4-B): wiki 导航 SQL 与两路接入（零迁移 / 关闭态零 SQL / set_config 三段式）
ea552c0 feat(step4-A): wiki 导航术语扩展 · 纯函数与契约（零 DB）+ N2 配置 + N6 别名截断修复
aa6551c docs(report): 纳入清障批次独立验证报告与 C4 别名截断量化报告
...
```

`$ git show --stat --format=%H %s 86f5fc7`：

```
86f5fc723b9b8e7641ab824d7b8a3a1d158cb46f feat(step4-C): ...
 code/apps/api/app/api/eval.py                      |  12 +
 code/apps/api/app/services/eval_service.py         |  30 +++
 code/apps/api/scripts/run_eval.py                  | 146 +++++++++-
 .../tests/wiki/test_eval_wiki_nav_observation.py   | 207 +++++++++++++++
 code/apps/api/tests/wiki/test_nav_acl.py           | 293 +++++++++++++++++++++
 5 files changed, 687 insertions(+), 1 deletion(-)
```

**差异 3 条**（均为信息同步问题，非代码缺陷，登记为 P2-4）：
1. 段 C 已提交为 `86f5fc7`，工作区干净，不再是「未提交态」；**提交链 = `ea552c0` → `6717a85` → `86f5fc7`**。本轮复核以磁盘当前内容为准，结论与提交态一致。
2. 段 C 是 **3 改 2 新增**（`test_eval_wiki_nav_observation.py` + `test_nav_acl.py`），不是「3 改 1 新增」。
3. `test_wiki_nav_off_state.py` 实际位于 `code/apps/api/tests/retrieve/`，`tests/wiki/` 下无此文件（下发路径写作 `tests/wiki/`）。

**补充轮追加核实（coder-3 通报的 message-only amend，本轮亲自复算）**：

```
$ git -C <root> show -s --format=%T ea1fe09   → f3c9b0c30877f89756305c2a0cd4e4ad03ed0cf2
$ git -C <root> show -s --format=%T 86f5fc7   → f3c9b0c30877f89756305c2a0cd4e4ad03ed0cf2
$ git -C <root> diff --stat ea1fe09 86f5fc7   → (空)
```

两提交**树对象逐字节相同**，仅提交信息不同；本报告 hash 以 `86f5fc7` 为准。`git show --stat 86f5fc7` 再次确认为 **3 改 + 2 新增 = 5 文件**（`test_eval_wiki_nav_observation.py` + `test_nav_acl.py`）。coder-3 已明确前者**也是段 C 新增**、不是上一批次遗留；本报告 §0.1 第 2 条与 H5 已将它纳入复核。

**工作区状态口径（避免「伪称干净」）**：首轮变异自检 / 静态核查执行时 `git status --porcelain=v1` 为空（实现文件零改动）；**本报告落盘之后**再查，唯一未跟踪项即本报告自身（`?? "04-test/测试报告-第4步wiki导航.md"`），无任何实现文件残留改动。

**增量复验轮提交链（本轮亲自复核）**：

```
$ git -C <root> log --oneline -3
3c5d209 fix(step4-C): 补 chunks 三个 NOT NULL 必填列，修复真库探针 9 FAILED（P1-1）+ 过时环境文案（P2-7）
86f5fc7 feat(step4-C): 评测观测、假绿防线与真库 ACL 探针（只记录，不进方向表）
6717a85 feat(step4-B): wiki 导航 SQL 与两路接入（零迁移 / 关闭态零 SQL / set_config 三段式）
$ git -C <root> rev-parse 3c5d209^   → 86f5fc723b9b8e7641ab824d7b8a3a1d158cb46f
$ git -C <root> diff-tree --no-commit-id --name-only -r 3c5d209
code/apps/api/tests/wiki/test_nav_acl.py
$ git -C <root> show --stat 3c5d209   → 1 file changed, 76 insertions(+), 6 deletions(-)
$ git -C <root> status --porcelain=v1
 M code/apps/api/tests/wiki/test_nav_acl.py
?? "04-test/测试报告-第4步wiki导航.md"
```

**★ 第一次 HEAD 改写（中间形态，已被撤销；见 §0.6）**：当时 `3c5d209` 一度**不在 HEAD 链上**，被 amend 为 `cbd7b0d`（以下为当时的复核记录；最终以 §0.7 的 `3c5d209` 为准）：

```
$ git -C <root> rev-parse HEAD
cbd7b0db0da22cdfff3624c4b2eef905ee0af8aa
$ git -C <root> log --oneline -2
cbd7b0d fix(step4-C): 补 chunks 三个 NOT NULL 必填列，交付文件在真库原样转绿（P1-1）+ 过时文案（P2-7）
86f5fc7 feat(step4-C): 评测观测、假绿防线与真库 ACL 探针（只记录，不进方向表）
$ git -C <root> show --stat cbd7b0d   → 1 file changed, 14 insertions(+), 4 deletions(-)
$ git -C <root> diff --stat 3c5d209 cbd7b0d
 code/apps/api/tests/wiki/test_nav_acl.py | 84 +++++---------------------------
 1 file changed, 12 insertions(+), 72 deletions(-)
$ git -C <root> show HEAD:…/test_nav_acl.py | grep -c 'def test_'                          → 3
$ git -C <root> show HEAD:…/test_nav_acl.py | grep -c 'test_seed_insert_includes_chunks_not_null_columns' → 0
$ git -C <root> diff --stat HEAD -- code/apps/api/tests/wiki/test_nav_acl.py              → (空，工作区 == HEAD)
```

即：`cbd7b0d` 把 §0.5 里的**工作区版本**（303 行 / 10 用例 / 绑定值内联字面量 / **无钉子**）写成提交，并删掉了 `3c5d209` 中新增的 `test_seed_insert_includes_chunks_not_null_columns` 与 `_SEED_*` 常量（`-72 行`）。当时据此登记 P2-8「回归护栏被删」，见 §0.6。

**★★ 二次改写：`reset` 回 `3c5d209`（最终形态，本轮亲自复核）**：

```
$ git -C <root> reflog -3 --format="%h %gs"
3c5d209 reset: moving to 3c5d209
cbd7b0d commit (amend): fix(step4-C): …交付文件在真库原样转绿…
3c5d209 commit: fix(step4-C): 补 chunks 三个 NOT NULL 必填列…
$ git -C <root> rev-parse HEAD   → 3c5d2094027370add0734e390c7a5963114d11c6
$ wc -l code/apps/api/tests/wiki/test_nav_acl.py                      → 363
$ grep -c 'test_seed_insert_includes_chunks_not_null_columns' …        → 2   （钉子已回归）
$ python <字节比对>  → 工作区 17057 bytes / CRLF 363 行；HEAD blob 16694 bytes / LF；
                       core.autocrlf=true；归一化后逐字节一致 = True；git diff HEAD -- 该文件 = 空
```

→ **当前最终交付形态 = `3c5d209`（363 行 / 11 用例 / 含回归钉子 / 补 3 列 / P2-7 文案已改）**。验证期间 HEAD 共被改写 3 次（`3c5d209` → `cbd7b0d` → reset 回 `3c5d209`），登记 **P2-9**（流程与行尾一致性建议）。本轮所有**最终**结论以 `3c5d209` 为对象，见 §0.7；§0.6 降级为中间形态审计记录。

**⚠ 验证对象漂移（登记 P2-8）**：提交态 `3c5d209` 的文件是 **363 行 / 11 用例**（含新钉子 `test_seed_insert_includes_chunks_not_null_columns`、模块级常量 `_SEED_*`）；磁盘当前工作区文件（**未提交 ` M`**）是 **303 行 / sha256 `8299e309…` / 10 用例**，钉子与模块常量被删、绑定值改为内联字面量、skip reason 又改一版。两版**都含** 3 列修复与 P2-7 整改。本轮真库证据一律以**磁盘当前内容**为准（镜像内 hash 已核对一致，见 §0.5.2），**不把「11 passed」写成当前版本结果**。

> **★ 状态更新（§0.7，最终口径）**：上述漂移经历 3 次 HEAD 改写后**已彻底消除**：当前 HEAD = `3c5d209`，工作区（CRLF）与 HEAD blob（LF）归一化后逐字节一致、镜像内文件与工作区逐字节一致（§0.7.1）；**回归钉子已回到最终交付中**，宿主无 DB 时钉子 + 纯函数共 2 例可跑（`sssssssss..`）→ **P2-8「回归护栏缺失」已消解并关闭**。§0.6.3 的对抗性实测仍保留，作为「为什么必须有这条钉子」的实证（无钉子形态下宿主全绿会掩盖 P1-1 回归）。历史漂移记录在此供追溯；新增 **P2-9** 登记 HEAD 并发改写与行尾差异。

### 0.2 环境

| 项 | 实测 |
|---|---|
| Python（宿主） | `Python 3.13.14`（`python --version`） |
| pytest / ruff（宿主） | `pytest 8.4.2` / `ruff 0.16.1`（`ruff.exe` 不在 PATH，用 `python -m ruff`） |
| 宿主已装依赖 | sqlalchemy 2.0.51 / asyncpg 0.31.0 / fastapi 0.141.1 / pydantic 2.9.2 / httpx 0.28.1 / alembic 1.20.0 / pytest-asyncio 0.26.0 |
| **宿主缺失依赖** | `python-jose`（`jose`）、`pypdf` 等未安装 → **宿主**全仓 pytest 收集阶段 26 个 `ModuleNotFoundError` |
| Docker（补充轮亲自复核） | `docker version` Client/Server **29.4.0**；`docker info --format '{{.ServerVersion}}'` = `29.4.0`；`docker compose version` = **v5.1.1** |
| PG 容器 | `code-postgres-1` **Up (healthy)**；`docker exec code-postgres-1 pg_isready -U user -d kagent` = `/var/run/postgresql:5432 - accepting connections` |
| **网络边界** | `docker port code-postgres-1` 输出**空**；宿主 `127.0.0.1:5432` = CLOSED。PG 只在 compose 网络 `code_default`（project name `code`）内，`docker inspect` 显示 alias `["code-postgres-1","postgres"]`，连接串 `postgresql+asyncpg://user:pass@postgres:5432/kagent` → **真库探针必须在容器内跑，宿主直连必然 skip** |
| 其余容器 | api / web / redis / minio / embedding / reranker = **Exited(255)**；`code-worker-1`、`code-ocr-1` = Up（与下发「其余均 Exited」略有差异，登记为环境事实，不影响结论） |
| 真库现状（psql 只读实测） | `alembic_version=0026`；`wiki_pages=1`（status=`ready`）；`eval_cases=217`；`documents=26`；`chunks=580`；`tenants=1` |
| 镜像 | `docker compose -f code/docker-compose.yml build api` → **BUILD_EXIT=0**（同时按共享 Dockerfile/context 重建了 `code-migrate`）；重建后镜像内 4 个段 A/B/C 文件 sha256 与工作区逐字节一致（见 I1.2） |
| 容器内依赖 | `requirements.lock` 含全部生产依赖（`python-jose==3.5.0` / `pypdf==5.9.0` / `asyncpg==0.32.0` / `SQLAlchemy==2.1.4`），但**不含 dev**（锁文件第 14 行明示「不含 dev 可选依赖（pytest 等）」）；每轮 `docker compose run` 现装 `pytest>=8,<9` + `pytest-asyncio>=0.24,<1` |
| PGlite | `.workbuddy/pglite-probe/` 目录存在但**本轮未启动、未做任何 PGlite 语义验证**（与真库严格分开，见 I3） |

### 0.3 必跑命令清单（testing-basics §8.4）与 SDK 确定性校验

| 类别 | 本轮实际执行 | 结果 |
|---|---|---|
| ① 启动验证 | 文档声明的启动方式是全服务 `docker compose up` + `alembic upgrade head`。补充轮已做：`docker compose build api` exit=0 + 镜像内文件 hash 与工作区一致 + `pg_isready` 通过 + `alembic_version=0026` 在库。**未做**：全服务 `compose up`、从零跑 26 迁移全链（库已是 0026，非本轮跑出来的）。`compileall` / `ruff check .` 宿主 exit=0；容器内全量 pytest 见 H10 | **部分执行；完整启动与全链迁移仍未跑（如实标注）** |
| ② 最小端到端用例 | 打桩全链路（E 的 4 条 SQL 顺序、`test_wiki_nav_off_state.py` 8 例、`build_fts_query` 达上限×别名真实调用）；**补充轮新增真库端到端**：合成页落真表后 `load_wiki_nav_page()` 的 8 个 ACL/状态/口径用例 + `statement_timeout` 还原（previous=`1234ms`）+ 逐表清理复核（见 I1.4 / I1.5） | 真实执行，有输出 |
| ③ 错误路径用例 | 导航 SQL 抛 `QueryCanceledError`、还原自身抛异常、缺观测字段按 0 计、Golden/F6 变异自检；**补充轮新增**：交付 `_seed` 在真库抛 `NotNullViolationError`（P1-1 复现）、真库探针缺 DB 显式 skip、容器内 `test_health.py` redis 名称解析失败；**中间形态复核轮新增（§0.6.3）**：临时副本删 3 列复现 P1-1 → 宿主 `sssssssss.` 全绿 exit=0 / 同副本容器真库 `FFFFFFFFF.` 9 FAILED exit=1；**最终形态复核轮新增（§0.7.5）**：钉子变异自检「删列名留占位符 → FAILED 且点名 `embedding_model` → 还原转绿」 | 真实执行，有输出 |
| SDK 校验（§9） | 全仓 Grep `codebuddy-agent-sdk` / `@tencent-ai/agent-sdk` / `codebuddy_agent_sdk` = **0 命中**。交付物为自研 FastAPI + SQLAlchemy 服务，**未使用 CodeBuddy Agent SDK** → `sdk-checklist.md` 逐条记 **N/A**，不虚构检查结果 | N/A |

### 0.4 结论速览（A~I）

| 项 | 结论 | 证据强度 | 问题 |
|---|---|---|---|
| A 关闭态等价 | **通过** | AST + 运行时单测 | — |
| B Golden 零行为改动 | **通过**（变异×1 击杀 + 还原复跑） | 变异自检 | — |
| C C4/F6 修复有牙 | **通过**（变异×1 击杀 2 条 + 还原复跑） | 变异自检 | — |
| D 导航 SQL 契约 | **通过**（与设计 §2.1.3 零差异 + 12 条契约 PASS） | 逐行比对 + 哨兵 | — |
| E statement_timeout 三段式 | **通过**（含 previous='1234ms'） | 打桩 SQL 顺序 | — |
| F 段 C「只记录」纪律 | **通过** | 运行时 + 源码 | P2-2（结论无效未传导到落库 `passed`） |
| G N5 | **通过**（代码零标识符） | 全仓扫描 | — |
| H 测试实跑 | **整改后：真库探针 10 passed / 0 failed / 0 skipped（exit=0）；容器内全量 941 / 929 passed / 1 failed / 0 errors / 11 skipped**，与整改前逐项相同 → 无回归。**最终形态 `3c5d209`（含钉子）复跑：真库 junit `tests=11 failures=0 errors=0 skipped=0`、exit=0；全量 `942 / 930 passed / 1 failed / 0 errors / 11 skipped`（§0.7）**，比 303 行形态恰好多 1 个钉子用例，无新增失败；唯一失败 `test_health.py` redis DNS 属环境、非本批次 | pytest（宿主 + 容器）/ ruff / compileall | P2-3、P2-5（容器侧已补齐）、P2-9 |
| I 真库边界 | **分层**：已实测 0026 在库 / 真库探针实跑**并转绿** / DDL 相容性 / 合成页入库清理 / 真库 `statement_timeout` 还原 / C4 217 条精确占比；仍未跑 26 迁移全链 / 217 A/B / EXPLAIN / P95 → **「不劣化」结论仍未验证** | 容器内真库 + psql 只读 | P1-1 已修复并经**最终形态**真库复跑确认；P2-8 已消解关闭（钉子回归）；新增 P2-9（HEAD 三次改写 + 行尾差异） |

**计数：P0 = 0，P1 = 0（P1-1 已修复并经最终形态复跑确认），P2 = 7 未结**（P2-7 / P2-8 已关闭、P2-9 本轮新增；详见「问题清单」）。

### 0.5 增量复验：`3c5d209` 修复 P1-1 / P2-7（本轮追加）

> 收到 coder-3「已修完」通报后**不采信声明**，重新走一遍：提交范围只读复核 → 重建镜像 → 真库复跑 → 全量回归 → 清理复核 → 变异自检。命令与原始输出见下列各节及附录 A。
>
> **本节为过程审计记录，验证对象是当时的磁盘工作区版本（sha256 `8299e309…` / 303 行 / 10 用例）**；其后 HEAD 一度被 amend 为 `cbd7b0d`（内容恰为该工作区版本，§0.6），又被 `reset` 回 `3c5d209`（363 行 / 11 用例 / 含钉子，§0.7）。**当前最终交付形态 = `3c5d209`**，其独立复跑见 §0.7；本节与 §0.6 均为中间形态的过程证据，**结论以 §0.7 为准**。

#### 0.5.1 提交范围与验证对象（发现版本漂移）

```
$ git -C <root> log --oneline -3
3c5d209 fix(step4-C): 补 chunks 三个 NOT NULL 必填列，修复真库探针 9 FAILED（P1-1）+ 过时环境文案（P2-7）
86f5fc7 feat(step4-C): 评测观测、假绿防线与真库 ACL 探针（只记录，不进方向表）
6717a85 feat(step4-B): wiki 导航 SQL 与两路接入
$ git -C <root> rev-parse 3c5d209^          → 86f5fc723b9b8e7641ab824d7b8a3a1d158cb46f
$ git -C <root> diff-tree --no-commit-id --name-only -r 3c5d209
code/apps/api/tests/wiki/test_nav_acl.py
$ git -C <root> show --stat 3c5d209         → 1 file changed, 76 insertions(+), 6 deletions(-)
$ git -C <root> diff --check               → (空)
$ git -C <root> status --porcelain=v1
 M code/apps/api/tests/wiki/test_nav_acl.py
?? "04-test/测试报告-第4步wiki导航.md"
```

coder-3 的提交说明本身诚实标注「本机无 PostgreSQL 实例，9 条真库探针仍显式 skip，**不能声称真库已转绿**，需 tester-3 在容器内复跑」——符合独立性红线。但**验证对象不一致**：提交态文件 363 行 / 11 用例（含钉子、模块常量 `_SEED_*`），磁盘当前工作区（未提交 ` M`）303 行 / sha256 `8299e309…` / 10 用例（钉子与常量被删、绑定值改内联字面量、skip reason 又改一版）。两版都含 3 列修复与 P2-7 整改。详见 §0.1 与 P2-8。

#### 0.5.2 镜像重建与 hash 核对（防止复跑旧镜像）

```
$ docker compose -f code/docker-compose.yml build api      → BUILD_EXIT=0（2m08s）
$ Get-FileHash tests/wiki/test_nav_acl.py -Algorithm SHA256
8299E309CFD29635D137FCC7D205BC2B20A15B13BBC1258A601541C3513D1CDA
$ docker run --rm --entrypoint sh code-api:latest -c
    "sha256sum /workspace/apps/api/tests/wiki/test_nav_acl.py; wc -l …; grep -c 'def test_' …"
8299e309cfd29635d137fcc7d205bc2b20a15b13bbc1258a601541c3513d1cda  …
303
3
```

镜像内 hash 与**磁盘当前文件**逐字节一致 → 复跑的是工作区版本，既不是 `86f5fc7` 旧版，也不是 `3c5d209` 提交态。

#### 0.5.3 真库复跑（核心结论）

```
$ docker compose -f … run --rm --no-deps -T \
    -e WIKI_NAV_ACL_DB_URL=postgresql+asyncpg://user:pass@postgres:5432/kagent \
    api sh -c "pip install -q pytest pytest-asyncio && python -m pytest tests/wiki/test_nav_acl.py -ra --junitxml=…"
..........                                                               [100%]
10 passed in 1.59s
RERUN_EXIT=0
NAV_JUNIT tests=10 failures=0 errors=0 skipped=0 passed=10
```

junit 用例清单逐条为 8 个 ACL 参数化（`ok / cross_kb / high_level / deny / acl_mismatch / stale / failed / caliber`）+ `test_success_path_restores_statement_timeout` + `test_hit_page_whose_terms_are_all_in_query_expands_nothing`。**原 9 条 FAILED 全部转 PASS，0 skipped / 0 failed / exit=0 → P1-1 修复经 P3 独立真库复跑确认。**

#### 0.5.4 全量回归、清理复核（★含合成页）

```
$ docker compose -f … run --rm --no-deps -T api sh -c "… python -m pytest -q -rs --tb=line --junitxml=…"
FULL_EXIT=1
FULL_JUNIT tests=941 failures=1 errors=0 skipped=11 passed=929
FAILED: tests.test_health::test_openapi_contains_health -> redis:6379 Name or service not known
```

与整改前 H10 的 `941 / 929 / 1 / 0 / 11` **逐项相同** → 修复未引入回归；唯一失败仍是 `code-redis-1` Exited(255) 的环境失败，非本批次引入。全量跑故意**不设** `WIKI_NAV_ACL_DB_URL` / `WIKI_ACL_DB_URL`，避免污染真库。

独立只读 SQL（真库复跑结束后）：

```
tenants WHERE code LIKE 'navacl-%'                         → 0
四表合成前缀 (wiki_pages / chunks / documents / kbs)      → 0|0|0|0
alembic_version|0026 / eval_cases|217 / documents|26 / chunks|580 / wiki_pages|1 / tenants|1
wiki_pages status → ready|1
```

★ **本轮复跑同样含合成页**（独立租户 + 2 kb + 8 文档/片/页，由交付文件 `_cleanup` / `_verify_cleanup` 清理），合成数据零残留、真实数据零改动；**合成页不得混算进真实页覆盖率**，真库真实页仍只有 1 页 `ready`。

#### 0.5.5 P2-7 整改确认 + 两版分别的变异自检证据

- **P2-7 已整改**：两版 docstring 与 `db_url` skip reason 均不再出现「本机无 Docker daemon / 无 PostgreSQL 实例」，改为「未显式提供 `WIKI_NAV_ACL_DB_URL` 或不在 compose 网络内时跳过」；skip 判据 `if not _DB_URL` 未动，N4「含合成页 / 独立租户+kb / finally 清理 + SQL 复核」纪律未动。
- **宿主侧护栏现状**：`python -m pytest -q -rs tests/wiki/test_nav_acl.py`（宿主、无 DB）→ `sssssssss.` = 9 skipped + 1 passed（纯函数），`HOST_EXIT=0`。当前 303 行版本**没有任何不依赖真库的用例检查 chunks INSERT 列形状** → P1-1 的「全 skip 全绿」掩盖机制重新成立。
- **第一次变异自检无效（如实标注，不采用）**：在磁盘当前 303 行副本上跑钉子用例 → `exit=4 ERROR: not found: …::test_seed_insert_includes_chunks_not_null_columns (no match)`。脚本里 `变异使钉子变红 = True` 属 **NOT_FOUND 假阳性**，不是断言变红；本报告明确**不把该次记为「自检通过」**。
- **第二次变异自检有效（提交态 `3c5d209`）**：用 `git show 3c5d209:…` 取出 363 行含钉子文件写入临时副本：

```
工作区当前文件 sha256 = 8299e309… (303 行 / 无钉子)
提交态 3c5d209 文件字节 = 16694  行数 = 363  含钉子函数名 = True
[基线(提交态/未变异)] exit=0    → 钉子基线 PASS
[变异] 仅删列清单中 embedding_model 列名；VALUES 的 :embedding_model 占位符与 params 保留
       变异后仍含 ':embedding_model' = True
[变异(删列名/留占位符)] exit=1
       E AssertionError: chunks INSERT 列清单漏了必填列 embedding_model
       失败信息点名 embedding_model = True
[还原后复跑] exit=0             → 转绿
还原后副本与提交态字节一致 = True；临时副本已删除 = True；工作区文件 sha256 未变 = True
```

→ **提交态的新钉子确实有牙**：能挡住「删列名但残留 `:embedding_model` 占位符」的弱断言绕过。工作区把它删掉，等于放弃宿主无 DB 场景下唯一能发现 P1-1 回归的护栏 → 登记 **P2-8**。

#### 0.5.6 增量复验结论

| 项 | 结论 |
|---|---|
| P1-1 | **已修复并真库复跑确认**：9 FAILED → 9 PASS；整文件 `10 passed / 0 failed / 0 skipped / exit=0` |
| P2-7 | **已整改**：过时环境文案两版均清除，skip 判据与 N4 纪律未动 |
| 回归 | 全量 `941 / 929 / 1 / 0 / 11` 与整改前逐项一致，无新增失败 |
| 新增问题 | **P2-8**：工作区未提交 ` M` 与 HEAD 不一致（303 行/10 用例 vs 363 行/11 用例）、回归钉子被删；交付前须明确最终版本并落盘提交 |
| 仍未变 | I2 六项仍未跑；**「不劣化」结论仍未验证** |

### 0.6 中间形态复核：`cbd7b0d`（已被撤销，历史记录，非最终结论）

> **状态说明（务必先读）**：本章写于 HEAD = `cbd7b0d` 期间；其后 reflog 出现 `reset: moving to 3c5d209`，该提交已被撤销、不再是 HEAD。**本章结论（尤其「P2-8 成立且已固化」）已失效**，最终口径见 §0.7。本章保留的理由：① 记录验证对象被并发改写的审计轨迹；② §0.6.3 的对抗性实测是「无钉子形态下宿主全绿掩盖 P1-1 回归」的唯一实证，恰好反证最终形态补回钉子的必要性。
>
> 触发原因：§0.5 落盘后复查 `git log` 发现 HEAD 不再是 `3c5d209`。按独立性红线，**验证对象变了就必须重验**，不能把上一轮证据直接套用到新提交上。

#### 0.6.1 三方 hash 链（确定「复跑的到底是什么」）

```
$ git -C <root> rev-parse HEAD
cbd7b0db0da22cdfff3624c4b2eef905ee0af8aa
WORKTREE  8299e309cfd29635d137fcc7d205bc2b20a15b13bbc1258a601541c3513d1cda *code/apps/api/tests/wiki/test_nav_acl.py
HEAD blob 8299e309cfd29635d137fcc7d205bc2b20a15b13bbc1258a601541c3513d1cda *-
IMAGE     8299e309cfd29635d137fcc7d205bc2b20a15b13bbc1258a601541c3513d1cda  /workspace/apps/api/tests/wiki/test_nav_acl.py
303
$ git show HEAD:…/test_nav_acl.py | grep -c 'def test_'                                    → 3
$ git show HEAD:…/test_nav_acl.py | grep -c 'test_seed_insert_includes_chunks_not_null_columns' → 0
$ git -C <root> diff --stat HEAD -- code/apps/api/tests/wiki/test_nav_acl.py                 → (空)
```

→ **工作区 == HEAD blob == 镜像内文件，逐字节一致**（303 行 / 10 用例 / 无钉子）。§0.5 的「版本漂移」已消除；`cbd7b0d` 相对 `3c5d209` 少了 72 行，即删除钉子用例与 `_SEED_*` 常量、绑定值改内联字面量。**该中间形态不含任何不依赖真库的 chunks INSERT 列形状护栏**（这正是 §0.6.3 登记 P2-8 的原因；最终形态已补回钉子，见 §0.7）。

#### 0.6.2 针对 `cbd7b0d` 的真库探针复跑（核心结论）

```
$ docker compose -f code/docker-compose.yml run --rm --no-deps -T -v <temp>/p3d_junit:/tmp/p3djunit -e WIKI_NAV_ACL_DB_URL=postgresql+asyncpg://user:pass@postgres:5432/kagent api sh -c "pip install -q ... pytest pytest-asyncio && python -m pytest tests/wiki/test_nav_acl.py -q -rs --junitxml=/tmp/p3djunit/nav_final.xml"
..........                                                               [100%]
--------------- generated xml file: /tmp/p3djunit/nav_final.xml ----------------
FINAL_EXIT=0

$ python <junit XML 解析>
tests=10 failures=0 errors=0 skipped=0
passed= 10
```

用例清单逐条为 8 个 ACL 参数化 + `test_success_path_restores_statement_timeout` + `test_hit_page_whose_terms_are_all_in_query_expands_nothing`。**P1-1 在最终交付形态上确认解除：9 FAILED → 9 PASS，0 skipped / 0 failed / exit=0。**（另有一次不带 junit 的复跑同样 `..........` + `FINAL_RERUN_EXIT=0`。）

#### 0.6.3 对抗性验证：删掉回归钉子后，宿主侧能否发现 P1-1 回归？（★ 本轮新增，坐实 P2-8）

不采信「理论上会掩盖」的推断，自己造回归：在**临时副本**（`shutil.copytree(code/apps/api)`，不改实现）里把 chunks INSERT 的**列清单 / VALUES 占位符 / params 三处**同时移除 `embedding_model` / `embedding_text_version` / `chunker_version`，完全复现 P1-1 原交付形态，然后同一份副本跑两种环境：

```
$ python <temp>/p3d_adversarial.py
原文件 sha256 = 8299e309…   原文件行数 = 303   原文件含钉子 = False
变异后文件仍含 'embedding_model' = False
变异后文件仍含 ':embedding_model' = False
变异后文件行数 = 297
[宿主无 DB / 变异后复跑] exit = 0
    sssssssss.                                                               [100%]
    SKIPPED [8] tests\wiki\test_nav_acl.py:226: 需显式提供 WIKI_NAV_ACL_DB_URL …
    SKIPPED [1] tests\wiki\test_nav_acl.py:262: 需显式提供 WIKI_NAV_ACL_DB_URL …
宿主仍全绿(exit=0) = True
原项目文件 sha256 未变 = True
```

```
$ docker compose -f … run --rm --no-deps -T -v <temp>/p3d_mutant/api/tests/wiki/test_nav_acl.py:/workspace/apps/api/tests/wiki/test_nav_acl.py:ro -e PYTHONDONTWRITEBYTECODE=1 -e WIKI_NAV_ACL_DB_URL=postgresql+asyncpg://user:pass@postgres:5432/kagent api sh -c "… python -m pytest tests/wiki/test_nav_acl.py -q --tb=line"
FFFFFFFFF.                                                               [100%]
sqlalchemy.exc.IntegrityError: (…asyncpg.NotNullViolationError) null value in column
  "embedding_model" of relation "chunks" violates not-null constraint   ×9
=========================== short test summary info ============================
FAILED … 8 个 ACL 参数化 + test_success_path_restores_statement_timeout  (9 条)
MUTANT_CONTAINER_EXIT=1
```

**结论（实测，非推断）**：同一份已回归的代码，宿主 / CI 无 DB 场景是 `sssssssss.` **全绿 exit=0**，只有显式注入真库 URL 的容器才 `9 FAILED exit=1`。而 `cbd7b0d` 恰好删掉了 `3c5d209` 里唯一能在无 DB 环境发现该回归的钉子（该钉子的「有牙」已在 §0.5.5 用提交态副本变异自检证明：删列名留占位符 → `AssertionError: 列清单漏了必填列 embedding_model`）。→ **当时 P2-8 成立**（该中间形态确无护栏）；但此后 HEAD `reset` 回 `3c5d209`、钉子回归，该问题已消解关闭，见 §0.7。此段保留为「为什么必须有这条钉子」的反证。

> 如实标注：脚本里两个自算布尔字段（`宿主仍出现 skipped` / `宿主出现任何 failed`）因大小写匹配写成 `False`，属脚本自身的弱判断，**本报告不引用它们**；结论只依据原始输出 `sssssssss.`、`SKIPPED [8]/[1]`、`exit=0` 与容器侧 `FFFFFFFFF.`、`exit=1`，不做美化。

#### 0.6.4 清理复核与零改动

```
$ docker exec code-postgres-1 psql -U user -d kagent -t -A -c "<5 表合成前缀 count>"
0|0|0|0|0
alembic_version|0026 / eval_cases|217 / documents|26 / chunks|580 / wiki_pages|1 / tenants|1
$ sha256sum code/apps/api/tests/wiki/test_nav_acl.py
8299e309cfd29635d137fcc7d205bc2b20a15b13bbc1258a601541c3513d1cda
```

合成数据零残留、真实数据总量与基线一致；工作区实现文件 hash 未变；临时副本 `p3d_mutant/` 已删除。**本轮未改任何实现文件、未 commit、未 push。**

#### 0.6.5 该中间形态当时的结论（**已被 §0.7 取代**）

| 项 | 当时结论（现已失效 / 已消解） |
|---|---|
| P1-1 | 已解除：`cbd7b0d` 交付文件在真库 `10 passed / 0 failed / 0 skipped / exit=0`（§0.6.2）；最终形态见 §0.7.3 的 11 passed |
| P2-7 | 已整改关闭：两处过时文案清除，skip 判据与 N4 纪律未动（§0.5.5） |
| 版本漂移 | 当时已消除（工作区 == HEAD == 镜像）；其后 HEAD 再次改写，见 §0.7.1 |
| **P2-8** | **当时成立、现已消解**：`cbd7b0d` 删除回归钉子，实测「删 3 列后宿主全绿 / 真库 9 FAILED」；`reset` 回 `3c5d209` 后钉子回归，宿主可跑且变异自检有牙（§0.7.2 / §0.7.5）→ **关闭** |
| 效果层 | **仍未验证**：I2 六项（26 迁移全链 / 217 A/B / EXPLAIN / P95 / 假绿门槛真实触发 / 多页库）全未跑；真实 wiki 页仍仅 1 页 `ready`（此条跨形态不变） |

### 0.7 最终交付形态复核：`3c5d209`（当前口径，最终结论）

> 触发：reflog 出现 `reset: moving to 3c5d209`，HEAD 又变了。按独立性红线**再次重验**，不复用 §0.6 的任何结论。

#### 0.7.1 交付形态与三方一致性（含行尾差异的如实登记）

```
$ git -C <root> rev-parse HEAD   → 3c5d2094027370add0734e390c7a5963114d11c6
$ git -C <root> show --stat HEAD → 1 file changed, 76 insertions(+), 6 deletions(-)
$ wc -l code/apps/api/tests/wiki/test_nav_acl.py                       → 363
$ grep -c 'test_seed_insert_includes_chunks_not_null_columns' …         → 2   （钉子回归）
$ sha256sum 工作区  → e9e0c01a19553f794c84a23a38add47ecdf867ddd19f24133a3f154426b02861（CRLF / 17057 bytes）
$ git show HEAD:… | sha256sum → fd741fe2fa6b080aefaed2d1f8a1a4abdbca33a25479468d2eb2083950ee9360（LF / 16694 bytes）
$ python <归一化字节比对>
    raw equal = False ; normalized equal = True ; work CRLF = 363 ; head CRLF = 0
    core.autocrlf = true ; git diff HEAD -- code/apps/api/tests/wiki/test_nav_acl.py = 空
$ docker compose -f code/docker-compose.yml build api                  → BUILD_EXIT=0
$ docker run --rm --entrypoint sh code-api:latest -c "wc -l …; sha256sum …; grep -c …"
363
e9e0c01a19553f794c84a23a38add47ecdf867ddd19f24133a3f154426b02861  /workspace/apps/api/tests/wiki/test_nav_acl.py
2
```

→ **镜像内文件与工作区逐字节一致**（`e9e0c01a…`，363 行，含钉子）；工作区与 HEAD blob 仅行尾差异（CRLF / LF，`core.autocrlf=true` 归一化后逐字节一致），**逻辑内容完全一致**，不影响测试结论。如实登记为 P2-9。

#### 0.7.2 宿主探针：钉子在无 DB 环境确实可跑（P2-8 消解）

```
$ python -m pytest -q -rs tests/wiki/test_nav_acl.py      （宿主，无 WIKI_NAV_ACL_DB_URL）
sssssssss..                                                              [100%]
SKIPPED [8] tests\wiki\test_nav_acl.py:242 / SKIPPED [1] …:278
HOST_EXIT=0
```

9 条真库探针显式 skip，**钉子 + 纯函数共 2 例真实执行并通过** → 与 §0.6.3 无钉子形态的 `sssssssss.`（仅 1 pass）形成对照：宿主侧现在有一条能在无 DB 时发现 chunks INSERT 列形状回归的用例。

#### 0.7.3 真库复跑（核心结论）

```
$ docker compose -f … run --rm --no-deps -T -v <temp>/p3e_junit:/tmp/p3ejunit -e WIKI_NAV_ACL_DB_URL=postgresql+asyncpg://user:pass@postgres:5432/kagent api sh -c "pip install -q ... pytest pytest-asyncio && python -m pytest tests/wiki/test_nav_acl.py -q -rs --junitxml=/tmp/p3ejunit/nav.xml"
...........                                                              [100%]
REALDB_EXIT=0
$ python <junit XML 解析>
{'tests': 11, 'failures': 0, 'errors': 0, 'skipped': 0}  passed= 11
```

11 个用例逐条为 8 个 ACL 参数化 + `test_success_path_restores_statement_timeout` + **`test_seed_insert_includes_chunks_not_null_columns`** + `test_hit_page_whose_terms_are_all_in_query_expands_nothing`。**P1-1 在最终交付形态上确认解除：9 FAILED → 9 PASS，整文件 `11 passed / 0 failed / 0 skipped / exit=0`。**

#### 0.7.4 全量回归（容器内，精确计数）

```
$ docker compose -f … run --rm --no-deps -T -v <temp>/p3e_junit:/tmp/p3ejunit api sh -c "… pytest -q -rs --tb=line --junitxml=/tmp/p3ejunit/full.xml"
FULL_EXIT=1
{'tests': 942, 'failures': 1, 'errors': 0, 'skipped': 11}  passed= 930
FAILED: test_openapi_contains_health   （redis:6379 Name or service not known）
```

与 303 行形态的 `941 / 929 / 1 / 0 / 11` **逐项对照仅 `tests` 与 `passed` 各 +1**（正是新增的钉子用例），其余完全一致 → 修复与钉子回归**未引入任何新增失败**；唯一失败仍是 `code-redis-1` Exited(255) 的环境失败，非本批次引入（H8）。全量跑故意不设 `WIKI_NAV_ACL_DB_URL` / `WIKI_ACL_DB_URL`，避免污染真库。

#### 0.7.5 钉子变异自检（最终形态，证明「有牙」而非摆设）

在临时副本（`shutil.copytree(code/apps/api)`，不改实现）上：

```
$ python <temp>/p3e_mutate.py
原文件 sha256 = e9e0c01a…  行数 = 363  含钉子 = True  CRLF 行数 = 363
[基线(交付形态/未变异)] exit=0        → 钉子基线 PASS = True
变异 = 仅删列清单中 embedding_model 列名；VALUES 的 :embedding_model 占位符与 params 保留
变异后仍含 ':embedding_model' = True
[变异(删列名/留占位符)] exit=1
    >  assert column in columns_part, f"chunks INSERT 列清单漏了必填列 {column}"
    E  AssertionError: chunks INSERT 列清单漏了必填列 embedding_model
    FAILED tests/wiki/test_nav_acl.py::test_seed_insert_includes_chunks_not_null_columns
变异使钉子 FAILED = True；失败信息点名 embedding_model = True
还原后副本与原文件字节一致 = True
[还原后复跑] exit=0        → 还原后基线转绿 = True
临时副本已删除 = True；原项目文件 sha256 未变 = True
```

→ 钉子能挡住「删列名但残留 `:embedding_model` 占位符」的弱断言绕过。**这是 P2-8 消解的直接证据**：最终形态具备宿主侧发现 P1-1 类回归的能力。（如实标注：首跑脚本把输出截断到 900 字符，导致自算字段 `失败信息点名 embedding_model = False`；改为保留 4000 字符后重跑得 `True`，以上为第二次完整输出，不美化首跑结果。）

#### 0.7.6 清理复核与零改动

```
$ docker exec code-postgres-1 psql -U user -d kagent -t -A -c "<5 表合成前缀 count>"
0|0|0|0|0
wiki_pages|1 / chunks|580 / documents|26 / tenants|1 / eval_cases|217
$ sha256sum code/apps/api/tests/wiki/test_nav_acl.py   → e9e0c01a…（未变）
```

合成数据零残留、真实数据总量与基线一致；工作区实现文件 hash 未变；临时副本已删除。**本轮未改任何实现文件、未 commit、未 push。**

#### 0.7.7 最终结论

| 项 | 最终结论 |
|---|---|
| P1-1 | **已解除**：`3c5d209` 交付文件在真库 `11 passed / 0 failed / 0 skipped / exit=0`（§0.7.3） |
| P2-7 | **已整改关闭**：两处过时文案清除，skip 判据与 N4 纪律未动 |
| P2-8 | **已消解关闭**：回归钉子回到最终交付；宿主可跑（`sssssssss..`），变异自检证明有牙（§0.7.2 / §0.7.5） |
| 版本漂移 | **逻辑上已消除**：工作区 CRLF / HEAD blob LF 归一化后一致、镜像与工作区逐字节一致；行尾差异与 HEAD 三次改写登记为 P2-9 |
| **P2-9（新）** | HEAD 在验证期间被并发改写 3 次（`3c5d209 → cbd7b0d → reset 回 3c5d209`），验证对象漂移；建议锁定交付形态、统一行尾 |
| 效果层 | **仍未验证**：I2 六项（26 迁移全链 / 217 A/B / EXPLAIN / P95 / 假绿门槛真实触发 / 多页库）全未跑；真实 wiki 页仍仅 1 页 `ready` |

---

## A. 关闭态等价（`WIKI_ENABLED=false` 零 import / 零 SQL / `path_stats` 无 `wiki_*`）

**要求**：用打桩或 Python AST 证明「延迟 import 确实在 `if wiki_on` 内」，不是只看字符串存在。

**命令**：自写只读 Python 脚本以 `ast.parse` 解析 `app/services/retrieve/__init__.py`，收集所有 module 以 `app.services.wiki` / `app.config.wiki_profile` 开头的 `ImportFrom`/`Import` 节点，回溯其祖先 `If` 链并打印 `test` 源码片段。

**原始输出**：

```
模块级/函数级 wiki 相关 import 节点总数 = 2
  L264  from app.config.wiki_profile import ['current_wiki_profile']
         enclosing If tests = ['wiki_on and ilike_terms']
         在含 wiki_on 的条件块内: True
  L265  from app.services.wiki.navigate import ['load_wiki_nav_page', 'select_nav_terms']
         enclosing If tests = ['wiki_on and ilike_terms']
         在含 wiki_on 的条件块内: True
结论: 全部 wiki import 均位于含 wiki_on 的 If 内
模块级(顶层) wiki import 数量 = 0（应为 0）
```

**运行时证据**（`tests/retrieve/test_wiki_nav_off_state.py`，本轮核心专项集内实跑）：
- `test_off_state_issues_zero_nav_sql_and_never_imports_navigate`：`monkeypatch.delitem(sys.modules, "app.services.wiki.navigate")` 后跑完整 `retrieve()`，断言 `session.wiki_sql_seen == []`、`"app.services.wiki.navigate" not in sys.modules`、`"wiki_nav" not in result.stage_ms`、`path_stats` 无 `wiki_` 前缀键、关闭态不打任何 wiki 指标。
- `test_off_state_pattern_and_needle_are_baseline`：`pattern == build_ilike_pattern(terms, {})`、`needle == terms[0].lower()`。
- `test_off_state_path_stats_keys_match_pre_step4_shape`：`set(result.path_stats) == {vector/tsquery/ilike × covered/unique 六键}`，全 int。

实现侧对应 `retrieve/__init__.py`：L261 `wiki_on = get_settings().wiki_enabled`；L263 `if wiki_on and ilike_terms:` 才 `from app.services.wiki.navigate import ...`；`nav_stats` 初始化为空 `{}`（L258），L802 `path_stats.update(nav_stats)` → 关闭态空字典，不写任何 `wiki_*` 键。

**结论：A 通过。**

---

## B. Golden 零行为改动 + 变异自检

**资产**：`tests/retrieve/golden_wiki_nav_baseline.json`（206 核心样本 + 20 代表问句，去重 **220** 条；`meta` 标注「改动前证据、不可重新生成、217 条精确清单不可得」）+ `test_golden_wiki_nav.py`（5 例）。

`test_all_golden_cases_are_byte_identical` 逐条比对 `build_ilike_terms` / `build_ilike_pattern` / `build_fts_query` 三函数输出；另有 `test_explicit_empty_extra_params_are_identical_to_omitting_them`、`test_extra_terms_append_or_branches_without_touching_original_terms`、`test_extra_index_tokens_keep_original_windows`。

**变异自检方法（独立性红线）**：不碰原实现文件。用 `shutil.copytree` 把 `code/apps/api` 复制到系统临时目录 `p3_mutant/api`（排除 `__pycache__`），在副本上变异并跑 pytest；`finally` 用原始字节写回副本、校验 SHA256、`git diff`、并删除整个副本。先确认副本 import 来源：

```
副本 import 来源 = C:\Users\86186\AppData\Local\Temp\p3_mutant\api\app\services\keyword_text.py
```

**变异 B**：在副本 `keyword_text.py` 的 `build_fts_query` 中，`groups: list[str] = []` 之前插入一行无条件 `windows = _dedupe(windows + ["P3_MUTANT_TOKEN"])`，即「extra 为空时也追加一个词元」。

```
[基线] pytest tests/retrieve/test_golden_wiki_nav.py tests/retrieve/test_fts_alias_cap.py -> exit=0
  .......                                                                  [100%]      (7 passed)

[变异 B] -> pytest tests/retrieve/test_golden_wiki_nav.py exit=1
  E       AssertionError: Golden 快照出现 220 处漂移（前 5 条）：
  FAILED tests/retrieve/test_golden_wiki_nav.py::test_all_golden_cases_are_byte_identical
  FAILED tests/retrieve/test_golden_wiki_nav.py::test_extra_index_tokens_keep_original_windows
  变异 B 使 Golden 变红: True
  还原后副本 hash = 07b774d0...c9；与原项目一致: True

[还原后复跑] exit=0；.......                        [100%]                        (7 passed)
```

**还原核验**：

```
原项目 keyword_text.py sha256 = 07b774d02ab0f307f6998958a9e4f8fba1a4977dc2b49d62842d2ace8d8fa5c9
与验证前一致: True
git diff -- keyword_text.py = ''（空即未改动）
git status --porcelain = ''
临时副本已删除: True
```

**记录**：变异次数 **1**（B），还原后基线复跑 **2 次**（含 C 之后一次），两次均 exit=0。

**结论：B 通过 —— Golden 确实能钉住零行为改动，变异一击即红且还原后 diff 为空。**

---

## C. C4/F6 修复有牙 + 变异自检

**资产**：`tests/retrieve/test_fts_alias_cap.py` 两条「达上限 × 传别名」断言，与 `02-design/C4-别名静默截断影响面量化.md` §3.7 待落地代码块逐字一致（CJK 26 互异汉字、ASCII 40 词元）。

**先核实「原 query 窗口被截到 24 后一个不少」**（真实调用 `build_fts_query`，非推断）：

```
CJK: 26 互异汉字 → base 窗口数 = 24（FTS_QUERY_MAX_TERMS=24）
  extended != base: True
  '访问' 在 extended: True
  原 24 个窗口全部仍在 extended: True
  丢失窗口 = []
ASCII: 40 词元 → base 项数 = 24
  extended != base: True；'accesstoken' 在 extended: True
  原 24 项全部仍在 extended: True
  丢失项 = []
```

修复落在 `keyword_text.py` L189-194：别名按 `LEXICON_FTS_ALIAS_MAX_TERMS=6` 独立限流后 `_dedupe(windows + cjk_aliases)` 追加，**不再 `[:FTS_QUERY_MAX_TERMS]` 重截**；wiki 扩词走独立 `if extra_index_tokens` 分支（L203-208），`cap = 24 + 6`。

**变异 C**：在副本上把这两行改回「修复前行为」：

```python
windows = _dedupe(windows + cjk_aliases)[: decisions.FTS_QUERY_MAX_TERMS]
ascii_items = _dedupe(ascii_items + ascii_aliases)[: decisions.FTS_QUERY_MAX_TERMS]
```

```
[变异 C] -> pytest tests/retrieve/test_fts_alias_cap.py exit=1
  >       assert extended != base, "别名被静默吞掉：带/不带别名输出逐字节相同"
  E       AssertionError: 别名被静默吞掉：带/不带别名输出逐字节相同
  >       assert extended != base, "ASCII 别名被静默吞掉：带/不带别名输出逐字节相同"
  E       AssertionError: ASCII 别名被静默吞掉：带/不带别名输出逐字节相同
  FAILED ...::test_fts_query_keeps_cjk_alias_when_windows_hit_cap
  FAILED ...::test_fts_query_keeps_ascii_alias_when_ascii_items_hit_cap
  FAILED 用例数 = 2；两条目标断言均变红: True
  还原后副本 hash 与原项目一致: True
[二次还原后复跑] exit=0；.......                  [100%]                        (7 passed)
```

**记录**：变异次数累计 **2**（B + C），两次均「立即还原 + 复跑转绿 + 原文件 hash/diff 零改动」。

**结论：C 通过 —— 两条红断言已转绿且回退实现即变红，修复有牙；原 24 窗口一个不少。**

---

## D. 导航 SQL 契约（与设计 §2.1.3 逐行比对）

**方法**：用 `ast.literal_eval` 从 `navigate.py` 取 `_WIKI_NAV_SQL` 常量、从 `retrieve/__init__.py` 取 `_WHERE_BASE` 常量；`.format(where_base=_WHERE_BASE)` 后，与从设计文档 §2.1.3 抽取的 SQL 代码块比对。规范化规则：去掉行尾 `--` 注释与空行、`{where_base}` 双向替换为**同一份** `_WHERE_BASE` 的 6 行。

```
_WIKI_NAV_SQL 模板长度 = 862 字符 / 27 行
_WHERE_BASE 长度 = 209 字符 / 6 行
设计 §2.1.3 SQL 块长度 = 1113 字符 / 28 行
规范化后 设计行数=31 实现行数=31
差异清单: 零差异（逐行一致）
```

`_WHERE_BASE` 原文（6 行，实现与设计共用）：

```
WHERE c.tenant_id = :tenant_id
  AND c.is_latest = true
  AND c.level_rank <= :clearance
  AND c.kb_id = ANY(:authorized_kb_ids)
  AND NOT (c.deny_subjects && :user_subjects)
  AND c.acl_tags && :user_subjects
```

**逐条契约检查（12/12 PASS）**：

```
[PASS] strpos(lower(kt.term), n.term) > 0
[PASS] 只 SELECT document_id, key_terms（顶层 SELECT）
[PASS] SQL 中不出现 summary / sections
[PASS] ORDER BY best_len DESC, hit_terms DESC, document_id ASC
[PASS] ORDER BY 不含 p.id
[PASS] LIMIT :max_pages
[PASS] :needles 用 CAST(... AS text[])
[PASS] 未用 string_to_array / 字符串拼接
[PASS] 四闸门 G1~G4 + is_latest 逐字内联
[PASS] c.document_id = p.document_id
[PASS] 口径双过滤 gm/pv
[PASS] p.scope='doc' / p.status='ready'
```

**「取同一字符串对象、不是复制副本」的强证据**：
1. 静态：`navigate.py` 中 `_WHERE_BASE` 的赋值语句行数 = **0**；唯一引用是 L260 `from app.services.retrieve import _WHERE_BASE`（在 `load_wiki_nav_page()` **函数内**延迟 import，避免模块级反向成环），L262 `sql = _WIKI_NAV_SQL.format(where_base=_WHERE_BASE)`。
2. 运行时哨兵：monkeypatch `retrieve._WHERE_BASE = "SENTINEL_WHERE_BASE_P3"` 后调用 `load_wiki_nav_page`，打桩 session 捕获的导航 SQL **含该哨兵** → 证明取的是模块当前对象，不是文件内副本。

```
E4 monkeypatch retrieve._WHERE_BASE 后，导航 SQL 是否含哨兵: True
```

3. `LIMIT :max_pages` 绑定值：L268 `"max_pages": decisions.WIKI_NAV_MAX_PAGES`，`decisions.WIKI_NAV_MAX_PAGES = 1`；打桩实测 `params['max_pages'] = 1`。`:needles` 实测为 Python `list[str]`（`['访问','令牌']`），非拼接字符串。

**结论：D 通过（零差异）。**

---

## E. `statement_timeout` 三段式（打桩记录 SQL 执行顺序）

**方法**：自写 `FakeSession`，记录每条 `scalar` / `execute` 的 SQL 与参数，可注入三类失败。真实 `await nav.load_wiki_nav_page(...)`。

### E1 成功路径，**调用前值非 0**（`previous='1234ms'`）

```
--- E1 成功路径 previous='1234ms' · SQL 执行顺序（共 4 条）---
  1. [scalar] SELECT current_setting('statement_timeout')
  2. [execute] SELECT set_config('statement_timeout', :ms, true)
       params={'ms': '50'}
  3. [execute] WITH hit AS ( SELECT p.document_id, p.key_terms, ... )
       params={'tenant_id': 'T', 'clearance': 40, 'authorized_kb_ids': ['KB'],
               'user_subjects': ['dept:it'], 'needles': ['访问', '令牌'],
               'gm': 'gm-x', 'pv': 'wprompt-v1', 'max_pages': 1}
  4. [execute] SELECT set_config('statement_timeout', :v, true)
       params={'v': '1234ms'}
  最后一条 SQL 是还原语句: True
  还原参数 v = '1234ms'  （调用前值 = '1234ms'）
  还原到调用前值而非硬编码 '0': True
  设置语句是事务级 set_config(..., true): True
  返回 degraded_reason = 'no_match'（no_match 是正常态）

  E1b previous='0' 时还原参数 v = '0'
```

### E2 失败路径：导航 SQL 抛 `asyncpg.exceptions.QueryCanceledError`

```
  1. [scalar] SELECT current_setting('statement_timeout')
  2. [execute] SELECT set_config('statement_timeout', :ms, true)   {'ms': '50'}
  3. [execute] WITH hit AS (...)
  4. [rollback] <rollback>
  rollback 被调用: True（位置 3）
  rollback 之后的执行条数 = 0（应为 0）
  rollback 之后是否还有还原 SQL: False
  全程是否出现过还原 SQL: False
  返回 degraded_reason = 'timeout'（应为 'timeout'）
  未把异常抛回主链路: True（函数正常返回 WikiNavPage）
```

### E3 还原自身失败也 fail-open

```
  1. [scalar] ...  2. [execute] set :ms  3. [execute] 导航 SELECT
  4. [execute] SELECT set_config('statement_timeout', :v, true)  {'v': '1234ms'}
  5. [rollback] <rollback>
  返回 degraded_reason = 'error'（应为 'error'）
  已 rollback: True
  未抛异常（fail-open）: True（函数正常返回 WikiNavPage）
```

### E4 位置纪律与事务级语义（注释 + 实现双成立）

```
E6 源码行号: 导航查询调用 L268 / 图召回调用 L552
    导航在图召回之前: True
    navigate.py L19-20: `set_config(..., is_local=true)` 的作用域是**整个事务**...
    navigate.py L243: ★ 位置特殊性：导航查询在三路关键词召回**之前**，比图召回更靠前。
```

`retrieve/__init__.py` L250-254 注释亦写明「导航查页在 ② 关键词召回之前」，`if wiki_on and ilike_terms` 内调用 `load_wiki_nav_page`，而图召回在 L544 `if get_settings().graph_enabled`（L1 闸门之后）。`navigate.py` 用 `set_config(..., true)` 而非 `SET LOCAL`（注释 L273-274 说明 SET 不接受绑定参数）。

**交叉证据**：实现者自带的 `tests/wiki/test_nav_side_channel.py::test_success_restores_previous_timeout_as_last_sql` 用 `current_timeout="30s"`，断言 `session.params[-1] == {"v": "30s"}` 且 `len(session.sql) == 3`；与我的独立打桩结论一致。

**结论：E 通过。**

---

## F. 段 C「只记录」纪律

### F1 三个 `wiki_nav_*` 字段不进方向表、不进判定

`scripts/run_eval.py::_compare_baseline` L275：

```python
directions = (("refusal_rate", 1), ("hit_rate", 1), ("miss_rate", -1))
```

运行时真实调用（基线 `wiki_nav_hit_cases=0` → 本次 `30`）：

```
_compare_baseline 返回 = 0；输出中 '基线对比' 次数 = 3
输出含 'wiki_nav': False
```

`EvalReport`（`eval_service.py` L122-124）三字段默认 0；`_accumulate_wiki_nav_stats`（L139-153）只累加三数；`test_accumulate_wiki_nav_stats_is_record_only` 断言 `report.passed == 0 and report.failed == 0 and report.total == 0`。`eval.py` 的 `EvalReportResponse` 与审计 `detail` 也只记录（注释「不改变任何判定」）。

### F2 `_wiki_nav_gate_ok` 三态（未过 = 本轮结论无效，既非通过也非失败）

```
_wiki_nav_gate_ok(0,0)   = False
_wiki_nav_gate_ok(19,1)  = False
_wiki_nav_gate_ok(20,0)  = False
_wiki_nav_gate_ok(20,1)  = True
WIKI_NAV_MIN_HIT_CASES   = 20   （N3 先验值）
```

`_print_wiki_nav_observation` 三态实测（捕获 stdout）：

```
三态[关闭态 WIKI_ENABLED=False]: verdict=不覆盖; 打印三数=True
三态[开启态 门槛已过]:           verdict=门槛已满足; 打印三数=True
三态[开启态 门槛未过]:           verdict=结论无效; 打印三数=True
    含「既不是通过也不是失败」: True
⚠ 假绿防线未过: hit_cases=0 (需 >= 20) 或 expanded_cases=0 (需 > 0)
  → **本轮评测结论无效**，既不是通过也不是失败；禁止宣称「不劣化通过」。
```

**门槛只在 `WIKI_ENABLED=true` 生效**：`_print_wiki_nav_observation` 的 `if not wiki_enabled:` 分支直接打印「本报告**不覆盖**第 4 步效果」并跳过 `_wiki_nav_gate_ok`；`elif not _wiki_nav_gate_ok(...)` 才判门槛。聚合口径 `_accumulate_wiki_nav_observation` 中 `hit_cases` / `expanded_cases` **只在可答类统计**（20 相对 111 条可答类），`expanded_terms` 全量轮按轮求和（§7-U-E），多轮下用 `> 0` 判「至少一次」。

### F3 `caliber["wiki_enabled"]` 与 `_warn_caliber_drift` 新键

`run_eval.py` L535 `caliber["wiki_enabled"] = bool(settings.wiki_enabled)`；L308-312 `_warn_caliber_drift` 比对三键含 `("wiki_enabled", "wiki 能力开关")`。运行时：

```
_warn_caliber_drift 报警含 'wiki 能力开关': True
```

### F4 `CROSS_PATH_NOTE` 双口径禁令完整、admin 门禁与 run_eval 共用同一份

`eval_service.py` L50-54 定义公开常量（160 字符），L65-74 的 `_NOTE_CHUNKS_ONLY` / `_NOTE_WITH_RECALL` 均以 `+ CROSS_PATH_NOTE` 拼接；`run_eval.py` L69-74 `from app.services.eval_service import CROSS_PATH_NOTE`，L480 打印、L529 写入 `caliber["context_note"]`。运行时：

```
CROSS_PATH_NOTE 长度 = 160；run_eval.py import 同一常量: True
禁令文本 = 两条评测链路的口径不同，结果禁止横向比较：admin 门禁（本服务 EvalService）
单趟 retrieve()，是否注入补充片由 with_recall_chunks 决定；scripts/run_eval.py 走
/api/chat/ask 生产链路，retrieve_chain() + 四类上下文全注入。
```

### F5 `caliber["wiki_nav"]["degraded_reasons"]` 留空有如实说明

`run_eval.py` L540-545 写入 `{"hit_cases", "expanded_cases", "expanded_terms", "degraded_reasons": {}}`；L537-539 注释：

> ★ `degraded_reasons` 刻意留空：设计 §2.4 明确**不新增** `RetrievalResult` 顶层降级字段，逐请求原因只走 `rag.wiki.nav.degraded` 聚合指标；SSE 载荷里没有可读的逐轮原因，这里若填数字就是伪造证据。

`_print_wiki_nav_observation` 亦打印「降级原因: 按设计 §2.4 只进聚合指标…caliber 里该键留空，不伪造」。**未伪造逐轮原因，合格。**

### F6 遗留问题（P2-2）

`run_eval.py` L561 `passed=(failed == 0)` 与 L568 返回 `failed`：**门槛未过时「本轮评测结论无效」只体现在 stdout，未传导到 `eval_runs.passed` / 进程退出码**。若下游只读 `passed` 字段（不读 stdout），会把「扩展一次都没触发的无效轮」当成「通过轮」。设计 §2.6 ②-b 要求「结论无效，既不是通过也不是失败」，落库字段仍是二态。**只报不改**（建议见问题清单）。

**结论：F 主体通过，另记 P2-2。**

---

## G. N5：全仓确认没有 `WIKI_NAV_ENABLED` 标识符

自写脚本 `rglob` 全仓（排除 `.git` / `node_modules`）：

```
全仓命中 = 3 处；code/ 目录命中 = 0 处
  02-design\架构设计文档.md:355  | `WIKI_NAV_ENABLED`（导航用途的第二个开关） | 同上，且 wiki 无数据时导航是自然 no-op。 |
  02-design\第4步-wiki导航增量设计.md:684  | **N5** `[待裁决]` | 是否新增 `WIKI_NAV_ENABLED` 独立开关… | **不新增**…
  .wiki-creator\raw\2026-11-03-架构设计文档.md:350  | `WIKI_NAV_ENABLED`（导航用途的第二个开关） | 同上…
```

**区分说明**：3 处均为**文档里的「刻意不设 / 否决项」措辞**——`架构设计文档.md` §7.1 表格标题是「**刻意不设的配置项（并说明为什么）**」，`第4步-wiki导航增量设计.md` §5 N5 裁决结论是「**不新增**」；`.wiki-creator/raw/` 是知识库归档副本。**代码目录 `code/` 零命中**，无新增开关，N5 满足。

---

## H. 测试实跑（精确数字，不写「全绿」）

> **补充轮口径**：H1~H5、H7~H9 是**宿主 Windows 环境**（缺 `python-jose` / `pypdf`）的执行结果；H6、H10 是**容器内完整依赖环境 + 真库**的执行结果。两者不可混算，也不得用 H10 反推「宿主全仓已跑通」。

### H1 全仓 pytest（宿主）—— **未跑通**

```
COMMAND: python -m pytest -q
CWD: ...\code\apps\api
EXIT_CODE=2
!!!!!!!!!!!!!!!!!! Interrupted: 26 errors during collection !!!!!!!!!!!!!!!!!!!
```

26 个收集错误全部是 `ModuleNotFoundError`（`jose` / `pypdf` 等本机未安装依赖），**0 个用例被执行**。错误清单含 `tests/test_health.py`、`tests/wiki/test_eval_wiki_nav_observation.py` 等 26 个文件。

### H2 三套件（含不可收集文件）—— **未跑通**

```
COMMAND: python -m pytest -q tests/wiki tests/retrieve tests/services
EXIT_CODE=2
!!!!!!!!!!!!!!!!!!! Interrupted: 5 errors during collection !!!!!!!!!!!!!!!!!!!
ERROR tests/wiki/test_eval_wiki_nav_observation.py
ERROR tests/wiki/test_page_endpoints.py
ERROR tests/wiki/test_page_endpoints_acl_integration.py
ERROR tests/wiki/test_worker_and_script.py
ERROR tests/services/test_ocr_client.py
```

### H3 三套件（排除上述 5 文件）—— **通过**

```
COMMAND: python -m pytest -q --junitxml=... tests/wiki tests/retrieve tests/services \
         --ignore=tests/wiki/test_eval_wiki_nav_observation.py \
         --ignore=tests/wiki/test_page_endpoints.py \
         --ignore=tests/wiki/test_page_endpoints_acl_integration.py \
         --ignore=tests/wiki/test_worker_and_script.py \
         --ignore=tests/services/test_ocr_client.py
EXIT_CODE=0
............sssssssss................................................... [ 19%]
........................................................................ [ 39%]
........................................................................ [ 59%]
........................................................................ [ 79%]
........................................................................ [ 99%]
...                                                                      [100%]
junit: tests=363 failures=0 errors=0 skipped=9 passed=354
```

### H4 本批次 8 个核心测试文件专项集 —— **通过**

```
COMMAND: python -m pytest -q --junitxml=... \
  tests/retrieve/test_wiki_nav_off_state.py tests/retrieve/test_golden_wiki_nav.py \
  tests/retrieve/test_fts_alias_cap.py tests/wiki/test_nav_sql_contract.py \
  tests/wiki/test_nav_side_channel.py tests/wiki/test_nav_constants.py \
  tests/wiki/test_nav_select_terms.py tests/wiki/test_nav_acl.py
EXIT_CODE=0
core: tests=69 failures=0 errors=0 skipped=9 passed=60
```

### H5 段 C 新增测试（P2-3：本机缺 `python-jose`，需注入最小 stub 才能跑）

`tests/wiki/test_eval_wiki_nav_observation.py` 在原生环境是 H1/H2 的收集错误之一。为验证其**逻辑**，在系统临时目录写 `sitecustomize.py` 注入最小 `jose` stub（`JWTError` + `jwt.encode/decode`），通过 `PYTHONPATH` 仅对该 pytest 子进程生效，**未改任何项目文件**：

```
⚠ 如实标注：本机缺 python-jose，该测试原本在收集阶段 ERROR；
  此处用最小 stub 绕过依赖以验证其逻辑，未改任何项目文件。
COMMAND: python -m pytest -q -ra --junitxml=... tests/wiki/test_eval_wiki_nav_observation.py
exit=0
junit: tests=21 failures=0 errors=0 skipped=0 passed=21
.....................                                                    [100%]
```

### H6 真库探针 —— 补充轮**已真跑**（原「未跑 / 显式 skip」口径作废）

**宿主侧（首轮）**：缺 `WIKI_NAV_ACL_DB_URL` 时

```
COMMAND: python -m pytest -q -rs tests/wiki/test_nav_acl.py
exit=0
sssssssss.                                                               [100%]
9 skipped / 1 passed（8 个 ACL 参数化 + 1 个 statement_timeout；纯函数 1 passed）
```

这是**预期行为**（缺显式 DB URL 才 skip）。容器内全量 pytest 同样如实列出这 9 个 skip（见 H10）。

**补充轮 · 交付文件原样实跑（容器内，注入真库 URL）**：

```
COMMAND: docker compose -f code/docker-compose.yml build api
BUILD_EXIT=0
COMMAND: docker compose -f code/docker-compose.yml run --rm --no-deps -T \
  -e WIKI_NAV_ACL_DB_URL=postgresql+asyncpg://user:pass@postgres:5432/kagent \
  api sh -c "pip install -q pytest pytest-asyncio && python -m pytest tests/wiki/test_nav_acl.py -q -rs --tb=short"
FFFFFFFFF.                                                               [100%]
9 failed, 1 passed
PYTEST_EXIT=1
```

9 个真库用例（8 ACL + 1 `statement_timeout`）**全部 FAILED**，唯一 PASSED 是不依赖 DB 的纯函数用例。首条失败原文：

```
E   asyncpg.exceptions.NotNullViolationError: null value in column "embedding_model"
    of relation "chunks" violates not-null constraint
E   [SQL: INSERT INTO chunks (id, tenant_id, kb_id, document_id, chunk_index, content,
    token_count, level_rank, acl_tags, deny_subjects, is_latest) VALUES (...)]
tests/wiki/test_nav_acl.py:203  in _real_db   await _seed(eng)
tests/wiki/test_nav_acl.py:131  in _seed     await conn.execute(...)
```

9 条失败的堆栈逐字相同（`ok / cross_kb / high_level / deny / acl_mismatch / stale / failed / caliber / statement_timeout`），全部在 `L131` 的 chunks INSERT。**首轮 I3 的静态 DDL 推断被真库实测坐实 → P1-1**。镜像内 4 个文件 sha256 与工作区逐字节一致（见 I1.2），证明跑的确实是段 C 交付代码，不是陈旧镜像。

**补充轮 · 独立诊断（补足 3 列后）**：交付文件自带的 10 个用例全部 PASS，含四类越权真库语义、`statement_timeout` 真库还原（非零前值）、`_cleanup` / `_verify_cleanup` 在有真实合成数据时逐表 count=0 —— 详见 I1.4。**注意：这是诊断性证据，不是交付文件已转绿；交付文件仍 FAIL。**

### H7 ruff / compileall

```
[python -m ruff check .] exit=0
All checks passed!
[python -m compileall -q app scripts tests] exit=0
(no output)
```

### H8 `tests/test_health.py` 遗留失败是否本批次引入

**宿主未复现、容器内已复现**：宿主侧它在收集阶段就被更早的 `ModuleNotFoundError: No module named 'jose'` 挡住（`app.main` → `app.api.auth` → `app.api.deps` → `app.services.auth` → `jose`），根本没有进入 TestClient/lifespan；补充轮在容器内（依赖齐全）跑全量时，`tests/test_health.py::test_openapi_contains_health` **确实失败**，错误为 `redis.exceptions.ConnectionError: Error -2 connecting to redis:6379. Name or service not known`（`socket.gaierror: [Errno -2] Name or service not known`）——即 team-lead 所说的 lifespan 名称解析 / DNS 遗留失败，根因是 `code-redis-1` 处于 Exited(255)。**属环境依赖缺失，非本批次引入**（改动清单比对见下）。

比对提交链改动文件（`git diff --name-only aa6551c HEAD`，共 26 个文件）：

```
02-design/第4步-wiki导航增量设计.md
code/apps/api/app/api/eval.py
code/apps/api/app/config/capabilities.py
code/apps/api/app/config/config_profile.py
code/apps/api/app/config/decisions.py
code/apps/api/app/config/self_check_ext.py
code/apps/api/app/config/settings.py
code/apps/api/app/services/eval_service.py
code/apps/api/app/services/keyword_text.py
code/apps/api/app/services/retrieve/__init__.py
code/apps/api/app/services/wiki/navigate.py
code/apps/api/scripts/run_eval.py
code/apps/api/tests/retrieve/golden_wiki_nav_baseline.json
code/apps/api/tests/retrieve/test_fts_alias_cap.py
code/apps/api/tests/retrieve/test_golden_wiki_nav.py
code/apps/api/tests/retrieve/test_governance_weight.py
code/apps/api/tests/retrieve/test_graph_side_channel.py
code/apps/api/tests/retrieve/test_section_recall.py
code/apps/api/tests/retrieve/test_table_recall.py
code/apps/api/tests/retrieve/test_wiki_nav_off_state.py
code/apps/api/tests/wiki/test_eval_wiki_nav_observation.py
code/apps/api/tests/wiki/test_nav_acl.py
code/apps/api/tests/wiki/test_nav_constants.py
code/apps/api/tests/wiki/test_nav_select_terms.py
code/apps/api/tests/wiki/test_nav_side_channel.py
code/apps/api/tests/wiki/test_nav_sql_contract.py
code/docs/DEPLOY.md
```

**`tests/test_health.py` / `app/services/auth.py` / `app/main.py` / `pyproject.toml` 均不在改动清单内 → 依赖缺失与任何遗留失败都与本批次无关。**

### H9 N2 配置四处同步（顺带核实）

| 同步点 | 证据 |
|---|---|
| `settings.py` | L195 `wiki_nav_timeout_ms: int = 50` |
| `self_check_ext.py` | L117 `if not (10 <= settings.wiki_nav_timeout_ms <= 200):` |
| `config_profile.py` | L77 白名单 `"wiki_nav_timeout_ms"`；L143 `runtime_config_snapshot` 同键 |
| `capabilities.py` | L252 `"nav_timeout_ms": settings.wiki_nav_timeout_ms` |
| `DEPLOY.md §3.5` | L204 变更后果矩阵新增「调 `WIKI_NAV_TIMEOUT_MS`（默认 50ms，取值 [10,200] 由启动自检卡住）」 |

N3 先验常量（`decisions.py`）：`WIKI_NAV_MAX_TERMS=2` / `MAX_FTS_TOKENS=6` / `MAX_PAGES=1` / `MIN_CJK_CHARS=2` / `MIN_ASCII_CHARS=4` / `MAX_TERM_CHARS=12` / `CANDIDATE_PAGE_GUARD=5000` / `LEXICON_FTS_ALIAS_MAX_TERMS=6` / `FTS_QUERY_MAX_TERMS=24` / `ILIKE_MAX_TERMS=3` —— 与设计默认值逐项一致，注释明确「先验值，MUST NOT 因评测不达标临时调参」。

### H10 容器内全量 pytest（补充轮新增，精确数字）

```
COMMAND: docker compose -f code/docker-compose.yml run --rm --no-deps -T \
  -v <temp>/p3b_junit:/tmp/p3junit \
  api sh -c "pip install -q pytest pytest-asyncio && python -m pytest -q -rs --tb=line --junitxml=/tmp/p3junit/full.xml"
........................................................................ [  7%] ... [100%]
............................F........................................... [ 91%]
.sssssssss.........................................................s.... [ 99%]
FULL_PYTEST_EXIT=1
junit: tests=941 failures=1 errors=0 skipped=11 passed=929
```

| 类别 | 数量 | 说明 |
|---|---|---|
| passed | **929** | 含 `tests/wiki/test_eval_wiki_nav_observation.py` 的 21 例**原生可跑、无需 jose stub**（P2-3 的「宿主原生 0」在容器侧补齐） |
| failed | **1** | `tests.test_health::test_openapi_contains_health` — `redis.exceptions.ConnectionError: Error -2 connecting to redis:6379. Name or service not known`（H8 复现；`code-redis-1` Exited(255)，环境依赖） |
| errors | **0** | 容器镜像 `requirements.lock` 含 `python-jose==3.5.0` / `pypdf==5.9.0` 等全部生产依赖 → **宿主那 26 个收集错误在容器内不复现** |
| skipped | **11** | 1 `tests/infra/test_redis_client`（需真实 redis）+ **9 `tests/wiki/test_nav_acl.py`**（缺 `WIKI_NAV_ACL_DB_URL`）+ 1 `tests/wiki/test_page_endpoints_acl_integration.py`（缺 `WIKI_ACL_DB_URL`） |

**未写「全绿」**：容器全量 `exit=1`、失败 1 例；该失败与本批次无关（H8 改动清单比对）。宿主 H1/H2 的 `exit=2` 仍是本机依赖缺失，两种环境不可混算。

**未污染真库的控制**：全量跑**故意不设** `WIKI_NAV_ACL_DB_URL` / `WIKI_ACL_DB_URL`。全 tests 目录 Grep `DATABASE_URL|create_async_engine|WIKI_NAV_ACL_DB_URL` 只命中 2 个连库文件，且都由显式 env 门禁；跑完独立只读 SQL 复核 `eval_cases=217` / `chunks=580` / `wiki_pages=1` 与探针前一致（I1.5）。

---

## I. 真库边界（补充轮重写：分层如实表述，PGlite 与真库严格分开）

> **前提更正**：首轮报告「Docker daemon 未启动、无 PostgreSQL 实例」在本轮已过时。本轮亲自复核：`docker info --format '{{.ServerVersion}}'` = `29.4.0`、`code-postgres-1` Up (healthy)、`pg_isready` = `accepting connections`。但 **PG 无宿主端口映射**（`docker port` 输出空、`127.0.0.1:5432` CLOSED），只在 `code_default` 网络内（alias `postgres`），故所有真库操作都在 `docker compose run` 容器内完成，宿主直连必然 skip。**PGlite 与真库是两套口径，分开写、绝不合流。**

### I1 已实测（真库：容器内执行 + psql 只读复核）

#### I1.1 环境、迁移状态与基线计数

```
$ docker exec code-postgres-1 psql -U user -d kagent -t -A -c "SELECT version_num FROM alembic_version"
0026

$ ... -c "SELECT 'eval_cases',count(*) FROM eval_cases UNION ALL SELECT 'documents',count(*) FROM documents
          UNION ALL SELECT 'chunks',count(*) FROM chunks UNION ALL SELECT 'wiki_pages',count(*) FROM wiki_pages
          UNION ALL SELECT 'tenants',count(*) FROM tenants"
eval_cases|217
documents|26
chunks|580
wiki_pages|1
tenants|1

$ ... -c "SELECT status,count(*) FROM wiki_pages GROUP BY status ORDER BY status"
ready|1

$ ... -c "SELECT count(*) FROM tenants WHERE code LIKE 'navacl-%'"      （探针前基线）
0
```

**已实测结论**：库在 `0026`（26 迁移的**终态**，不是本轮从零跑出来的）；217 条 `eval_cases` 齐全；真实 wiki 页仅 **1 页 ready**（远不足以让 `hit_cases >= 20` 自然成立）。

#### I1.2 镜像吃到段 C 当前代码（防止「跑的是陈旧镜像」）

```
$ docker compose -f code/docker-compose.yml build api        → BUILD_EXIT=0
$ docker run --rm --entrypoint sh code-api:latest -c "sha256sum /workspace/apps/api/... "
e223ac92c139a168573ca3aabe5b0358197669589692dae49f9d22150b1e9110  tests/wiki/test_nav_acl.py
a7695b1d1c6415ed70c02ed7d33770af2f0313d5c40ac7dca1db8bd2fb7f29aa  tests/wiki/test_eval_wiki_nav_observation.py
07b774d02ab0f307f6998958a9e4f8fba1a4977dc2b49d62842d2ace8d8fa5c9  app/services/keyword_text.py
335bf09aab3a9463c635a3946857dc38a972fc0c004f5949dcfd7bbd67420b99  app/services/wiki/navigate.py
```

四个 hash 与主机工作区 `Get-FileHash` 逐字节一致。重建前旧镜像中 `test_nav_acl.py` / `test_eval_wiki_nav_observation.py` / `navigate.py` 均为 `MISSING`、`keyword_text.py` = `1f6ebac7…` → 证明 rebuild 必要且生效，下面跑的确是段 C 交付代码。

#### I1.3 交付探针原样实跑 → P1-1 真库复现（9 failed / 1 passed）〔修复前，历史证据〕

见 H6 完整命令与堆栈。摘录：

```
FFFFFFFFF.                                                               [100%]
9 failed, 1 passed      PYTEST_EXIT=1
E   asyncpg.exceptions.NotNullViolationError: null value in column "embedding_model"
    of relation "chunks" violates not-null constraint
tests/wiki/test_nav_acl.py:203  in _real_db   await _seed(eng)
tests/wiki/test_nav_acl.py:131  in _seed     await conn.execute(...)
```

9 例（8 ACL + 1 statement_timeout）全部在 `_seed` L131 失败；纯函数 1 passed。**这不是「未跑」，是真的跑了、而且是红的。**

#### I1.4 独立诊断：补 3 列后 10/10 PASS（真库语义级）★含合成页

不改任何实现文件：自写临时脚本用 `importlib` 载入交付的 `test_nav_acl.py`，**仅替换模块全局 `_seed`** 为「与交付逐字相同、chunks INSERT 多 3 列」的版本，然后直接 `await` 交付文件自带的测试函数；`_real_db` 的 `finally` 仍执行交付文件自带的 `_cleanup` / `_verify_cleanup`。

```
$ docker compose -f ... run --rm --no-deps -T -v <temp>/p3b_probe.py:/tmp/p3b_probe.py:ro \
    -e WIKI_NAV_ACL_DB_URL=postgresql+asyncpg://user:pass@postgres:5432/kagent \
    api sh -c "pip install -q pytest pytest-asyncio && python /tmp/p3b_probe.py"
DIAG_EXIT=0

=== (1) 交付文件原样 _seed 在真库的真实结果 ===
ORIGINAL_SEED = FAIL  type = IntegrityError
ERROR = (asyncpg.NotNullViolationError) null value in column "embedding_model" ... violates not-null constraint
原样 seed 失败后，交付文件 _cleanup/_verify_cleanup 仍通过（事务回滚，无残留）

=== (2) 补足 3 列后，跑交付文件自带的用例（真库语义级） ===
  ACL[ok           ] expect_hit=True  -> PASS
  ACL[cross_kb     ] expect_hit=False -> PASS
  ACL[high_level   ] expect_hit=False -> PASS
  ACL[deny         ] expect_hit=False -> PASS
  ACL[acl_mismatch ] expect_hit=False -> PASS
  ACL[stale        ] expect_hit=False -> PASS
  ACL[failed       ] expect_hit=False -> PASS
  ACL[caliber      ] expect_hit=False -> PASS
  statement_timeout(默认 previous=0) -> PASS
  pure_function(选词去重) -> PASS
交付用例 PASS 数 = 10 / 10
```

**★ 本次验证含合成页**：独立租户 + 2 kb + 8 文档 / 8 chunks / 8 wiki_pages（N4 允许：单独 kb / 事务内 / 跑完清理 / 报告标注）。这些合成页**不得混算进真实页覆盖率**；真库真实页仍只有 1 页 `ready`。设计 §2.6 ②-b 的 `hit_cases >= 20` 门槛并未因此「真实触发」，本轮仍**未跑 217 条 A/B**。

`_cleanup` / `_verify_cleanup` 在 10 次用例的 `finally` 中全部执行且未抛异常 → **逐表 count=0 是在「库里真有合成数据」的情况下通过的**，不是首轮那种「seed 事务回滚后空库」的空转。

#### I1.5 独立只读 SQL 复核清理 + 真库未污染

不依赖交付文件断言，另用 `docker exec code-postgres-1 psql` 只读查询：

```
（诊断脚本内独立 SQL，跑完合成数据后）
wiki_pages      WHERE tenant_id=本次合成租户 -> 0
chunks          WHERE tenant_id=本次合成租户 -> 0
documents       WHERE tenant_id=本次合成租户 -> 0
knowledge_bases WHERE tenant_id=本次合成租户 -> 0
tenants WHERE code LIKE 'navacl-%' -> 0
tenants WHERE id = 本次合成租户     -> 0
全库总量: wiki_pages=1 / chunks=580 / documents=26 / tenants=1

（全部 docker 操作结束后，宿主再次独立只读复核）
SELECT count(*) FROM tenants WHERE code LIKE 'navacl-%'                        -> 0
SELECT (SELECT count(*) FROM wiki_pages WHERE title LIKE '合成页-%'),
       (SELECT count(*) FROM chunks WHERE content='nav-acl-probe-chunk'),
       (SELECT count(*) FROM documents WHERE filename LIKE 'nav-acl-%'),
       (SELECT count(*) FROM knowledge_bases WHERE name LIKE 'nav-acl-kb-%')  -> 0|0|0|0
alembic_version|0026 / eval_cases|217 / documents|26 / chunks|580 / wiki_pages|1 / tenants|1
wiki_pages status: ready|1
```

「清理逻辑本身有洞」的风险已排除；**合成数据零残留、真实数据零改动**。

#### I1.6 DDL / INSERT 列相容性（真库 `information_schema` 实测，非读迁移脚本推断）

```
SELECT table_name,column_name,is_nullable,coalesce(column_default,'<NO_DEFAULT>')
FROM information_schema.columns
WHERE table_schema='public'
  AND table_name IN ('chunks','wiki_pages','documents','knowledge_bases','tenants')
  AND is_nullable='NO' ORDER BY table_name,ordinal_position
```

关键行（原始输出节选）：

```
chunks|embedding_model|NO|<NO_DEFAULT>
chunks|embedding_text_version|NO|<NO_DEFAULT>
chunks|chunker_version|NO|<NO_DEFAULT>
chunks|governance_weight|NO|0
chunks|is_latest|NO|true
wiki_pages|scope|NO|'doc'::text
wiki_pages|sections|NO|'[]'::jsonb
wiki_pages|source_chunk_ids|NO|'{}'::uuid[]
documents|graph_status|NO|'pending'::text
knowledge_bases|visibility|NO|'restricted'::text
tenants|status|NO|'active'::text
```

`_seed` 唯一不相容表仍是 `chunks` 的 3 列；其余「缺列」均有默认值 → 相容（与首轮静态 AST 判定一致，但这次是**真库元数据实测**）。另用 `pg_indexes` 只读核对 `wiki_pages` / `chunks`：`idx_wiki_pages_kb` / `uq_wiki_pages_document` / `wiki_pages_pkey` / `idx_chunk_filter` 等确实存在 —— **索引「存在」不等于 EXPLAIN 证明它参与过滤**，见 I2 第 3 项。

#### I1.7 `statement_timeout` 真库还原（调用前值非 0）

```
=== (3) 真库 statement_timeout：调用前值非 0（'1234ms'） ===
  before = 1234ms  after = 1234ms  document_id = eff96e58-0446-4eef-ba35-31e38c04d033
  -> PASS（非零前值还原到原值，未硬编码 0）
```

与 E 节打桩（FakeSession 记录 4 条 SQL 顺序、`previous='1234ms'`）互为交叉证据：真库 `set_config(..., true)` 事务级语义成立、导航查询返回基线命中页、超时预算未泄漏到后续 SQL。

### I2 仍未跑（如实列出；「不劣化」结论未验证）

| # | 未跑项 | 状态 / 原因 |
|---|---|---|
| 1 | `alembic upgrade head` **26 迁移从零全链** | **未跑**。库已是 `0026`（终态在库实测），但本轮没有从空库重放 26 个迁移；迁移链可重放性仍无本轮证据 |
| 2 | **217 条 A/B 评测**（`run_eval.py` 生产链路 + admin 门禁交叉验证） | **未跑**。team-lead 明确「不要你跑」：需 api + embedding/reranker + LLM，约 78 分钟/轮 × 开/关 wiki 两轮，由 team-lead 在 P3 结论后单独向用户确认 |
| 3 | 导航 SQL 的 **EXPLAIN**（证明 `idx_wiki_pages_kb` 参与过滤、非 Seq Scan） | **未跑**。真库 `wiki_pages` 仅 1 页，计划器必然走 Seq Scan，此时 EXPLAIN 无统计意义；须补齐真实页后再跑 |
| 4 | **压测 P95 对照**（`rag.retrieve.wiki_nav` p95 ≤ 10ms、端到端 P95 增幅 ≤ 1%） | **未跑**（需完整服务栈 + 真实流量） |
| 5 | 假绿门槛 `hit_cases ≥ 20 且 expanded_cases > 0` 的**真实触发** | **未跑**。依赖 #2；本轮合成页只用于诊断，不计入业务结论 |
| 6 | 全服务 `docker compose up` 启动验证 | **未跑**。api / web / redis / minio / embedding / reranker 仍 Exited(255)；本轮只起了 PG 单容器 + `run --no-deps` 临时容器 |

> **「不劣化」结论仍未验证。** 本轮通过项覆盖「代码路径 / 纯函数 / 打桩 SQL 顺序 / 静态契约 / **真库单请求级 ACL 与超时语义**」五层；但行为层的 217 条四项指标、性能预算、全链迁移可重放性一律没有证据，**不得推断成通过**。
>
> **状态更新（§0.7，最终口径）**：上文「交付文件仍是 9 FAILED」是**补充轮（修复前）**口径，现已作废 —— P1-1 已修复，交付文件在最终形态 `3c5d209` 上真库 `11 passed / 0 failed / 0 skipped / exit=0`（§0.7.3）。但**「真库探针转绿」不等于「不劣化通过」**：效果层六项仍未跑，本节结论（不劣化未验证）不变。9 条真库探针须在注入 `WIKI_NAV_ACL_DB_URL` 的容器内跑；宿主现在另有钉子 + 纯函数 2 例可跑（§0.7.2），但 skip 的 9 例仍不能由宿主「全绿」代替。

### I3 PGlite 口径隔离（不得写成真库全链通过）

上一批次报告 `04-test/测试报告-清障批次.md` 记录过 PGlite（WASM PostgreSQL）验证：其 `.workbuddy/pglite-probe/server.mjs` 只建 **5 张表的最小投影 schema**，不重放 26 个迁移、不建 vector 列。**本轮未启动 PGlite 服务、未做任何 PGlite 语义验证**；I1 的真库语义验证全部在容器内真实 PG 16 上完成，两者口径严格分开，本报告不含任何「PGlite = 真库全链通过」的结论。

只读核对 `server.mjs` L49-65 的 `chunks` 投影列：

```sql
CREATE TABLE chunks (
  id, tenant_id, kb_id, document_id, chunk_index, content, token_count,
  heading_path, page_no, level_rank, acl_tags, deny_subjects, is_latest,
  fts_text, created_at
);
```

**该投影没有 `embedding_model` / `embedding_text_version` / `chunker_version` 三列** —— 这正是 P1-1 在此前 PGlite 集成测试中被掩盖的原因；本轮用跑完 26 迁移的真库**已把它暴露出来**（I1.3）。

### I4 P1-1 根因与「唯一阻塞」证明

`_seed` 的 5 条 INSERT 列（首轮 AST 提取，补充轮真库 DDL 复核一致）：

```
INSERT INTO tenants (id, name, code, status)
INSERT INTO knowledge_bases (id, tenant_id, name)
INSERT INTO documents (id, tenant_id, kb_id, doc_group_id, filename, ext, size_bytes, storage_key, level_rank, acl_tags, deny_subjects)
INSERT INTO chunks (id, tenant_id, kb_id, document_id, chunk_index, content, token_count, level_rank, acl_tags, deny_subjects, is_latest)
INSERT INTO wiki_pages (id, tenant_id, kb_id, document_id, title, summary, key_terms, generator_model, prompt_version, content_hash, status)
```

首轮静态相容性判定：

```
[FAIL] chunks: 缺失必填列 ['embedding_model', 'embedding_text_version', 'chunker_version']
[PASS] documents / knowledge_bases / tenants / wiki_pages: 无缺列
合计不相容表数 = 1
```

**根因（可复算）**：`alembic/versions/0011_add_chunk_index_profile.py` L29-31 以 `nullable=True` 加三列 → L33-40 `UPDATE chunks SET ...` 回填 → L42-44 `op.alter_column("chunks", ..., nullable=False)` 置为 NOT NULL，**全程未设置 `server_default`**；真库 `information_schema` 实测确认三列为 `NO | <NO_DEFAULT>`（I1.6）。

补充排除项（避免过度报告）：
- `wiki_pages` 缺 `scope` / `sections` / `source_chunk_ids`，但真库三列均有默认值（I1.6）→ 相容。
- `documents` 缺 `graph_status` / `governance_status` 等均有默认 → 相容。
- `0008` 的 `chk_chunks_level_rank CHECK (level_rank BETWEEN 0 AND 100)`：`high_level` 用例 `level_rank=50` 合法；`chunks.acl_tags CHECK (cardinality > 0)` 各用例均非空 → 相容。
- `uq_wiki_pages_document`：8 个用例各用不同 `document_id` → 相容。

**补充轮新增的「唯一阻塞」证明**（I1.4）：把 `_seed` 的 chunks INSERT 补上这 3 列后，交付文件自带的 8 个 ACL/状态/口径用例 + statement_timeout 用例 + 纯函数用例 **10/10 PASS**，且 `_cleanup` / `_verify_cleanup` 在有真实合成数据时逐表 count=0。说明 P1-1 是**唯一**阻塞项，其余 DDL / CHECK / 唯一约束 / 校准口径均无隐藏问题；修这 3 列即可让 9 个真库探针由 FAILED 转 PASS。

**影响（按实测修正）**：交付文件在真库执行时 `_seed` 第一条 chunks INSERT 即抛 `NotNullViolationError`，`_real_db` 在 `try` 之前就失败 → 9 个真库探针全部无法执行。首轮「本机无 DB 显式 skip 全绿」掩盖了它；补充轮在容器内注入 `WIKI_NAV_ACL_DB_URL` 后**真实复现为 9 FAILED / 1 PASSED / exit=1**。**以上均为修复前（86f5fc7）的实测记录**；整改后交付文件已在真库转绿（§0.5.3 / §0.6.2 / §0.7.3 最终形态 11 passed），此处保留仅作根因与掩盖机制的审计证据。

### I5 C4「217 条精确占比」—— 由「推断」升级为「实测」（单列一节）

`02-design/C4-别名静默截断影响面量化.md` §5 第 14 行把「217 条精确占比 = 0」标为**推断，不是实测**，理由是「真库 `eval_cases` 不可达、11 条题面不可得」。补充轮真库可达且 217 条齐全，本轮按该文 §2.2「真库可达时」脚本 + 附录 A 的 `stats()` / `verify()` **逐字复用**复算。

**取数（二进制安全，避免 PowerShell 中文编码损坏）**：

```
$ docker exec code-postgres-1 psql -U user -d kagent -c \
  "\copy (SELECT id, question, expected_answerable FROM eval_cases ORDER BY id) \
   TO '/tmp/p3_eval217.csv' WITH (FORMAT csv, HEADER true, ENCODING 'UTF8')"
COPY 217
$ docker cp code-postgres-1:/tmp/p3_eval217.csv <temp>/p3b_eval217.csv      （18383 bytes）
$ docker exec code-postgres-1 rm -f /tmp/p3_eval217.csv                     （容器内临时文件已删）
```

**复算**（宿主 Python 3.13，只读加载生产 `decisions.py` / `keyword_text.py`，sha256 `07b774d0…`；`CAP = d.FTS_QUERY_MAX_TERMS = 24`）：

```
CAP = 24  CJK_RUNS = 2
CSV rows = 217  columns = ['id','question','expected_answerable']
217 条 answerable 分布 = {'t': 111, 'f': 106}
eval_cases(真库 217)      n_raw=217 n_uniq=217 cap_hit=  0 (0.00%) a_hit=0 maxW=23 maxCJK=27 verify=True
cases_all.json(核心 206)  n_raw=206 n_uniq=206 cap_hit=  0 (0.00%) a_hit=0 maxW=23 maxCJK=27 verify=True
剩余容量=1 的条数 = 2 ；剩余容量<=6 的条数 = 7
206 与 217 的题面交集 = 206 ；217 中不在 206 的题面数 = 11 ；206 中不在 217 的题面数 = 0
=== 结论 ===
217 条精确占比 = 0/217 = 0.00%  【实测，真库导出 + 附录A 同一函数】
最大 W = 23；ascii_items 达 24 = 0；verify(all) = True
```

**方法一致性校验**：同一脚本对仓库 206 核心样本复现了 C4 §2.3 的 `0/206`、`maxW=23`、`verify=True`，证明复算用的 `W()` 与生产 `build_fts_query` 内部状态一致，不是另写近似逻辑。注：`keyword_text.py` 已是段 A 修复后的文件，但 B 项 Golden 已证明「extra / alias 为空时逐字节等价」，故附录 A 的 `verify()` 仍适用。

**结论**：C4 §5 第 14 行与 §6-U1「精确 217 条 query 清单不可得」**可以关闭** —— 11 条题面已取回（例如 `m1001 主板组件在上海仓有多少库存？`、`xyz1000 的销售单价是多少？`、`XYZ-9000 的销售单价是多少？`…），精确占比 **0/217 = 0.00% 为实测值**。但 **§6-U2（部分截断实际发生数）仍未知**：`alias_map` 来自 `kb_terms`，本轮未取，不得顺带宣称已解决；「剩余容量 1 的 2 条 / ≤6 的 7 条」只是触发条件，不是发生数。

**顺带发现（登记 P2-6）**：C4 §5 第 8 行「11 条 = +1 可答 / +10 不可答」是按 `111−110` / `106−96` 的算术推断，隐含假设离线 206 样本与真库标签一致。实测二者有 **9 条标签不一致**，11 条新增题面的真实分布是 **8 可答 / 3 不可答**：

```
DB 217 expected_answerable = {'t': 111, 'f': 106}
cases_all.json n = 206  keys = ['q','answerable','expect','cat']
cases_all.json answerable 原值分布 = {'True': 110, 'False': 96}
206 与 217 题面交集 = 206 ；标签不一致条数 = 9
217 中不在 206 的条数 = 11 ；其中 answerable=t 的条数 = 8 ；f 的条数 = 3
（复算：110−8+1+8 = 111 ；96+8−1+3 = 106，与真库分布自洽）
```

**结论：I = 分层实测（I1）+ 6 项未跑（I2）+ PGlite 口径隔离（I3）+ 1 个 P1 真库坐实（I4）+ C4 精确占比实测（I5）；「不劣化」结论仍未验证。**

---

## 问题清单（只报问题、不改实现）

### P0（阻断）：0 个

### P1（应该修）：0 个 —— P1-1 已修复并关闭

> 下表保留原缺陷描述供审计追溯，但**状态已由「阻断」改为「闭环」**。文末「放行建议」为最终口径。

| # | 位置 | 最终状态 | 闭环证据（真实执行） |
|---|---|---|---|
| **P1-1** | `code/apps/api/tests/wiki/test_nav_acl.py::_seed` 的 `INSERT INTO chunks (...)`（原 L131-143） | **已修复 → 已复跑确认 → 已关闭** | **原缺陷**：真库 `FFFFFFFFF.` = 9 FAILED / 1 PASSED / exit=1，逐条 `null value in column "embedding_model" of relation "chunks" violates not-null constraint`（H6 / I1.3）；根因 `0011` 三列 `NOT NULL` 且无 `server_default`，真库 `information_schema` 实测 `NO \| <NO_DEFAULT>`（I1.6）。**整改**：`3c5d209`（最终 HEAD；中间形态 `cbd7b0d` 同样含此修复）在 chunks INSERT 列清单 / VALUES / params 三处补 `embedding_model` / `embedding_text_version` / `chunker_version`，绑定 `bge-m3` / `etext-v1` / `c400-800-60`。**独立复跑**：§0.5.3 `10 passed in 1.59s / RERUN_EXIT=0`；§0.6.2 中间形态 `cbd7b0d` junit `10/0/0/0`；**§0.7.3 最终 HEAD `3c5d209` junit `tests=11 failures=0 errors=0 skipped=0`、`REALDB_EXIT=0`（镜像与工作区逐字节一致）**。**反向对照**：§0.6.3 把三列删回后真库 `9 FAILED exit=1`、宿主（无钉子形态）`sssssssss.` 全绿 exit=0；§0.7.5 最终形态下删列名则钉子立即 FAILED → 修复确实生效、缺陷可复现、护栏有效。**本报告不修改实现。** |

### P2（建议）：7 个未结（另 2 个已关闭：P2-7 已整改、P2-8 已消解）

| # | 位置 | 说明 | 建议 |
|---|---|---|---|
| **P2-1** | `code/docs/DEPLOY.md` **L147** | 能力开关表仍写「**wiki 不进检索（术语扩展属第 4 步，未实现）**」。第 4 步已交付，文案过时且会误导运维以为什么都没进检索。清障批次报告 §V8 记录「`:147` 有意保留」，那是**上一批次第 4 步未实现时**的正确表述，本批次应同步。N2 要求的「同步四处」只改了变更后果矩阵（L204），未改能力表 | 同步为「检索侧术语扩展（第 4 步）已实现，`WIKI_ENABLED=false` 即关闭态零 SQL」；勿改「开 wiki 要重建页、不用重跑解析」结论（设计 §6-C5 认定仍成立） |
| **P2-2** | `scripts/run_eval.py` L561 `passed=(failed == 0)` / L568 `return failed` | 假绿门槛未过时「**本轮评测结论无效**」只体现在 stdout，未传导到 `eval_runs.passed` 与进程退出码。下游若只读 `passed` 字段会把「扩展一次都没触发的无效轮」当成「通过轮」，与设计 §2.6 ②-b「既不是通过也不是失败」的语义不符 | 在 `caliber["wiki_nav"]` 增加 `gate_ok` / `verdict` 可读标记，或让 `passed` 反映三态；至少在 `note` 里留痕 |
| **P2-3** | `tests/wiki/test_eval_wiki_nav_observation.py`（段 C 新增） | **宿主**缺 `python-jose`，该测试在宿主全仓/三套件均为收集 `ERROR`，原生实跑数字为 0；首轮用最小 `jose` stub 注入跑通 21 passed（须始终标注「stub 注入」）。**补充轮已在容器内补齐**：全量 pytest 中该文件原生收集并通过、无需 stub（H10 的 929 passed 含其 21 例） | 以 H10 为准；宿主 21 passed 仍只作 stub 注入的诊断证据 |
| **P2-4** | 交付口径同步 | 下发称「段 C 工作区未提交态、3 改 1 新增、`tests/wiki/test_wiki_nav_off_state.py`」；实际是「已提交 `86f5fc7`、3 改 2 新增、`tests/retrieve/test_wiki_nav_off_state.py`」（详见 §0.1）。补充轮另核实 `ea1fe09`→`86f5fc7` 为 message-only amend（树 hash 相同），且 coder-3 确认第 2 个新增文件也属段 C | 以本报告 §0.1 为准更新交付台账 |
| **P2-5** | 宿主测试环境（容器侧已补齐证据） | **宿主**全仓 pytest `exit=2` / **26 errors during collection / 0 用例执行**；三套件（含 5 个不可收集文件）同样 `exit=2`，「宿主全绿」不成立。**补充轮容器内全量 = 941 / 929 passed / 1 failed / 0 errors / 11 skipped**（H10），26 个收集错误在容器内不复现；`test_health.py` 的 redis DNS 失败在容器内**已复现**且确认非本批次引入（H8） | 结论已由 H10 补齐；宿主 354/60/21 passed 仍只作「本机可跑子集」，不可与容器数字混算 |
| **P2-6** | `02-design/C4-别名静默截断影响面量化.md` §5 第 8 行 / `.workbuddy/cases_all.json` | 文档把「11 条 = +1 可答 / +10 不可答」标为算术推断，隐含假设离线 206 样本与真库标签一致。**真库实测**：二者有 **9 条 `answerable` 标签不一致**，11 条新增题面真实分布为 **8 可答 / 3 不可答**（I5）。这会影响后续任何基于离线样本与真库 `eval_cases` 的口径对齐，是 217 条 A/B 基线可比性的潜在偏差源 | 以真库 `eval_cases.expected_answerable` 为唯一口径；若要用 `cases_all.json` 复算基线，先对齐这 9 条标签。§5 第 14 行「217 条精确占比 = 0」则已由本轮升级为**实测 0/217**，可关闭（U1 解除，U2 仍未知） |
| **P2-7**（★ 已整改关闭） | `tests/wiki/test_nav_acl.py` L20 docstring / L66 skip reason | 原写「本机无 Docker daemon / 无 PostgreSQL 实例」「本机…**未跑**」，与补充轮实测环境（Docker 29.4.0 / PG healthy、探针已真跑）冲突。**仅文案，不影响行为**（skip 判据仍是 `if not _DB_URL`） | **已闭环**：§0.5.5 / §0.7.2 复核三种形态，两处均不再出现「本机无 Docker daemon / 无 PostgreSQL 实例」，改为「未显式提供 `WIKI_NAV_ACL_DB_URL` 或不在 compose 网络内时跳过」「PG 无宿主机端口映射，须在 compose 网络 `code_default` 内的容器里跑」（当前 HEAD `3c5d209` 措辞略异但语义相同）；「含合成页 / 逐表清理 + SQL 复核」纪律文字保留未动 |
| **P2-8**（★ 已消解关闭） | 中间形态 `cbd7b0d`（303 行 / 10 用例 / 无钉子），**已被 `reset` 撤销** | 曾实测：无钉子形态下把 3 列删回、复现 P1-1，宿主 / CI 无 DB 仍是 `sssssssss.`（9 skip + 1 pass）**全绿 exit=0**，只有真库容器才 `FFFFFFFFF.` 9 FAILED（§0.6.3）→ 宿主侧无护栏。**当前最终形态 `3c5d209` 已把钉子补回**：宿主 `sssssssss..`（9 skip + **2** pass，§0.7.2）、钉子变异自检「删列名留占位符 → `AssertionError: 列清单漏了必填列 embedding_model` → 还原转绿」（§0.7.5）→ 风险已消除 | **关闭，无需裁决**。保留 §0.6.3 对抗性证据作为「为什么必须有这条钉子」的实证；后续若再删钉子，须重跑该对照 |
| **P2-9**（本轮新增） | Git 提交链 / 行尾一致性 | **HEAD 在验证期间被并发改写 3 次**：`3c5d209`（含钉子）→ `cbd7b0d`（amend，删钉子）→ `reset: moving to 3c5d209`（含钉子）。验证对象反复漂移，任一中间态的测试结论都不能直接套用到下一形态，本轮为此按三种形态分别重验。另：当前工作区文件为 CRLF（17057 bytes / sha256 `e9e0c01a…`），HEAD blob 为 LF（16694 bytes / `fd741fe2…`）；`core.autocrlf=true` 使 `git diff` 为空、逻辑内容归一化后一致，**但「逐字节 hash 三方一致」不成立**（镜像与工作区一致，HEAD blob 不同） | 建议 team-lead **锁定最终交付形态**（明确 commit hash，验证 / 交付期间不再 amend / reset）；如需字节级 hash 证据，统一行尾（LF→LF 或关闭 autocrlf 后重提交）。此条为流程建议，不影响测试结论 |

---

## 放行建议（最终口径：按 `3c5d209` 复核后重写）

> **结论：P0 = 0，P1 = 0（P1-1 已修复并经最终形态真库复跑确认），P2 = 7 未结（P2-7 / P2-8 已关闭、P2-9 本轮新增）。建议「不以『第 4 步验收通过』放行」，改为「代码层有条件放行（保持 `WIKI_ENABLED=false` 默认关闭态）+ 效果层验收挂起」。** P1-1 硬阻塞**已解除**、回归护栏已补回，但 **「不劣化」仍完全未验证** → 结论不放宽。
>
> 注意：本节曾写作「P1 = 1 / 交付文件在真库上是 9 FAILED」（补充轮、修复前），后按 `cbd7b0d` 写过一版，又因 HEAD `reset` 改写为当前 `3c5d209` 口径。历史证据保留在 H6 / I1.3 / §0.6 供追溯。

**理由**：

1. **代码层证据充分，且已延伸到最终交付形态的真库单请求级**：A~G 七项全部通过——关闭态零 import/零 SQL 有 AST + 运行时双证；Golden 与 C4/F6 各经一次变异自检击杀并还原复跑、原文件 hash 与 `git diff` 零改动；导航 SQL 与设计 §2.1.3 **零差异**、12 条契约全 PASS、`_WHERE_BASE` 取同一对象有哨兵实证；三段式含 `previous='1234ms'`、失败/还原失败路径符合设计；段 C「只记录」纪律与 N5 成立。补充轮 + 增量复验轮 + 最终形态复核轮新增：镜像内文件与工作区逐字节一致（`e9e0c01a…`，§0.7.1），工作区（CRLF）与 HEAD blob（LF）归一化后一致；真库 `0026` 在库；交付探针**实跑并转绿**（§0.5.3 `10 passed in 1.59s`、§0.6.2 中间形态 `10/0/0/0`、**§0.7.3 最终形态 `3c5d209` junit `tests=11 failures=0 errors=0 skipped=0 / REALDB_EXIT=0`**）；四类越权 / 状态 / 口径语义、真库 `statement_timeout` 非零前值还原、合成页清理逐表 0；容器内全量 **930 passed**（942 / 1 failed / 0 errors / 11 skipped，唯一失败 `test_health.py` redis DNS 属环境、非本批次）；宿主探针 `9 skip + 2 pass`（钉子可跑）；钉子变异自检击杀并还原复跑（§0.7.5）。`ruff check .` / `compileall` exit=0。
2. **但效果层仍完全没有证据**：26 迁移从零全链、217 条 A/B、EXPLAIN、P95 对照、假绿门槛真实触发 **全部未跑**（I2）；「不劣化」结论 **未验证**。真库真实 wiki 页只有 **1 页 ready**，远不足以让 `hit_cases >= 20` 自然成立。
3. **P1-1 已解除、回归护栏已补回（P2-8 关闭），但存在流程风险（P2-9）**：当前最终形态 `3c5d209` 含不依赖真库的钉子，宿主 `sssssssss..` 有 2 例真实执行，变异自检证明删列名即 FAILED（§0.7.2 / §0.7.5）；§0.6.3 的无钉子对照恰好反证其必要性。但 HEAD 在验证期间被并发改写 3 次（`3c5d209 → cbd7b0d → reset 回 3c5d209`），任一中间态结论都不能套用到下一形态，本轮为此按三种形态分别重验。**仍需强调**：9 条真库探针必须在注入 `WIKI_NAV_ACL_DB_URL` 的容器内跑，宿主 skip 不等于真库通过。请 team-lead 锁定交付形态（P2-9）。

**放行前置条件（按优先级；第 1 条已完成）**：
1. **已完成**：修 P1-1（补 3 列）→ 容器内重跑 `pytest tests/wiki/test_nav_acl.py -q`，9 例由 FAILED 转 PASS、`exit=0`（§0.5.3 / §0.6.2 / **§0.7.3 最终 HEAD `3c5d209` 复跑 `11 passed`**）；P2-7 过时文案整改完成（§0.5.5）；P2-8 回归钉子已补回且变异自检有牙（§0.7.5）。
2. **P2-9 锁定交付形态（新增）**：由 team-lead 明确最终 commit hash，验证 / 交付期间不再 amend / reset；如需字节级 hash 三方一致，先统一行尾。并在交付说明 / CI 约定写明「9 条真库探针须在注入 `WIKI_NAV_ACL_DB_URL` 的容器内跑，宿主全绿不代表真库通过」。
3. 补齐真实页（当前仅 1 页 ready）→ 由 team-lead 单独向用户确认后跑 **217 条 A/B**（开/关 wiki 两轮），报告打印 `hit_cases / expanded_cases / expanded_terms` 三数并确认门槛通过；**不满足门槛时不得宣称「不劣化通过」**。N4 合成页只能用于让门槛可被满足，**不得混算进真实页覆盖率、不得产出业务结论**。
4. 补 EXPLAIN（须在多页库上才有统计意义）与 P95 对照；从空库重放 `alembic upgrade head` 26 迁移全链。
5. 同步 P2-1 文案、处理 P2-2 落库三态、对齐 P2-6 的 9 条标签。

在此之前，唯一安全的交付形态是 **`WIKI_ENABLED=false` 关闭态**（A 项已证明该状态与未实现第 4 步逐字节等价），并在交付说明中保留「真库效果层未验证」边界。

---

## 附录 A：本轮执行的全部命令（可重跑复算）

```
# git / 环境
git -C <root> status --porcelain=v1
git -C <root> log --oneline -8
git -C <root> show --stat --format=%H %s ea552c0 / 6717a85 / 86f5fc7
git -C <root> diff --name-only aa6551c HEAD
python --version ; python -m pytest --version ; python -m ruff --version

# 静态证据（自写只读脚本，AST / SQL 逐行比对 / DDL 相容性 / 全仓扫描）
python p3_static.py       → p3_static_out.txt
python p3_runtime.py      → p3_runtime_out.txt   （FakeSession 打桩 + C/F 运行时）
python p3_mutate.py       → p3_mutate_out.txt   （临时副本变异自检）
python p3_counts.py       → p3_counts_out.txt   （junit 计数 / ruff / compileall / 提交链）

# pytest（cwd = code/apps/api）
python -m pytest -q
python -m pytest -q tests/wiki tests/retrieve tests/services
python -m pytest -q --junitxml=... tests/wiki tests/retrieve tests/services [5x --ignore]
python -m pytest -q --junitxml=... <8 个核心测试文件>
python -m pytest -q -ra --junitxml=... tests/wiki/test_eval_wiki_nav_observation.py   # + jose stub
python -m pytest -q -rs tests/wiki/test_nav_acl.py

# lint / 语法（宿主）
python -m ruff check .
python -m compileall -q app scripts tests

# ---- 补充轮：环境亲自复核 ----
docker version ; docker info --format '{{.ServerVersion}}' ; docker compose version
docker ps -a --format '{{.Names}}|{{.Status}}|{{.Image}}'
docker port code-postgres-1                          # 输出为空 → 无宿主端口映射
docker exec code-postgres-1 pg_isready -U user -d kagent
docker inspect code-postgres-1 --format '{{json .NetworkSettings.Networks}}'
docker compose -f code/docker-compose.yml config --format json | Select-String '"name"'
git -C <root> show -s --format=%T ea1fe09 ; git -C <root> show -s --format=%T 86f5fc7
git -C <root> diff --stat ea1fe09 86f5fc7          # (空)

# ---- 补充轮：真库只读元数据 / 计数 / 清理复核 ----
docker exec code-postgres-1 psql -U user -d kagent -t -A -c "SELECT version_num FROM alembic_version"
docker exec code-postgres-1 psql -U user -d kagent -t -A -c "<5 表 count 联合查询>"
docker exec code-postgres-1 psql -U user -d kagent -t -A -c \
  "SELECT table_name,column_name,is_nullable,coalesce(column_default,'<NO_DEFAULT>') \
   FROM information_schema.columns WHERE table_schema='public' \
   AND table_name IN ('chunks','wiki_pages','documents','knowledge_bases','tenants') \
   AND is_nullable='NO' ORDER BY table_name,ordinal_position"
docker exec code-postgres-1 psql -U user -d kagent -t -A -c "SELECT count(*) FROM tenants WHERE code LIKE 'navacl-%'"
docker exec code-postgres-1 psql -U user -d kagent -t -A -c \
  "SELECT tablename,indexname FROM pg_indexes WHERE tablename IN ('wiki_pages','chunks')"

# ---- 补充轮：rebuild + 镜像 hash 核对 ----
docker compose -f code/docker-compose.yml build api       # BUILD_EXIT=0
docker run --rm --entrypoint sh code-api:latest -c "sha256sum /workspace/apps/api/<4 文件>"

# ---- 补充轮：交付探针真跑（容器内，注入真库 URL）----
docker compose -f code/docker-compose.yml run --rm --no-deps -T \
  -e WIKI_NAV_ACL_DB_URL=postgresql+asyncpg://user:pass@postgres:5432/kagent \
  api sh -c "pip install -q -i https://mirrors.aliyun.com/pypi/simple/ 'pytest>=8,<9' 'pytest-asyncio>=0.24,<1' \
             && python -m pytest tests/wiki/test_nav_acl.py -q -rs --tb=short"

# ---- 补充轮：独立诊断（临时脚本只读挂载，不改实现）----
docker compose -f code/docker-compose.yml run --rm --no-deps -T \
  -v <temp>/p3b_probe.py:/tmp/p3b_probe.py:ro \
  -e PYTHONIOENCODING=utf-8 -e WIKI_NAV_ACL_DB_URL=postgresql+asyncpg://user:pass@postgres:5432/kagent \
  api sh -c "pip install -q ... pytest pytest-asyncio && python /tmp/p3b_probe.py"

# ---- 补充轮：容器内全量 pytest（junit 写回宿主临时目录）----
docker compose -f code/docker-compose.yml run --rm --no-deps -T \
  -v <temp>/p3b_junit:/tmp/p3junit \
  api sh -c "pip install -q ... pytest pytest-asyncio && python -m pytest -q -rs --tb=line --junitxml=/tmp/p3junit/full.xml"
python <temp>/p3b_junit_parse.py                    # 解析 junit 精确计数

# ---- 补充轮：C4 217 条精确占比复算（二进制安全导出）----
docker exec code-postgres-1 psql -U user -d kagent -c \
  "\copy (SELECT id, question, expected_answerable FROM eval_cases ORDER BY id) \
   TO '/tmp/p3_eval217.csv' WITH (FORMAT csv, HEADER true, ENCODING 'UTF8')"    # COPY 217
docker cp code-postgres-1:/tmp/p3_eval217.csv <temp>/p3b_eval217.csv
docker exec code-postgres-1 rm -f /tmp/p3_eval217.csv
python <temp>/p3b_c4.py                            # 附录 A 同一 stats()/verify()
python <temp>/p3b_labels.py                       # 206/217 标签口径核对

# ---- 增量复验轮：提交范围 / 重建 / 真库复跑 / 全量回归 / 钉子变异 ----
git -C <root> log --oneline -4 ; git -C <root> show --stat 3c5d209
git -C <root> show 3c5d209:code/apps/api/tests/wiki/test_nav_acl.py > <temp>/committed.py
docker compose -f code/docker-compose.yml build api                        # BUILD_EXIT=0
docker run --rm --entrypoint sh code-api:latest -c "sha256sum /workspace/apps/api/tests/wiki/test_nav_acl.py; wc -l …"
docker compose -f … run --rm --no-deps -T -e WIKI_NAV_ACL_DB_URL=postgresql+asyncpg://user:pass@postgres:5432/kagent \
  api sh -c "pip install -q ... pytest pytest-asyncio && python -m pytest tests/wiki/test_nav_acl.py -q -rs --junitxml=/tmp/p3c_junit/nav.xml"
python <temp>/p3c_counts.py                       # junit 精确计数 10/0/0/0 与全量 941/929/1/0/11
docker compose -f … run --rm --no-deps -T -v <temp>/p3c_junit:/tmp/p3c_junit \
  api sh -c "pip install -q ... && python -m pytest -q -rs --tb=line --junitxml=/tmp/p3c_junit/full.xml"
python <temp>/p3c_mutate2.py                      # 提交态钉子：基线 PASS → 删列名留占位符 FAILED → 还原转绿

# ---- 中间形态复核轮（§0.6：HEAD 一度改为 cbd7b0d 后重验；已被 reset 撤销）----
git -C <root> rev-parse HEAD ; git -C <root> show --stat cbd7b0d ; git -C <root> diff --stat 3c5d209 cbd7b0d
sha256sum code/apps/api/tests/wiki/test_nav_acl.py
git -C <root> show HEAD:code/apps/api/tests/wiki/test_nav_acl.py | sha256sum
docker run --rm --entrypoint sh code-api:latest -c "sha256sum /workspace/apps/api/tests/wiki/test_nav_acl.py; wc -l …"
docker compose -f … run --rm --no-deps -T -v <temp>/p3d_junit:/tmp/p3djunit \
  -e WIKI_NAV_ACL_DB_URL=postgresql+asyncpg://user:pass@postgres:5432/kagent \
  api sh -c "pip install -q ... pytest pytest-asyncio && python -m pytest tests/wiki/test_nav_acl.py -q -rs --junitxml=/tmp/p3djunit/nav_final.xml"
python <junit XML 解析>                           # tests=10 failures=0 errors=0 skipped=0
python <temp>/p3d_adversarial.py                  # 临时副本删 3 列 → 宿主 sssssssss. / exit=0
docker compose -f … run --rm --no-deps -T \
  -v <temp>/p3d_mutant/api/tests/wiki/test_nav_acl.py:/workspace/apps/api/tests/wiki/test_nav_acl.py:ro \
  -e PYTHONDONTWRITEBYTECODE=1 -e WIKI_NAV_ACL_DB_URL=postgresql+asyncpg://user:pass@postgres:5432/kagent \
  api sh -c "pip install -q ... && python -m pytest tests/wiki/test_nav_acl.py -q --tb=line"     # FFFFFFFFF. / exit=1
docker exec code-postgres-1 psql -U user -d kagent -t -A -c "<5 表合成前缀 count + 总量>"        # 0|0|0|0|0

# ---- 最终形态复核轮（§0.7：reset 回 3c5d209 后再次重验）----
git -C <root> reflog -3 --format="%h %gs"     # 3c5d209 reset: moving to 3c5d209 / cbd7b0d commit (amend) / 3c5d209 commit
git -C <root> rev-parse HEAD ; git -C <root> show --stat HEAD
wc -l code/apps/api/tests/wiki/test_nav_acl.py ; grep -c 'test_seed_insert_includes_chunks_not_null_columns' …
python <归一化字节比对>                        # raw != / normalized == / CRLF 363 vs 0 / core.autocrlf=true
docker compose -f code/docker-compose.yml build api                         # BUILD_EXIT=0
docker run --rm --entrypoint sh code-api:latest -c "wc -l …; sha256sum …; grep -c …"   # 363 / e9e0c01a… / 2
python -m pytest -q -rs tests/wiki/test_nav_acl.py                         # 宿主：sssssssss.. / HOST_EXIT=0
docker compose -f … run --rm --no-deps -T -v <temp>/p3e_junit:/tmp/p3ejunit \
  -e WIKI_NAV_ACL_DB_URL=postgresql+asyncpg://user:pass@postgres:5432/kagent \
  api sh -c "pip install -q ... pytest pytest-asyncio && python -m pytest tests/wiki/test_nav_acl.py -q -rs --junitxml=/tmp/p3ejunit/nav.xml"   # 11 passed / REALDB_EXIT=0
python <junit XML 解析>                          # tests=11 failures=0 errors=0 skipped=0
docker compose -f … run --rm --no-deps -T -v <temp>/p3e_junit:/tmp/p3ejunit \
  api sh -c "… python -m pytest -q -rs --tb=line --junitxml=/tmp/p3ejunit/full.xml"   # 942 / 930 / 1 / 0 / 11
python <temp>/p3e_mutate.py                     # 钉子基线 PASS → 删列名 FAILED(AssertionError 点名 embedding_model) → 还原转绿
docker exec code-postgres-1 psql -U user -d kagent -t -A -c "<5 表合成前缀 count + 总量>"   # 0|0|0|0|0
```

## 附录 B：数字与证据的复算方式

| 数字 | 来源 | 复算方式 |
|---|---|---|
| 220 条 Golden 样本 | `golden_wiki_nav_baseline.json` `meta.source.deduped_total`；变异 B 输出「Golden 快照出现 220 处漂移」 | 重跑 `test_golden_wiki_nav.py`；或 `len(json.load(... )['cases'])` |
| 24 个窗口 / 24 个 ASCII 项 | `p3_runtime_out.txt` C 节 | `len(build_fts_query("".join(chr(0x4E00+i) for i in range(26))).strip("()").split(" | "))` |
| 363 / 354 / 9 / 0 / 0 | H3 的 junit XML `tests/failures/errors/skipped` | 重跑 H3 命令后解析 `--junitxml`；或数 `-q` 进度点（72×5 + 3 = 363，其中 `s` 9 个） |
| 69 / 60 / 9 | H4 的 junit XML | 重跑 H4 命令 |
| 21 passed | H5 的 junit XML（stub 注入） | 重跑 H5 命令 |
| 26 / 5 个收集错误 | H1 / H2 的 `short test summary info` | 重跑对应 pytest 命令 |
| 31 行零差异 | D 节规范化比对 | 重跑 `p3_static.py` 的 `normalize()` 与 `difflib.unified_diff` |
| 3 个缺失必填列 | I4 的 AST/DDL 解析 + I1.6 真库 `information_schema` | 读 `0011_add_chunk_index_profile.py` L29-44 + `test_nav_acl.py` L131-143；或重跑 I1.6 的 SQL |
| SHA256 `07b774d0…c9` | 变异脚本前后各一次 + I1.2 镜像核对 | `hashlib.sha256(open(keyword_text.py,'rb').read()).hexdigest()`；镜像内 `sha256sum` 输出一致 |
| 9 FAILED / 1 PASSED / `PYTEST_EXIT=1` | H6 / I1.3 交付探针真库实跑 | 重跑附录 A 的「交付探针真跑」命令；或数 `-q` 进度串 `FFFFFFFFF.` |
| 10 / 10 PASS（诊断副本，修复前） | I1.4 `p3b_diag_utf8.txt` | 重跑附录 A 的「独立诊断」命令；这是**诊断副本**证据。交付文件本身已在整改后转绿（§0.5.3 / §0.6.2），不再「仍红」 |
| `10 passed / 0 failed / 0 skipped`（交付文件，整改后） | §0.5.3 `p3c_realdb_rerun.txt` / `p3c_junit/nav.xml` | 容器内注入 `WIKI_NAV_ACL_DB_URL` 跑 `pytest tests/wiki/test_nav_acl.py`；解析 junit |
| `tests=10 failures=0 errors=0 skipped=0`（**中间形态** `cbd7b0d`，已撤销） | §0.6.2 `p3d_junit/nav_final.xml` | 同上，且当时 `git show HEAD:… \| sha256sum` 与工作区 / 镜像三方一致 |
| `8299e309…` 三方一致 / 303 行 / 无钉子（**中间形态**） | §0.6.1 | `sha256sum` 工作区、`git show HEAD:… \| sha256sum`、镜像 `sha256sum` 三方比对；`grep -c 'def test_'` |
| 宿主 `sssssssss.` exit=0 / 真库 `FFFFFFFFF.` exit=1（对抗性） | §0.6.3 `p3d_adversarial_out.txt`、`p3d_mutant_container.txt` | 临时副本删 3 列复现 P1-1，同一副本分别跑宿主无 DB 与容器真库；副本已删 |
| `tests=11 failures=0 errors=0 skipped=0`（**最终 HEAD** `3c5d209`） | §0.7.3 `p3e_junit/nav.xml` | 容器内注入 `WIKI_NAV_ACL_DB_URL` 跑 `pytest tests/wiki/test_nav_acl.py`；解析 junit |
| `942 / 930 passed / 1 failed / 0 errors / 11 skipped`（最终形态） | §0.7.4 `p3e_junit/full.xml` | 解析 `--junitxml`；与 303 行形态 `941/929/1/0/11` 对照仅 `tests` 与 `passed` 各 +1（钉子用例） |
| `e9e0c01a…` / 363 行 / 含钉子；CRLF vs LF 归一化一致 | §0.7.1 | `sha256sum` 工作区 / `git show HEAD:… \| sha256sum` / 镜像 `sha256sum`；`python` 归一化比对；`core.autocrlf=true` |
| 宿主 `sssssssss..`（9 skip + 2 pass） | §0.7.2 `p3e_host.txt` | 宿主无 `WIKI_NAV_ACL_DB_URL` 跑 `pytest -q -rs tests/wiki/test_nav_acl.py` |
| 钉子删列名 → FAILED 且点名 `embedding_model` → 还原转绿 | §0.7.5 `p3e_mutate_out.txt` | 重跑 `p3e_mutate.py`（输出保留 4000 字符）；副本已删、原文件 hash 未变 |
| 941 / 929 / 1 / 0 / 11 | H10 的容器内 `full.xml` | 解析 `--junitxml`：`tests/failures/errors/skipped`，`passed = tests−failures−errors−skipped` |
| 0 / 217 = 0.00% | I5 `p3b_c4_out.txt` | `docker cp` 导出 217 条 → 附录 A 的 `stats()/verify()`；`sum(w>=24)`、`max(w)`、`verify(all)` |
| 9 条标签不一致 / 11 条新增 = 8 可答 + 3 不可答 | I5 `p3b_labels_out.txt` | 按 `question` 连接 `eval_cases.expected_answerable` 与 `cases_all.json.answerable`，`norm()` 归一后计数 |
| `COPY 217` / 18383 bytes | I5 导出 | `psql \copy` 回显 `COPY 217`；`Get-Item` 读文件字节数 |
| 清理后 0 / 0 / 0 / 0 与总量 1/580/26/1 | I1.5 独立只读 SQL | 重跑附录 A 的两组只读 SQL；`navacl-%` 与合成前缀计数全 0 |
| tree `f3c9b0c3…` | §0.1 message-only amend | `git show -s --format=%T ea1fe09` / `86f5fc7` / `git diff --stat` |

> **临时产物如实登记（不声称「全部删除」）**：项目仓库内无任何临时脚本 / 副本；本验证轮在宿主临时目录 `%TEMP%` 下保留了可复算证据 `p3c_*.txt` / `p3c_mutate2.py` / `p3c_junit/*.xml` / `p3d_adversarial.py` / `p3d_adversarial_out.txt` / `p3d_final.txt` / `p3d_mutant_container.txt` / `p3d_junit/nav_final.xml` / `p3e_*.txt` / `p3e_mutate.py` / `p3e_junit/*.xml`（均在仓库之外、不进 git，**未删除，供 team-lead 复算**）；临时变异副本 `p3d_mutant/` 与 `p3e_mutant/` 已删除；容器内 `/tmp/p3_eval217.csv` 已 `rm -f`；`docker compose run --rm` 的临时容器自动回收。**仓库内唯一未跟踪文件即本报告**；`git status` 中另有若干 ` M` 文档文件，属文档刷新 worker 的未提交改动，**非本验证者所为**。原实现文件 SHA256 前后一致（当前形态 `e9e0c01a…`；中间形态 `8299e309…`），本轮未 `git commit` / `git push`，未改 `docker-compose.yml` / `Dockerfile` / 实现代码；真库真实数据零改动（§0.6.4 / §0.7.6 复核：合成前缀 5 表全 0，总量 `wiki_pages=1 / chunks=580 / documents=26 / tenants=1 / eval_cases=217` 与基线一致）。
