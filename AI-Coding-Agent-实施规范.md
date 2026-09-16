# 企业知识库问答 AI Agent · AI Coding Agent 实施规范

> **文档类型**：机器可执行实施规范（Implementation Spec for AI Coding Agent）
> **适用范围**：`backend/`（Python / FastAPI） + `frontend/`（React / Next.js / Taro / Expo）
> **上游文档**：`企业知识库问答AI-Agent-开发流程.md`（人类可读设计文档，含原理与论证）
> **版本**：v1.0 · 2026.09

---

## 0. 执行协议

### 0.1 这份文档与上游文档的分工

| | 本文档（Agent Spec） | 上游文档（Design Doc） |
|---|---|---|
| 面向 | 编码 Agent | 人（评审 / 汇报） |
| 内容 | 契约、不变量、工作单元、验收命令 | 选型论证、原理、权衡、风险分析 |
| 语气 | 祈使句，MUST / MUST NOT | 说明句，含"为什么" |
| 处理方式 | **直接执行**，不需要复述理由 | 需要时查阅 |

**规则**：本文档没有写"为什么"。**任何需要理解动机才能做的判断，先查上游文档对应章节，再动手。** 章节对应关系见 §11 索引表。

### 0.2 冲突解决优先级

当本文档与上游文档、与既有代码、或与用户即时指令冲突时，按此优先级裁决：

```
1. 用户当前会话中的即时指令
2. 本文档 §1 不变量（INVARIANTS）        ← 唯一不可协商的技术底线
3. 本文档 §3/§4 数据与接口契约
4. 本文档 §5 工作单元定义
5. 上游设计文档
6. 仓库既有代码风格
```

**若用户指令与 §1 不变量冲突：不要静默执行。** 停下来，指出冲突的不变量编号，说明后果，请用户确认。用户确认后仍需在代码里留 `# DESIGN-OVERRIDE: INV-xx` 注释。

### 0.3 工作单元模型

所有实现工作被拆成 **WU-xx（Work Unit）**，见 §5。一个 WU 是一个**可独立提交、可独立验收**的最小单元。

执行循环：

```
读 WU 定义
  → 确认依赖 WU 已完成（否则先做依赖）
  → 按"产出文件"清单创建/修改文件
  → 运行"验收命令"，直到全绿
  → 检查该 WU 关联的不变量（§1 索引）
  → 输出 §10 规定的汇报块
  → 进入下一个 WU
```

**MUST NOT** 跳过 WU 依赖顺序。**MUST NOT** 在一个提交里混两个 WU。

### 0.4 动手前的强制检查（每个 WU 都要做）

```
□ 1. 该 WU 是否触及 §1 不变量？若是，先列出涉及的不变量编号
□ 2. 该 WU 是否要新增依赖？若是，检查 §2.1 依赖白名单
□ 3. 该 WU 是否新增 API / 表 / 环境变量？若是，必须同步更新 §3/§4/§9 的清单表
□ 4. 该 WU 是否引入新的 if/else 分支？若是，检查 §6 决策表是否已有定义
□ 5. 该 WU 的验收命令是否可在本地跑通？（不能跑通的 WU 不允许标记完成）
```

### 0.5 不确定时的处置

| 情况 | 处置 |
|---|---|
| 契约里没写某个字段 | **MUST NOT 自行发明**。在汇报里列为 BLOCKER，请求补充契约 |
| 两个不变量看似冲突 | 报告冲突 + 两条不变量编号，寻求裁决。不自行取舍 |
| 上游文档给的代码与 §1 冲突 | 以 §1 为准，并在汇报中指出上游文档需要修订 |
| 依赖库行为不确定 | 先写 20 行探针脚本验证，把结论写进汇报。**MUST NOT 靠猜实现** |
| 业务规则不明确 | MUST NOT 交付一个"看起来合理"的默认值。列为 BLOCKER |

---

## 1. 不变量（INVARIANTS）

> **本节是最高优先级技术约束。** 违反任何一条，即使功能可用、测试全绿，也必须回滚重做。
> 每条含：规则、违反后果、强制检查点、关联工作单元。
> **每个 WU 完成后必须逐条自查本节中标注为该 WU 的不变量。**

### A 组 · 回答边界（"只能使用这些知识回答"）

---

**INV-01 · 检索优先于生成**
任何涉及事实的回答，**MUST** 先经过检索。检索为空或全部低于阈值时，**MUST** 拒答，**MUST NOT** 让模型凭自身知识作答。

- 违反后果：直接违反产品核心约束，幻觉自动化输出
- 强制检查点：`app/generation/pipeline.py::run_rag` 中 gate 决策在生成之前；`app/agent/executor.py::guard_no_retrieval`
- 关联 WU：WU-14, WU-24
- 反例代码：`if not chunks: return await llm.chat(messages)` ❌

---

**INV-02 · 双重闸门**
拒答判定 **MUST** 同时使用两个信号：`max_score < RERANK_THRESHOLD`（分数闸门）与 `len(kept) < MIN_KEPT_CHUNKS`（数量闸门）。**MUST NOT** 只用其中一个。

- 违反后果：单用分数闸门 → 一堆 0.36 的边缘片段凑出一段"看似有据"的回答；单用数量闸门 → 一条高相似但不相干的片段直接进上下文
- 强制检查点：`app/retrieval/gate.py::decide`
- 关联 WU：WU-12

---

**INV-03 · 阈值必须标定**
`RERANK_THRESHOLD` **MUST** 由 `scripts/calibrate_threshold.py` 在正负样本集上扫描 F1 最优点后写入配置。**MUST NOT** 使用任何未标定的默认值上线。

- 违反后果：拒答准确率与误拒率同时失控
- 强制检查点：CI 中 `scripts/calibrate_threshold.py --assert-calibrated` 必须通过（检查配置里带 `# calibrated: <date>` 标记）
- 关联 WU：WU-13

---

**INV-04 · 引用元数据来自检索层，不来自模型**
模型 **MUST** 只输出正文中的 `[n]` 角标。文档名、页码、章节、相似度、更新时间 **MUST** 由后端从检索结果填充。**MUST NOT** 要求模型复述任何来源元数据。

- 违反后果：模型编造页码与文件名，引用可信度归零
- 强制检查点：`app/generation/validator.py` 只校验角标编号；`citations` 数组的每个字段在 `_hydrate_parents` 之后才生成
- 关联 WU：WU-14, WU-16

---

**INV-05 · 引用越界即不合格**
若正文出现 `[n]` 而 `n > len(contexts)`，或某条有事实论断的句子无引用，**MUST** 判定校验失败并触发重试/拒答。

- 违反后果：出现"引用了不存在的来源"，比无引用更糟
- 强制检查点：`validator.validate_answer` 返回 `ValidationResult(ok=False, reasons=[...])`
- 关联 WU：WU-15

---

**INV-06 · 接地校验失败必须拒答**
重试 1 次后接地校验（答案中的数字/专名能否在片段中找到）仍失败，**MUST** 拒答。**MUST NOT** 把未通过校验的答案降级输出。

- 违反后果：幻觉直接触达用户
- 强制检查点：`app/agent/grounding.py::verify_grounding` 返回 `ok=False` → 走拒答分支
- 关联 WU：WU-25

---

### B 组 · 权限与隔离

---

**INV-07 · 权限过滤下推到查询层**
权限条件 **MUST** 写进 SQL `WHERE` / Milvus `expr`，使越权片段**不参与候选竞争**。**MUST NOT** 使用"全库检索 Top-K → 结果层再剔除"的实现。

- 违反后果：① Top-K 被越权文档占满，有效召回骤降；② 越权文档参与排序，挤掉合法结果；③ 攻击者可通过返回条数变化反推无权文档存在
- 强制检查点：`app/retrieval/store/*.py::hybrid_search_secure` 的签名中 ACL 参数为必填；不存在只接 `kb_ids` 的检索入口
- 关联 WU：WU-19, WU-20
- 例外：`assert_visible` 结果层兜底断言 **MUST** 同时保留（防御纵深），但它不是替代方案

---

**INV-08 · `acl_tags` 入库时写入**
每个 chunk **MUST** 在入库时携带完整的 `acl_tags` 数组。**MUST NOT** 依赖检索时 JOIN 权限表计算。

- 违反后果：无法把权限条件下推到向量库（回到 INV-07 的违规形态）
- 强制检查点：`ingest/embedder.py` 写库前校验 `acl_tags` 非空；DDL 中该列为 `NOT NULL`
- 关联 WU：WU-08

---

**INV-09 · 权限变更 fail-closed**
权限服务 / ACL 缓存不可用时，**MUST** 拒绝检索并返回"系统维护中"。**MUST NOT** fail-open（放行）。

- 违反后果：故障窗口期全员可见全部文档，最严重的事故形态
- 强制检查点：`security/acl.py::get_user_subjects_guarded` 的 except 分支必须 `raise`，不得 `return frozenset()`
- 关联 WU：WU-21

---

**INV-10 · 权限缓存 key 必须带 `acl_version`**
任何缓存权限结果的 key **MUST** 形如 `acl:u:{user_id}:v{acl_version}`。权限变更时 **MUST** 递增 `acl_version`。

- 违反后果：调岗 / 离职后旧权限静默延续至 TTL 过期
- 强制检查点：`_user_acl_key()` 为唯一 key 构造函数；代码中不存在其他拼接权限 key 的位置
- 关联 WU：WU-21

---

**INV-11 · 答案缓存 key 必须带权限指纹**
语义缓存与答案缓存的 key **MUST** 含 `hash(sorted(user_subjects))`，且语义缓存 **MUST** 按 `semantic_cache_scope()` 分域。

- 违反后果：A 有权问出财务数据并缓存 → B 用语义相近的问句直接命中 → 静默越权，链路无任何报错
- 强制检查点：`security/acl.py::answer_cache_key` / `semantic_cache_scope` 为唯一构造入口
- 关联 WU：WU-22

---

**INV-12 · 模型产出的工具参数等同用户输入**
Agent 工具调用中，模型生成的任何参数（尤其 `kb_scope`）**MUST** 经 `filter_authorized_kbs()` 重新鉴权并求交集。**MUST NOT** 直接采信。

- 违反后果：用户用 prompt 诱导模型指定越权知识库 ID，越权检索成功
- 强制检查点：`app/agent/executor.py::execute_tool` 的参数净化分支；所有工具 handler 不得绕过 executor 直接调用
- 关联 WU：WU-24

---

**INV-13 · 越权与不存在不可区分**
无权访问时，HTTP 状态码、文案、可用字段数 **MUST** 与"资源不存在"完全一致；响应耗时 **MUST** 补齐到正常区间（±10%）。

- 违反后果：侧信道可枚举出无权文档的存在与数量
- 强制检查点：`app/api/v1/*.py` 中不存在 403 分支；`chat_with_timing_alignment()` 包裹所有鉴权失败路径
- 关联 WU：WU-21, WU-23

---

**INV-14 · `kb_ids` 服务端二次过滤**
前端传入的 `kb_ids` **MUST** 经 `filter_authorized_kbs()` 重新裁剪，且裁剪是**静默的**（不报错、不提示）。

- 违反后果：前端过滤仅是可忽略的体验层，改一下请求体即越权
- 强制检查点：`chat` 路由的第一行业务逻辑必须是这次过滤
- 关联 WU：WU-23

---

### C 组 · 合规与密级

---

**INV-15 · L3 涉密默认不入向量库**
`level >= SECRET` 的文档 **MUST NOT** 进入共享向量库。**MUST NOT** 依赖"检索时过滤"来控制涉密内容。

- 违反后果：只要进库，它就会出现在候选列表、缓存、trace、日志里，每个落地点都是泄漏面
- 强制检查点：`ingest/tasks.py` 在 embed 前调用 `policy_for(level).ingestable`，为 False 则终止任务、置 `status=SKIPPED` 并记录审计
- 关联 WU：WU-17

---

**INV-16 · 入参前缀与出参恢复必须成对**
入库侧脱敏（`mask_for_ingest`）与出参恢复（`restore_on_output`）**MUST** 使用同一份映射表。恢复时 **MUST** 校验当前用户对该文档的权限。

- 违反后果：A 的脱敏数据在 B 的提问中被还原
- 强制检查点：`restore_on_output(text, mapping, user)` 的 `user` 参数为必填且被使用
- 关联 WU：WU-18

---

**INV-17 · 审计日志只可追加**
`audit_log` 表 **MUST** 对应用账号 `REVOKE UPDATE, DELETE`。每条记录 **MUST** 含 `prev_hash` 与 `record_hash`。

- 违反后果：审计可被篡改，合规追溯失效
- 强制检查点：迁移脚本中包含 REVOKE 语句；`append_audit()` 是唯一写入入口
- 关联 WU：WU-26

---

**INV-18 · 审计覆盖四类事件**
登录、权限变更、问答、越权尝试四类事件 **MUST** 全部入审计。缺任一类即不合规。

- 违反后果：查不到"谁在什么时候改了谁的权限"
- 强制检查点：`tests/integration/test_audit_coverage.py` 逐个断言四类事件的写入
- 关联 WU：WU-26

---

**INV-19 · 出域清单必须完整**
Embedding、**Rerank**、LLM、监控四个环节的模型服务地址 **MUST** 全部指向内网。**MUST NOT** 遗漏 Rerank。

- 违反后果：Rerank 的输入是**最相关的候选片段全文**，泄漏量大于 embedding
- 强制检查点：保留密级（L2 及以上）文档存在时，`RERANK_BASE_URL` / `EMBED_BASE_URL` / `LLM_BASE_URL` 必须命中内网白名单，否则服务启动失败
- 关联 WU：WU-29

---

### D 组 · 数据一致性

---

**INV-20 · 删除顺序：向量库先，PG 后标**
文档删除 **MUST** 按「向量库删除 → PG 软删标记」顺序执行。**MUST NOT** 反向。

- 违反后果：出现"引用指向已删除文档"的中间态，合规场景下是明确事故
- 强制检查点：`ingest/sync.py::consistent_delete` 的语句顺序；`consistency_check` 抽样发现不一致时告警
- 关联 WU：WU-27

---

**INV-21 · 版本号是幂等的唯一依据**
外部源同步 **MUST** 以 `compute_doc_hash(content, meta)` + 外部版本号做幂等判断。**MUST NOT** 以"最后同步时间"判断。

- 违反后果：Webhook 乱序 / 重放导致旧版本覆盖新版本
- 强制检查点：`sync_document` 的 `_cmp_version` 比较在写入之前
- 关联 WU：WU-27

---

### E 组 · 流式与交互

---

**INV-22 · 每个 SSE 事件必须带递增 `id:`**
所有 SSE 帧 **MUST** 带递增序号。断点续流 **MUST** 依据 `Last-Event-ID` 从该序号之后继续。**MUST NOT** 重放整个回答。

- 违反后果：无法实现续流，断网即丢失整段回答
- 强制检查点：`app/api/v1/chat.py::sse_generator` 中序号单调递增；`events.ts` 的 `id` 字段为必填
- 关联 WU：WU-30, WU-32

---

**INV-23 · 心跳间隔 15s**
SSE 流 **MUST** 每 15s 发送一次心跳注释帧。**MUST NOT** 大于 30s。

- 违反后果：Nginx / 企业代理 / 移动 NAT 静默断开，且不触发 `onerror`，用户只看到回答卡住
- 强制检查点：`HEARTBEAT_INTERVAL = 15`，且 `sse_generator` 的主循环里有对应 `asyncio.wait_for` 超时分支
- 关联 WU：WU-30

---

**INV-24 · 任何中断都保留已输出内容**
流式中断（LLM 报错 / 超时 / 网络断）**MUST** 保留已下发的 `delta` 内容，并在其后就地追加错误提示。**MUST NOT** 清空或回滚已显示内容。

- 违反后果：破坏"内容在增长"的心理契约，用户感受比直接失败更差
- 强制检查点：`packages/core/src/hooks/useChatStream.ts` 的错误分支只 append 不 reset
- 关联 WU：WU-33

---

### F 组 · 成本与安全

---

**INV-25 · 密钥零暴露面**
`LLM_API_KEY` / 向量库凭证 / DB 连接串 **MUST NOT** 出现在任何 `NEXT_PUBLIC_*` 变量或客户端 bundle 中。前端 **MUST** 只经 BFF（Next.js Route Handler）访问后端能力。

- 违反后果：密钥公开泄漏
- 强制检查点：构建产物扫描 `grep -r "sk-\|Bearer " .next/static/` 必须为空；`tests/unit/test_no_secret_leak.py`
- **配套约束**：**RSC 能保护代码，不能保护数据。** 从 Server Component 传给 Client Component 的 props 会被序列化下发到浏览器，**MUST** 视为公开数据。任何文档正文、chunk 内容、权限标签 **MUST NOT** 通过 props 传给客户端组件——客户端要用则通过已鉴权的接口请求
- 关联 WU：WU-31

---

**INV-26 · 向量库故障必须拒答**
向量库不可用 / 超时 **MUST** 返回拒答。**MUST NOT** 降级为纯 LLM 回答或无来源回答。

- 违反后果：直接违反 INV-01
- 强制检查点：异常兜底矩阵（§6.4）第 5、6 行的实现分支
- 关联 WU：WU-29

---

**INV-27 · 限流 key 不带权限指纹**
限流 key **MUST NOT** 包含权限相关字段。**MUST** 使用稳定的 `user_id`。

- 违反后果：权限变更会重置配额，等于提供绕过限额的路径（与 INV-11 的缓存规则刚好相反）
- 强制检查点：`security/ratelimit.py` 的 key 构造函数与 `_user_acl_key` 无任何共用字段
- 关联 WU：WU-28

---

**INV-28 · 缓存 key 与限流 key 规则相反**
见 INV-11 与 INV-27。二者 **MUST** 分别由独立函数构造，**MUST NOT** 共用构造函数。

- 违反后果：为满足一侧而破坏另一侧（这是最容易被"顺手统一"破坏的一对约束）
- 强制检查点：代码评审检查点；两函数的单元测试互相断言 key 不含对方特有字段
- 关联 WU：WU-22, WU-28

---

### 1.1 不变量速查索引

| 组 | 编号 | 一句话 | 关联 WU |
|---|---|---|---|
| A 边界 | INV-01 | 检索优先于生成 | 14, 24 |
| | INV-02 | 双重闸门 | 12 |
| | INV-03 | 阈值必须标定 | 13 |
| | INV-04 | 引用元数据来自检索层 | 14, 16 |
| | INV-05 | 引用越界即不合格 | 15 |
| | INV-06 | 接地失败必须拒答 | 25 |
| B 权限 | INV-07 | 权限下推到查询层 | 19, 20 |
| | INV-08 | acl_tags 入库时写入 | 08 |
| | INV-09 | 权限服务 fail-closed | 21 |
| | INV-10 | 权限缓存 key 带版本 | 21 |
| | INV-11 | 答案缓存 key 带权限指纹 | 22 |
| | INV-12 | 工具参数等同用户输入 | 24 |
| | INV-13 | 越权与不存在不可区分 | 21, 23 |
| | INV-14 | kb_ids 服务端二次过滤 | 23 |
| C 合规 | INV-15 | L3 默认不入库 | 17 |
| | INV-16 | 脱敏与恢复成对 | 18 |
| | INV-17 | 审计只可追加 | 26 |
| | INV-18 | 审计覆盖四类事件 | 26 |
| | INV-19 | 出域清单完整（含 Rerank） | 29 |
| D 一致性 | INV-20 | 先删向量库后标 PG | 27 |
| | INV-21 | 版本号幂等 | 27 |
| E 流式 | INV-22 | SSE 事件带递增 id | 30, 32 |
| | INV-23 | 心跳 15s | 30 |
| | INV-24 | 中断保留已输出 | 33 |
| F 成本 | INV-25 | 密钥零暴露面 | 31 |
| | INV-26 | 向量库挂必拒答 | 29 |
| | INV-27 | 限流 key 不带权限 | 28 |
| | INV-28 | 缓存与限流 key 规则相反 | 22, 28 |

---

## 2. 技术栈与仓库结构

### 2.1 依赖白名单（MUST）

**新增任何不在下表或不在 `pyproject.toml` / `package.json` 已声明列表中的依赖，必须先请求确认。**

#### 后端（Python 3.11+）

| 用途 | 依赖 | 备注 |
|---|---|---|
| Web 框架 | `fastapi`, `uvicorn[standard]` | |
| 数据校验 | `pydantic>=2`, `pydantic-settings` | |
| ORM / 迁移 | `sqlalchemy[asyncio]`, `asyncpg`, `alembic` | |
| 向量库 | `pgvector` / `pymilvus` | 二选一，由 `VECTOR_BACKEND` 决定 |
| 中文全文检索 | `zhparser`（PG 扩展） | pgvector 方案必需 |
| 任务队列 | `celery[redis]`, `redis` | |
| PDF 解析 | `pymupdf` | 首选，速度快 |
| PDF 兜底 | `pdfplumber` | 表格提取 |
| Word | `python-docx` | |
| Excel | `openpyxl` | |
| HTML | `selectolax` 或 `beautifulsoup4` | |
| OCR | `paddleocr` | 仅扫描件分支 |
| HTTP | `httpx` | 全异步，禁用 `requests` |
| 分词 | `tiktoken` 或 `tokenizers` | |
| 观测 | `langfuse`, `opentelemetry-*`, `prometheus-client` | |
| 测试 | `pytest`, `pytest-asyncio`, `httpx`（TestClient） | |

**MUST NOT 引入**：`langchain`, `llama-index`, `haystack`（理由见上游 §2.3；解析能力可借鉴，RAG 编排自己串）。若确需某段解析逻辑，**复制实现，不要引入框架**。

#### 前端

| 用途 | 依赖 |
|---|---|
| Web / H5 / 桌面 | `next@15`（App Router）, `react@19`, `tailwindcss` |
| 状态 | `zustand` |
| 数据获取 | `swr` 或 `@tanstack/react-query` |
| 包管理 / 构建 | `pnpm`, `turborepo` |
| 小程序 | `@tarojs/*` |
| App | `expo`, `react-native` |
| 桌面壳 | `@tauri-apps/cli` |

**MUST NOT** 在 `packages/core` 中引入任何 UI 库或 DOM API。

### 2.2 仓库结构（权威版本）

> 上游 §6.1 与 §7.1 分别给出后端与前端结构，此处**合并并补齐** `agent/` 目录与 `grounding.py`（这两个模块在上游第 15 章引入，但未回填到 §6.1 的目录树）。**以此处为准。**

```
repo/
├── README.md
├── docker-compose.yml
├── .env.example
│
├── backend/
│   ├── pyproject.toml
│   ├── alembic/                        # 迁移脚本，versions/ 下按序命名
│   ├── app/
│   │   ├── main.py                     # FastAPI 入口，注册路由与中间件
│   │   ├── config.py                   # Pydantic Settings，字段与 §9 一一对应
│   │   ├── deps.py                     # 依赖注入：current_user / db / redis / gateway
│   │   │
│   │   ├── api/
│   │   │   ├── deps.py                 # 路由级依赖：require() / require_kb_access()
│   │   │   ├── health.py               # /health, /health/deps
│   │   │   └── v1/
│   │   │       ├── chat.py             # POST /chat (SSE), POST /chat/sync
│   │   │       ├── auth.py             # /auth/exchange, /auth/refresh, /auth/logout
│   │   │       ├── knowledge.py        # 知识库 CRUD
│   │   │       ├── documents.py        # 上传 / 列表 / 状态 / 预览 / 删除
│   │   │       ├── sources.py          # 外部源接入与手动同步
│   │   │       ├── sessions.py         # 会话历史
│   │   │       ├── webhooks.py         # POST /webhooks/{source_type}
│   │   │       └── admin.py            # 权限管理、审计查询
│   │   │
│   │   ├── llm/
│   │   │   ├── client.py               # ModelGateway：chat / chat_stream / embed / rerank
│   │   │   └── embedding.py            # 向量化封装 + 批处理 + 缓存
│   │   │
│   │   ├── ingest/
│   │   │   ├── parsers/                # pdf.py md.py txt.py excel.py docx.py web.py ocr.py
│   │   │   ├── cleaner.py              # 页眉页脚 / 导航栏清洗
│   │   │   ├── chunker.py              # 父子双层分块
│   │   │   ├── embedder.py             # 写向量库（含 acl_tags 校验）
│   │   │   ├── sync.py                 # 增量同步 / 一致性删除 / 对账
│   │   │   └── tasks.py                # Celery 任务 + 进度上报
│   │   │
│   │   ├── retrieval/
│   │   │   ├── query_rewrite.py        # 指代消解 / 关键词扩展 / HyDE
│   │   │   ├── hybrid.py               # 混合检索编排 + RRF
│   │   │   ├── reranker.py             # 重排 + 分数归一化
│   │   │   ├── gate.py                 # 拒答闸门（INV-02）
│   │   │   ├── escalate.py             # 重检索三级参数
│   │   │   └── store/
│   │   │       ├── base.py             # 抽象接口（hybrid_search_secure 签名在此定义）
│   │   │       ├── pgvector.py
│   │   │       └── milvus.py
│   │   │
│   │   ├── generation/
│   │   │   ├── prompts.py              # 全部 Prompt 常量（禁散落在业务代码里）
│   │   │   ├── context_builder.py      # 上下文预算与拼接顺序
│   │   │   ├── validator.py            # 出口校验（INV-04/05）
│   │   │   └── pipeline.py             # run_rag 主链路
│   │   │
│   │   ├── agent/
│   │   │   ├── loop.py                 # Agent Loop
│   │   │   ├── executor.py             # 工具执行器（INV-12 参数净化在此）
│   │   │   ├── registry.py             # ToolSpec 注册表
│   │   │   ├── grounding.py            # verify_grounding（INV-06）
│   │   │   ├── memory.py               # 多轮记忆与摘要
│   │   │   ├── budget.py               # 上下文窗口治理与 token 配额
│   │   │   └── tools/
│   │   │       ├── kb_search.py
│   │   │       ├── doc_summary.py
│   │   │       ├── doc_export.py
│   │   │       └── translate.py
│   │   │
│   │   ├── security/
│   │   │   ├── acl.py                  # 权限标签计算 + 缓存（INV-09/10/11）
│   │   │   ├── ratelimit.py            # 分层限流（INV-27）
│   │   │   ├── signature.py            # 请求签名与重放防护
│   │   │   ├── injection.py            # 注入检测（入库侧 + 用户侧）
│   │   │   ├── dlp.py                  # 敏感信息识别与脱敏（INV-16）
│   │   │   ├── classification.py       # 密级判定（Level / LevelPolicy）
│   │   │   └── audit.py                # 审计哈希链（INV-17/18）
│   │   │
│   │   ├── models/                     # SQLAlchemy ORM
│   │   ├── schemas/                    # Pydantic 出入参
│   │   └── observability/
│   │       ├── tracing.py
│   │       ├── metrics.py
│   │       └── business.py             # 业务埋点（命中率 / 拒答率 / 引用点击）
│   │
│   ├── tests/
│   │   ├── unit/
│   │   ├── integration/
│   │   ├── security/                   # 越权矩阵、侧信道、注入
│   │   └── eval/                       # 评估集回归
│   │
│   └── scripts/
│       ├── calibrate_threshold.py
│       ├── run_eval.py
│       ├── verify_audit_chain.py
│       ├── consistency_check.py
│       └── seed_demo_data.py
│
├── eval/                               # 评估集（与 backend 解耦，便于单独维护）
│   ├── positive.jsonl                  # 200 条
│   ├── negative.jsonl                  # 200 条
│   ├── adversarial.jsonl               # 50 条
│   └── multi_turn.jsonl                # 50 条
│
├── frontend/
│   ├── pnpm-workspace.yaml
│   ├── turbo.json
│   ├── packages/
│   │   ├── core/                       # ★ 五端共享，零 UI、零 DOM
│   │   │   └── src/
│   │   │       ├── types/              # 从后端 OpenAPI 生成
│   │   │       ├── types/events.ts     # SSE 事件联合类型（与 §4.3 同源）
│   │   │       ├── api/client.ts       # fetch 封装 + 鉴权 + 重试
│   │   │       ├── api/chat.ts         # SSE 消费（分端适配器）
│   │   │       ├── api/documents.ts
│   │   │       ├── stores/             # zustand
│   │   │       ├── hooks/              # useChatStream / useUpload / useCitation
│   │   │       ├── citation.ts         # 引用解析与角标编号映射
│   │   │       └── tokens.ts           # 设计令牌
│   │   ├── ui-web/                     # Next.js 栈组件
│   │   ├── ui-mini/                    # Taro 组件
│   │   └── ui-rn/                      # React Native 组件
│   └── apps/
│       ├── web/                        # Next.js：Web + H5 + 桌面端前端
│       │   └── app/
│       │       ├── (chat)/chat/[sessionId]/page.tsx
│       │       ├── (admin)/knowledge/...
│       │       ├── api/                # Route Handler = BFF（INV-25）
│       │       └── error.tsx / loading.tsx / not-found.tsx
│       ├── desktop/                    # Tauri 壳
│       ├── mini/                       # Taro 小程序
│       └── mobile/                     # Expo
│
└── deploy/
    ├── nginx/nginx.conf                # SSE 必配项见上游附录 C.1
    ├── docker/
    └── k8s/
```

### 2.3 命名约定（MUST）

#### ID 前缀

| 实体 | 前缀 | 示例 |
|---|---|---|
| 用户 | `usr_` | `usr_3f9a` |
| 部门 | `dept_` | `dept_fin` |
| 用户组 | `grp_` | `grp_audit` |
| 知识库 | `kb_` | `kb_finance` |
| 文档 | `doc_` | `doc_3f9a` |
| 外部源 | `src_` | `src_confluence` |
| Parent 块 | `ck_` | `ck_8f21` |
| Child 块 | `ck_` | `ck_8f21a`（parent 后加字母） |
| 会话 | `sess_` | `sess_9f2c` |
| 消息 | `msg_` | `msg_01` |
| 一次运行 | `run_` | `run_r_8f3a` |
| 答案 | `ans_` | `ans_7d21` |
| 解析任务 | `task_` | `task_b91c` |
| 审计记录 | 自增 `seq` | 1, 2, 3… |

**MUST** 生成时包含类型前缀。**MUST NOT** 用自增整数作为对外暴露的 ID。

#### 命名风格

| 位置 | 风格 | 示例 |
|---|---|---|
| Python 模块 / 函数 / 变量 | `snake_case` | `hybrid_search_secure` |
| Python 类 | `PascalCase` | `GateResult` |
| Pydantic 字段（API 出入参） | `snake_case` | `kb_ids`, `rerank_top_n` |
| TypeScript 类型 / 组件 | `PascalCase` | `SseEvent`, `CitationCard` |
| TypeScript 函数 / 变量 | `camelCase` | `useChatStream` |
| TS 中映射的后端字段 | **保持 `snake_case`** | `data.kb_ids` |
| 环境变量 | `UPPER_SNAKE` | `RERANK_THRESHOLD` |
| PG 表 / 列 | `snake_case`，表名复数 | `doc_acl`, `acl_tags` |
| 枚举值 | `UPPER_SNAKE` | `NO_RELEVANT_CONTEXT` |
| 事件名（SSE） | `lower_snake` | `delta`, `tool` |

> **TS 中后端字段一律 `snake_case`，不做 camelCase 转换。** 转换层是 bug 温床，且会让 OpenAPI 生成的类型与手写代码不一致。

#### 提交与分支

- 分支：`wu-XX-<短描述>`，例 `wu-19-retrieval-acl`
- 提交：`WU-XX: <做了什么>`，一个提交对应一个 WU
- 触及不变量的提交，正文里 **MUST** 列出 `Affects: INV-07, INV-08`

## 3. 数据契约

> **本节把上游文档中散落的 schema 收敛为唯一权威版本。**
> 上游未明确定义、但实现必需的字段以本节为准，并 **MUST** 回写到上游文档。
> **MUST NOT** 在任何模块内私自定义与本节不一致的字段名。

### 3.1 关系库表清单

| 表 | 用途 | 关键约束 | 关联 WU |
|---|---|---|---|
| `departments` | 组织架构，物化路径 | `path` 支持子树查询（★ DEC-22/Q3 的**子孙展开**靠它） | WU-04 |
| `users` | 用户 | `acl_version`、`external_id` | WU-04 |
| `user_groups` / `user_group_members` | 用户组 | 成员变更须递增相关用户 `acl_version` | WU-04 |
| `roles` / `permissions` / `role_permissions` / `user_roles` | RBAC：功能权限 | 只控功能，不控数据 | WU-05 |
| `knowledge_bases` | 知识库 | `acl_version`、`visibility`、**`is_public`（全局最多 1 行为 true，部分唯一索引）** | WU-06 |
| `kb_members` | 知识库成员授权 | `(kb_id, subject_type, subject_id)` 唯一；`subject_type ∈ {user, group, dept, role}`（DEC-22/Q1） | WU-06 |
| `documents` | 文档 | `acl_version`、`content_hash`、`version`、`is_latest`、`deleted_at` | WU-06 |
| `doc_acl` | 文档级 ACL 明细 | `effect='deny'` 优先；`expires_at` 支持临时授权 | WU-06 |
| `chunks` | 块（pgvector 方案） | `acl_tags NOT NULL`，GIN 索引 | WU-08 |
| `sessions` / `messages` | 会话与消息 | 按 `user_id` 隔离 | WU-30 |
| `ingest_tasks` | 解析任务与进度 | `stage`、`progress`、`retry_count` | WU-11 |
| `sources` | 外部知识源 | `sync_mode`、`webhook_secret`、`paused` | WU-27 |
| `audit_log` | 审计（哈希链） | `REVOKE UPDATE, DELETE`；`PARTITION BY RANGE (ts)` | WU-26 |
| `token_usage` | 成本归因 | `PARTITION BY RANGE (ts)` | WU-28 |

#### 3.1.1 权限相关字段的语义（MUST 精确理解）

| 字段 | 语义 | 递增时机 |
|---|---|---|
| `users.acl_version` | 该用户权限指纹的版本 | 用户角色变更 / 用户组增减成员 / 用户部门调动 / 离职 |
| `documents.acl_version` | 该文档权限指纹的版本 | `doc_acl` 增删改 / `visibility` 变更 / `owner_dept_id` 变更 |
| `knowledge_bases.acl_version` | 该库权限指纹的版本 | `kb_members` 增删改 / 库 `visibility` 变更 / **`is_public` 变更** |
| **部门树结构（全局）** | DEC-22/Q3 的 A2 展开让用户主体依赖**其下属组织树** | **部门新增 / 删除 / 改名 / 移动 → 该部门祖先链上所有用户的 `acl_version` +1，且 `acl_epoch += 1`（全局失效）** |

> **INV-10 落地要点**：三个 `acl_version` 任一变化，相关缓存 key 必须随之变化。**MUST NOT** 依赖 TTL 过期来实现权限失效。

#### 3.1.2 `doc_acl.effect` 的裁决顺序（MUST）

```
前置必要条件（AND，任一不满足即无权，不可被后续任何规则"救回来"）
  P1. 文档未删除 且 is_latest = true
  P2. doc.level_rank <= user.clearance            ← 闸门 G1（DEC-22）
  P3. doc.kb_id ∈ user.authorized_kb_ids          ← 闸门 G2 · 知识库成员是必要条件（DEC-22）

在 P1~P3 全部满足之后，再按下列顺序裁决
  1. 命中的 deny 记录（未过期）        → 无权
  2. 命中的 allow 记录（未过期）        → 有权
  3. documents.visibility = 'public'   → 有权
  4. documents.visibility = 'inherit'  → 递归到所属 knowledge_base 的 kb_members 判定
  5. 其余                              → 无权
```

**deny 优先于 allow**，**显式优先于继承**。此顺序 **MUST** 在 `compute_doc_acl_tags()` 中实现为单一路径，**MUST NOT** 在别处重复实现。

> **★ 本次修正（DEC-22 落盘）**：原写法把 `visibility = 'public'` 直接判为"有权"，**绕过了 G2**。后果是：一份 `visibility='public'` 的财务文档放进任何人可访问的库，研发同事即可命中。
> 契约 §3.2 的判定式是 **G1 ∧ G2 ∧ ¬G3 ∧ G4 的合取**，G2 是**必要条件**，不能被任何 `visibility` 取值跳过。上方已把 P1~P3 提为前置必要条件。

### 3.2 Chunk 元数据契约（向量库文档结构）

每个 chunk **MUST** 携带以下全部字段。标 ★ 的字段 **MUST NOT** 为空。

| 字段 | 类型 | 空？ | 说明 |
|---|---|---|---|
| `chunk_id` | string | ★ | `ck_` 前缀 |
| `parent_id` | string | ★ | child 指向其 parent；parent 指向自身 |
| `doc_id` | string | ★ | |
| `kb_id` | string | ★ | |
| `content` | string | ★ | 用于 embedding 与展示的文本 |
| `content_type` | enum | ★ | `text` \| `table` \| `title` |
| `section_path` | string | | `"第三章 > 3.2 住宿标准"` |
| `page` | int | | 起始页 |
| `token_count` | int | ★ | |
| `acl_tags` | string[] | ★ | 见 §3.2.1 |
| `owner_dept` | string | ★ | |
| `visibility` | enum | ★ | `public` \| `dept` \| `private` \| `inherit`（★ `inherit` 递归到所属知识库的 `kb_members`，见 §3.1.2） |
| `level` | int | ★ | 0=公开 1=内部 2=保密 3=涉密 |
| `doc_updated_at` | ISO8601 | ★ | |
| `effective_from` | date | | |
| `effective_to` | date | | |
| `is_latest` | bool | ★ | |
| `doc_name` | string | ★ | |
| `doc_type` | string | | `policy` \| `manual` \| `report` … |
| `doc_url` | string | ★ | 深链，支持 `#page=N` |
| `content_hash` | string | ★ | 用于幂等与一致性校验 |
| `lang` | string | ★ | `zh` \| `en` |

#### 3.2.1 `acl_tags` 的构造规则（唯一实现）

标签 **MUST** 由 `compute_doc_acl_tags()` 单一函数产出，格式为 `<type>:<id>`：

```
dept:finance                     文档**自身所属**的部门路径（★ MUST NOT 展开祖先）
group:grp_audit                  授权用户组
role:kb_admin                    授权角色
user:usr_3f9a                    单点授权
public                           全员可见（★ 互斥：仅当无其他主体标签时才写入）
```

**三条 MUST NOT（DEC-22 · 2026-09-16 冻结）**

| # | 规则 | 违反后果 |
|---|---|---|
| 1 | **MUST NOT 对 `dept:` 做祖先展开** | 用户侧与文档侧都含共同祖先 → 组织中任意两个主体最终共享根节点 → 交集非空 → **全公司互通** |
| 2 | **MUST NOT 写入 `{public, ...}` 混合标签** | `public` 是每个用户的常规主体，共存时 G4 对全员恒真 → 只剩 G2 在守（配合公开库即泄漏） |
| 3 | **MUST NOT 包含 `level:` 标签** | 用户主体含 `level:<n>`（≤ clearance），与 G4 组合会**绕过 G1 的密级比较** |

**MUST NOT** 在标签里包含 `deny` 语义（deny 在入库时已通过"不写入 allow 标签"表达）。

**写库前断言（MUST）**

```python
tags = compute_doc_acl_tags(doc_meta=..., kb=...)
assert tags, "空标签 = 可见性未定义（INV-08）"
assert not (PUBLIC_SUBJECT in tags and len(tags) > 1), "public 必须互斥（契约 §3.3(3)）"
assert not any(t.startswith("level:") for t in tags), "密级 MUST 只走 G1（契约 §3.3(4)）"
```

> **本节判定依据**：`AI-Coding-Agent-决策契约.md` §3.3。**两处如有冲突，以契约为准**（契约 §0.6 优先级第 2 条）。

**★ `level` 与 `level_rank` 的标尺（跨文档对齐）**

本节 §3.3 的 `Level(IntEnum)` 是 `0/1/2/3`（API 层表述）。**检索下推字段 MUST 是数值化的 `level_rank = (level + 1) * 10`** → `10/20/30/40`。
若向量库字段名仍是 `level`，其取值 **MUST 是 rank**，不是 `0..3`。否则 `clearance` 与文档密级不在同一标尺上比较 —— **G1 恒真是泄漏，恒假是服务不可用**。

#### 3.2.2 检索时的匹配语义

用户侧的 `user_subjects` 集合（由 `compute_user_subjects()` 产出）与 chunk 的 `acl_tags` **取交集非空即可见**：

```python
def build_acl_expr(user_subjects: set[str]) -> str:
    # Milvus expr 形式；pgvector 方案对应 SQL 的 acl_tags && ARRAY[...]
    joined = ", ".join(f'"{s}"' for s in sorted(user_subjects))
    return f"acl_tags in [{joined}]"
```

> **INV-07 落地要点**：这个表达式 **MUST** 作为 `hybrid_search_secure` 的必需参数参与查询，**MUST NOT** 在结果返回后再用 `assert_visible` 做第一次过滤。
> **边界情况**：`user_subjects` 为空集合 → **直接短路返回空结果，不发起查询**（且必须走 fail-closed，见 INV-09）。
> 但注意：**正常情况下这个分支不应被走到** —— `compute_user_subjects` MUST 恒含 `public`（契约 §3.3 动作 B）。出现空集应当**记一条错误日志**，而不是一次静默短路。

**★ 两侧语义不同是有意的（MUST 理解，否则会把它"优化"成漏洞）**

| 侧 | 集合含义 | 展开方式 |
|---|---|---|
| 用户主体 | "我**能看到的**部门集合" | 祖先 ∪ 自己 ∪ **子孙**（DEC-22/Q3：上级可见下级） |
| 文档标签 | "我**归属的**部门" | **仅自己所属的那一条路径，不展开** |

这两侧**不对称是设计要求**：对称（两侧都展开祖先）会让任意两个主体因共享根节点而互相可见。
**见到"文档标签没有展开祖先"时 MUST NOT 认为是遗漏并补上** —— 那会把系统改成全公司互通（契约 §3.5 用例 10 专门守这条）。

### 3.3 枚举值字典（MUST 唯一）

```python
# backend/app/schemas/enums.py —— 所有枚举唯一定义处
# frontend/packages/core/src/types/enums.ts —— 由 OpenAPI 生成，禁止手写

class DocStatus(StrEnum):
    PENDING    = "PENDING"      # 已上传，待解析
    PARSING    = "PARSING"
    CHUNKING   = "CHUNKING"
    EMBEDDING  = "EMBEDDING"
    INDEXED    = "INDEXED"      # 终态·成功
    FAILED     = "FAILED"       # 终态·失败
    DELETED    = "DELETED"      # 终态·软删
    SKIPPED    = "SKIPPED"      # 终态·密级过高不入库（INV-15）

class Stage(StrEnum):           # 解析进度阶段，与 DocStatus 同源
    QUEUED = "QUEUED"; PARSE = "PARSE"; CLEAN = "CLEAN"
    CHUNK = "CHUNK"; MASK = "MASK"; EMBED = "EMBED"; INDEX = "INDEX"

class RefuseReason(StrEnum):
    NO_RELEVANT_CONTEXT   = "NO_RELEVANT_CONTEXT"
    LOW_RELEVANCE         = "LOW_RELEVANCE"
    INSUFFICIENT_CONTENT  = "INSUFFICIENT_CONTENT"
    GROUNDING_FAILED      = "GROUNDING_FAILED"
    VECTOR_STORE_DOWN     = "VECTOR_STORE_DOWN"
    ACL_UNAVAILABLE       = "ACL_UNAVAILABLE"
    QUOTA_EXCEEDED        = "QUOTA_EXCEEDED"
    INJECTION_BLOCKED     = "INJECTION_BLOCKED"

class FinishReason(StrEnum):
    STOP = "stop"; LENGTH = "length"; REFUSED = "refused"
    TIMEOUT = "timeout"; ERROR = "error"

class Level(IntEnum):
    PUBLIC   = 0    # 公开
    INTERNAL = 1    # 内部
    SECRET   = 2    # 保密
    TOP      = 3    # 涉密

# ★ 检索下推字段 MUST 用数值化的 rank：level_rank = (level + 1) * 10 → 10/20/30/40
#   理由：user.clearance 与文档密级必须在同一标尺上比较（契约 §3.3 动作 C）。
#   违反后果：G1 恒真 = 密级过滤失效；G1 恒假 = 全部拒答。

class KbRole(StrEnum):
    ADMIN = "admin"; EDITOR = "editor"; VIEWER = "viewer"

class SubjectType(StrEnum):
    USER = "user"; GROUP = "group"; DEPT = "dept"; ROLE = "role"; ALL = "all"; LEVEL = "level"

class Effect(StrEnum):
    ALLOW = "allow"; DENY = "deny"

class QuotaAction(StrEnum):
    NORMAL = "NORMAL"; SMALL_MODEL = "SMALL_MODEL"
    CACHE_ONLY = "CACHE_ONLY"; REJECT = "REJECT"
```

**MUST NOT** 在业务代码里出现裸字符串 `"refused"` / `"refuse"` / `"NO_RELEVANT"` 等——一律引用枚举。

### 3.4 向量库集合 schema

```python
# 仅 Milvus 方案；pgvector 方案对应 chunks 表（§3.1）
CHUNKS_COLLECTION = "kb_chunks"
# 字段
chunk_id      VARCHAR(64)   PK
parent_id     VARCHAR(64)
doc_id        VARCHAR(64)
kb_id         VARCHAR(64)
content       VARCHAR(8192)
content_type  VARCHAR(16)
section_path  VARCHAR(512)
page          INT32
token_count   INT32
acl_tags      ARRAY<VARCHAR(64)>       # ★ 标量过滤字段
visibility    VARCHAR(16)
level         INT8
is_latest     BOOL
doc_updated_at TIMESTAMPTZ
content_hash  VARCHAR(64)
dense         FLOAT_VECTOR(dim=VECTOR_DIM)
sparse        SPARSE_FLOAT_VECTOR      # BM25 用

# 必需索引
#   dense  → HNSW / IVF_FLAT
#   sparse → SPARSE_INVERTED_INDEX
#   acl_tags / kb_id / doc_id / is_latest / level → 标量索引
```

> **MUST** 为 `acl_tags`、`kb_id`、`is_latest`、`level` 建标量索引。缺索引会导致过滤退化为全表扫描，检索延迟劣化 10 倍以上。

---

## 4. 接口契约

### 4.1 REST 接口清单

| 方法 | 路径 | 权限码 | 幂等 | 备注 |
|---|---|---|---|---|
| POST | `/api/v1/auth/exchange` | — | 否 | 五端登录换取 Token |
| POST | `/api/v1/auth/refresh` | — | 否 | refresh_token → access_token |
| POST | `/api/v1/auth/logout` | — | 是 | 撤销 refresh_token |
| GET | `/api/v1/auth/me` | — | 是 | 当前用户 + 权限清单 |
| **POST** | **`/api/v1/chat`** | `chat:use` | 否 | **创建 run，返回 `run_id` + `stream_url`**（核心接口） |
| **GET** | **`/api/v1/chat/stream`** | `chat:use` | 是 | **SSE 订阅（EventSource），支持 `Last-Event-ID` 续流** |
| POST | `/api/v1/chat/sync` | `chat:use` | 否 | 一次性返回，小程序无流式能力时降级 |
| GET | `/api/v1/sessions` | `chat:use` | 是 | 分页 |
| GET | `/api/v1/sessions/{id}/messages` | `chat:use` | 是 | 仅本人会话 |
| DELETE | `/api/v1/sessions/{id}` | `chat:use` | 是 | 仅本人会话 |
| POST | `/api/v1/knowledge-bases` | `kb:create` | 否 | |
| GET | `/api/v1/knowledge-bases` | `chat:use` | 是 | **按权限过滤后返回** |
| PATCH | `/api/v1/knowledge-bases/{id}` | `kb:manage` | 是 | |
| GET | `/api/v1/knowledge-bases/{id}/members` | `kb:manage` | 是 | |
| POST | `/api/v1/knowledge-bases/{id}/members` | `kb:manage` | 否 | 触发 `invalidate_on_acl_change` |
| DELETE | `/api/v1/knowledge-bases/{id}/members/{subject}` | `kb:manage` | 是 | 同上 |
| POST | `/api/v1/documents/upload` | `doc:upload` | 否 | multipart，返回 `task_id` |
| GET | `/api/v1/documents` | `chat:use` | 是 | 按权限过滤 |
| GET | `/api/v1/documents/{id}/status` | `chat:use` | 是 | 轮询解析进度 |
| GET | `/api/v1/documents/{id}/preview` | `chat:use` | 是 | 原文定位到页 |
| DELETE | `/api/v1/documents/{id}` | `doc:delete` | 是 | 软删 + 向量库删除（INV-20） |
| POST | `/api/v1/sources` | `kb:manage` | 否 | 接入外部源 |
| POST | `/api/v1/sources/{id}/sync` | `kb:manage` | 否 | 手动触发同步 |
| POST | `/api/v1/webhooks/{source_type}` | — | 是 | **签名校验**，非 JWT |
| GET | `/api/v1/admin/audit` | `audit:read` | 是 | 审计查询 |
| GET | `/api/v1/admin/audit/verify` | `audit:read` | 是 | 哈希链完整性校验 |
| GET | `/api/v1/health` | — | 是 | |
| GET | `/api/v1/health/deps` | — | 是 | LLM / 向量库 / DB 连通性 |

> **MUST NOT** 新增 `4xx=403` 的权限错误响应。无权一律与"不存在"同构（INV-13）。

### 4.2 聊天接口出入参

> **三个入口的分工**：
> - `POST /chat` → 只返回 `{run_id, stream_url, session_id, message_id}`，**MUST NOT** 返回回答内容
> - `GET /chat/stream?run_id=` → 回答内容全部从这里的事件流出（§4.3）
> - `POST /chat/sync` → 一次性返回下面的完整 Response，**内部复用同一条 `run_rag` 链路**

#### Request

```ts
// POST /api/v1/chat
interface ChatRequest {
  session_id: string;              // 必填；不存在则创建
  question: string;                // 必填，1 <= len <= 2000
  kb_ids: string[];                // 必填，可为空数组（服务端会二次过滤 INV-14）
  filters?: {
    department?: string;
    doc_type?: string[];
    updated_after?: string;        // ISO8601 date
    level_max?: number;            // 上限密级
  };
  stream: boolean;                 // 默认 true
  top_k?: number;                  // 默认 RETRIEVE_TOP_K，上限 50
  rerank_top_n?: number;           // 默认 RERANK_TOP_N，上限 10
}
```

**校验规则（MUST 在 Pydantic 层拦截）**

| 字段 | 规则 | 违反处理 |
|---|---|---|
| `question` | 长度 1–2000 | 422 |
| `kb_ids` | 长度 ≤ 20 | 静默截断（不报错，避免侧信道） |
| `top_k` | 1–50，超出则 clamp | clamp，不报错 |
| `rerank_top_n` | 1–10，超出则 clamp | clamp，不报错 |
| `session_id` | 属于当前用户，否则视为不存在 | 新建会话（不报错） |

#### Response（一次性，`/chat/sync`）

```jsonc
{
  "answer_id": "ans_7d21",
  "session_id": "sess_9f2c",
  "message_id": "msg_01",
  "refused": false,
  "answer": "根据《差旅费用管理办法（2025 修订）》第 3.2 节，……[1]",
  "citations": [
    {
      "no": 1,
      "chunk_id": "ck_8f21a",
      "doc_id": "doc_3f9a",
      "doc_title": "差旅费用管理办法（2025 修订）.pdf",
      "doc_url": "/files/doc_3f9a/preview#page=4",
      "page": 4,
      "breadcrumb": "第三章 > 3.2 住宿标准",
      "snippet": "一线城市住宿标准为 600 元/晚……",
      "updated_at": "2025-11-08T00:00:00Z",
      "score": 0.92,
      "level": 1
    }
  ],
  "usage": { "prompt_tokens": 2380, "completion_tokens": 96, "cached_tokens": 0 },
  "latency_ms": 1840,
  "finish_reason": "stop"
}
```

**拒答时**（`refused: true`，`answer` 为拒答文案，`citations` 为 `[]`）：

```jsonc
{
  "answer_id": "ans_7d22",
  "refused": true,
  "finish_reason": "refused",
  "reason": "NO_RELEVANT_CONTEXT",
  "answer": "知识库中没有找到与「XX」相关的内容。你可以补充相关文档，或转人工咨询。",
  "suggestions": ["换一种问法", "扩大知识库范围", "转人工"],
  "usage": { "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0 }
}
```

> **MUST NOT** 在 `refused: true` 时返回任何 `citations`。
> **MUST NOT** 在拒答文案中透露"存在但你无权访问"（INV-13）。

### 4.3 SSE 事件协议（前后端唯一契约）

```ts
// frontend/packages/core/src/types/events.ts
export type SseEvent =
  | { type: 'meta';      runId: string; messageId: string; model: string }
  | { type: 'tool';      name: string; status: 'start' | 'done'; count?: number; ms?: number }
  | { type: 'citation';  no: number; docId: string; docTitle: string;
                         page?: string; breadcrumb?: string; level: number }
  | { type: 'delta';     text: string }
  | { type: 'heartbeat'; ts: number }
  | { type: 'done';      usage: { prompt: number; completion: number; cached?: number };
                         finishReason: 'stop' | 'length' | 'refused' | 'timeout' | 'error' }
  | { type: 'refused';   reason: string; suggestions: string[] }
  | { type: 'error';     code: string; message: string; retryable: boolean };
```

#### 4.3.0 两步式订阅（MUST）

**MUST NOT** 用「POST 直接返回 SSE 流 + 前端手工解析」的形态。原因：`EventSource` 只支持 GET 且不能带 body，用 `fetch` 手解析就失去了**浏览器原生自动重连**与 `Last-Event-ID` 续流能力。

```
① POST /api/v1/chat          → 创建 run，立即返回 { run_id, stream_url }（不返回任何回答内容）
② GET  /api/v1/chat/stream?run_id=...  → EventSource 订阅该 run 的事件流
```

**前端 `readyState` 分支（MUST）**：

| `readyState` | 处理 |
|---|---|
| `CONNECTING`(0) | 断开是瞬时的，**交给浏览器自动重连**，前端不介入 |
| `OPEN`(1) | 正常接收 |
| `CLOSED`(2) | **一律不重连**。`CLOSED` 表示服务端已明确结束（`done` / `refused`）或返回了不可重试错误；此时重连会变成死循环——权限过期场景下会无限刷接口 |

> 这条规则的价值：**只有把"该不该重连"交给 `readyState` 判断，才能同时避免「网络抖动丢答」与「权限过期刷爆接口」两类问题。**

#### 4.3.1 线上帧格式

```
id: 1
event: meta
data: {"runId":"run_8f3a","messageId":"msg_01","model":"Qwen2.5-72B-Instruct"}

id: 2
event: tool
data: {"name":"kb_search","status":"start"}

id: 3
event: citation
data: {"no":1,"docId":"d_771","docTitle":"差旅费用管理办法","page":"3.2","breadcrumb":"第三章 > 3.2 住宿标准","level":1}

id: 4
event: delta
data: {"text":"根据"}

: heartbeat 1757750000000

id: 5
event: done
data: {"usage":{"prompt":3120,"completion":186},"finishReason":"stop"}
```

#### 4.3.2 时序约束（MUST）

| 约束 | 规则 |
|---|---|
| 序号 | `id` 从 1 开始单调递增，**跨心跳也递增**（心跳帧本身就是一次 send） |
| 首帧 | **MUST** 是 `meta`，且 **MUST** 在首个 `delta` 之前 |
| 终帧 | **MUST** 且只能是 `done` / `refused` / `error` 三者之一 |
| `delta` | 只能出现在 `meta` 之后、终帧之前 |
| `refused` | **MUST NOT** 与任何 `delta` 同时出现 |
| `citation` | **MUST** 在它引用的正文 `delta` 之前下发 |
| 心跳 | 空闲 ≥ 15s 时 **MUST** 发送；**MUST NOT** 计入 `id` 序列之外（它是独立注释行） |

> **注意**：心跳使用 SSE 注释行 `: heartbeat <ts>`，**不带 `id:`**，因此**不参与**续流序号。这是刻意的——心跳不应出现在重放窗口里。

#### 4.3.3 断点续流

```
EventSource 自动重连 → 浏览器自动带上请求头 Last-Event-ID: <最后收到的 id>
服务端              → 从该 id 之后重放已产生的事件，然后继续
                     （重放窗口由 Redis Stream 保存，TTL 10 分钟）
超出窗口            → 下发 event: error { code: "RESUME_EXPIRED", retryable: true }，随后关闭流
```

**MUST NOT** 重放整个回答。**MUST NOT** 因续流重跑检索与生成。
**MUST** 保证续流后前端拼出的文本与不断线时**逐字一致**（WU-32 的验收标准）。
**续流的额外收益**：因为 run 的状态在服务端，同一 run 可跨设备 / 跨标签页恢复。

### 4.4 错误码字典

```python
# 收敛所有对外错误码；MUST NOT 出现不在本表的 code
ERROR_CODES = {
    # 认证与授权
    "AUTH_REQUIRED":        (401, False, "请先登录"),
    "AUTH_EXPIRED":         (401, True,  "登录已过期"),      # 前端触发无感续期
    "ACCOUNT_INACTIVE":     (401, False, "账号已停用"),
    "TOKEN_REVOKED":        (401, False, "登录状态已失效"),
    "PERMISSION_DENIED":    (403, False, "当前角色无法执行该操作"),  # ★ 仅限功能权限
    # 资源（数据无权与不存在同构，INV-13）
    "NOT_FOUND":            (404, False, "没有找到相关内容"),
    # 请求
    "INVALID_REQUEST":      (422, False, "请求参数不合法"),
    "QUESTION_TOO_LONG":    (422, False, "问题过长"),
    "UNSUPPORTED_FILE_TYPE":(422, False, "不支持的文件类型"),
    "FILE_TOO_LARGE":       (413, False, "文件超过大小限制"),
    # 限流与配额
    "RATE_LIMITED":         (429, True,  "请求过于频繁，请稍后再试"),
    "QUOTA_EXCEEDED":       (429, False, "今日额度已用完"),
    "BUSY":                 (503, True,  "当前咨询较多，请稍后重试"),
    # 系统
    "MAINTENANCE":          (503, True,  "系统维护中，暂时无法查询"),
    "VECTOR_STORE_DOWN":    (503, True,  "知识库暂时不可用"),
    "ACL_UNAVAILABLE":      (503, True,  "系统维护中，暂时无法查询"),
    "LLM_TIMEOUT":          (504, True,  "响应较慢，请重试"),
    "LLM_ERROR":            (502, True,  "生成失败"),
    # 内容处置
    "INJECTION_BLOCKED":    (400, False, "提问包含不被允许的指令"),
    "GROUNDING_FAILED":     (200, False, None),   # 走 refused 事件，不走 error
    "RESUME_EXPIRED":       (200, True,  None),   # SSE 流内 error 事件
}
```

> **`PERMISSION_DENIED`（403）只用于功能权限**（如无 `doc:upload` 却调上传接口）。
> **数据权限永远不返回 403**：一律 `NOT_FOUND`（404）或 `200 + refused`。这是 INV-13 的直接要求，也是越权矩阵 `deny_or_empty` 用例的断言依据。

### 4.5 内部模块接口（跨模块调用必须走这些签名）

> 签名 **MUST** 保持一致。**MUST NOT** 新增绕过这些入口的平行实现。
> 关键参数用 ★ 标记——它们是不变量落地所依赖的。

```python
# ── 权限 ─────────────────────────────────────────────────────────────
async def compute_user_subjects(db, user_id: str) -> set[str]: ...
async def compute_doc_acl_tags(db, doc_id: str) -> tuple[list[str], list[str]]:
    """返回 (allow_tags, deny_tags)。"""
async def get_user_subjects(redis, db, user: dict) -> set[str]:
    """★ 带 acl_version 的缓存读取（INV-10）。"""
async def get_user_subjects_guarded(redis, db, user) -> set[str]:
    """★ fail-closed 包装（INV-09）。所有检索路径必须调用此函数。"""
async def invalidate_user_acl_cache(redis, user_id: str) -> None: ...
async def invalidate_on_acl_change(redis, *, user_id=None, doc_id=None, kb_id=None) -> int: ...
async def filter_authorized_kbs(db, user: dict, requested: list[str]) -> list[str]:
    """★ 静默裁剪（INV-14）。返回空 list 表示无可用库。"""
def build_acl_expr(user_subjects: set[str]) -> str: ...
def assert_visible(chunk: dict, user_subjects: set[str]) -> None:
    """★ 结果层兜底断言（防御纵深，不是替代 INV-07）。"""

# ── 缓存（★ 与限流 key 规则相反，INV-28）────────────────────────────
def answer_cache_key(question: str, kb_ids: list[str], **kw) -> str:
    """★ 必须含 hash(sorted(user_subjects))（INV-11）。"""
async def semantic_cache_scope(redis, user_subjects, kb_ids) -> str:
    """★ 语义缓存分域（INV-11）。"""
async def lookup_semantic_cache(redis, vec, question, kb_ids, user_subjects): ...

# ── 检索 ─────────────────────────────────────────────────────────────
async def hybrid_search_secure(
    self, *, dense_vec, query_text, kb_ids, user_subjects: set[str],
    top_k: int, filters: dict | None = None, max_level: int | None = None,
) -> list[dict]:
    """★ ACL 条件下推（INV-07）。user_subjects 为必填关键字参数。"""
async def retrieve_with_acl(*, query, kb_ids, user, top_k, ctx) -> list[dict]:
    """检索唯一入口。内部串起 subjects → 下推查询 → 兜底断言。"""
async def rewrite_query(gateway, question: str, history: list[dict]) -> str: ...
class Reranker:
    async def rerank(self, query: str, docs: list[dict], top_n: int) -> list[dict]: ...
def decide(kept_chunks: list[dict], stats: dict, *,
           score_threshold: float, min_chunks: int, min_content_len: int) -> GateResult:
    """★ 双重闸门（INV-02）。阈值不许有默认值——必须由配置注入。"""
def escalate(params: RetrievalParams, attempt: int) -> RetrievalParams: ...

# ── 生成与校验 ───────────────────────────────────────────────────────
def build_context(chunks: list[dict], *, max_tokens: int) -> tuple[str, list[dict]]: ...
def build_messages(question: str, contexts: list[dict], history: list[dict]) -> list[dict]: ...
def validate_answer(answer: str, contexts: list[dict]) -> ValidationResult:
    """★ 只校验角标编号与引用覆盖，不校验元数据（INV-04/05）。"""
async def generate_with_guard(gateway, question, contexts, history) -> tuple[str, ValidationResult]: ...
async def run_rag(*, gateway, store, reranker, question, kb_ids,
                  user_subjects: set[str], history, top_k, rerank_top_n
                  ) -> tuple[str | None, list[dict], GateResult, RagTrace]:
    """★ 主链路。user_subjects 必填（INV-07）。"""
async def _hydrate_parents(store, kept: list[dict]) -> tuple[list[dict], dict]:
    """★ 引用元数据在此填充（INV-04）。"""

# ── Agent ────────────────────────────────────────────────────────────
async def execute_tool(name: str, args: dict, ctx: AgentContext) -> ToolResult:
    """★ 唯一工具执行入口（INV-12）。模型参数净化在此。"""
async def verify_grounding(answer: str, snippets: list[str]) -> GroundingResult:
    """★ 接地校验（INV-06）。"""
async def guard_no_retrieval(answer: str, tool_calls: list[dict]) -> GuardResult:
    """★ 未检索却输出事实 → 拦截（INV-01）。"""

# ── 合规 ─────────────────────────────────────────────────────────────
def resolve_level(doc_meta: dict, kb_meta: dict, content_head: str) -> tuple[Level, str]: ...
def policy_for(level: int) -> LevelPolicy:
    """★ 密级 → 行为映射（§6.2）。ingestable 为 False 时禁止入库（INV-15）。"""
def detect(text: str) -> list[Finding]: ...
def mask_for_ingest(text: str, doc_id: str) -> tuple[str, list[dict]]: ...
def restore_on_output(text: str, mapping: list[dict], user) -> str:
    """★ user 必填并参与权限校验（INV-16）。"""
def guard_output(answer: str, level_policy: LevelPolicy) -> tuple[str, bool]: ...
async def append_audit(rec: dict) -> None:
    """★ 审计唯一写入入口（INV-17）。"""
async def verify_chain(start_seq: int, end_seq: int) -> tuple[bool, int | None]: ...

# ── 安全 ─────────────────────────────────────────────────────────────
async def detect_injection(text: str, *, side: str) -> InjectionVerdict:
    """side='ingest' | 'query'。两侧规则不同，MUST NOT 混用。"""
def sanitize_chunk(text: str) -> tuple[str, list[str]]:
    """★ 清除特殊 token 与隐藏指令（入库侧）。"""
def verify_signature(headers: dict, body: bytes, secret: str) -> bool: ...
def ratelimit_key(user_id: str, route: str) -> str:
    """★ MUST NOT 含权限字段（INV-27）。"""

# ── 一致性 ───────────────────────────────────────────────────────────
def compute_doc_hash(content: str, meta: dict) -> str: ...
async def sync_document(source: dict, doc_payload: dict) -> str: ...
async def apply_doc_update(doc_id: str, new_chunks: list[dict]) -> None: ...
async def consistent_delete(doc_id: str) -> None:
    """★ 向量库先删 → PG 后标（INV-20）。"""
async def consistency_check(sample_rate: float) -> dict: ...
```

## 5. 工作单元（Work Units）

### 5.1 总览与批次

共 40 个 WU，分 7 个批次。**批次之间有 GATE，GATE 未通过不得进入下一批次。**

| 批次 | WU | 主题 | GATE |
|---|---|---|---|
| **B0** | 01–04 | 地基：仓库、配置、数据模型 | GATE-0 |
| **B1** | 05–11 | 知识接入：解析、分块、入库、同步 | GATE-1 |
| **B2** | 12–18 | 检索与生成：闸门、检索、校验、引用、合规 | GATE-2 |
| **B3** | 19–26 | 隔离与服务：权限下推、缓存、请求链路、Agent、审计 | GATE-3 |
| **B4** | 27–30 | 一致性、成本、部署、流式服务端 | GATE-4 |
| **B5** | 31–37 | 前端：BFF、流式消费、页面、五端 | GATE-5 |
| **B6** | 38–40 | 评估、可观测、灰度 | GATE-6 |

| WU | 名称 | 依赖 | 不变量 |
|---|---|---|---|
| 01 | 仓库骨架与本地环境 | — | — |
| 02 | 配置体系与依赖健康检查 | 01 | — |
| 03 | 迁移：组织架构与 RBAC | 01 | — |
| 04 | 迁移：知识库、文档、doc_acl | 03 | — |
| 05 | 解析器：PDF / MD / TXT / XLS / XLSX | 02 | — |
| 06 | 清洗器：页眉页脚与导航栏剔除 | 05 | — |
| 07 | 向量化封装 | 02 | — |
| 08 | 分块与入库 | 05, 07 | INV-08 |
| 09 | 异步解析任务与进度上报 | 08 | — |
| 10 | 外部源接入、Webhook 与手动同步 | 09 | — |
| 11 | 会话与消息存储 | 03 | — |
| 12 | 拒答闸门 | 13 | INV-02 |
| 13 | 混合检索、Rerank 与阈值标定 | 07, 08 | INV-03 |
| 14 | RAG 主链路 | 12, 13 | INV-01, INV-04 |
| 15 | 出口校验 | 14 | INV-05 |
| 16 | 引用两段式与元数据填充 | 14 | INV-04 |
| 17 | 密级判定 | 04 | INV-15 |
| 18 | DLP 脱敏与恢复 | 17 | INV-16 |
| 19 | 检索隔离：主体计算与条件下推 | 04, 21 | INV-07 |
| 20 | 向量库适配层 | 13, 19 | INV-07 |
| 21 | 权限缓存与 fail-closed | 03, 19 | INV-09, INV-10, INV-13 |
| 22 | 缓存体系（含权限指纹） | 19 | INV-11, INV-28 |
| 23 | 请求链路收敛与越权不可区分 | 14, 19, 21, 22 | INV-13, INV-14 |
| 24 | Agent 工具注册与执行器 | 23 | INV-12 |
| 25 | Agent 接地校验与边界守护 | 24 | INV-06 |
| 26 | 审计哈希链 | 03 | INV-17, INV-18 |
| 27 | 同步一致性与删除传播 | 08, 10 | INV-20, INV-21 |
| 28 | 限流、签名与配额治理 | 22, 23 | INV-27, INV-28 |
| 29 | 网络隔离、出域清单与异常兜底 | 20, 23 | INV-19, INV-26 |
| 30 | SSE 服务端（心跳 + 续流窗口） | 14, 23 | INV-22, INV-23 |
| 31 | Next.js BFF 与密钥隔离 | 23 | INV-25 |
| 32 | 前端 SSE 消费与断点续流 | 30, 31 | INV-22 |
| 33 | 前端错误兜底与保留已输出 | 32 | INV-24 |
| 34 | Agent Loop 与工具路由 | 24, 25 | INV-01 |
| 35 | 多轮记忆与上下文窗口治理 | 34 | — |
| 36 | 前端页面与路由结构 | 32, 33 | — |
| 37 | 五端渲染栈（Taro / Expo / Tauri） | 36 | — |
| 38 | 评估流水线与 CI 门禁 | 14, 23 | — |
| 39 | 可观测与业务埋点 | 23, 26 | — |
| 40 | 灰度与发布 | 29, 38 | — |

> **依赖倒置说明**：WU-12（闸门）依赖 WU-13（检索），因为闸门的参数来自标定结果。实现顺序上先做 13 再做 12。

---

### B0 · 地基

#### WU-01 · 仓库骨架与本地环境
- **产出**：`repo/` 目录结构（§2.2 全量）、`docker-compose.yml`（PG + Redis + MinIO + 向量库二选一）、`frontend/pnpm-workspace.yaml` + `turbo.json`、`.env.example`
- **关键动作**：
  - `docker-compose up -d` 后 `pgvector` 与 `zhparser` 扩展可加载
  - `pnpm install` 与 `uv sync`（或 `pip install -e .`）均无错
- **验收**：
  - `docker compose ps` 全部 healthy
  - `curl -s localhost:8000/health` 返回 200
  - `pnpm -w run build` 通过
- **DoD**：新同学 clone 后 3 条命令可起本地环境

#### WU-02 · 配置体系与依赖健康检查
- **产出**：`app/config.py`（Pydantic Settings，字段与 §9 一字不差）、`app/api/health.py`、`app/llm/client.py`（`ModelGateway` 骨架）
- **关键动作**：
  - **MUST NOT** 在业务代码里出现 `os.getenv()`；一律通过 `settings.xxx`
  - **MUST** 启动时校验：`VECTOR_BACKEND` 与对应必填项；`RERANK_THRESHOLD` 注释含 `# calibrated:` 才能在 `ENV=prod` 下启动
  - `/health/deps` 逐项探测 LLM / Embed / Rerank / 向量库 / DB / Redis，返回 `{name, ok, latency_ms, detail}`
- **验收**：`pytest tests/unit/test_config.py -q`；`curl localhost:8000/api/v1/health/deps | jq`
- **DoD**：故意断开一个依赖，`/health/deps` 能准确指出是哪一项

#### WU-03 · 迁移：组织架构与 RBAC
- **产出**：`alembic/versions/0001_org_rbac.py`
- **表**：`departments`, `users`, `user_groups`, `user_group_members`, `roles`, `permissions`, `role_permissions`, `user_roles`
- **关键动作**：
  - `departments.path` 用物化路径（`/1/4/9/`），配 GiST 索引
  - `users.acl_version INT NOT NULL DEFAULT 0`
  - 权限码 **MUST** 使用 §4.1 表中出现的字符串；新增权限码须同步更新本表
  - 种入初始权限与三个内置角色
- **验收**：`alembic upgrade head && alembic downgrade -1 && alembic upgrade head`
- **DoD**：`tests/unit/test_org_path.py` 验证子树查询正确（如查"财务部"能带出"财务部>结算组"）

#### WU-04 · 迁移：知识库、文档、doc_acl
- **产出**：`alembic/versions/0002_kb_doc_acl.py`
- **表**：`knowledge_bases`, `kb_members`, `documents`, `doc_acl`
- **关键动作**：
  - 三个 `acl_version` 字段齐全
  - `doc_acl.effect` 有 CHECK 约束（`allow` / `deny`）
  - `chunks` 表（pgvector 方案）同时建：`acl_tags TEXT[] NOT NULL`、GIN 索引、`tsv` 列 + zhparser 配置、`is_latest` 部分索引
- **验收**：`pytest tests/unit/test_doc_acl_order.py -q`（验证 §3.1.2 的五步裁决顺序，含 deny 优先与 expires_at 过期）
- **DoD**：裁决顺序为单一实现，无第二处重复代码

**GATE-0**：`alembic upgrade head` 可重复执行；`/health/deps` 全绿；CI 中 `ruff` + `mypy` 通过。

---

### B1 · 知识接入

#### WU-05 · 解析器
- **产出**：`app/ingest/parsers/{pdf,md,txt,excel,docx,web,ocr}.py`
- **接口**：`parse_<type>(path) -> list[ParsedBlock]`，`ParsedBlock` 含 `text / page / block_type / section_path`
- **关键动作**：
  | 格式 | 实现要点 |
  |---|---|
  | PDF | `pymupdf` 抽文本；表格用 `pdfplumber`；无文本层 → 走 OCR 分支 |
  | MD | 按标题层级生成 `section_path`；代码块保留 |
  | TXT | 编码嗅探（UTF-8 / GBK / GB18030） |
  | XLSX | `openpyxl`；每 sheet 转 markdown 表；**单元格值 MUST 带表头** |
  | XLS | 同上，注意 `.xls` 需 `xlrd` 兼容路径 |
  | DOCX | `python-docx`，保留标题样式 |
  | HTML | `selectolax` 去 script/style |
- **MUST NOT** 引入 `langchain` / `llama-index` 的解析器
- **验收**：`pytest tests/unit/test_parsers.py -q`（含 5 个真实样本文件，断言块数与关键文字命中）
- **DoD**：5 种必需格式 + 3 种扩展格式全部产出 `ParsedBlock`；异常文件不抛未捕获异常，返回 `ParseError`

#### WU-06 · 清洗器
- **产出**：`app/ingest/cleaner.py`
- **接口**：`detect_chrome(blocks, page_count) -> set[str]`、`clean_blocks(blocks) -> list[Block]`
- **关键动作**：
  - **三信号投票制**，**≥ 2 条信号命中才剔除**（单一信号会误伤正文）：
    | 信号 | 判据 |
    |---|---|
    | 跨页重复 + 位置 | 在多页的相同位置出现、且文本高度相似 |
    | 位置 + 短文本 | 位于页面顶/底 10% 区域且长度 < 40 字 |
    | 关键词 | 命中页码正则、版权、导航链接、"第 N 页 共 M 页" |
  - **MUST** 保留表头与章节编号（清洗最容易误删的内容）
  - **MUST NOT** 用"重复率 > 阈值就删"的单条件判定
- **验收**：`pytest tests/unit/test_cleaner.py -q`（构造 10 页、每页相同页眉的样本，断言页眉被删且正文 0 丢失）
- **DoD**：清洗日志记录被删文本与原因，便于回溯

#### WU-07 · 向量化封装
- **产出**：`app/llm/embedding.py`
- **接口**：`embed_batch(texts) -> list[list[float]]`、`embed_chunks(gateway, chunks, concurrency=4)`
- **关键动作**：
  - 批大小 `EMBED_BATCH=32`；重试用 `tenacity`（指数退避，最多 3 次）
  - 超过 `EMBED_MAX_CHARS` 的文本 **MUST** 截断并告警（不静默）
  - 文本归一化：去多余空白、统一全角半角
- **验收**：`pytest tests/unit/test_embedding.py -q`（含超长文本截断、重试、并发不串序）
- **DoD**：同一批文本两次调用结果一致（可复现）

#### WU-08 · 分块与入库
- **产出**：`app/ingest/chunker.py`、`app/ingest/embedder.py`
- **接口**：`chunk_document(blocks, *, parent_size=1000, child_size=280, overlap=64)`、`embed_chunks()` 写库
- **关键动作**：
  - 父子双层：parent 供生成上下文，child 供检索
  - **★ INV-08**：写库前断言 `acl_tags` 非空；为空则抛 `MissingAclTagsError` 并终止该文档入库
  - 表格块 **MUST** 整块独立（不跨块切分），并转 markdown
  - `content_hash` 逐 chunk 计算，用于幂等
- **验收**：
  - `pytest tests/unit/test_chunker.py -q`（重叠窗口、表格不切分、parent/child 完整性）
  - `pytest tests/integration/test_embed_acl.py -q`（**断言库中无任何 chunk 的 acl_tags 为空**）
- **DoD**：重复入库同一文档不产生重复 chunk（幂等）

#### WU-09 · 异步解析任务与进度上报
- **产出**：`app/ingest/tasks.py`、`app/api/v1/documents.py`（upload / status）
- **关键动作**：
  - Celery 任务按文件大小分流（`pick_queue`）：小文件默认队列，大文件 `heavy` 队列
  - `Stage` 枚举与权重映射到 `progress`（0–100），**MUST** 单调不减
  - 失败重试上限 3 次，耗尽 → 死信队列 + `documents.status='FAILED'` + 失败原因入库
  - 前端轮询 `/documents/{id}/status` 返回 `{status, stage, progress, error}`
- **验收**：`pytest tests/integration/test_ingest_progress.py -q`（模拟各阶段，断言 progress 单调不减且终态正确）
- **DoD**：上传 100MB PDF 时，进度条可持续更新，不出现长时间无变化（每阶段至少上报一次）

#### WU-10 · 外部源接入与同步
- **产出**：`app/api/v1/sources.py`、`app/api/v1/webhooks.py`、`app/ingest/sync.py`（同步部分）
- **关键动作**：
  - 支持源类型：Confluence / Wiki / 企业网盘 / 自建 API
  - Webhook **MUST** 校验签名（`verify_signature`），失败返回 401 且**不区分**"签名错"与"源不存在"
  - Webhook 只做 **入队**，解析在 Celery 中做（避免超时）
  - 手动同步与 Webhook 走同一条 `sync_document` 路径
  - **MUST** 遵守第三方平台 API 协议（限速、User-Agent、分页）
- **验收**：`pytest tests/integration/test_webhook.py -q`（含签名错误、重放、乱序）
- **DoD**：同步任务面板能看到上次同步时间、成功/失败数、暂停状态

#### WU-11 · 会话与消息存储
- **产出**：`alembic/versions/0003_sessions.py`、`app/api/v1/sessions.py`
- **关键动作**：
  - `messages` 保存 `role / content / citations(jsonb) / usage / created_at`
  - **MUST** 按 `user_id` 隔离查询；跨用户访问返回 `NOT_FOUND`
  - 会话标题自动生成（用 `LLM_MODEL_SMALL`，异步不阻塞主流程）
- **验收**：`pytest tests/integration/test_session_isolation.py -q`
- **DoD**：A 用户的 session_id 被 B 用户访问时，返回 404 而非 403

**GATE-1**：上传 5 种格式文件均能走完解析→分块→入库；库中无 `acl_tags` 为空的 chunk；进度可观测；`tests/integration/test_ingest_*.py` 全绿。

---

### B2 · 检索与生成

#### WU-12 · 拒答闸门
- **产出**：`app/retrieval/gate.py`
- **依赖**：WU-13（阈值来自标定）
- **接口**：`decide(kept_chunks, stats, *, score_threshold, min_chunks, min_content_len) -> GateResult`
- **关键动作**：
  - **★ INV-02**：四层判定全实现 —— ① `kept` 为空 ② `max_score < score_threshold` ③ `total_content_len < min_content_len` ④ 通过
  - **MUST NOT** 给 `score_threshold` 设默认值；必须由配置注入（防止有人漏配后静默用上 0.35）
  - `reason` **MUST** 取自 `RefuseReason` 枚举
- **验收**：
  - `pytest tests/unit/test_gate.py -q`
  - 边界用例：`max_score` 恰好等于阈值 → 通过；低于 0.001 → 拒答
- **DoD**：闸门为纯函数，无 IO，可被单测完全覆盖

#### WU-13 · 混合检索、Rerank 与阈值标定
- **产出**：`app/retrieval/hybrid.py`、`app/retrieval/reranker.py`、`app/retrieval/query_rewrite.py`、`scripts/calibrate_threshold.py`
- **关键动作**（三步，必须全做）：
  1. **混合检索**：向量 + BM25，用 RRF 融合（`RRF_K=60`），权重来自 `RetrievalParams`
  2. **Rerank**：`bge-reranker-v2-m3`，输出分数 **MUST** 归一化到 0–1（不同模型分数尺度不同，直接比会错）
  3. **标定**：跑 `scripts/calibrate_threshold.py`，在 `eval/positive.jsonl` + `negative.jsonl` 上扫描，输出 F1 最优点，写回配置并追加 `# calibrated: YYYY-MM-DD`
- **★ INV-03**：**MUST NOT** 手工填 `RERANK_THRESHOLD`
- **验收**：
  - `pytest tests/unit/test_rrf.py -q`
  - `python scripts/calibrate_threshold.py --out .env.threshold --report` → 报告含 `refuse_acc >= 0.95` 且 `fpr <= 0.05`
- **DoD**：标定报告作为交付物归档；阈值写入 `.env.example` 的注释说明来源

> **注意**：本 WU 是全项目**最需要耐心**的一环。若标定结果 `refuse_acc` 与 `fpr` 无法同时达标，**不要调整目标值**——先检查分块质量与 rerank 分数归一化是否正确。

#### WU-14 · RAG 主链路
- **产出**：`app/generation/pipeline.py`、`app/generation/context_builder.py`、`app/generation/prompts.py`
- **依赖**：WU-12、WU-13。校验环节本 WU 内先接一个**恒通过**的占位实现（`validate_answer` 返回 `ok=True`），等 WU-15 替换为真实实现。**MUST NOT** 因占位实现而省略 `generate_with_guard` 的调用结构
- **接口**：`run_rag(*, gateway, store, reranker, question, kb_ids, user_subjects, history, top_k, rerank_top_n)`
- **关键动作**：
  - 顺序固定：改写 → 检索 → 重排 → 闸门 → 组装上下文 → 生成 → 校验 → 落库
  - **★ INV-01**：闸门在生成**之前**；`refuse` 分支 **MUST NOT** 调用 LLM
  - **★ INV-04**：Prompt 中**MUST NOT** 要求模型输出任何来源元数据
  - 上下文预算：按 `CONTEXT_MAX_TOKENS` 裁剪，顺序为「高相关在前」，被裁的片段 **MUST NOT** 参与引用编号
  - `RagTrace` 记录各阶段耗时与决策，用于埋点
  - 全部 Prompt 常量集中在 `prompts.py`，**MUST NOT** 散落在业务代码
- **验收**：
  - `pytest tests/integration/test_rag_pipeline.py -q`（正常 / 拒答 / 空库 / 单条候选 4 条路径）
  - 断言拒答路径下 `ModelGateway.chat` **未被调用**（用 mock 计数）
- **DoD**：`rag_trace` 可在日志中按 `request_id` 完整还原

#### WU-15 · 出口校验
- **产出**：`app/generation/validator.py`
- **接口**：`validate_answer(answer, contexts) -> ValidationResult`
- **关键动作**：
  - 校验 ① 角标编号范围 ② 有事实论断的句子是否带引用 ③ `[n]` 是否连续且无重复滥用
  - **★ INV-05**：越界编号 → `ok=False, reason="CITATION_OUT_OF_RANGE"`，触发重试
  - **MUST NOT** 在这种校验里判断文档名/页码——那是检索层的事（INV-04）
- **验收**：`pytest tests/unit/test_validator.py -q`（含 `[9]` 越界、无引用长句、正常通过）
- **DoD**：校验失败原因可枚举输出，供 `_retry_hint()` 生成重试提示

#### WU-16 · 引用两段式与元数据填充
- **产出**：`app/generation/pipeline.py` 中的 `_hydrate_parents`、`packages/core/src/citation.ts`
- **关键动作**：
  - **★ INV-04**：`citations` 数组的每个字段从检索结果取，**MUST NOT** 来自模型
  - 编号 `[n]` → `contexts[n-1]` 的映射在服务端完成；前端只做渲染
  - `doc_url` **MUST** 带页码锚点，点击可跳原文
  - 引用角标与来源卡**五端都必须渲染**，不做降级省略
- **验收**：
  - `pytest tests/integration/test_citation_hydration.py -q`（断言 citations 字段与 DB 中 chunk 元数据逐字段一致）
  - `pnpm --filter @kb/core test citation`
- **DoD**：任意引用的 `updated_at` 与文档实际更新时间一致

#### WU-17 · 密级判定
- **产出**：`app/security/classification.py`
- **接口**：`resolve_level(doc_meta, kb_meta, content_head) -> (Level, reason)`、`policy_for(level) -> LevelPolicy`
- **关键动作**：
  - 判定优先级（**只升不降**）：源系统声明 > 人工标注 > 知识库默认 > 规则推断（关键词/正则）> 保守默认（`INTERNAL`）
  - **「只升不降」的含义**：后续任何一步的判定只能把密级**抬高**，**MUST NOT** 把已有结论降低。规则推断不得推翻源系统声明
  - **★ INV-15**：`LevelPolicy.ingestable` 对 L3 为 `False`
  - **★ INV-15 落地**：在 `ingest/tasks.py` 的 embed 环节之前检查，为 False 则把文档置 `SKIPPED` 并写审计，**MUST NOT** 静默跳过
  - `LevelPolicy` 字段 **MUST** 与 §6.2 表逐列对应：
    `ingestable`、`allow_external_model`、`mask_on_ingest`、`log_mode`、`citation_mode`、`allow_export`、`cache_ttl`
- **验收**：`pytest tests/unit/test_classification.py -q`；`pytest tests/integration/test_level_skip.py -q`（断言 L3 文档入库后向量库 0 条）
- **DoD**：密级判定结果与判定依据（命中的规则名 / 显式标注来源）一并落库，可审计复核

#### WU-18 · DLP 脱敏与恢复
- **产出**：`app/security/dlp.py`
- **接口**：`detect(text) -> list[Finding]`、`mask_for_ingest(text, doc_id) -> (masked, mapping)`、`restore_on_output(text, mapping, user) -> str`、`guard_output(answer, policy) -> (answer, changed)`
- **关键动作**：
  - 识别类型：手机号、身份证（含校验位）、银行卡（Luhn）、邮箱、`sk-`/`AKIA` 类密钥、内网 IP、员工工号
  - **★ INV-16**：`restore_on_output` 必须校验 `user` 对该文档的权限，无权限保持脱敏
  - **★ INV-16**：入参脱敏与出参恢复共用同一份 `mapping`，`mapping` 随 chunk 持久化
  - 出口脱敏 **MUST** 就地打码（`[已脱敏]`），**MUST NOT** 整段拒绝（上游 §17.8 第 13 行）
- **验收**：`pytest tests/unit/test_dlp.py -q`（每类敏感信息正负样本各 3 条）；`pytest tests/security/test_dlp_leak.py -q`
- **DoD**：脱敏后文本仍能通过 embedding 拿到合理相似度（不能脱敏到失去语义）

**GATE-2**：`run_eval.py` 在评估集上：拒答准确率 ≥ 0.95、误拒率 ≤ 0.05、Citation Precision ≥ 0.95、Faithfulness ≥ 0.95；L3 文档实测 0 条入库。

---

### B3 · 隔离与服务

#### WU-19 · 检索隔离：主体计算与条件下推
- **产出**：`app/security/acl.py`（主体计算部分）、`app/retrieval/store/base.py`
- **接口**：`compute_user_subjects`、`compute_doc_acl_tags`、`get_user_subjects`、`build_acl_expr`、`assert_visible`
- **关键动作**：
  - 主体集合 **MUST** 含：`user:<id>`、**所有祖先部门 + 所有子孙部门 `dept:<id>`（传递闭包）**、所有用户组、所有角色、`public`
    - ★ **上下两个方向缺一不可**（DEC-22/Q3 冻结）：漏祖先 → 下级看不到上级制度；漏子孙 → **上级看不到下级文档**
    - **MUST NOT** 横向展开到兄弟部门 —— 兄弟部门之间不可见（契约 §3.5 用例 9）
    - **MUST NOT** 产出 `level:<n>` 形式的 subject —— 密级只走 G1 的数值比较
  - 祖先与子孙 **MUST** 各自用一条 `departments.path` 前缀查询取出（不要递归、不要逐层）
  - **★★ 文档侧反向**：`compute_doc_acl_tags` **MUST NOT** 对 `dept:` 做祖先展开（契约 §3.3(2)）
  - **★ 文档侧互斥**：`public` 仅当切片无其他主体标签时才写入（契约 §3.3(3)）
  - **★ INV-07**：`hybrid_search_secure` 的 `user_subjects` 为**必填关键字参数**；`store/base.py` 中不存在不带该参数的检索方法
  - 空 `user_subjects` → 短路返回 `[]` 并记错误日志（正常不应发生，MUST 恒含 `public`）
  - 主体集合规模超 `SUBJECT_EXPANSION_SOFT_LIMIT`(300) → **只 WARN + 打点，MUST NOT 裁剪集合**
- **验收**：
  - `pytest tests/unit/test_user_subjects.py -q`（含跨部门、多组、**上下级双向**、密级上限）
  - `pytest tests/security/test_acl_matrix.py -q`（**契约 §3.5 的 10 条用例，一条不能少**）
  - `pytest tests/security/test_acl_pushdown.py -q`（用 mock 断言查询表达式里确实带上了 ACL 条件，而不是在结果层过滤）
  - `pytest tests/security/test_sample_org_scale.py -q`（合成 200 节点组织树，断言主体集合规模符合 §3.3 的核验结论）
- **DoD**：全仓搜索 `hybrid_search(` 只有 `hybrid_search_secure` 一个可用入口；
  全仓搜索 `DOC_TAG_ANCESTOR_EXPANSION` 必须为 `False`，且 `enforce()` 有对应断言

#### WU-20 · 向量库适配层
- **产出**：`app/retrieval/store/{pgvector,milvus}.py`
- **关键动作**：
  - 两套实现必须**行为一致**（同一组输入返回同一组 chunk_id，允许顺序差异）
  - 抽象出共同契约测试 `tests/integration/test_store_contract.py`，两套实现都要跑
  - `is_latest=true` 过滤 **MUST** 下推
  - **★ INV-26**：连接失败 / 超时（> 3s）**MUST** 抛 `VectorStoreUnavailable`，**MUST NOT** 返回空列表伪装成"没有检索到"
- **验收**：`pytest tests/integration/test_store_contract.py -q --backend=pgvector` 与 `--backend=milvus` 双双通过
- **DoD**：切换 `VECTOR_BACKEND` 不改动任何上层代码

#### WU-21 · 权限缓存与 fail-closed
- **产出**：`app/security/acl.py`（缓存部分）
- **接口**：`_user_acl_key`、`get_user_subjects_guarded`、`invalidate_user_acl_cache`、`invalidate_on_acl_change`
- **关键动作**：
  - **★ INV-10**：key 固定为 `acl:u:{user_id}:v{acl_version}`；`_user_acl_key` 为唯一构造函数
  - **★ INV-09**：`get_user_subjects_guarded` 的 except 分支 **MUST** 抛出，**MUST NOT** 返回空集合
  - **★ INV-13**：鉴权失败路径统一走 `chat_with_timing_alignment`，耗时补齐到正常区间 ±10%
  - `invalidate_on_acl_change` 覆盖面：用户角色变更、用户组增删成员、部门调动、`doc_acl` 变更、`kb_members` 变更、离职
- **验收**：
  - `pytest tests/security/test_acl_cache_invalidation.py -q`（改权限 → 立刻生效，不等 TTL）
  - `pytest tests/security/test_fail_closed.py -q`（mock Redis 抛异常 → 断言**拒绝检索**而非放行）
- **DoD**：`tests/security/test_side_channel.py` 断言无权与不存在的响应耗时差 < 10%

#### WU-22 · 缓存体系（含权限指纹）
- **产出**：`app/security/acl.py`（`answer_cache_key`、`semantic_cache_scope`、`lookup_semantic_cache`）
- **关键动作**：
  - **★ INV-11**：`answer_cache_key` **MUST** 含 `hash(sorted(user_subjects))`
  - **★ INV-11**：语义缓存 **MUST** 用 `semantic_cache_scope` 分域（同域才可比）
  - **★ INV-28**：与 `ratelimit_key`（WU-28）**MUST** 是两个独立函数，**MUST NOT** 共用构造逻辑
  - 缓存不可用时 **MUST** 直连后端（降级但不报错）
- **验收**：
  - `pytest tests/security/test_cache_acl_leak.py -q`（A 问过的问题，B 语义相近的提问 **MUST NOT** 命中）
  - `pytest tests/unit/test_cache_key_rules.py -q`（断言两函数 key 不含对方特有字段）
- **DoD**：缓存命中率与省下的 token 数可观测

#### WU-23 · 请求链路收敛与越权不可区分
- **产出**：`app/api/v1/chat.py`（含 `/chat` 与 `/chat/sync`）
- **关键动作**（顺序固定，**MUST NOT** 调整）：
  ```
  1. 鉴权 → current_user
  2. ★ filter_authorized_kbs(db, user, body.kb_ids)   # INV-14，静默裁剪
  3. 取权限主体 get_user_subjects_guarded             # INV-09 fail-closed
  4. 缓存查询（key 含权限指纹）                        # INV-11
  5. 检索（ACL 下推）                                 # INV-07
  6. 兜底断言 assert_visible                          # 防御纵深
  7. 密级上限 min(用户可读, 工具允许)                   # INV-15
  8. 生成 / 校验 / 引用填充
  9. 审计 + 埋点
  ```
  - `kb_ids` 被裁剪到空 → 直接拒答（不报错、不提示"无权"）
  - 无权与不存在的响应 **MUST** 完全同构（状态码、文案、字段数、耗时）
- **验收**：
  - `pytest tests/security/test_acl_matrix.py -q`（§8.3 的 14 个用例）
  - `pytest tests/security/test_side_channel.py -q`
- **DoD**：越权矩阵全绿且进 CI 门禁

#### WU-24 · Agent 工具注册与执行器
- **产出**：`app/agent/registry.py`、`app/agent/executor.py`、`app/agent/tools/kb_search.py`
- **关键动作**：
  - `ToolSpec` 含：`name / schema / required_permission / handler / max_top_k / max_input_level / visible_when`
  - **★ INV-12**：`execute_tool` 的五步固定为：① 工具权限 ② **参数净化（`kb_scope` 求交集）** ③ 参数上界 clamp ④ 密级前置 ⑤ 执行 + 审计
  - **★ INV-12**：`kb_scope` 净化后为空 → **静默**回落到用户默认可见范围，**MUST NOT** 报错
  - 工具返回给模型的是「带编号的片段」，**MUST NOT** 返回全文
  - 所有工具 handler **MUST** 经 `execute_tool` 调用，**MUST NOT** 被直接引用
- **验收**：
  - `pytest tests/security/test_tool_param_injection.py -q`（构造诱导 prompt，断言模型给的越权 `kb_scope` 被裁掉）
  - `pytest tests/unit/test_tool_registry.py -q`
- **DoD**：新增一个工具只需注册 `ToolSpec`，不改 executor

#### WU-25 · Agent 接地校验与边界守护
- **产出**：`app/agent/grounding.py`
- **接口**：`verify_grounding(answer, snippets) -> GroundingResult`、`guard_no_retrieval(answer, tool_calls) -> GuardResult`
- **关键动作**：
  - `guard_no_retrieval`：若本轮未发生检索却输出了事实性陈述 → **拦截**（return refuse）
  - `verify_grounding`：抽取答案中的数字、日期、专有名词、金额，逐个在 snippets 中查证；未命中比例 > 阈值 → 失败
  - **★ INV-06**：失败 → 重试 1 次 → 仍失败 → **拒答**（不降级输出）
- **验收**：`pytest tests/unit/test_grounding.py -q`（含"数字被篡改"、"数字不存在于片段"、"正确接地"）
- **DoD**：接地校验失败率作为独立指标上报，与拒答率分开统计

#### WU-26 · 审计哈希链
- **产出**：`alembic/versions/0004_audit.py`、`app/security/audit.py`、`app/api/v1/admin.py`
- **关键动作**：
  - **★ INV-17**：迁移中含 `REVOKE UPDATE, DELETE ON audit_log FROM app_user`
  - 每条记录含 `prev_hash` 与 `record_hash`（`compute_hash` 用确定性序列化 `_canonical`）
  - **★ INV-18**：四类事件全部覆盖 —— 登录 / 权限变更 / 问答 / 越权尝试
  - 问答审计 **MUST** 记录：谁、何时、问了什么、检索了哪些 doc_id、返回了哪些 citation、是否拒答
  - 审计写入 **MUST** 异步、失败不阻塞主链路，但失败本身要告警
- **验收**：
  - `pytest tests/integration/test_audit_coverage.py -q`（四类事件逐个断言）
  - `python scripts/verify_audit_chain.py --start 1 --end 10000` → 输出链完整
  - 篡改一条记录后重跑校验 → **MUST** 定位到该条
- **DoD**：审计可按 actor / action / 时间范围查询；导出为 CSV

**GATE-3**：越权矩阵 14 用例全绿；侧信道一致性测试通过；`verify_audit_chain` 通过；工具参数注入测试通过。

---

### B4 · 一致性、成本、部署、流式

#### WU-27 · 同步一致性与删除传播
- **产出**：`app/ingest/sync.py`（一致性部分）、`scripts/consistency_check.py`
- **关键动作**：
  - **★ INV-21**：幂等判据 = `compute_doc_hash + 外部版本号` 比较，**MUST NOT** 用时间戳
  - **★ INV-20**：删除顺序固定为「向量库删除 → PG 软删标记」，**MUST NOT** 反向
  - 更新传播：`apply_doc_update` 使用"新 chunk 先写入 → 再删旧 chunk"顺序，避免中间出现空文档
  - **旧 chunk 必须物理删除**，**MUST NOT** 只标 `is_latest=false`。理由：新旧内容会同时出现在一次检索结果里，模型会把已废止的条款也答出来
  - `is_latest` 仅用于"同一文档多版本并存"的场景（如需要保留历史版本供审计），且此时检索查询 **MUST** 带 `is_latest=true` 过滤（WU-20）
  - 权限变更传播：`documents.acl_version` +1 → 触发 chunk 的 `acl_tags` 批量重算
  - 每日对账任务 `daily_reconcile`：比对 PG 与向量库的 chunk_id 集合，输出差异报告
- **验收**：
  - `pytest tests/integration/test_consistency.py -q`（含删除中途失败、更新中途失败、乱序 Webhook）
  - `python scripts/consistency_check.py --sample 0.05` → 差异率 0
- **DoD**：一致性检查有独立定时任务与告警

#### WU-28 · 限流、签名与配额治理
- **产出**：`app/security/ratelimit.py`、`app/security/signature.py`、`app/cost/quota.py`、`alembic/versions/0005_token_usage.py`
- **关键动作**：
  - **★ INV-27**：`ratelimit_key(user_id, route)` **MUST NOT** 含权限字段、**MUST NOT** 与缓存 key 共用函数
  - 分层限流：用户级 / IP 级 / 全局级；**MUST** 用 Redis + Lua 实现**滑动窗口**（固定窗口有临界双倍问题）
  - 超限返回 429 + `Retry-After`
  - 模型并发 **MUST** 排队而非直接拒绝（直接拒绝会让用户重试，反而加剧拥堵，见 §6.4 第 9 行）
  - 请求签名：HMAC-SHA256 + 时间戳 + nonce；nonce 在 Redis 去重（防重放）
  - 配额四级降级（§6.6）：`<80%` 正常 → `≥100%` 小模型 → `≥150%` 仅缓存 → `≥200%` 拒绝
  - `token_usage` 记录按 user / dept / kb / model 可聚合
- **验收**：
  - `pytest tests/security/test_ratelimit.py -q`（含"权限变更后额度不重置"）
  - `pytest tests/unit/test_signature.py -q`（含重放、时间偏移、签名篡改）
- **DoD**：单用户 token 用量与费用可在管理后台查看

#### WU-29 · 网络隔离、出域清单与异常兜底
- **产出**：`deploy/`、`app/main.py`（异常处理器注册）、异常兜底分支
- **关键动作**：
  - **★ INV-19**：启动时校验 `LLM_BASE_URL` / `EMBED_BASE_URL` / **`RERANK_BASE_URL`** 均在内网白名单；存在 L2+ 文档时任一不符合 → **启动失败**
  - **★ INV-26**：向量库异常 → `refused(reason=VECTOR_STORE_DOWN)`
  - 实现 §6.4 的 18 行兜底矩阵，每行 **MUST** 有对应测试
  - `deploy/nginx/nginx.conf` **MUST** 含：`proxy_buffering off`、`proxy_cache off`、`X-Accel-Buffering: no`、`proxy_read_timeout 300s`
  - 灰度开关：从 §6.4 与上游 §17.9 提取的全部开关集中到 `settings.feature_flags`
- **验收**：
  - `pytest tests/integration/test_fallback_matrix.py -q`（逐行覆盖 18 条）
  - `bash deploy/scripts/smoke_test.sh`
- **DoD**：18 条兜底全部有可复现的测试；SSE 经 Nginx 代理后逐字输出正常

#### WU-30 · SSE 服务端
- **产出**：`app/api/v1/chat.py`（SSE 部分）
- **关键动作**：
  - **★ INV-23**：`HEARTBEAT_INTERVAL = 15`，空闲时以 `: heartbeat <ts>` 注释帧发送
  - **★ INV-22**：每帧带递增 `id:`；事件写入 Redis Stream（TTL 10 分钟）供续流重放
  - 支持 `Last-Event-ID` 请求头；超窗口返回 `error{code:"RESUME_EXPIRED", retryable:true}`
  - 时序约束按 §4.3.2 全部实现（首帧 meta、终帧唯一、refused 不与 delta 共存等）
  - LLM 首 token 超时 15s；生成中途报错 → 保留已输出 + `error` 终帧
- **验收**：
  - `pytest tests/integration/test_sse_protocol.py -q`（逐条断言 §4.3.2 时序约束）
  - 手工：`curl -N` 观察 15s 心跳；断开后带 `Last-Event-ID` 重连续传
- **DoD**：续流不重跑检索与生成（用 mock 断言调用次数）

**GATE-4**：一致性检查 0 差异；限流与签名测试全绿；18 条兜底矩阵全绿；SSE 协议测试全绿。

---

### B5 · 前端

#### WU-31 · Next.js BFF 与密钥隔离
- **产出**：`apps/web/app/api/**/route.ts`、`packages/core/src/api/client.ts`
- **关键动作**：
  - **★ INV-25**：三类密钥（LLM Key / 向量库凭证 / DB 串）**MUST NOT** 出现在任何 `NEXT_PUBLIC_*` 或客户端 bundle
  - 前端 **MUST** 只访问同源的 `/api/*`（Route Handler），由 BFF 携带服务端凭据转发
  - Server Component 中读密钥的模块 **MUST** 加 `import 'server-only'`
  - 401 处理：无感续期（refresh 成功后重放原请求 1 次，仍失败则跳登录）
- **验收**：
  - `pnpm --filter web build && grep -rE "sk-|service_role|postgresql\\+asyncpg" apps/web/.next/static/ || echo CLEAN`
  - `pytest tests/unit/test_no_secret_leak.py -q`（扫描构建产物）
- **DoD**：构建产物扫描 CLEAN 进 CI 门禁

#### WU-32 · 前端 SSE 消费与断点续流
- **产出**：`packages/core/src/api/chat.ts`（分端适配器）、`packages/core/src/hooks/useChatStream.ts`、`packages/core/src/types/events.ts`
- **关键动作**：
  - 三套适配器：
    | 端 | 机制 |
    |---|---|
    | Web / H5 / 桌面 | `fetch` + `ReadableStream` 手工解析（**MUST NOT** 用 `EventSource`，它不支持 POST body） |
    | 小程序 | `wx.request` + `enableChunked` + `onChunkReceived` |
    | App | `XMLHttpRequest` + `onprogress` |
  - 三套适配器 **MUST** 产出同一个 `SseEvent` 联合类型
  - 断连：指数退避重连，携带 `Last-Event-ID`
  - **★ INV-22**：续流后 **MUST** 从断点追加，**MUST NOT** 重建整个消息
- **验收**：
  - `pnpm --filter @kb/core test chat-adapter`
  - 手工：捏造断开，断言续流后文本与不断开时**完全一致**
- **DoD**：三端适配器共用同一套断言测试

#### WU-33 · 前端错误兜底与保留已输出
- **产出**：`packages/core/src/hooks/useChatStream.ts`（错误分支）、`apps/web/app/error.tsx`、`loading.tsx`、`not-found.tsx`
- **关键动作**：
  - **★ INV-24**：任何错误分支 **MUST** 只 append，**MUST NOT** reset 已显示的 delta
  - 错误分类映射到 §4.4 错误码表，`retryable=true` 才显示重试按钮
  - 拒答态渲染 **MUST** 与正常回答视觉区分（虚线告警态 + 补充文档/换说法/转人工三个动作）
  - 加载态、骨架屏、超时兜底（前端 30s 无新事件 → 提示）
- **验收**：`pnpm --filter web test`；手工覆盖 8 类错误码
- **DoD**：任一错误下，用户已看到的内容不会消失

#### WU-34 · Agent Loop 与工具路由
- **产出**：`app/agent/loop.py`
- **关键动作**：
  - Loop 收尾条件：模型返回无 tool_call 且非空中断
  - **★ INV-01**：若本轮无任何 `kb_search` 调用却要输出事实 → `guard_no_retrieval` 拦截，**强制**再检索一次或拒答
  - 工具路由：模型只能选"用哪个工具"，**MUST NOT** 允许"不查就答"
  - `visible_tools(user, ctx)` 按权限与密级过滤可暴露的工具
- **验收**：`pytest tests/integration/test_agent_loop.py -q`（含诱导不检索、多轮工具调用、工具循环超限）
- **DoD**：Agent 模式下拒答率与固定管线的差异 < 3%

#### WU-35 · 多轮记忆与上下文窗口治理
- **产出**：`app/agent/memory.py`、`app/agent/budget.py`
- **关键动作**：
  - 分层：最近 N 轮原文 + 历史摘要 + 当前检索片段，三段各有预算
  - 摘要触发：token 超阈值时异步摘要；**MUST NOT** 同步阻塞回答
  - 指代消解在 `rewrite_query` 中完成（"那二线城市呢？"→ 补全主语）
  - **裁剪顺序固定为**：① 先裁历史（最早的轮次）② 再减检索片段条数 ③ **绝不截断片段正文**
  - **MUST NOT** 静默丢弃当前轮的检索片段（宁可拒答，不可给无根据的回答）
  - **历史对话只用于理解意图（指代消解），MUST NOT 作为事实依据**。否则上一轮的错误数值会被当作事实继续推理 —— 事实只能来自本轮检索片段
- **验收**：`pytest tests/integration/test_multi_turn.py -q`（用 `eval/multi_turn.jsonl` 50 条）
- **DoD**：多轮指代准确率 ≥ 0.90

#### WU-36 · 前端页面与路由结构
- **产出**：`apps/web/app/**`
- **页面清单**：

  | 路由 | 页面 | 关键要求 |
  |---|---|---|
  | `/chat` | 对话首页 | 空态引导 + 知识库选择器 |
  | `/chat/[sessionId]` | 对话详情 | 流式、引用角标、来源抽屉 |
  | `/knowledge` | 知识库列表 | 按权限过滤 |
  | `/knowledge/[kbId]/documents` | 文档管理 | 上传、进度、删除、重试 |
  | `/knowledge/[kbId]/members` | 成员管理 | 授权变更 |
  | `/sources` | 同步任务面板 | 上次同步、失败数、暂停/恢复 |
  | `/history` | 问答历史 | 搜索、跳回原文 |
  | `/admin/audit` | 审计查询 | 四类事件筛选、导出 |
  | `/admin/usage` | 用量与成本 | 按用户/部门聚合 |

- **关键动作**：三栏 → 两栏 → 单列的响应式降级（Web / H5 / 桌面共用一套）
- **验收**：`pnpm --filter web build`；`pnpm --filter web test:e2e`（Playwright 走通"提问→引用→跳原文"）
- **DoD**：九个页面均可访问，权限不同看到的导航项不同

#### WU-37 · 五端渲染栈
- **产出**：`apps/mini/`（Taro）、`apps/mobile/`（Expo）、`apps/desktop/`（Tauri）
- **关键动作**：
  - 严格按 §6.7 五端差异矩阵实现；**MUST NOT** 在 `@kb/core` 里写 `Platform.select`
  - 小程序：导航栏避让胶囊按钮；单包 ≤ 2MB；文件上传走微信会话文件；登录走 `unionid → 企业账号` 绑定
  - App：底部胶囊标签栏；安全区适配（62 / 34）；本地 SQLite 缓存最近问答
  - 桌面端：Tauri 壳 + 原生菜单；仅桌面端开放批量上传与原文对照
- **验收**：三端各自 `build` 通过；真机冒烟（小程序 / iOS / Android 至少各 1 台）
- **DoD**：五端登录链路均可用；引用角标与来源卡在五端均可见

**GATE-5**：五端可跑；构建产物无密钥；续流一致性测试通过；九页面可用。

---

### B6 · 评估与发布

#### WU-38 · 评估流水线与 CI 门禁
- **产出**：`backend/tests/eval/`、`scripts/run_eval.py`、`.github/workflows/eval.yml`
- **关键动作**：
  - 三层评估：检索层（Recall@5 / MRR / NDCG@10）、生成层（Faithfulness / Answer Relevancy）、边界层（拒答准确率 / 误拒率）
  - 引用评估：`compute_citation_precision` + 逐句 LLM 判定
  - **CI 门禁**：任一指标低于 §8.4 阈值 → PR 红
  - 评估结果 **MUST** 存档（JSON + 报告），支持按版本对比
- **验收**：`python scripts/run_eval.py --layer all --report out/` → 报告含全部指标与达标判定
- **DoD**：故意让阈值失效，CI **MUST** 失败（验证门禁真的在起作用）

#### WU-39 · 可观测与业务埋点
- **产出**：`app/observability/{tracing,metrics,business}.py`
- **关键动作**：
  - 技术指标：各阶段延迟 P50/P95、错误率、重连率、重试率
  - 业务指标：检索命中率、拒答率、误拒率（抽样）、引用点击率、追问率、负反馈率
  - trace **MUST** 关联 `request_id`，可从日志一路追到 LLM 调用
  - **告警分级**：权限服务不可用 = P0；向量库不可用 / LLM 全挂 = P1；其余 P2
- **验收**：制造一次向量库故障，Prometheus 告警 **MUST** 触发
- **DoD**：Grafana 面板含 §8.4 全部指标

#### WU-40 · 灰度与发布
- **产出**：发布流程、灰度开关、回滚脚本
- **关键动作**：
  - 灰度维度：按用户 ID 尾号 / 部门 / 知识库
  - 阶段：内部 10 人 → 单部门 50 人 → 全量
  - **回滚须在 5 分钟内可完成**，且回滚不丢数据
  - 每阶段有明确的**继续/回滚判据**（指标阈值 + 人工确认）
- **验收**：完整走一遍"发布 → 触发阈值 → 回滚"演练
- **DoD**：演练记录归档

**GATE-6**：评估门禁接入 CI；P0/P1 告警可触发；灰度回滚演练通过。

## 6. 决策表

> **本节是所有分支逻辑的唯一依据。** 写 `if/else` 前 **MUST** 先在此查到对应行。
> 若发现代码需要的分支不在此表 → 停下来，把缺的行补进本节，再写代码。
> **MUST NOT** 在代码中出现本表没有的分支。

### 6.1 拒答闸门（`gate.decide`）

| 序 | 条件 | action | reason | 是否调 LLM |
|---|---|---|---|---|
| 1 | `user_subjects` 为空 | refuse | `ACL_UNAVAILABLE` | ❌ |
| 2 | `kept_chunks` 为空 | refuse | `NO_RELEVANT_CONTEXT` | ❌ |
| 3 | `max_score < score_threshold` | refuse | `LOW_RELEVANCE` | ❌ |
| 4 | `sum(len(content)) < min_content_len` | refuse | `INSUFFICIENT_CONTENT` | ❌ |
| 5 | 以上均否 | answer | `OK` | ✅ |

**阈值来源**：`score_threshold` ← `RERANK_THRESHOLD`（标定产物）；`min_chunks` ← 配置；`min_content_len` ← 30（可配置）。
**MUST NOT** 在函数签名里给这三个参数默认值。

### 6.2 密级 → 行为

| 密级 | `ingestable` | 存储 | `allow_external_model` | `mask_on_ingest` | `log_mode` | 引用原文 `citation_mode` | `allow_export` | `cache_ttl` |
|---|---|---|---|---|---|---|---|---|
| **L0 公开** | ✅ | 主索引 | ✅ | ❌ | `full` | `full` | ✅ | 3600 |
| **L1 内部** | ✅ | 主索引 | ❌ 仅内网 | ❌ | `redacted` | `full` | ❌ | 3600 |
| **L2 敏感** | ✅ | 主索引 + `level=2` 过滤位 | ❌ 仅内网 | ✅ | `redacted` | `masked` | ❌ | 600 |
| **L3 涉密** | ❌ **默认不入库** | 独立索引（白名单审批，与主系统零共享） | ❌ 不调模型 | ✅ | `meta_only` | `hidden` | ❌ | 0 |

**L3 落地约束（MUST）**：
```
① ingestable=False → 文档置 SKIPPED，写审计，向量库 0 条
② 若确需涉密库：独立 collection + 独立 LLM 端点 + 独立网络域 + 白名单用户 + 独立审计
③ 用户密级许可（clearance）与 ACL 是两个正交条件，必须同时满足
④ 检索时密级作为第二维下推：`AND level <= :max_level`
```

### 6.3 权限裁决（`compute_doc_acl_tags` / `assert_visible`）

**前置必要条件（AND，任一不满足即无权，不可被后续规则救回）**

| 序 | 条件 | 结果 |
|---|---|---|
| P1 | 文档已删除 或 `is_latest = false` | 无权（INV-04） |
| P2 | `doc.level_rank > user.clearance` | 无权 —— 闸门 **G1** |
| P3 | `doc.kb_id ∉ user.authorized_kb_ids` | 无权 —— 闸门 **G2，知识库成员是必要条件**（DEC-22） |

**P1~P3 全过之后，按下列顺序裁决**

| 序 | 条件 | 结果 |
|---|---|---|
| 1 | 命中未过期的 `deny` 记录 | 无权（**G3 优先**） |
| 2 | 命中未过期的 `allow` 记录 | 有权 |
| 3 | `documents.visibility = 'public'` | 有权 |
| 4 | `documents.visibility = 'inherit'` | 递归到 `knowledge_bases` 的 `kb_members` |
| 5 | `chunk.acl_tags ∩ user_subjects = ∅` | 无权（**G4**） |
| 6 | 其余 | 有权 |
| 7 | `user_subjects` 为空集合 | **短路，不发起查询**（正常不应发生，MUST 恒含 `public`；INV-09） |
| 8 | 权限服务抛异常 | **拒绝检索**，返回 `ACL_UNAVAILABLE`（INV-09） |

### 6.4 异常兜底矩阵

> **每一行 MUST 有对应测试。** 18 行 = 18 个测试用例。

| # | 异常 | 系统动作 | 用户可见 | 错误码 / 事件 | 告警 | 可恢复 |
|---|---|---|---|---|---|---|
| 1 | LLM 首 token 超时（15s） | 中止，保留已输出 | "响应较慢，请重试" + 重试按钮 | `LLM_TIMEOUT` | P2 | ✅ |
| 2 | LLM 生成中途报错 | 中止，**保留已输出** | 部分答案 + 内联错误提示 | `LLM_ERROR` | P2 | ✅ |
| 3 | LLM 全部实例不可用 | 降级为维护态，禁用输入框 | 维护横幅 | `MAINTENANCE` | **P1** | ⏳ |
| 4 | 检索无结果（低于阈值） | 走 `escalate` 重检索 → 仍无 | 拒答卡 + 三动作 | `NO_RELEVANT_CONTEXT` | 命中率 | ✅ |
| 5 | 向量库不可用 | **拒绝回答** | "知识库暂时不可用" | `VECTOR_STORE_DOWN` | **P1** | ⏳ |
| 6 | 向量库返回慢（> 3s） | 超时中止 → 拒答 | 同上 | `VECTOR_STORE_DOWN` | P2 | ✅ |
| 7 | 权限服务不可用 | **fail-closed：拒绝所有检索** | "系统维护中，暂时无法查询" | `ACL_UNAVAILABLE` | **P0** | ⏳ |
| 8 | 缓存不可用 | 直连 DB / 向量库 | 无感（略慢） | — | P2 | ✅ |
| 9 | 模型并发队列满 | 排队 ≤ 20s，超时拒绝 | "当前咨询较多，请稍后重试" | `BUSY` | 队列深度 | ✅ |
| 10 | 用户配额超限 | 四级降级（§6.6） | 按档位文案 | `QUOTA_EXCEEDED`（仅 ≥200%） | 配额 | ⏳ |
| 11 | 上下文超限 | 裁剪历史（§6.6 预算） | 无感 | — | — | ✅ |
| 12 | 接地校验失败 | 重试 1 次 → 仍失败拒答 | 拒答（不暴露原因细节） | `GROUNDING_FAILED` | 指标 | ✅ |
| 13 | 出口脱敏触发 | 就地打码，**不整段拒绝** | 敏感处显示 `[已脱敏]` | — | 计数 | ✅ |
| 14 | 前端网络中断 | 自动重连 + 断点续流 | 短暂停顿后继续 | — | 重连率 | ✅ |
| 15 | 上传中途失败 | 断点续传或重传 | 进度条 + 重试 | — | P2 | ✅ |
| 16 | 异步解析失败（重试耗尽） | 死信队列 + `status=FAILED` | 列表显示"处理失败" + 原因 + 重试 | — | P2 | ✅ |
| 17 | 同步任务连续失败 | 自动暂停该源 + 通知管理员 | 面板显示"已暂停" | — | **P1** | ✅ |
| 18 | 检测到注入攻击 | 按 §6.7 四档处置 | 分档文案 | `INJECTION_BLOCKED` | 计数 | — |

**三条铁律（MUST）**：
```
① 权限服务挂 → fail-closed（拒绝服务）。MUST NOT fail-open。
② 向量库挂   → 拒答。MUST NOT 退化成"纯 LLM 回答"。
③ 任何中断   → 保留已输出内容。MUST NOT 清空。
```

### 6.5 重检索升级（`escalate`）

| attempt | 触发条件 | `top_k` | `rerank_top_n` | `vec_threshold` | `vec_weight` / `bm25_weight` | 理由 |
|---|---|---|---|---|---|---|
| 1 | 默认 | 20 | 6 | 0.55 | 0.6 / 0.4 | 正常检索 |
| 2 | 第 1 次低于阈值 | 30 | 6 | 0.42 | 0.45 / 0.55 | 可能是术语不匹配 → 提关键词权重 |
| 3 | 第 2 次仍低于阈值 | 40 | 8 | 0.0 | 0.4 / 0.6 | 表达差异太大 → 去掉向量阈值 |
| — | 第 3 次仍失败 | — | — | — | — | **拒答**（不再重试） |

**约束**：重检索 **MUST NOT** 突破 ACL 与密级过滤；**MUST NOT** 超过 3 次。

### 6.6 配额四级降级（`enforce_quota`）

| 用量比 | `QuotaAction` | 行为 | 用户可见 |
|---|---|---|---|
| `< 0.8` | `NORMAL` | 正常 | 无感 |
| `0.8 – 1.0` | `NORMAL` + 告警 | 正常，记软告警 | 无感 |
| `1.0 – 1.5` | `SMALL_MODEL` | 路由到 `LLM_MODEL_SMALL` | 无感（质量略降） |
| `1.5 – 2.0` | `CACHE_ONLY` | 仅允许命中缓存的问题 | 未命中提示"今日额度接近上限" |
| `≥ 2.0` | `REJECT` | 拒绝 | "今日额度已用完" |

**基准值**：用户 200k tokens/日、¥30/月；部门 2M tokens/日、¥300/月；全局 ¥2000/日、¥40000/月。

### 6.7 注入四档处置

| 档位 | 分数 | 动作 | 用户感受 |
|---|---|---|---|
| `PASS` | `< 25` | 正常处理，审计记 `signals` | 无感 |
| `CLARIFY` | `25–44` | 用 `clarify` 工具反问 | 温和引导 |
| `GUARD` | `45–79` | ① 追加强化声明 ② **强制走检索** ③ 关闭 `doc_export` / `doc_translate` ④ 降低详细度 | 仍能答，但更保守 |
| `BLOCK` | `≥ 80` | 明确拒绝，**不做检索、不调模型**，写审计 + 计数 | 被拒，有清晰原因 |

**归一化前置（MUST）**：检测**之前**必须先解码 / 归一化，否则绕过成本极低：
```
① Base64 解码（连续 ≥60 字符的 base64 段）
② 零宽字符剥离（U+200B–U+200D、U+FEFF）
③ 全角转半角
④ 去除多余空白与不可见控制符
```
**MUST NOT** 在原始文本上直接跑正则匹配。

**升级规则（MUST）**：单用户 1 小时内 `BLOCK` ≥ 5 次 → 临时限制 30 分钟 + 通知安全人员。
一次性拦截容易被"换个说法继续试"，**限额才真正阻断**。

**两侧规则不同（MUST NOT 混用）**：`side='ingest'` 侧重指令模式与隐藏字符清洗；`side='query'` 侧重会话级行为特征（多轮铺垫、突发频率、新用户爆发）。

### 6.8 模型路由

| 场景 | 模型 |
|---|---|
| 正常问答 | `LLM_MODEL` |
| 多轮上下文摘要 | `LLM_MODEL_SMALL` |
| 会话标题生成 | `LLM_MODEL_SMALL` |
| `QuotaAction.SMALL_MODEL` | `LLM_MODEL_SMALL` |
| `mask_on_ingest=True` 的文档 | 内网模型（`allow_external_model=False`） |
| 密级 L3 | 不调用任何模型 |

**参数**：`LLM_TEMPERATURE=0.1`、`LLM_MAX_TOKENS=1024`、`LLM_TIMEOUT=60`。
**MUST NOT** 在业务代码里覆盖 temperature —— 事实类问答必须低温。

### 6.9 五端能力矩阵

| 维度 | Web | 桌面端 | H5 | 小程序 | App |
|---|---|---|---|---|---|
| 渲染栈 | Next.js | Tauri + Next.js 静态导出 | Next.js（响应式） | Taro | Expo (RN) |
| 布局 | 三栏 | 三栏 + 原生菜单 | 单列 | 单列 | 单列 |
| 会话列表 | 左侧栏 | 左侧栏 | 抽屉 | 独立页 | 独立页 |
| 来源侧栏 | 固定右栏 | 固定右栏 | 底部半屏 | 底部半屏 | 底部半屏 |
| 上传 | 拖拽 + 批量 | 拖拽 + 系统选择 | 文件选择器 | 微信会话文件 | 相册 / 文件 / 拍照 |
| 流式 | `fetch` stream | 同左 | 同左 | `enableChunked` | `XHR.onprogress` |
| 首次输入延迟 | 无 | 无 | 无 | **有（需降级）** | 无 |
| 语音提问 | 可选 | 可选 | 需 HTTPS | 微信原生录音 | 原生录音 |
| 离线 | SW 缓存 | 本地库 | SW 缓存 | 无 | SQLite |
| 推送 | Web Push | 系统通知 | 无 | 订阅消息 | APNs / FCM |
| 批量上传 | ✅ | ✅ | ❌ | ❌ | ❌ |
| 原文对照 | ✅ | ✅ | ❌ | ❌ | ❌ |
| 深色模式 | ✅ | ✅ 跟随系统 | ✅ | ✅ | ✅ |
| 宽度 | ≥1024 | ≥1024 | <768 | 375 | 390 / 360 |

**硬约束**：
- 引用角标与来源卡 **五端全部渲染**，**MUST NOT** 因端能力不足而省略
- 小程序单包 **MUST** ≤ 2MB
- `@kb/core` 中 **MUST NOT** 出现 `Platform.select` / `window` / `document` / `localStorage`

### 6.10 权限变更传播链路

| 事件 | 必须执行的动作 | 时延要求 |
|---|---|---|
| 用户角色变更 | `users.acl_version += 1`；清该用户所有缓存 | 立即 |
| 用户组增删成员 | 受影响用户 `acl_version += 1`；清缓存 | 立即 |
| 用户部门调动 | `users.acl_version += 1`；清缓存 | 立即 |
| 用户离职 / 停用 | `acl_version += 1`；撤销 refresh token；清缓存 | 立即 |
| 知识库成员变更 | `kb.acl_version += 1`；重算受影响文档 chunk 的 `acl_tags`；清相关答案缓存 | 5 min |
| 文档 visibility 变更 | `documents.acl_version += 1`；重写该文档全部 chunk 的 `acl_tags`；`acl_epoch += 1` | 立即 |
| 文档级 ACL 明细变更 | 同上 | 立即 |
| 文档删除 | 软删 + 向量库移除 + `acl_epoch += 1` | 立即 |
| `acl_epoch` 递增 | 全量语义缓存自然失效（影响面不可精确枚举时使用） | 立即 |

**`acl_epoch` 机制（MUST）**：当文档级权限变更的影响面无法精确枚举时，用全局 epoch 让所有语义缓存失效。**代价是命中率短暂下降，但正确性优先。**

---

## 7. 禁区（反模式对照表）

> 左侧写法一旦出现在代码里，**MUST** 立即改正。每条都对应至少一个 §1 不变量。

| # | ❌ 禁止 | ✅ 正确 | 违反 |
|---|---|---|---|
| 1 | `if not chunks: return await llm.chat(...)` | 走拒答分支，不调 LLM | INV-01 |
| 2 | `if max_score < th:` 单一闸门 | 分数 + 数量 + 长度三重 | INV-02 |
| 3 | 硬编码 `RERANK_THRESHOLD = 0.35` | 由 `calibrate_threshold.py` 标定后注入 | INV-03 |
| 4 | 让模型输出 `{"doc_name": ..., "page": ...}` | 模型只输出 `[n]`，元数据服务端填 | INV-04 |
| 5 | 校验只检查"有没有引用" | 校验编号范围 + 覆盖 + 越界 | INV-05 |
| 6 | 接地失败 → 输出原文 | 接地失败 → 拒答 | INV-06 |
| 7 | `chunks = search(kb_ids); chunks = [c for c in chunks if visible(c)]` | ACL 条件下推进查询 | INV-07 |
| 8 | 检索时才 JOIN 权限表算 `acl_tags` | 入库时写入 `acl_tags` | INV-08 |
| 9 | `except Exception: return frozenset()` | `except Exception: raise` | INV-09 |
| 10 | `key = f"acl:u:{user_id}"` | `key = f"acl:u:{user_id}:v{acl_version}"` | INV-10 |
| 11 | `key = f"ans:{question_hash}"` | key 含 `hash(sorted(user_subjects))` | INV-11 |
| 12 | `kb_ids = args["kb_scope"]` | `kb_ids = await filter_authorized_kbs(user, args["kb_scope"])` | INV-12 |
| 13 | `if not allowed: return 403` | 返回 404 或 200+拒答；耗时对齐 | INV-13 |
| 14 | `kb_ids = body.kb_ids` | 先 `filter_authorized_kbs()` 静默裁剪 | INV-14 |
| 15 | L3 文档入库后在检索时过滤掉 | L3 文档在入库前 `SKIPPED` | INV-15 |
| 16 | `restore(text, mapping)` | `restore_on_output(text, mapping, user)` | INV-16 |
| 17 | `UPDATE audit_log SET ...` | 只 `INSERT`；DB 层 REVOKE | INV-17 |
| 18 | 审计只记问答 | 登录 + 权限变更 + 问答 + 越权尝试四类 | INV-18 |
| 19 | 换内网 embedding，rerank 仍走公网 | Embed / Rerank / LLM / 监控四环全部内网 | INV-19 |
| 20 | 先标 PG 软删，再删向量库 | 先删向量库，后标 PG | INV-20 |
| 21 | `if last_sync_at > doc.updated_at:` | 比 `content_hash` + 外部版本号 | INV-21 |
| 22 | 断线后重放整个回答 | 按 `Last-Event-ID` 从断点续传 | INV-22 |
| 23 | `HEARTBEAT_INTERVAL = 60` | 15（且主循环有超时分支） | INV-23 |
| 24 | `setMessages([])` 在错误分支 | 只 append，不清空 | INV-24 |
| 25 | `NEXT_PUBLIC_LLM_API_KEY` | 密钥只在服务端；前端走 BFF | INV-25 |
| 26 | `except VectorError: return []` | 抛 `VectorStoreUnavailable` → 拒答 | INV-26 |
| 27 | 限流 key 复用缓存 key 构造函数 | 两个独立函数 | INV-27, INV-28 |
| 28 | `from langchain.chains import RetrievalQA` | 自己串编排（白名单外依赖） | §2.1 |
| 29 | `os.getenv("RERANK_THRESHOLD")` | `settings.rerank_threshold` | §2.2 / WU-02 |
| 30 | `prompt = f"请根据以下内容回答：{ctx}"` 散在业务代码 | Prompt 集中在 `prompts.py` | WU-14 |
| 31 | 在 `@kb/core` 里 `import { View } from 'react-native'` | 共享层零 UI | §2.2 |
| 32 | 业务代码里写 `if reason == "NO_RELEVANT"` | 用 `RefuseReason` 枚举 | §3.3 |
| 33 | 新增 `4xx=403` 表示数据无权 | 只有功能权限用 403 | INV-13 |
| 34 | 前端把后端 `snake_case` 字段转成 `camelCase` | 保持 `snake_case` | §2.3 |
| 35 | Agent 允许"不检索直接作答" | 强制检索或拒答 | INV-01, INV-12 |
| 36 | 文档更新只把旧 chunk 标 `is_latest=false` | **物理删除**旧 chunk | §5 WU-27 |
| 37 | 用 RSC props 把文档正文传给 Client Component | 客户端经已鉴权接口取数 | INV-25 |
| 38 | `onerror` 里无条件重连 | 按 `readyState` 分支：`CONNECTING` 交浏览器、`CLOSED` 不重连 | §4.3.0 |
| 39 | 限流用固定窗口计数 | Redis + Lua 滑动窗口 | §5 WU-28 |
| 40 | 在原始文本上直接跑注入正则 | 先 Base64 / 零宽 / 全角归一化 | §6.7 |
| 41 | 用"跨页重复率 > 阈值"单条件删页眉 | 三信号投票，≥2 命中才删 | §5 WU-06 |
| 42 | 把上一轮回答里的数值当作事实继续推理 | 历史只用于指代消解；事实只来自本轮片段 | §5 WU-35 |
| 43 | 裁上下文时截断片段正文 | 先裁历史 → 再减条数，正文不截断 | §5 WU-35 |

---

## 8. 测试与门禁

### 8.1 测试文件清单（MUST 全部存在）

```
backend/tests/
├── unit/
│   ├── test_config.py                    # WU-02
│   ├── test_org_path.py                  # WU-03
│   ├── test_doc_acl_order.py             # WU-04
│   ├── test_parsers.py                   # WU-05
│   ├── test_cleaner.py                   # WU-06
│   ├── test_embedding.py                 # WU-07
│   ├── test_chunker.py                   # WU-08
│   ├── test_gate.py                      # WU-12  ← INV-02
│   ├── test_rrf.py                       # WU-13
│   ├── test_validator.py                 # WU-15  ← INV-05
│   ├── test_classification.py            # WU-17
│   ├── test_dlp.py                       # WU-18
│   ├── test_user_subjects.py             # WU-19
│   ├── test_tool_registry.py             # WU-24
│   ├── test_grounding.py                 # WU-25  ← INV-06
│   ├── test_signature.py                 # WU-28
│   ├── test_no_secret_leak.py            # WU-31  ← INV-25
│   └── test_cache_key_rules.py           # WU-22  ← INV-28
│
├── integration/
│   ├── test_embed_acl.py                 # WU-08  ← INV-08（无空 acl_tags）
│   ├── test_ingest_progress.py           # WU-09
│   ├── test_webhook.py                   # WU-10
│   ├── test_session_isolation.py         # WU-11
│   ├── test_rag_pipeline.py              # WU-14  ← INV-01
│   ├── test_citation_hydration.py        # WU-16  ← INV-04
│   ├── test_level_skip.py                # WU-17  ← INV-15
│   ├── test_store_contract.py            # WU-20（双后端）
│   ├── test_fallback_matrix.py           # WU-29（18 条）
│   ├── test_sse_protocol.py              # WU-30  ← INV-22/23
│   ├── test_agent_loop.py                # WU-34
│   ├── test_multi_turn.py                # WU-35
│   └── test_audit_coverage.py            # WU-26  ← INV-18
│
├── security/
│   ├── test_acl_matrix.py                # §8.3 越权矩阵
│   ├── test_acl_pushdown.py              # WU-19  ← INV-07
│   ├── test_acl_cache_invalidation.py    # WU-21  ← INV-10
│   ├── test_fail_closed.py               # WU-21  ← INV-09
│   ├── test_side_channel.py              # WU-21  ← INV-13
│   ├── test_cache_acl_leak.py            # WU-22  ← INV-11
│   ├── test_tool_param_injection.py      # WU-24  ← INV-12
│   ├── test_dlp_leak.py                  # WU-18
│   └── test_ratelimit.py                 # WU-28  ← INV-27
│
└── eval/
    └── test_eval_gates.py                # WU-38
```

### 8.2 CI 门禁（MUST 全绿方可合并）

```yaml
# .github/workflows/ci.yml（要点）
- ruff check backend && mypy backend/app
- pytest backend/tests/unit -q
- pytest backend/tests/integration -q
- pytest backend/tests/security -q          # ★ 硬门禁
- pytest backend/tests/integration/test_fallback_matrix.py -q   # ★ 18 条兜底
- python scripts/calibrate_threshold.py --assert-calibrated     # ★ INV-03
- python scripts/verify_audit_chain.py --start 1 --end 100000   # ★ INV-17
- python scripts/consistency_check.py --sample 0.01             # ★ INV-20
- pnpm -w run lint && pnpm -w run typecheck
- pnpm --filter web build
- ! grep -rE "sk-|postgresql\+asyncpg|service_role" apps/web/.next/static/   # ★ INV-25
- python scripts/run_eval.py --layer all --gate                         # ★ 指标门禁
```

**任一门禁失败 → PR 红。MUST NOT 用 `--no-verify` 或跳过标记绕过。**

### 8.3 越权测试矩阵（`tests/security/test_acl_matrix.py`）

```python
# (用户, 目标, 操作, 期望结果)
MATRIX = [
    # ── 部门隔离 ──
    ("finance_user",   "kb_finance",   "chat", "allow"),
    ("finance_user",   "kb_hr_salary", "chat", "deny_or_empty"),
    ("sales_user",     "kb_finance",   "chat", "deny_or_empty"),
    # ── 跨部门同级 ──
    ("sales_user",     "kb_sales",     "chat", "allow"),
    ("sales_user",     "kb_tech",      "chat", "deny_or_empty"),
    # ── 职级门槛 ──
    ("junior_user",    "kb_exec_ma",   "chat", "deny_or_empty"),
    ("exec_user",      "kb_exec_ma",   "chat", "allow"),
    # ── 显式 deny 优先 ──
    ("denied_user",    "kb_finance",   "chat", "deny_or_empty"),
    # ── 功能权限（唯一允许 403 的场景）──
    ("viewer_user",    "kb_finance",   "upload", "403_permission"),
    ("editor_user",    "kb_finance",   "upload", "allow"),
    ("viewer_user",    "kb_finance",   "delete", "403_permission"),
    # ── 伪造 kb_ids ──
    ("sales_user",     "kb_hr_salary", "chat_with_forged_kb_id",   "deny_or_empty"),
    # ── 越权文档不可被引用 ──
    ("junior_user",    "kb_public",    "chat_citing_restricted_doc","no_restricted_citation"),
    # ── 离职 / 停用 ──
    ("resigned_user",  "kb_finance",   "chat", "401_inactive"),
    ("suspended_user", "kb_finance",   "chat", "401_inactive"),
]
```

**断言规则（MUST）**：

| expected | 断言 |
|---|---|
| `allow` | `status == 200` 且 `refused is not True` |
| `deny_or_empty` | `status in (200, 404)`；200 时 `refused is True or citations == []`；**响应全文不含受限词** |
| `403_permission` | `status == 403` 且 `code == "PERMISSION_DENIED"` |
| `401_inactive` | `status == 401` 且 `code in ("ACCOUNT_INACTIVE", "TOKEN_REVOKED")` |
| `no_restricted_citation` | `cited_docs ∩ RESTRICTED_DOC_IDS == ∅` |

**另加侧信道一致性测试**（`test_side_channel.py`）：
```
场景 A：资源存在但无权          → 记录状态码、响应体、耗时
场景 B：资源完全不存在          → 同上
断言：① 状态码相同 ② 文案相同 ③ 字段集合相同 ④ 耗时差 < 10%
```

### 8.4 验收指标（`run_eval.py --gate` 的判定表）

| 层 | 指标 | 目标 | 数据来源 |
|---|---|---|---|
| 检索 | Recall@5 | ≥ 0.90 | `eval/positive.jsonl` |
| | MRR | ≥ 0.85 | 同上 |
| | NDCG@10 | ≥ 0.80 | 同上 |
| 重排 | Rerank 提升度 | ≥ 0.15 相对 | 同上 |
| 生成 | Faithfulness | ≥ 0.95 | RAGAS / LLM-as-Judge |
| | Answer Relevancy | ≥ 0.90 | 同上 |
| 引用 | Citation Precision | ≥ 0.95 | 人工抽样 200 条 |
| | Citation Coverage | ≥ 0.90 | 规则校验 |
| **边界** | **拒答准确率** | **≥ 0.95** | `eval/negative.jsonl` |
| | **误拒率** | **≤ 0.05** | `eval/positive.jsonl` |
| 多轮 | 指代消解准确率 | ≥ 0.90 | `eval/multi_turn.jsonl` |
| 安全 | 注入拦截率 | 1.00 | `eval/adversarial.jsonl` |
| | 越权泄漏数 | **0** | `tests/security/` |
| 性能 | 首 Token 延迟 P95 | ≤ 1.2s | APM |
| | 完整响应 P95 | ≤ 4s | APM |
| 成本 | 单次问答成本 | ≤ ¥0.05 | `token_usage` |

**MUST NOT** 通过放松目标值来让 CI 变绿。指标不达标 → 修实现。

---

## 9. 配置契约

> **`app/config.py` 的字段名 MUST 与本表逐字一致。**
> 新增配置项：先加进本表 → 再加进 `.env.example` → 再加进 `config.py`。

### 9.1 应用

| 变量 | 默认 | 消费方 | 备注 |
|---|---|---|---|
| `ENV` | `dev` | 全局 | `prod` 时启用标定校验 |
| `APP_SECRET_KEY` | — | JWT 签名 | ≥32 随机字符 |
| `API_BASE_URL` | — | 链接生成 | |
| `CORS_ORIGINS` | — | 中间件 | 逗号分隔 |
| `LOG_LEVEL` | `INFO` | 日志 | |

### 9.2 数据与存储

| 变量 | 默认 | 备注 |
|---|---|---|
| `DATABASE_URL` | — | `postgresql+asyncpg://` |
| `REDIS_URL` | — | |
| `CELERY_BROKER_URL` | — | |
| `CELERY_RESULT_BACKEND` | — | |
| `S3_ENDPOINT` / `S3_ACCESS_KEY` / `S3_SECRET_KEY` / `S3_BUCKET` | — | 对象存储 |

### 9.3 向量库

| 变量 | 默认 | 备注 |
|---|---|---|
| `VECTOR_BACKEND` | `pgvector` | `pgvector` \| `milvus` |
| `MILVUS_URI` | — | |
| `MILVUS_COLLECTION` | `kb_chunks` | |
| `VECTOR_DIM` | `1024` | 必须与 embedding 模型一致 |

### 9.4 模型服务（全部 MUST 指向内网）

| 变量 | 默认 | 备注 |
|---|---|---|
| `LLM_BASE_URL` / `LLM_API_KEY` | — | OpenAI 兼容协议 |
| `LLM_MODEL` | — | 主模型 |
| `LLM_MODEL_SMALL` | — | 摘要 / 标题 / 降级 |
| `LLM_TEMPERATURE` | `0.1` | |
| `LLM_MAX_TOKENS` | `1024` | |
| `LLM_TIMEOUT` | `60` | |
| `EMBED_BASE_URL` / `EMBED_API_KEY` / `EMBED_MODEL` | — | |
| `EMBED_BATCH` | `32` | |
| `EMBED_MAX_CHARS` | `8000` | 超长截断并告警 |
| **`RERANK_BASE_URL`** | — | ★ **最容易被遗漏的出域点**（INV-19） |
| `RERANK_MODEL` | — | |
| `RERANK_TOP_N` | `4` | |
| **`RERANK_THRESHOLD`** | — | ★ **MUST 由标定产出**，行尾带 `# calibrated: YYYY-MM-DD` |

### 9.5 检索与缓存

| 变量 | 默认 | 备注 |
|---|---|---|
| `RETRIEVE_TOP_K` | `40` | |
| `RRF_K` | `60` | |
| `CONTEXT_MAX_TOKENS` | `3000` | |
| `MIN_KEPT_CHUNKS` | `1` | 闸门参数 |
| `MIN_CONTENT_LEN` | `30` | 闸门参数 |
| `ENABLE_QUERY_REWRITE` | `true` | |
| `ENABLE_HYDE` | `false` | 短查询场景可开 |
| `ENABLE_SEMANTIC_CACHE` | `true` | |
| `CACHE_TTL_SECONDS` | `3600` | |
| `SEMANTIC_CACHE_THRESHOLD` | `0.97` | |

### 9.6 解析

| 变量 | 默认 | 备注 |
|---|---|---|
| `UPLOAD_MAX_SIZE_MB` | `100` | |
| `ALLOWED_FILE_TYPES` | `pdf,md,txt,xls,xlsx,docx` | 白名单 |
| `OCR_ENABLED` | `true` | |
| `QUEUE_HEAVY_THRESHOLD_MB` | `20` | 超过则进 `heavy` 队列 |

### 9.7 安全

| 变量 | 默认 | 备注 |
|---|---|---|
| `AUTH_MODE` | `oidc` | `oidc` \| `jwt` \| `wecom` \| `dingtalk` |
| `OIDC_ISSUER` / `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` | — | |
| `JWT_ALGORITHM` | `RS256` | |
| `JWT_PUBLIC_KEY_PATH` | — | |
| `ACCESS_TOKEN_TTL` | `900` | 15 min |
| `REFRESH_TOKEN_TTL` | `604800` | 7 d |
| `AUDIT_LOG_ENABLED` | `true` | |
| `AUDIT_LOG_RETENTION_DAYS` | `365` | |
| `INJECTION_DETECT_ENABLED` | `true` | |
| `RATE_LIMIT_PER_USER` | `60/min` | |
| `RATE_LIMIT_PER_IP` | `600/min` | |
| `NETWORK_EGRESS_WHITELIST` | — | ★ 启动时校验模型服务地址（INV-19） |

### 9.8 可观测

| 变量 | 默认 |
|---|---|
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST` | — |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | — |
| `PROMETHEUS_MULTIPROC_DIR` | — |

### 9.9 前端（**MUST NOT** 含任何密钥）

| 变量 | 备注 |
|---|---|
| `NEXT_PUBLIC_API_BASE_URL` | **仅可含此一项公开变量** |
| `NEXT_PUBLIC_APP_NAME` | |
| `NEXT_PUBLIC_ENABLE_STREAM` | |

> ⚠️ 任何 `NEXT_PUBLIC_*` 变量都会进客户端 bundle。**invariant INV-25 的检查点就是扫描 `.next/static/`。**

---

## 10. 交付与汇报格式

### 10.1 单个 WU 完成后 **MUST** 输出以下块

```markdown
## WU-XX · <名称>

**状态**：DONE | PARTIAL | BLOCKED

**产出文件**
- `path/to/file.py`（新增 / 修改）
- `path/to/other.py`

**验收命令与结果**
| 命令 | 结果 |
|---|---|
| `pytest tests/unit/test_xxx.py -q` | 12 passed |
| `python scripts/yyy.py` | exit 0 |

**触及的不变量及自查**
| 不变量 | 自查结论 |
|---|---|
| INV-07 | 已确认 `hybrid_search_secure` 的 `user_subjects` 为必填；全仓无绕过入口 |
| INV-08 | 已确认写库前断言 `acl_tags` 非空 |

**契约变更**（若有）
- 新增环境变量：`FOO_BAR`（已同步 §9.x、`.env.example`、`config.py`）
- 新增 API：`POST /api/v1/xxx`（已同步 §4.1）

**偏离与风险**
- <若有偏离上游设计的地方，写在这里；若无写"无">

**BLOCKER**（若有）
- <具体问题 + 需要谁裁决>
```

### 10.2 标记规则（MUST）

| 状态 | 允许的条件 |
|---|---|
| `DONE` | 全部验收命令通过 + 触及的不变量已逐条自查 + 契约变更已同步 |
| `PARTIAL` | 有验收命令未通过，或契约变更未同步。**MUST** 说明差什么 |
| `BLOCKED` | 缺契约 / 缺外部依赖 / 规则不明确。**MUST** 说明卡在哪一步 |

**禁止事项**：
- **MUST NOT** 在验收命令未通过时标 `DONE`
- **MUST NOT** 在 `PARTIAL` / `BLOCKED` 时继续推进下一 WU
- **MUST NOT** 隐去失败的命令输出；失败就原样贴出来

---

## 11. 与上游设计文档的章节索引

> 需要理解"为什么"时按此表查阅。**本文档不重复动机。**
> 若发现两者冲突 → 先按本文档执行，并在汇报中指出需修订的位置。

| 本文档 | 上游文档章节 | 用途 |
|---|---|---|
| §0 执行协议 | 第 0 章 | 方案边界与核心决策 |
| §1 INV-01~06 | 第 0 章 0.1、第 5 章、第 15 章 15.6 | 回答边界三层约束 |
| §1 INV-07~14 | 第 10 章 10.1、第 12 章全章 | 权限模型与检索隔离 |
| §1 INV-15~19 | 第 13 章全章、第 17 章 17.5 | 合规、密级、出域 |
| §1 INV-20~21 | 第 3 章 3.4、第 14 章 14.5 | 同步一致性 |
| §1 INV-22~24 | 第 16 章 16.3、16.5 | 流式与续流 |
| §1 INV-25~28 | 第 16 章 16.1、第 17 章 17.3、17.7 | 密钥、限流、成本 |
| §2.1 依赖白名单 | 第 2 章 2.2、2.3 | 选型与"为什么不用 LangChain" |
| §3.1 数据模型 | 第 12 章 12.2、第 13 章 13.4 | DDL |
| §3.2 Chunk 元数据 | 第 3 章 3.3、第 4 章 4.2、4.3 | 元数据与索引 |
| §4.2 聊天接口 | 第 1 章 1.1、第 6 章 6.2 | 输入输出契约 |
| §4.3 SSE 协议 | 第 16 章 16.3 | 事件契约 |
| §4.4 错误码 | 第 17 章 17.4、17.8 | 错误与兜底 |
| §5 WU-05~11 | 第 3 章、第 14 章 14.1–14.4 | 解析、清洗、分块、同步 |
| §5 WU-12~14 | 第 4 章 4.6、4.5、第 6 章 6.3 | 闸门、重排、主链路 |
| §5 WU-15~16 | 第 5 章 5.3 | 出口校验与引用两段式 |
| §5 WU-17~18 | 第 13 章 13.1–13.3 | 密级与脱敏 |
| §5 WU-19~23 | 第 12 章 12.4–12.6 | Token 设计、检索隔离、请求链路 |
| §5 WU-24~25 | 第 15 章 15.1–15.3、15.6 | 工具化与边界保证 |
| §5 WU-26 | 第 12 章 12.10、第 13 章 13.4 | 审计与合规 |
| §5 WU-27 | 第 14 章 14.4、14.5 | Webhook 与一致性 |
| §5 WU-28 | 第 17 章 17.2、17.3、17.7 | 签名、限流、成本 |
| §5 WU-29 | 第 17 章 17.5、17.8–17.9、附录 C.1 | 隔离、兜底、Nginx |
| §5 WU-30 | 第 16 章 16.3 | 心跳与续流 |
| §5 WU-31~37 | 第 7 章、第 8 章、第 16 章 | 前端与五端 |
| §5 WU-38~40 | 第 9 章、第 10 章 10.3–10.4、第 11 章 | 评估、观测、灰度 |
| §6.2 密级映射 | 第 13 章 13.2 | LevelPolicy |
| §6.4 异常兜底 | 第 17 章 17.8 | 18 条兜底 |
| §6.5 重检索 | 第 14 章 14.6 | escalate 参数 |
| §6.7 注入四档 | 第 17 章 17.1 | 检测与处置 |
| §6.9 五端矩阵 | 第 8 章 8.1、第 16 章 16.0 | 端差异与架构冲突 |
| §8.3 越权矩阵 | 第 12 章 12.8 | 14 个用例 |
| §8.4 验收指标 | 第 1 章 1.2、第 9 章 | 指标与门禁 |
| §9 配置契约 | 附录 A | 环境变量 |
| — | 附录 B | 依赖清单 |
| — | 附录 D | 交付清单（Definition of Done） |
| — | 附录 E | 常见坑速查表 |
| — | 附录 F | 需求覆盖矩阵 |

---

## 附 · 快速自检清单（每个 WU 收尾时逐条打勾）

```
□ 验收命令全部通过，输出已贴进汇报
□ 触及的不变量逐条自查，结论已写明
□ 新增/修改的契约（API / 表 / 枚举 / 环境变量）已同步回本文档
□ 无新增白名单外依赖
□ 无硬编码阈值、无硬编码 Prompt、无裸字符串枚举
□ 无 403 表示数据无权
□ 错误分支只 append 不清空（前端）
□ 检索路径都经过 retrieve_with_acl（无绕过）
□ 工具都经过 execute_tool（无直接调用 handler）
□ 缓存 key 与限流 key 由两个独立函数构造
□ 新增分支已在 §6 决策表中有对应行
□ 文档更新走物理删除旧 chunk（不是 is_latest=false）
□ 注入检测跑在归一化之后的文本上
□ 无密钥 / 文档正文经 RSC props 下发到客户端
□ 汇报块已按 §10.1 格式输出
```

---

*本文档版本 v1.0 · 2026.09 · 对应上游设计文档 v1.2*
*面向 AI Coding Agent 的可执行实施规范 · 共 11 节 + 1 个自检清单*
*不变量 28 条 · 工作单元 40 个 · 决策表 10 组*
