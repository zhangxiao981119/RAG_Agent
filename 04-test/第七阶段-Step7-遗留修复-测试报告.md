# 第七阶段 Step7 遗留修复 —— P3 独立对抗性验证报告

- 验证范围：Web 入口 `/chat/ask` 可选 `scenario_id`（L3 场景接线）
- 验证环境：Windows / PowerShell 5 / Python 3.13.14 / `code\apps\api`
- 验证角色：测试工程师（P3），未修改任何业务代码
- 结论：**PASS**（失败集合与基线完全一致，未发现缺陷；有 3 条建议与若干未覆盖项）

---

## 一、改动清单（git diff 实查）

工作区改动（`git diff --stat`）：

| 文件 | 变更 |
| --- | --- |
| `app/services/scenario/errors.py` | 新增 `ScenarioCode.NOT_FOUND = "SCENARIO_NOT_FOUND"`，映射 `(404, "场景不存在")`（+6 行） |
| `app/services/scenario/resolver.py` | 新增 `resolve_scenario_by_id(...)`，复用 `_load_published` / `build_snapshot`（+49/-1） |
| `app/services/scenario/__init__.py` | 导出 `resolve_scenario_by_id`，docstring 同步（+5/-3） |
| `app/schemas/chat.py` | `ChatAskRequest` 新增 `scenario_id: uuid.UUID \| None = None`（+2） |
| `app/api/chat.py` | 运行时导入 `ScenarioError` / `resolve_scenario_by_id`；`chat_ask` 传 `scenario_id`；`_chat_ask_impl` 新增参数，并在 kb 归属校验后、库位解析前解析场景（+29/-1） |
| `tests/scenario/test_resolver_by_id.py` | 新增（编码者 8 例，本次补 5 例 → 13 例） |
| `tests/chat/test_chat_scenario.py` | 新增（编码者 5 例，本次补 6 例 → 11 例） |

未改动其它文件（`git diff --name-only` 仅上述 5 个源码文件；新增 2 个测试文件为 untracked）。

静态审查结论：

- 无 fallback：新增路径只有 `raise ScenarioError` 与 `raise HTTPException`，`except` 仅捕获 `ScenarioError`，没有任何「回退默认场景 / 默认 policy」的分支。
- 无 emoji：对 7 个文件做字符区间扫描（0x1F000–0x1FAFF、0x2600–0x27BF、0xFE0F 等），命中仅为仓库既有注释符号 `★`（U+2605，BLACK STAR，非 emoji，原文件已有）。
- 注释为中文：新增注释与 docstring 全部中文，UTF-8。
- `ruff check`（7 个文件）：`All checks passed!`
- 新增内容未引入重复的真值表：`resolve_scenario_by_id` 复用 `_load_published` + `build_snapshot`，与 `resolve_scenario` 同源。

---

## 二、逐条承诺的对抗性验证

### 承诺 1：不传 scenario_id → 不解析场景，policy/policy_caps 为 None，行为等价

- 代码证据：`app/api/chat.py:345` 的 `if scenario_id is not None:` 整块跳过，`policy` / `policy_caps` 保持调用方传入值（Web 入口即默认 `None`）。
- 用例证据（`tests/chat/test_chat_scenario.py`）：
  - `test_without_scenario_id_skips_resolution`：把 `chat_mod.resolve_scenario_by_id` 替换为「被调用即 AssertionError」的替身；请求返回 200，`retrieval.seen_policy is None`、`seen_caps is None`。
  - `test_chat_ask_request_scenario_id_defaults_to_none`（本次补）：`ChatAskRequest(kb_ids=[...], question=...).scenario_id is None`。
- 回归证据：全量 1609 passed，失败集合与基线相同（见第四节），等价性无回归。

### 承诺 2：解析入参必须是已过授权交集的 effective_kb_ids，不得用 snapshot.kb_ids 覆盖 KB 范围

- 代码证据：`chat.py:351` 传 `requested_kb_ids=effective_kb_ids`；解析后只取 `snapshot.policy` / `snapshot.caps`（`chat.py:359-360`），检索仍用局部变量 `effective_kb_ids`（`chat.py:750`）。
- 用例证据：
  - `test_resolution_uses_authorized_intersection`（本次补）：非管理员（clearance=10，authorized 仅 KB1）请求 `[KB1, KB2]` → 解析入参 `[KB1]`，检索范围 `[KB1]`（KB2 在解析前即被交集剔除）。
  - `test_snapshot_kb_ids_cannot_widen_retrieval_scope`（本次补）：替身快照故意携带 `kb_ids=(KB2,)` → 检索范围仍是 `[KB1]`，证明生产代码不读 `snapshot.kb_ids`。
  - `test_scenario_declaration_does_not_widen_requested_kb_ids`（本次补，resolver 层）：声明 `(KB_A, KB_B)`、请求 `[KB_A]` → `snapshot.kb_ids == (KB_A,)`、`binding_kb_ids == (KB_A, KB_B)`。
  - 编码者原用例 `test_scenario_policy_and_caps_reach_retrieval` 断言 `captured["requested_kb_ids"] == [_KB_ID]`。

### 承诺 3：不存在/跨租户 → 404 且不可区分；停用 → 400；超范围 → 400；失败不回退默认场景

- resolver 层用例：
  - `test_missing_row_returns_404`：`code is NOT_FOUND`、`http_status == 404`、`message == "场景不存在"`、`diagnostic["reason"] == "scenario_not_found"`。
  - `test_other_tenant_row_returns_same_404`：外部租户行 → 同码同状态。
  - `test_cross_tenant_error_is_indistinguishable_from_missing`（本次补）：异常载荷逐字段相等（`code` / `http_status` / `message` / `diagnostic`），存在性探针不可用。
  - `test_disabled_scenario_returns_not_allowed`：停用 → `NOT_ALLOWED`、400。
  - `test_requested_kb_outside_explicit_scope_returns_mismatch`：超范围 → `MISMATCH`、400。
- 端点层用例（`tests/chat/test_chat_scenario.py`）：
  - `test_scenario_not_found_returns_404_before_retrieval`（参数化「不存在 / 跨租户诊断」）：404 + `detail == "场景不存在"` + `retrieval.seen_session is None`。
  - `test_scenario_not_allowed_returns_400_before_retrieval`：400 + `detail == "场景不可用"` + 未检索。
  - `test_scenario_mismatch_returns_400_before_retrieval`（本次补）：400 + `detail == "请求的知识库不在场景范围内"` + 未检索。
  - `test_scenario_unresolvable_returns_500_before_retrieval`（本次补）：500 + `detail == "场景配置不可解析"` + 未检索（证明该分支也不会回退默认场景继续跑）。
- 「不回退默认场景」的结构证据：`chat.py:353-358` 捕获 `ScenarioError` 后必然抛 `HTTPException`，无 `else` 分支、无默认快照构造。

### 承诺 4：解析必须在进入 StreamingResponse 生成器之前、检索之前失败（HTTP 4xx/5xx，而不是流里半截错误）

- 代码顺序证据：`chat.py:345-360`（解析）位于 `resolve_kb_db_key`（362）、检索（739-756）与 `StreamingResponse(...)`（236）之前；解析代码位于 `_chat_ask_impl` 直线段，不在生成器体内。
- 用例证据：上述 4 条端点用例均断言「响应是 JSON 错误 + `retrieval.seen_session is None`」；若错误发生在流里，TestClient 会得到 200 与 SSE 事件，这些断言会失败。

### 承诺 5：`/integrations/ask` 不接受请求体场景字段，行为不受影响

- 代码证据：`app/api/integrations.py:350-377` 仍只用 `IntegrationAskRequest` 并调用 `_resolve_or_reject`（应用绑定场景），`ChatAskRequest(...)` 构造未传 `scenario_id`（默认 None）；`app/schemas/integration.py:40-45` 为 `extra="forbid"` 且无 `scenario_id` 字段。
- 用例证据：
  - `test_integration_ask_request_forbids_scenario_id`（本次补）：`"scenario_id" not in IntegrationAskRequest.model_fields`，且带该字段校验抛 `ValidationError`。
  - 全量回归含 `tests/test_integrations.py`，无新增失败。
- OpenAPI 证据：`/api/integrations/ask` 的请求体 `IntegrationAskRequest` 属性仅 `['conversation_id', 'kb_ids', 'question']`。

### 承诺 6：只缓存命中行，不做负缓存；缓存键与 resolve_scenario 共用 (tenant_id, scenario_id)

- 代码证据：`resolver.py:301-307` 仅在 `row is not None` 时 `_cache_put`；键 `(tenant_id, scenario_id)` 与 `resolve_scenario`（`resolver.py:253`）同一键空间。
- 用例证据：
  - `test_second_call_hits_row_cache`：两次命中 → 1 条 SQL。
  - `test_missing_row_is_not_negatively_cached`（本次补）：两次 miss → 2 条 SQL，且两次都抛错（补种后立刻可见）。
  - `test_cache_key_shared_with_resolve_scenario`（本次补）：先 `resolve_scenario`（应用绑定同 ID）再 `resolve_scenario_by_id` → 共 1 条 SQL，且两者 `fingerprint` 相同。

### 承诺 7：关闭 TENANT_GOVERNANCE_ENABLED 时 caps 为空、version_source=published

- 用例证据：
  - `test_governance_disabled_caps_empty`：行里伪带 `recall_top_k_max/threshold_*/rerank_allowed/pinned_version=3/version_state=archived`，解析后 `caps.is_empty() is True`、`version_source == "published"`。
  - `test_governance_enabled_caps_applied`：开启时四个上限正确读出、`version_source == "published"`。
  - `test_governance_pinned_version_reports_pinned`（本次补）：开启且钉定 v3（archived）→ `version_source == "pinned"`、`version == 3`，与集成入口同源。

---

## 三、命令与真实输出

### 3.1 定向用例（命令 B）

```
cd code\apps\api
python -m pytest tests/scenario/test_resolver_by_id.py tests/chat/test_chat_scenario.py -q
```

输出（`-q` 与 pyproject `addopts="-q"` 叠加为 `-qq`，pytest 会隐藏汇总行，故另以无 `-q` 形式复核）：

```
........................                                                 [100%]
24 passed in 1.99s
```

→ **24 passed / 0 failed / 0 skipped**（其中编码者原 13 例 + 本次补 11 例）。

### 3.2 目录回归（命令 C）

```
python -m pytest tests/chat tests/scenario -q
```

```
505 passed, 139 skipped in 9.75s
```

→ **505 passed / 0 failed / 139 skipped**。139 skip 全部是 `SCENARIO_TEST_DB_URL` 缺失导致的真库用例（本次命令 C 未注入该变量，符合模板）。

### 3.3 全量回归（接真库，命令 D）

```
$pw = ((Get-Content "code\.env" | Where-Object { $_ -match '^POSTGRES_PASSWORD=' }) -replace '^POSTGRES_PASSWORD=', '').Trim();
$env:SCENARIO_TEST_DB_URL = "postgresql+asyncpg://user:***@localhost:5433/kagent_test";
python -m pytest
```

```
2 failed, 1609 passed, 15 skipped, 1 warning in 145.02s (0:02:25)
```

真库生效性旁证（避免「全部 skip 也算绿」）：

```
python -m pytest tests/scenario/test_0028_pg_constraints.py tests/scenario/test_step7_pin_pg.py -rs
→ 22 passed（0 skipped）
```

说明：全量日志中出现的 `socket.gaierror: [Errno 11001] getaddrinfo failed` 来自 `DATABASE_URL` 的容器主机名解析（自检/健康检查路径），与 `SCENARIO_TEST_DB_URL`（localhost:5433）无关，属既有环境差异。

### 3.4 静态检查（命令 E）

```
openapi_paths: 78
chat_ask_methods: ['post']
integration_ask_methods: ['post']
ChatAskRequest.scenario_id: {'anyOf': [{'type': 'string', 'format': 'uuid'}, {'type': 'null'}], ...}
ChatAskRequest.required: ['kb_ids', 'question']
IntegrationAskRequest props: ['conversation_id', 'kb_ids', 'question']
```

- HEAD 与工作区 `app/api/chat.py` 的 `@router.` 装饰器均为 4 条且逐行相同（`identical: True`）；其余包含路由的文件未出现在 `git diff --name-only` 中。
- 结论：**未新增/删除任何路由**；OpenAPI 的唯一差异是 `/api/chat/ask` 请求体新增可选属性 `scenario_id`（非必填，required 仍只有 `kb_ids` / `question`）。

---

## 四、全量回归失败集合与基线对比

| 项 | 基线（改动前） | 本次 | 差异 |
| --- | --- | --- | --- |
| 用例总数 | 1602 | 1626 | +24（本次新增用例） |
| passed | 1585 | 1609 | +24 |
| failed | 2 | 2 | 0 |
| errors | 0 | 0 | 0 |
| skipped | 15 | 15 | 0（未新增 skip 换绿） |

失败集合（按 nodeid，逐条与基线一致，均为已知环境失败）：

1. `tests/test_health.py::test_openapi_contains_health`（`socket.gaierror` — 外部主机名解析）
2. `tests/wiki/test_worker_and_script.py::test_rebuild_wiki_script_fails_closed_when_disabled`（子进程同样因 `getaddrinfo failed` 在导入期失败）

→ **无新增失败项，无新增 error，无新增 skip。**

---

## 五、发现的问题

- 阻断：无
- 缺陷：无
- 建议（均不影响验收）：
  1. **可观测性缺口（设计取舍的副作用）**：场景「存在但无 published 版本」与「ID 不存在 / 跨租户」在 Web 入口完全同形（同 404、同文案、`diagnostic.reason` 也是 `scenario_not_found`），运维无法区分「客户端传错 ID」与「库内配置损坏」。若需要区分，只能在日志/metrics 侧加一个「行存在但无 published 版本」的计数（不改变响应体，避免存在性探针）。
  2. `app/api/chat.py:28-32` 的注释仍是「`chat` → `services.scenario` 的运行时导入没有必要」，而本次已在 79-80 行新增运行时导入（`ScenarioError` / `resolve_scenario_by_id`）。注释语义只对 `ScenarioPolicy` 类型导入成立，建议改写以免误导后人。
  3. 缓存写入早于租户一致性校验（`resolver.py:303-310`）。当前 SQL 在 `s.tenant_id` / `v.tenant_id` 双端过滤，生产不可触发；但若将来有人放宽 SQL，会把外部租户的行写进本租户缓存键，建议把租户校验前移到 `_cache_put` 之前（仅建议，非本次缺陷）。

---

## 六、未覆盖 / 存疑项

1. **Web 入口的真库端到端链路未覆盖**：新增的端点用例全部打桩 `resolve_scenario_by_id` / Session；resolver 的真 SQL 由 `tests/scenario` 的既有真库用例覆盖，但「请求体 scenario_id → 真库行 → 检索口径」整链路未在真库上跑通。属建议补测范围，不影响本次判定。
2. **治理开关热切换与行缓存**：缓存键不含 `TENANT_GOVERNANCE_ENABLED`，运行中开关切换后 TTL（60s）内可能沿用旧口径行（该行为在 `resolve_scenario` 中已存在，非本次引入）。
3. **请求体 `scenario_id` 非法 UUID 的 422**：由 pydantic 拦截，未单独用例覆盖；属入参校验层既有行为。
4. **场景存在但无 published 版本**：本次按承诺实现为 404「场景不存在」，未单独构造「行存在 + 无版本」的用例（`_load_published` 无行，与不存在同路径）；已在第五节建议中登记可观测性影响。
5. **`status` 为空请求分支**：`build_snapshot` 的 R2（`requested` 为空直接返回快照）早于 R3（停用判定），因此「停用场景 + 空 kb」在 resolver 层返回快照而非 400；该顺序是既有设计（防枚举），在 `/chat/ask` 上不可达（`effective_kb_ids` 为空会先 403）。未新增用例锁定此组合。
