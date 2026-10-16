# 企业知识库问答 AI Agent · AI Coding Agent 实施规范 · M7 增量：多模态路由与知识治理

> **文档类型**：机器可执行实施规范（增量，承接 `AI-Coding-Agent-实施规范.md`）
> **适用范围**：`code/apps/api`（Python / FastAPI）、`code/apps/web`（React / Vite）、`code/docker-compose*.yml`
> **上游文档**：
> - `知识库问答Agent-AI-Coding开发手册.md`（口径真源，"手册 §3 → decisions.py → 启动自检"）
> - `企业知识库问答AI-Agent-开发流程.md`（人类可读设计文档）
> - `AI-Coding-Agent-实施规范.md`（本文是它的**增量**，§0 执行协议、§1 不变量体例沿用）
> **版本**：v0.1 · 评审稿 —— **未获批准前 MUST NOT 进入编码**

---

## 0. 执行协议（增量）

### 0.1 与总规范的体例关系

1. 本文档**不重复**总规范已有的执行协议、不变量编号、依赖白名单。
2. 总规范的 §0（执行协议）、§1 体例、§10（汇报块）、附录自检清单**全部继续适用**，本文档只做增量。
3. 本文档新编号一律加前缀 `M`：不变量 `INV-M-xx`、工作单元 `WU-M-xx`、门禁 `GATE-M-x`，**MUST NOT** 与既有编号复用。

### 0.2 冲突解决优先级（继承，增补一条）

```
1. 用户当前会话中的即时指令
2. 总规范 §1 不变量 + 本文档 §1 新增不变量        ← 唯一不可协商的技术底线
3. 本文档 §2/§3 数据与接口契约
4. 本文档 §4 工作单元定义
5. 上游设计文档（开发流程 / 手册）
6. 仓库既有代码风格
```

**增补**：若本次需求与**手册 §1.4 Non-Goals 冲突**（已发生：扫描件 OCR 被列为不做项），MUST NOT 静默执行 —— 先在 §0.4 列出需回改的手册章节，获用户批准后再改代码，并在代码留 `# M7-OVERRIDE: 手册 §1.4` 注释。

### 0.3 工作单元模型（沿用）

1. 所有实现拆为 `WU-M-xx`，每个 WU 是**可独立提交、可独立验收**的最小单元。
2. 批次之间设 `GATE-M-x`，**GATE 未通过不得进入下一批次**。
3. **MUST NOT** 在一个提交里混两个 WU。
4. 执行循环与总规范 §0.3 一致：读 WU → 确认依赖 → 改产出文件 → 跑验收命令到全绿 → 自查不变量 → 输出 §7 汇报块。

### 0.4 需回改的上游文档清单（**本次的前置条件**）

> 手册明确"修改流程：手册 §3 → decisions.py → 启动自检"。以下章节 MUST 在编码前同步修订，否则口径与代码会互相矛盾。

| 上游文档 | 章节 | 需修订内容 |
|---|---|---|
| 手册 | §1.3 功能清单 | 增补：图片/扫描件知识接入、图片提问、内部网址接入、四维审查 |
| 手册 | §1.4 Non-Goals | **删除/改写**「❌ 扫描件 PDF 的 OCR」一行 —— 改为「由多模态视觉链路承接，仅限静态图片与扫描件 PDF」 |
| 手册 | §3.5 常量表 | `OCR_ENABLED` 由冻结 `False` 改为受 `settings.ocr_enabled` 控制的开关（默认 false） |
| 手册 | §4.2 表结构 | 增补 `documents` 治理字段、`governance_jobs` 表、`sync_sources` web 字段 |
| 手册 | §5.1 API 清单 | 增补 §3.1 中 8 个新接口 |
| 手册 | §6 里程碑 | 增补 `M7`（本增量）并标注依赖 `M6` |
| 手册 | §8 决策清单 | 增补 §5 中拍板的 6 条决策 |
| `decisions.py` | — | `OCR_ENABLED` 改写 + `self_check()` 中对应 fail-closed 分支改写为读配置 |
| `.env.example` / `.env.prod.example` | — | `ALLOWED_EXT` 增补图片扩展名；新增 OCR / 视觉 / 抓取相关配置项 |

**MUST NOT** 出现"手册写不做、代码已做"的状态。

---

## 1. 新增不变量（INVARIANTS）

> 体例同总规范 §1：规则 / 违反后果 / 强制检查点 / 关联 WU / 反例。
> 每个 WU 完成后 MUST 逐条自查其关联不变量。

### A 组 · 多模态与路由

---

**INV-M-01 · 模态判定只用显式信号**
模态路由 **MUST** 依据显式信号判定（扩展名 / 请求体字段 / MIME），**MUST NOT** 依赖内容嗅探猜测。

- 违反后果：扫描件被判成文本层 PDF → 抽出空文本、产出空 chunk 或错误向量，**静默入库**，检索期才暴露
- 强制检查点：`app/services/router/modality.py::detect`
- 关联 WU：WU-M-02
- 反例：`if not text: modality = guess_from_bytes(data)` ❌

---

**INV-M-02 · 图片不进独立向量空间**
图片类知识 **MUST** 经视觉抽取文本化后写入既有 `chunks.embedding`（`vector(1024)`），**MUST NOT** 新增第二向量列 / 第二向量空间。

- 违反后果：RRF 融合、rerank 调用、阈值口径三处同时破裂；`decisions.self_check` 的 `embedding_dim==1024` 校验随之失守
- 强制检查点：`decisions.self_check()` 的 `_profile.embedding_dim != 1024` 分支保持抛错；`chunks` 表无新增向量列
- 关联 WU：WU-M-09

---

**INV-M-03 · 图片提问不落库**
以图片提问时，图片与抽取文本 **MUST NOT** 写入知识库（`documents` / `chunks`）。

- 违反后果：用户一次性提问图片被永久沉淀为"知识"，污染检索与引用
- 强制检查点：`app/api/chat.py` 的 ask 路径**不调用** `upload_document`、不创建 `Document` / `ParseJob`
- 关联 WU：WU-M-07
- 反例：ask 内复用 `kbs.upload_document` ❌

---

**INV-M-04 · 视觉不可用时不降级猜测**
视觉理解链路不可用时 **MUST** 明确报错（400 / SSE `refused`），**MUST NOT** 退回"只看文本、凭模型先验作答"。

- 违反后果：直接违反"只依据知识库作答"，产生幻觉且无引用
- 强制检查点：`app/services/multimodal/vision.py` 异常**直抛**，调用方不做 try/except 兜底
- 关联 WU：WU-M-06, WU-M-07

---

**INV-M-05 · 图片解析必须可回链**
图片 / 扫描件解析产出的块 **MUST** 保留 `storage_key` 回链；多页扫描件 **MUST** 逐页填 `page_no`。

- 违反后果：引用无法回跳原图 / 原页，溯源断链（引用可信度归零）
- 强制检查点：`ParsedBlock.page_no` 在扫描件路径非空；`Document.storage_key` 可被 citation 反查
- 关联 WU：WU-M-09, WU-M-10

### B 组 · 网页接入与出域

---

**INV-M-06 · 出站域名白名单**
`web` 源抓取 **MUST** 校验目标域名在源配置的白名单内，**MUST NOT** 抓取白名单之外的任意 URL。

- 违反后果：SSRF —— 内部站点配置一处被篡改即可探测内网
- 强制检查点：`app/services/sync/web_fetcher.py::_assert_allowed_domain`
- 关联 WU：WU-M-12

---

**INV-M-07 · 凭据密文存储**
`web` 源的鉴权凭据 **MUST** 经 `app/services/crypto.py` 加密后落库，**MUST NOT** 明文落库、写日志、写消息 `meta`。

- 违反后果：内部站点凭据泄露；且会触发 `decisions.self_check` 的"快照含密钥明文"校验
- 强制检查点：`SyncSource.auth_payload` 存取均走 crypto；日志中无凭据明文
- 关联 WU：WU-M-11, WU-M-12

---

**INV-M-08 · 抓取限速与并发有上限**
`web` 抓取 **MUST** 具备可配的限速与并发上限，默认值 **MUST** 保守。

- 违反后果：同步任务打垮内部站点（生产事故）
- 强制检查点：`settings` 中抓取并发 / 间隔配置项存在且默认值保守
- 关联 WU：WU-M-12

---

**INV-M-09 · 页面消失走软失效**
同步发现页面已删除时 **MUST** 将该文档 `is_latest` 置 `false`，**MUST NOT** 物理删除。

- 违反后果：无法回溯"这条知识何时消失"，且断掉既有 INV-20/INV-21 的同步一致性口径
- 强制检查点：`sync_service.py` web 分支无 DELETE 语句
- 关联 WU：WU-M-14

### C 组 · 知识治理

---

**INV-M-10 · 审查不阻塞解析**
四维审查 **MUST** 为独立 arq 任务，**MUST NOT** 混入 `run_parse_job` 主路径。

- 违反后果：审查 LLM 抖动会让文档解析整体变慢或失败，连带 `ParseJob` 重试与死信
- 强制检查点：`run_governance_job` 独立注册；`parse_job.py` 内无治理调用
- 关联 WU：WU-M-15

---

**INV-M-11 · 审查不改写原文**
四维结论 **MUST** 只写入治理字段与治理报告，**MUST NOT** 改写 `chunks.content` / `documents.filename` 等原文。

- 违反后果：审查变成"重写知识"，引用回显与原文不一致，审计失真
- 强制检查点：`app/services/governance/` 无 `UPDATE ... content` 语句
- 关联 WU：WU-M-15

---

**INV-M-12 · 治理失败不使知识不可检索**
治理任务失败时 **MUST** 降级为"未审查"，检索 **MUST** 仍按基础分工作。

- 违反后果：一次治理失败让整批知识查不到（可用性事故）
- 强制检查点：`retrieve` 对 `governance_weight IS NULL` 按 0 处理，不参与过滤
- 关联 WU：WU-M-17

---

**INV-M-13 · 治理加权不得改 RRF 口径**
治理分数 **MUST** 在 rerank 之后叠加，**MUST NOT** 进入 RRF 融合。

- 违反后果：已按 30 条用例标定的 `RELEVANCE_THRESHOLD_VECTOR / _RERANK` 同时失效，`EvalRun` 基线不可比
- 强制检查点：`app/services/retrieve/__init__.py` 中治理加权位于 rerank 之后
- 关联 WU：WU-M-17

---

**INV-M-14 · 时效性优先最新版本**
检索命中同一 `doc_group_id` 的多个版本时 **MUST** 优先 `is_latest`；非最新版本 **MUST NOT** 进入上下文（显式查询历史除外）。

- 违反后果：用户拿到过期制度作为答案，时效性审查形同虚设
- 强制检查点：`retrieve` 过滤条件含 `is_latest = true`（既有已具备，此处升级为不变量并补测试）
- 关联 WU：WU-M-17

---

**INV-M-15 · 审查提示词走注册表且锁定区含禁止项**
治理与视觉抽取的提示词 **MUST** 注册进 `app/config/prompts.py` + `prompt_registry`；其锁定区 **MUST** 含"不得臆造 / 不得改写原文 / 仅依据给定内容"三类约束。

- 违反后果：管理员在线改掉约束后，审查结论可被臆造；与既有"拒答文案 MUST 在锁定区"同源风险
- 强制检查点：`decisions.self_check()` 对新增 prompt key 的锁定区校验通过
- 关联 WU：WU-M-16

### D 组 · 存储、溯源与上传

---

**INV-M-16 · 原图走对象存储，溯源链接走后端鉴权端点**
图片原始字节 **MUST** 存入既有对象存储（`services/storage.py` / MinIO），`documents.storage_key` 只存 key；溯源链接 **MUST** 由后端鉴权端点提供，**MUST NOT** 向前端直出 MinIO 地址或 presigned URL。

- 违反后果：MinIO 直连地址绕过租户 / KB 级鉴权（可越权读取他人上传图片）；presigned URL 一经泄露在有效期内无法吊销
- 强制检查点：`GET /api/documents/{id}/raw` 走既有鉴权链；响应体中不出现 `S3_ENDPOINT` 值或 `X-Amz-Signature` 签名串
- 关联 WU：WU-M-19
- 反例：`return {"url": f"{s3_endpoint}/{bucket}/{key}?X-Amz-Signature=..."}` ❌

---

**INV-M-17 · 文本化结果必须可人工核对**
图片类知识 **MUST** 支持"原图 + 文本化结果"同屏回显，供人工核对文本化是否正确。

- 违反后果：文本化错误（错字、表格串行、漏读数）无人工检出口，错误向量静默入库并被长期引用
- 强制检查点：`GET /api/documents/{id}/parse-preview` 同时返回原图链接与逐块文本；前端并排展示
- 关联 WU：WU-M-20

---

**INV-M-18 · 批量上传只接受同格式**
批量上传 **MUST** 只接受**同一扩展名**的文件集合；混合格式 **MUST** 在前端拦截，并在后端返回 400。

- 违反后果：混合格式共用一次提交会让解析分派、失败归因、状态机（部分成功 / 部分失败）同时复杂化，问题难定位
- 强制检查点：前端提交前校验扩展名集合基数 == 1；`POST /api/kbs/{kb_id}/documents/batch` 后端复校验
- 关联 WU：WU-M-21
- 反例：`for f in [a.pdf, b.docx]: upload(f)` ❌

### 1.1 不变量速查索引

| 组 | 编号 | 主题 | 关联 WU |
|---|---|---|---|
| A | INV-M-01 | 模态显式判定 | WU-M-02 |
| A | INV-M-02 | 单一向量空间 | WU-M-09 |
| A | INV-M-03 | 图片提问不落库 | WU-M-07 |
| A | INV-M-04 | 视觉不可用不降级 | WU-M-06, 07 |
| A | INV-M-05 | 图片可回链 | WU-M-09, 10 |
| B | INV-M-06 | 出站白名单 | WU-M-12 |
| B | INV-M-07 | 凭据密文 | WU-M-11, 12 |
| B | INV-M-08 | 抓取限速 | WU-M-12 |
| B | INV-M-09 | 页面软失效 | WU-M-14 |
| C | INV-M-10 | 审查不阻塞解析 | WU-M-15 |
| C | INV-M-11 | 审查不改写原文 | WU-M-15 |
| C | INV-M-12 | 治理失败可检索 | WU-M-17 |
| C | INV-M-13 | 加权不进 RRF | WU-M-17 |
| C | INV-M-14 | 时效优先最新 | WU-M-17 |
| C | INV-M-15 | 提示词注册与锁定 | WU-M-16 |
| D | INV-M-16 | 原图存储与溯源回链 | WU-M-19 |
| D | INV-M-17 | 文本化结果可核对 | WU-M-20 |
| D | INV-M-18 | 批量上传同格式 | WU-M-21 |

---

## 2. 数据契约增补

### 2.1 迁移清单

当前最大 revision = `0014_add_audit_trace_id`。新增迁移**严格线性**，一个逻辑一个文件。

| 迁移 | 名称 | 内容 |
|---|---|---|
| `0015` | `add_document_governance` | `documents` 治理字段 + 索引 |
| `0016` | `add_governance_jobs` | `governance_jobs` 表 |
| `0017` | `add_web_sync_fields` | `sync_sources` web 字段 + 改 CHECK 约束 + `chunks.governance_weight` |

### 2.2 表字段（增量）

**`documents`（0015 增补）**

```sql
freshness_score     INTEGER     NULL,      -- 时效性 0-100，NULL = 未审查
authority_score     INTEGER     NULL,      -- 权威性 0-100
consistency_score   INTEGER     NULL,      -- 一致性 0-100
completeness_score  INTEGER     NULL,      -- 完整度 0-100
governance_status   TEXT        NOT NULL DEFAULT 'pending',  -- pending|running|done|failed
governance_report   JSONB       NOT NULL DEFAULT '{}',       -- 四维判定依据（可审计）
source_kind         TEXT        NOT NULL DEFAULT 'upload',   -- upload|sync_local|sync_git|sync_web
effective_at        TIMESTAMPTZ NULL,      -- 生效时间（时效性输入）
expire_at           TIMESTAMPTZ NULL,      -- 失效时间（时效性输入）
content_hash        TEXT        NULL,      -- 内容指纹（web 增量判定）
-- CHECK: governance_status IN ('pending','running','done','failed')
-- CHECK: source_kind IN ('upload','sync_local','sync_git','sync_web')
-- INDEX: idx_doc_governance (tenant_id, kb_id, governance_status)
```

**`governance_jobs`（0016 新增，与 `parse_jobs` 同构）**

```sql
id / tenant_id / document_id / status / attempts / max_attempts
last_error / started_at / finished_at / created_at
-- CHECK: status IN ('queued','running','succeeded','failed','dead')
```

**`sync_sources`（0017 增补）**

```sql
auth_type     TEXT  NOT NULL DEFAULT 'none',  -- none|basic|bearer|cookie
auth_payload  TEXT  NULL,                     -- crypto 加密后的凭据（MUST NOT 明文）
entry_urls    JSONB NOT NULL DEFAULT '[]',    -- 入口 URL 列表
scope_rules   JSONB NOT NULL DEFAULT '{}',    -- 页面发现范围（前缀/正则/深度）
-- ★ 修改既有 CHECK：source_type IN ('local','git','web')
```

**`chunks`（0017 增补）**

```sql
governance_weight INTEGER NOT NULL DEFAULT 0  -- 检索期加权，避免每次 join documents
```

---

## 3. 接口契约增补

### 3.1 REST 清单增补

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | `/api/chat/ask` | 登录 | 请求体增补 `images`（见 3.2） |
| GET | `/api/documents/{id}/governance` | 可见该文档 | 四维结论 + 报告 |
| POST | `/api/documents/{id}/governance/rerun` | admin | 手动重跑审查 |
| POST | `/api/sync/sources` | admin | `source_type` 支持 `web`，增补鉴权/入口字段 |
| POST | `/api/sync/sources/{id}/probe` | admin | 鉴权联通探活（新增） |
| GET | `/api/documents/{id}/raw` | 可见该文档 | 原文件 / 原图流式回链（鉴权代理，见 INV-M-16） |
| GET | `/api/documents/{id}/parse-preview` | 可见该文档 | 解析预览：原图链接 + 逐块文本化结果（见 INV-M-17） |
| POST | `/api/kbs/{kb_id}/documents/batch` | 登录 + KB 可见 | 批量上传，**仅同格式**（见 INV-M-18） |

### 3.2 聊天接口出入参变更

```jsonc
// POST /api/chat/ask  请求体增补字段（其余不变）
{
  "kb_ids": ["..."],
  "question": "...",
  "conversation_id": null,
  "images": [                       // 新增，默认 []
    { "storage_key": "..." }        // 或 { "data_url": "data:image/png;base64,..." }
  ]
}
```

**MUST**：单轮图片数 ≤ 3、单图 ≤ `settings.max_upload_mb`；超限直接 400，**MUST NOT** 静默截断（总规范 §0.5：MUST NOT 发明默认值）。

### 3.3 SSE 事件变更

1. `meta` 事件增补 `modality`（`text|image`）。
2. 事件顺序 **MUST NOT** 变：`meta → stage → citations → delta... → done/refused`。
3. `Citation` 结构 **MUST NOT** 变（图片知识同样按 `chunk_id/doc_id` 溯源）。

### 3.4 内部模块接口（跨模块调用 MUST 走这些签名）

```python
# ── 路由（唯一形态判定点）───────────────────────────────
# app/services/router/modality.py
def detect(*, ext: str | None, has_images: bool, source_type: str | None) -> Modality: ...
def dispatch(modality: Modality) -> str: ...

# ── 视觉理解（复用 LLMService，不改第二套客户端）────────
# app/services/multimodal/vision.py
async def extract_text_from_image(image_ref: ImageRef) -> ParsedBlock: ...

# ── 网页抓取 ────────────────────────────────────────────
# app/services/sync/web_fetcher.py
async def probe_source(source: SyncSource) -> ProbeResult: ...
async def discover_pages(source: SyncSource) -> list[str]: ...
async def fetch_pages(source: SyncSource, urls: list[str]) -> list[FetchedPage]: ...

# ── 治理 ────────────────────────────────────────────────
# app/services/governance/__init__.py
async def review_document(document_id: UUID) -> GovernanceResult: ...

# ── 存储与溯源（复用既有 storage，不新增存储后端）────────
# app/services/storage.py
async def open_object(storage_key: str) -> AsyncIterator[bytes]: ...   # 流式读，供 /raw 端点代理
# app/services/preview.py
async def build_parse_preview(document_id: UUID) -> ParsePreview: ...  # 原图链接 + 逐块文本

# ── 批量上传 ────────────────────────────────────────────
# app/api/kbs.py
def assert_same_ext(files: Sequence[UploadFile]) -> str: ...           # 同格式校验，失败即 400
```

### 3.5 溯源回链的 URL 形态（前后端唯一约定）

```jsonc
// GET /api/documents/{id}/parse-preview 响应（示意）
{
  "document_id": "...",
  "modality": "image",                                  // text | image
  "source": {
    "raw_url": "/api/documents/{id}/raw",               // ★ 相对路径，前端拼 baseURL 请求
    "page_count": 3,                                    // 扫描件多页
    "page_raw_urls": ["/api/documents/{id}/raw?page=1"] // 多页时逐页
  },
  "blocks": [
    { "index": 0, "page_no": 1, "is_table": false, "text": "……" }
  ]
}
```

**MUST**：`raw_url` 一律为**相对路径**（后端鉴权端点），**MUST NOT** 返回对象存储直连地址或签名 URL（INV-M-16）。

---

## 4. 工作单元（Work Units）

### 4.1 总览与批次

共 21 个 WU，分 6 个批次。**批次之间有 GATE，GATE 未通过不得进入下一批次。**

| 批次 | WU | 主题 | GATE |
|---|---|---|---|
| **B7-0** | M-01 ~ M-05 | 地基：口径变更、路由骨架、迁移、多模态签名 | GATE-M-0 |
| **B7-1** | M-06 ~ M-08 | 图片提问（能力域 A） | GATE-M-1 |
| **B7-2** | M-09, M-10, M-19, M-20 | 图片/扫描件入库 + 存储溯源与核对（能力域 B） | GATE-M-2 |
| **B7-3** | M-11 ~ M-14 | 内部网址接入（能力域 C） | GATE-M-3 |
| **B7-4** | M-15 ~ M-18 | 四维审查（能力域 D） | GATE-M-4 |
| **B7-5** | M-21 | 批量上传（同格式约束） | GATE-M-5 |

| WU | 名称 | 依赖 | 不变量 |
|---|---|---|---|
| M-01 | 口径变更：OCR 改配置驱动 | — | — |
| M-02 | 路由层骨架与 `Modality` 枚举 | — | INV-M-01 |
| M-03 | 迁移 0015 / 0016 | — | — |
| M-04 | 迁移 0017（web 字段 + 加权列） | M-03 | — |
| M-05 | `LLMService` 多模态签名放宽 | — | — |
| M-06 | 视觉抽取服务 | M-05 | INV-M-04 |
| M-07 | chat ask 接入图片 + SSE `modality` | M-02, M-06 | INV-M-03, INV-M-04 |
| M-08 | 前端聊天附件交互 | M-07 | — |
| M-09 | 图片解析器 + 扩展名白名单 | M-02 | INV-M-02, INV-M-05 |
| M-10 | 扫描件 PDF 改道视觉链路 | M-06, M-09 | INV-M-05 |
| M-11 | web 源 schema 与凭据加密 | M-04 | INV-M-07 |
| M-12 | web_fetcher：探活 / 发现 / 抓取 | M-11 | INV-M-06, INV-M-07, INV-M-08 |
| M-13 | html 解析清洗器 | M-12 | — |
| M-14 | sync_service web 分支与增量 | M-12, M-13 | INV-M-09 |
| M-15 | 治理任务与四维判定 | M-03, M-05 | INV-M-10, INV-M-11 |
| M-16 | 治理提示词注册 | M-15 | INV-M-15 |
| M-17 | 检索期治理加权 + 时效优先 | M-15 | INV-M-12, INV-M-13, INV-M-14 |
| M-18 | admin 知识治理面板 | M-15, M-17 | — |
| M-19 | 图片存储与溯源回链端点 | M-09 | INV-M-16 |
| M-20 | 解析预览（原图 + 文本化） | M-09 | INV-M-17 |
| M-21 | 批量上传（同格式约束） | M-02 | INV-M-18 |

### 4.2 各 WU 定义

#### B7-0 · 地基

##### WU-M-01 · 口径变更：OCR 改配置驱动
- **产出**：`app/config/decisions.py`（改 `OCR_ENABLED` 与 `self_check` 分支）、`app/config/settings.py`（新增 `ocr_enabled: bool = False`）、两套 `.env` 示例
- **关键动作**：
  - `OCR_ENABLED` 由冻结常量改为读 `settings.ocr_enabled`，`self_check` 中原"抛错禁止"分支改为"读配置 + 关闭时警告"
  - **同步回改** §0.4 表中手册 §1.4 / §3.5 两处（先文档后代码）
- **验收**：`python -c "from app.config.decisions import self_check; self_check()"`（两种配置下均不抛错）
- **DoD**：手册、decisions、settings、.env 四处口径一致，无残留矛盾

##### WU-M-02 · 路由层骨架与 Modality 枚举
- **产出**：`app/services/router/__init__.py`、`app/services/router/modality.py`、`app/infra/metrics.py`（增补路由指标）
- **关键动作**：
  - `detect()` 只读显式信号；**MUST NOT** 做 IO、**MUST NOT** 调模型
  - 每次判定记 `rag.router.decision{modality=...}`
  - 新增 `Modality` 枚举，禁止字符串散落
- **验收**：`pytest tests/router/test_modality.py -q`（扩展名/字段/源类型三组用例 + 未知输入报错用例）
- **DoD**：全仓无第二处 `if ext in (...)` 形态判定

##### WU-M-03 · 迁移 0015 / 0016
- **产出**：`alembic/versions/0015_add_document_governance.py`、`0016_add_governance_jobs.py`、`app/models/entities.py`（对应字段）
- **验收**：`alembic upgrade head && alembic downgrade -1 && alembic upgrade head`
- **DoD**：`alembic_version == 0016`，`documents` 治理字段与索引齐备

##### WU-M-04 · 迁移 0017
- **产出**：`alembic/versions/0017_add_web_sync_fields.py`、`entities.py`（SyncSource / Chunk）
- **关键动作**：**MUST** 先 drop 旧 CHECK 再建新 CHECK（含 `web`）
- **验收**：`alembic upgrade head && alembic downgrade -1 && alembic upgrade head`
- **DoD**：`source_type='web'` 可插入；`chunks.governance_weight` 存在

##### WU-M-05 · LLMService 多模态签名放宽
- **产出**：`app/services/llm.py`
- **关键动作**：`stream_chat` 的 `messages` 类型由 `list[dict[str, str]]` 放宽，支持 OpenAI 兼容 `content: [{type, ...}]`；**MUST NOT** 引入厂商私有参数（沿用"任意 OpenAI 兼容接口"口径）
- **验收**：`pytest tests/llm/test_llm_multimodal.py -q`（构造多模态 body + 流式解析）
- **DoD**：既有纯文本调用参数与行为**完全不变**（回归）

**GATE-M-0**：`self_check()` 通过；迁移可到 `0017` 且可回退；路由单测全绿；纯文本问答回归通过。

---

#### B7-1 · 图片提问

##### WU-M-06 · 视觉抽取服务
- **产出**：`app/services/multimodal/__init__.py`、`vision.py`、`app/schemas/chat.py`（`ImageRef`）
- **关键动作**：复用 `LLMService`；异常直抛（INV-M-04）
- **验收**：`pytest tests/multimodal/test_vision.py -q`（含"模型判定无可读内容"分支）
- **DoD**：无第二套 LLM 客户端

##### WU-M-07 · chat ask 接入图片
- **产出**：`app/api/chat.py`、`app/schemas/chat.py`
- **关键动作**：按路由结论处理图片 → 抽取文本 → 与原 question 拼接后进既有链路；SSE `meta` 增补 `modality`；**MUST NOT** 落库（INV-M-03）
- **验收**：容器内 `pytest tests/chat/test_ask_with_image.py -q`；不传图路径回归通过
- **DoD**：带图问答返回答案与引用；知识库无新增文档

##### WU-M-08 · 前端聊天附件交互
- **产出**：`apps/web/src/pages/ChatPage.tsx`、`apps/web/src/api/*`
- **关键动作**：粘贴 / 上传 / 拖拽；超限前端即拦
- **验收**：`npx tsc -b` 通过；**由用户截图确认交互**
- **DoD**：图片随请求提交，回答区展示 `modality`

**GATE-M-1**：带图问答可用；纯文本问答**逐项回归**通过；知识库无污染。

---

#### B7-2 · 图片/扫描件入库

##### WU-M-09 · 图片解析器 + 白名单
- **产出**：`app/services/parse/image_parser.py`、`parse/base.py`（分派表）、`app/config/settings.py` + 两套 `.env`（`ALLOWED_EXT`）
- **关键动作**：输出 `ParsedBlock`（复用既有契约）；**MUST NOT** 新增向量列（INV-M-02）
- **验收**：`pytest tests/parse/test_image_parser.py -q`
- **DoD**：图片上传 → `ParseJob` → chunk → 可被检索命中

##### WU-M-10 · 扫描件 PDF 改道
- **产出**：`app/services/parse/pdf_parser.py`
- **关键动作**：扫描件分支由抛 `ParseError` 改为转交视觉链路；**文本层优先原则不变**；多页填 `page_no`
- **验收**：`pytest tests/parse/test_pdf_parser.py -q`（文本层 PDF 回归用例 MUST 全绿）
- **DoD**：纯扫描件不再抛错；文本层 PDF 行为与改造前一致

##### WU-M-19 · 图片存储与溯源回链端点
- **产出**：`app/services/storage.py`（增补 `open_object` 流式读）、`app/api/documents.py`（`GET /{id}/raw`）
- **关键动作**：
  - 图片字节存既有 MinIO，`documents.storage_key` 只存 key（**MUST NOT** 把二进制塞进 PostgreSQL）
  - `/raw` 端点走既有鉴权链后流式转发对象，支持 `?page=N`（扫描件逐页）
  - 响应头带 `Content-Type` / `Content-Disposition`；**MUST NOT** 出现对象存储直连地址（INV-M-16）
- **验收**：`pytest tests/documents/test_raw_endpoint.py -q`（含"无权限用户取他人文档 raw 必 403")
- **DoD**：前端可通过相对路径拿图；响应中无 `S3_ENDPOINT` / 签名串

##### WU-M-20 · 解析预览（原图 + 文本化）
- **产出**：`app/services/preview.py`、`app/api/documents.py`（`GET /{id}/parse-preview`）、`app/schemas/document.py`
- **关键动作**：返回 §3.5 约定的结构（`raw_url` 相对路径 + `blocks` 逐块文本 + `page_no`）
- **验收**：`pytest tests/documents/test_parse_preview.py -q`
- **DoD**：结构与 §3.5 一致；`raw_url` 为相对路径

**GATE-M-2**：图片与扫描件可入库并生成引用；**原图可经鉴权端点回链**；**解析预览可同时看到原图与文本化结果**；文本层 PDF 回归通过。

---

#### B7-3 · 内部网址接入

##### WU-M-11 · web 源 schema 与凭据加密
- **产出**：`app/schemas/sync.py`、`app/api/sync.py`、`app/services/crypto.py`（复用）
- **关键动作**：`source_type` 支持 `web`；凭据**MUST** 加密落库（INV-M-07）
- **验收**：`pytest tests/sync/test_web_source_schema.py -q`
- **DoD**：库内无凭据明文；日志无凭据明文

##### WU-M-12 · web_fetcher
- **产出**：`app/services/sync/web_fetcher.py`
- **关键动作**：`probe_source` 建立会话并探活；`discover_pages` 按 `scope_rules` 枚举；`_assert_allowed_domain`（INV-M-06）；限速并发可配（INV-M-08）
- **验收**：`pytest tests/sync/test_web_fetcher.py -q`（白名单外 URL 必拒）
- **DoD**：鉴权失败时源置 `error` 且有明确错误，不进入后续步骤

##### WU-M-13 · html 解析清洗器
- **产出**：`app/services/parse/html_parser.py`
- **关键动作**：去导航 / 页脚 / 脚本；抽正文与表格；表格复用既有 `is_table` 契约
- **验收**：`pytest tests/parse/test_html_parser.py -q`
- **DoD**：表格识别结果与 `chunk` 层"整表不切"契约兼容

##### WU-M-14 · sync_service web 分支与增量
- **产出**：`app/services/sync_service.py`
- **关键动作**：新增 `web` 分支（`local|git` 保持不动）；增量按 `etag / last-modified / content_hash`；页面消失置 `is_latest=false`（INV-M-09）
- **验收**：`pytest tests/sync/test_sync_web_incremental.py -q`；二次同步未变更页面 `last_sync_count == 0`
- **DoD**：完整走通"鉴权 → 发现 → 清洗 → 切片 → 增量"

**GATE-M-3**：内网站点可接入；增量生效；凭据与出域约束通过自查。

---

#### B7-4 · 四维审查

##### WU-M-15 · 治理任务与四维判定
- **产出**：`app/services/governance/__init__.py`、`app/workers/parse_job.py`（注册 `run_governance_job`）
- **关键动作**：四维各有可判定判据；**只写治理字段**（INV-M-11）；独立任务（INV-M-10）
- **验收**：`pytest tests/governance/test_review.py -q`
- **DoD**：四维结论落库且有 `governance_report` 依据

##### WU-M-16 · 治理提示词注册
- **产出**：`app/config/prompts.py`、`app/services/prompt_registry.py`
- **关键动作**：锁定区含"不得臆造 / 不得改写原文 / 仅依据给定内容"（INV-M-15）
- **验收**：`python -c "from app.config.decisions import self_check; self_check()"`
- **DoD**：新增 prompt key 可在线切版本，锁定区不可在线改

##### WU-M-17 · 检索期治理加权 + 时效优先
- **产出**：`app/services/retrieve/__init__.py`、`app/config/decisions.py`（加权系数常量）
- **关键动作**：加权位于 rerank **之后**（INV-M-13）；`governance_weight IS NULL` 按 0（INV-M-12）；`is_latest=true` 过滤（INV-M-14）
- **验收**：`pytest tests/retrieve/test_governance_weight.py -q` + `scripts/run_eval.py --baseline`（回退不得超既有 `--max-regression`）
- **DoD**：治理结论影响排序；未审查文档仍可检索

##### WU-M-18 · admin 知识治理面板
- **产出**：`apps/web/src/pages/admin/GovernancePanel.tsx`、`AdminPage.tsx`
- **验收**：`npx tsc -b` 通过；**由用户截图确认**
- **DoD**：可按 KB / 状态筛选，展示四维结论与依据

**GATE-M-4**：四维审查全链路可用；`eval` 基线不回退；治理失败不阻断检索。

---

#### B7-5 · 批量上传

##### WU-M-21 · 批量上传（同格式约束）
- **产出**：`app/api/kbs.py`（`POST /{kb_id}/documents/batch`）、`app/schemas/document.py`、`apps/web/src/pages/**`（文档上传组件）
- **关键动作**：
  - 后端：`assert_same_ext(files)` 校验扩展名集合基数 == 1，混合格式**直接 400**（INV-M-18）
  - 后端：逐个文件复用既有 `upload_document` 内部流程（存 MinIO → 建 `Document` → 建 `ParseJob` → 入队），**MUST NOT** 另写一套上传逻辑
  - 前端：多选文件后**提交前**校验同格式，混合即拦并给出明确提示
  - 返回逐文件结果（成功/失败与原因），便于定位
- **验收**：`pytest tests/kbs/test_batch_upload.py -q`（同格式通过 / 混合格式必 400）；`npx tsc -b` 通过；**由用户截图确认交互**
- **DoD**：同格式批量可上传并各自入队；混合格式在前端与后端均被拦

**GATE-M-5**：批量上传同格式可用；混合格式被拦；单文件上传行为与改造前一致（回归）。

---

## 5. 决策表（**待你拍板** · 含百分制打分）

> 打分口径：与既有架构契合度 40% + 改动面/风险 30% + 可维护性 20% + 工期 10%。
> 按既有规则：**< 90 分的方案我不建议直接开发**；若你选该项，我会先给出优化方案再开发。

| # | 决策点 | 候选方案 | 分 | 说明 |
|---|---|---|---|---|
| D1 | 图片内容如何进入检索体系 | **A. 文本化后进同一向量空间** | **92** | 复用 1024 维文本 embedding，与 RRF/rerank 天然兼容 |
| | | B. 新增图文双向量空间 | 74 | 需迁表结构 + 改 RRF/rerank 三处；bge-reranker 不支持图文混合 |
| | | C. 图片只做问题增强、不进库 | 81 | 不满足"知识库支持图片"要求 |
| D2 | 视觉理解模型选型 | **A. 复用现有 LLM 视觉能力** | **91** | 走 OpenAI 兼容 `image_url`，贴合"任意 OpenAI 兼容接口"定位，无新容器 |
| | | B. 本地 OCR 容器 | 85 | 纯文字更准但无版面理解，新增权重与容器 |
| | | C. 独立 VLM 容器 | 88 | 版面好但 +4GB 内存与冷启动（embedding/reranker 已踩过 10 分钟冷启动坑） |
| D3 | 内部网址抓取方式 | **A. 会话式 HTTP + 站点地图/链接发现** | **93** | httpx 维持会话，适配内部 Wiki/文档站，占用低易限速 |
| | | B. 无头浏览器渲染后抓 | 89 | 支持 JS 站点，但新增 Chromium 依赖与内存 |
| | | C. A 为主 + B 兜底 | 82 | 折中方案，两套链路维护与排障成本翻倍 |
| D4 | 四维审查计算时机 | **A. 入库后异步治理 + 落库 + 检索加权** | **93** | 不阻塞解析、不在线耗时，结论可审计可复算 |
| | | B. 检索期在线计算 | 70 | 每次问答多一次 LLM 调用，不可审计 |
| | | C. 规则优先 + LLM 兜底 | 88 | 两套逻辑并存 |
| D5 | 治理加权的作用位置 | **A. rerank 之后叠加** | **92** | 不破坏既有 RRF(k=60) 与已标定阈值 |
| | | B. 进 RRF 参与融合 | 76 | 已标定阈值失效，基线不可比 |
| | | C. 只展示不参与排序 | 80 | 不满足"审查作用于检索" |
| D6 | `OCR_ENABLED` 口径变更 | **必须批准**（非选择题） | — | 现为 fail-closed 冻结口径，`self_check` 会抛错；需按 §0.4 先改手册再改代码 |
| D7 | 图片存储与溯源链接方式 | **A. 字节存既有 MinIO + 后端鉴权代理端点回链** | **92** | 复用既有 storage；统一走租户/KB 鉴权；链接为稳定相对路径 |
| | | B. MinIO presigned URL 直出 | 83 | URL 有时效、需 MinIO 对外可达，且泄露后有效期内无法吊销 |
| | | C. 图片二进制存 PostgreSQL `bytea` | 62 | 撑大 DB 与备份，与 pgvector 抢同库资源，违反单一职责 |
| D8 | 文本化核对的前端形态 | **A. 文档详情/上传结果页并排（左原图｜右文本块）** | **92** | 一眼可比对，符合"清晰看出文本化是否正确" |
| | | B. 独立「解析核对」tab | 88 | 可用但需多一次跳转，核对效率低 |
| | | C. 只在 admin 治理面板展示 | 84 | 上传者看不到，问题反馈链路长 |
| D9 | 批量上传的同格式约束落地 | **A. 前端提交前拦截 + 后端复校验** | **93** | 双重防线，前端即时反馈、后端兜底；符合"避免出现问题" |
| | | B. 仅后端校验 | 86 | 用户要等请求返回才知道被拒，体验差 |
| | | C. 前端逐文件串行上传 | 78 | 退回单文件语义，批量无实际收益，且产生 N 个提交 |

**请逐点选定（D1~D5、D7~D9）并批准 D6。**

---

## 6. 与上游文档的章节索引

| 本文档 | 上游文档章节 | 用途 |
|---|---|---|
| §0.4 回改清单 | 手册 §1.3 / §1.4 / §3.5 / §4.2 / §5.1 / §6 / §8 | 口径变更流程 |
| §1 A 组 | 开发流程 第 3 章、第 14 章；手册 §3.3 | 解析与分块契约 |
| §1 B 组 | 开发流程 第 14 章 14.5；手册 §8 D-07 | 同步一致性与外部源 |
| §1 C 组 | 开发流程 第 9 章；手册 §7.3 | 评估体系与门禁 |
| §2 数据契约 | 手册 §4.2；总规范 §3 | DDL 与 Table 清单 |
| §3 接口契约 | 手册 §5.1；总规范 §4 | API 与 SSE |
| §4 WU 批次 | 总规范 §5 | 工作单元体例 |

---

## 7. 汇报格式

**沿用总规范 §10**：单个 WU 完成后 MUST 输出 `状态 / 产出文件 / 验收命令与结果 / 触及的不变量及自查 / 契约变更 / 偏离与风险 / BLOCKER` 块；标记规则（`DONE` / `PARTIAL` / `BLOCKED`）与禁止事项同总规范 §10.2。

---

## 附 · 快速自检清单（每个 WU 收尾时逐条打勾）

```
□ 验收命令全部通过，输出已贴进汇报
□ 触及的不变量逐条自查，结论已写明
□ 新增/修改的契约（API / 表 / 枚举 / 环境变量）已同步回本文档与手册
□ 手工册回改清单（§0.4）逐项落实，无"手册说不做、代码已做"
□ 无新增向量空间、无新增 LLM 客户端、无绕过 grounding / 注入防护
□ 无硬编码阈值、无硬编码 Prompt、无裸字符串枚举
□ 凭据一律密文存储，日志与 meta 无明文
□ 原图走对象存储，溯源链接为后端鉴权端点的相对路径（无 MinIO 直连地址 / 签名 URL）
□ 图片类知识可同时看到原图与文本化结果
□ 批量上传已在前端与后端双向校验同格式
□ 治理路径不写 content，检索路径未绕过 retrieve
□ 迁移均有 downgrade，且可重复执行
```
