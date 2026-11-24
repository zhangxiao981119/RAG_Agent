# 第七阶段 Step7 遗留修复7 测试报告（P3 独立对抗性验证）

- 角色：测试工程师（独立于编码者，不修改业务代码；仅补测/复现/报告）
- 阶段：P3 对抗性验证
- 被测变更：容量常量/阈值可标定（方案 A，P2 完成，未 commit）
- 结论：**PASS**

---

## 1. 环境与执行方式

- 工作目录：`code\apps\api`，系统 `python`
- 真库：已启动（`localhost:5433` → `code-postgres-1`）
- 注入真库地址（口令替换为 `***`）：

```
$pw = ((Get-Content "code\.env" | ? { $_ -match '^POSTGRES_PASSWORD=' }) -replace '^POSTGRES_PASSWORD=','').Trim()
$env:SCENARIO_TEST_DB_URL = "postgresql+asyncpg://user:***@localhost:5433/kagent_test"
```

> 注：项目 `pyproject.toml` 的 `addopts = "-q --strict-markers"`，命令行再叠加 `-q` 会变成 `-qq` 从而吞掉汇总行。为取得真实计数，执行时使用 `-o addopts=""`（命令 1/2）或 `-o addopts="--strict-markers"`（命令 3）覆盖，测试集合不变。

---

## 2. 改动清单（A：git diff 审查）

实际内容改动 8 个文件，`615 → 692` 行（含本次补测）：

| 类型 | 文件 |
|---|---|
| 源码 | `code/apps/api/app/config/settings.py`（+8 项 `CAPACITY_*`，Field 约束） |
| 源码 | `code/apps/api/app/services/capacity.py`（按请求读 settings、calibrated 判定、size/p95 可评估、埋点读取、lay低延迟两口径） |
| 源码 | `code/apps/api/app/services/retrieve/__init__.py`（`retrieve_chain` try/finally 埋点 `rag.retrieve.total` ms） |
| 测试 | `code/apps/api/tests/scenario/test_step7_capacity.py` |
| 测试 | `code/apps/api/tests/retrieve/test_chain.py` |
| 文档 | `02-design/部署标定手册.md`（§1.5） |
| 文档 | `02-design/第七阶段-Step7-tasks.md` |
| 文档 | `02-design/第七阶段-Step7-前置裁决.md` |

★ 历史脏文件确认（不计入、不提交）：`app/api/sync.py`、`app/services/sync_service.py`、`scripts/rebuild_wiki.py`、`tests/chat/test_ask_with_image.py`、`tests/wiki/test_worker_and_script.py`。
`git diff --numstat` 对这 5 个路径输出为空 → 内容与 HEAD 一致，仅 stat/mtime 变化。结论：确认是历史脏文件。

---

## 3. 12 条设计承诺逐条验证

| # | 承诺 | 结论 | 证据（命令 + 关键输出） |
|---|---|---|---|
| 1 | 默认配置下除新增字段外原响应/阈值规则不变，旧断言未削弱 | **基本成立（有 1 处有意契约变更，见 §7）** | 默认 `total_lower_mb`/`formula`/三阈值数值(0.95/0.30/1.5) 不变；旧断言按新契约调整且更严（`test_capacity_endpoint_structure_and_arithmetic` 改为 M_SHARED/M_SCENE=false 且新增 `M_CONTROL.calibrated is True` + `control["value"]==inputs[...]`；`test_capacity_thresholds_structure` 增加默认值与 reason 断言）。命令 1：`41/44 passed` |
| 2 | 8 项 settings 有真实约束，非法值启动即失败，默认与文档一致 | 成立 | 新增 `test_capacity_settings_reject_invalid_values`：7 项非法值 + 命中率 1.2 + `ref` 超长 201，逐一 `pytest.raises(ValidationError)`，且断言错误 loc 命中被测字段（防别的字段顺带报错假绿）；默认值断言与 §1.5 文档一致（8192/300/0/0.0/""/0.95/0.30/1.5）。命令 1 通过 |
| 3 | M_shared/M_scene 改变后 constants/inputs/result 同步且公式恒等 | 成立 | `test_capacity_settings_override_constants_and_result`：4096/512 覆盖后 constants、inputs 同步，`total_lower_mb == compute_m_a(0, m_shared_mb=4096, m_scene_pg_mb=512, control_pg_mb=...)` |
| 4 | ref 空→三条数值阈值 calibrated=false；非空→true；空白仍 false；independent 恒 false；顶层回显原值 | 成立 | `test_capacity_calibration_ref_controls_calibrated`（空/非空）；**补测** `test_capacity_calibration_ref_whitespace_is_uncalibrated`：ref="   " → `calibration_ref=="   "`（原值回显）、`result.calibrated is False`、常量与三阈值 false、independent false |
| 5 | `M_CONTROL_PG` calibrated 恒 true，value=runtime `shared_buffers` | 成立 | `test_capacity_endpoint_structure_and_arithmetic`：`control["calibrated"] is True` 且 `control["value"] == inputs["m_control_pg_lower_mb"]` |
| 6 | size：实例内存=0→evaluable=false/over=None/reason 指配置；>0→observed=最大库/实例内存，over 正确 | 成立 | `test_build_thresholds_size_uses_largest_database`（0 → false/None + reason 含 `CAPACITY_INSTANCE_MEMORY_MB`；1000 → observed 0.9、over True）；**补测** `test_build_thresholds_size_uses_max_not_order`：最大库排首位仍取 `huge`(900) → 证明按 `size_mb` 取 max，不是取首/末位 |
| 7 | P95：基线=0→reason 指基线；基线>0 无样本→reason 指样本；齐备→observed=p95/基线、over 正确 | 成立 | `test_build_thresholds_retrieval_requires_baseline_and_sample`：三态齐全，reason 分别含 `CAPACITY_RETRIEVAL_P95_BASELINE_MS` / `rag.retrieve.total`；齐备 observed 1.2、over False |
| 8 | 阈值覆盖能翻转 over（hit/size/p95 三条各自验证） | 成立 | `test_build_thresholds_override_flips_over`：strict→三条 over=(True,True,False)；loose→(False,False,True)，三条均发生翻转 |
| 9 | `rag.retrieve.total` 每次 `retrieve_chain` 恰好 1 次、单位 ms、异常也观测；内部 retrieve() 不重复 | 成立 | 代码检索：`rag.retrieve.total` 观测点仅 `retrieve/__init__.py:1008` 一处（grep 全 app 仅此 1 处 `observe_ms`）；`test_retrieve_chain_observes_rag_total_once_in_ms`（3 轮×20ms，count==1 且 ≥10 证明单位为 ms）；`test_retrieve_chain_observes_rag_total_on_exception`（异常路径 count==1） |
| 10 | placeholders 镜像仍通过；banner 默认与 settings 默认一致 | 成立 | `test_capacity_placeholders_and_banner_defaults_match_settings`：三个镜像值一致；`model_fields` 默认与常量一致；capacity.py import 期断言未触发（所有测试均成功 import） |
| 11 | §1.5 只写方法与记录格式，无虚构实测数字；配置名逐字一致 | 成立 | 阅读 §1.5：仅默认值/采样方法/CV<20%/≥200 请求等口径，无实测数字；出现的 `CAPACITY_*` 名称与 settings 字段（`capacity_*` 大写）逐字一致 |
| 12 | `/integrations/search` 与评测直连 `retrieve()` 不计入该指标，响应/文档已如实注明口径 | **部分满足（口径未显式说明排除项，见 §7）** | 代码：`integrations.py:399` 直连 `retrieve()`、`eval_service.py:235`（native=False）直连 `retrieve()`，均不走 `retrieve_chain` → 确实不计入。响应/文档措辞为「检索链路总耗时（含改写/多轮/重排/回捞，不含 LLM 生成）」，已写明范围但未显式点名这两条路径被排除 |

---

## 4. 真实执行结果（C）

### 命令 1（P2 原状 / 补齐后）
```
python -m pytest tests/scenario/test_step7_capacity.py tests/retrieve/test_chain.py -q -o addopts=""
```
- P2 原状：`41 passed in 4.43s`
- **补测后（最终）：`44 passed in 4.95s`**（+3 条补测）

### 命令 2
```
python -m pytest tests/scenario tests/retrieve tests/config -q -o addopts=""
```
- `813 passed, 2 warnings in 115.96s`

### 命令 3（全量）
```
python -m pytest -q -o addopts="--strict-markers"
```
- P2 原状：`2 failed, 1682 passed, 15 skipped in 150.31s`（collected 1699）
- **补测后（最终）：`2 failed, 1685 passed, 15 skipped in 149.84s`（collected 1702）**

### 失败集合与基线比对
基线（第 7 项完成时）：collected 1690 / 1673 passed / 2 failed / 15 skipped。
当前失败集合（2 条，与基线完全相同，无新增）：
- `tests/test_health.py::test_openapi_contains_health` — `socket.gaierror: [Errno 11001] getaddrinfo failed`
- `tests/wiki/test_worker_and_script.py::test_rebuild_wiki_script_fails_closed_when_disabled` — 子进程起服务时 `getaddrinfo failed`（同 DNS 环境）

差异：collected +12、passed +12（P2 +9，本次补测 +3），failed/skipped 集合不变。**无新增失败节点、无新增 skip**（15 skipped 与基线一致）。

---

## 5. 敏感度抽查（B：改坏即红，恢复后字节一致）

原文件 SHA256（恢复后复核一致）：
- `app/services/capacity.py` = `1F51E0AA...D9441C0BC`
- `app/services/retrieve/__init__.py` = `DEB14BE6...68D94F01`

| 变异 | 期望变红用例 | 实测 |
|---|---|---|
| 1. `size_over = size_observed > size_ratio_max` → `size_over = False` | size/override 两用例 | `2 failed`（`assert False is (0.9 > 0.3)`、`assert False is True`）→ 已恢复 |
| 2. `calibrated = bool(raw_ref.strip())` → `calibrated = True` | calibration 用例 | `1 failed`（`assert True is False`）→ 已恢复 |
| 3. 删除 `finally` 内 `metrics.observe_ms("rag.retrieve.total", ...)` | 埋点两用例 | `2 failed`（`assert 0 == 1`）→ 已恢复 |
| 4. `if instance_memory_mb > 0 and sized:` → `if instance_memory_mb < 0 and sized:` | size 用例 | `1 failed`（`assert False is True`）→ 已恢复 |

4 处变异全部「改坏即红」，恢复后两个文件 SHA256 与原始值一致，`git diff --stat` 回到 8 文件布局（业务代码零残留）。

---

## 6. 安全检查（E）

- `capacity.py` 全文检索 `dsn|password|secret|database_url|credential`：仅 2 处为**注释**提及 DSN 解析，无任何凭据回显；响应体 key 集合（`inputs`/`databases`/`latency` 等）不含 DSN/口令。
- `02-design/部署标定手册.md` 检索 `postgresql://|password=|PASSWORD=|口令`：无匹配。§1.5 示例无真实口令。
- 结论：无凭据泄漏。

---

## 7. 发现的问题（分级）

**阻断：无。缺陷：无。**

建议（建议级，不影响 PASS）：

1. **承诺 1 与承诺 5 的张力（有意变更，非弱化）**：默认配置下 `constants.M_CONTROL_PG_LOWER_MB.calibrated` 由旧值 `False` 变为 `True`，且 `source`/`reason`/`note` 等说明性文案重写。这是承诺 5 明确要求的契约变更；数值与三阈值规则未变。旧用例断言按新契约调整并加严，非削弱。建议在交付说明里显式列出这一「非新增字段的响应变化」，避免被误读成违反承诺 1。

2. **承诺 12 口径未显式点名排除项**：响应 `latency.note` 与文档 §1.5 表述为「检索链路总耗时（含改写/多轮/重排/回捞，不含 LLM 生成）」，能让人推断是 `retrieve_chain`，但**未显式列出** `/api/integrations/search`（直连 `retrieve()`）与评测 `native=False`（直连 `retrieve()`）不计入该指标。指标实际未覆盖这两条路径，建议补充一句显式说明，避免读者把「检索链路 P95」误读为覆盖全部检索入口。

---

## 8. 未覆盖 / 存疑项

- 本次补测新增 3 条：`test_capacity_settings_reject_invalid_values`、`test_capacity_calibration_ref_whitespace_is_uncalibrated`、`test_build_thresholds_size_uses_max_not_order`（均在 `tests/scenario/test_step7_capacity.py`，未改弱任何既有断言）。
- `M_shared` 的标定需要真实目标机 `docker stats`，本环境无法实测，仅验证了「可配置 + 公式恒等」，未验证真实驻留；属设计预期（用量与标定由部署手册走）。
- `rag.retrieve.total` 为进程内直方图，多副本场景的聚合口径不在本次范围（与既有 `http.request.duration` 同口径，未变）。

---

## 9. 结论

**PASS**

- 三条命令均真实执行：命令1 最终 `44 passed`；命令2 `813 passed`；命令3 最终 `2 failed / 1685 passed / 15 skipped`。
- 失败集合与本阶段基线完全一致（同 2 条 DNS 环境失败），无新增失败、无新增 skip。
- 12 条承诺中 11 条完全成立，1 条（第 12 条）口径已如实但未显式点名排除路径，列为建议。
- 4 处敏感度变异全部改坏即红，恢复后字节一致。
- 无凭据回显、文档无真实口令。
