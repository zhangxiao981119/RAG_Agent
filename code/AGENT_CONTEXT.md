# AGENT_CONTEXT - 给 Agent 的上下文恢复文件

> 这个文件是给 Trae Agent 读的，不是给人看的 README。如果你是新接手的 Agent，读完这个文件你应该能瞬间知道：这是什么项目、改了哪些坑、什么不能动、接下来做什么。
> 生成时间: 2026-09-22

---

## 1. 项目定位

**企业知识库 RAG 问答 Agent**。FastAPI 后端 + React18 前端，Docker Compose 8 容器编排。

核心链路：文档上传 -> MinIO 存储 -> 解析+分块 -> bge-m3 embedding -> pgvector -> vector+ILIKE 混合召回 -> RRF(k=60) 融合 -> bge-reranker-base 重排(阈值 0.35) -> LLM(SSE 流式) -> grounding 归因检查 -> 拒答/输出。

技术栈：
- 后端: Python 3.11 + FastAPI + SQLAlchemy + asyncpg + arq
- 前端: React 18 + TypeScript + Vite + Antd 5
- 数据: PostgreSQL(+pgvector) + Redis + MinIO
- 模型: bge-m3(embedding, 1024维) + bge-reranker-base(重排) + qwen3-plus(LLM)
- 编排: Docker Compose 8 容器 (postgres/redis/minio/embedding/reranker/worker/api/web)

---

## 2. 硬约束（来自 project_memory.md，不能动）

```
1. 所有 middleware 必须 async/await，不用 callback 风格
2. 路由文件在 middleware 重构时不能改
3. API 响应严格遵循 §5.2 schema ({"code":0, "data":..., "msg":""})
4. LLM 流式序列: meta→stage→citations→delta...→done / refused
5. 文档解析覆盖 PDF/xlsx/md/txt/docx/xls，扫描版 PDF 抛错
6. 表格整表作为一个chunk；超 CHUNK_MAX_TOKENS 时按行组切，**每组重复注入表头**
7. PDF 表格由 pdfplumber 识别（pypdf 只出文本层，会把列关系拍平）；
   同页正文用词坐标排除表格区域，避免同一数据入块两次
8. Embedding 维度固定 1024
9. Rerank 超时 3s，超时降级为 vector_score 显示
10. LLM 首 token 超时 15s
11. 检索: vector + tsquery + ILIKE → RRF(k=60) → rerank，阈值 0.50
12. Grounding 检查剥离无归因句子，空时降级拒答
13. Parse 失败重试 3 次后进死信队列
14. LLM 编号列表每项单独一行
15. 对话历史压缩到最近 6 条（3 轮问答）
16. 所有业务 API 需要 JWT Bearer Token
```

---

## 3. 架构关键决策（已锁定，不要改）

| 决策 | 选项 | 为什么 |
|---|---|---|
| 重排模型 | bge-reranker-base (200MB) | v2-m3 在 2GB mem_limit 下 OOM |
| 前端加密 | jsencrypt@3.3.2 (PKCS1-v1_5) | Web Crypto 在 LAN http 下 `crypto.subtle` undefined |
| RSA 密钥持久化 | Redis SETNX 单例 | 多 worker 各自生成不同密钥会解密失败 |
| Worker 队列 | arq + Redis | parse→chunk→embed→write 四步流水线 |
| LLM 流式 | 后端 passthrough 前端逐帧 | 去掉 typewriter 假打字，首 token 3.9s |
| 数据库 | PostgreSQL + pgvector | FK SET NULL 软删除，gen_random_uuid |
| PII 脱敏 | 在 generate 出口 _ensure_line_breaks 之后 | 只处理 text 字段，保留 citations.snippet |
| Docker 内存 | Desktop 至少 12GB，reranker 2g / embedding 4g | 两个模型加载时峰值占内存 |

---

## 4. 里程碑进度

| M0 | 2026-09-16 | Docker 编排 + 种子数据 + 基础设施跑通 |
|---|---|---|
| M1 | 2026-09-16 | RAG 核心: 解析+分块+embedding+rerank+流式+grounding |
| M2 | 2026-09-16 | 真实检索链验收，6 个启动 bug 现场修复 |
| M3 | 2026-09-17 | 鉴权大模块: JWT(24h) + RSA 2048 + Redis 限流(10次/分) + 5次失败锁定15分钟 + 组/角色/部门/ACL epoch |
| M4 | 2026-09-17 | 知识库成员管理 + doc acl_tags + 前端界面补全 |
| M5 | 2026-09-18 | 部署包: docker-compose.prod.yml + seed_prod.py + DEPLOY.md + OnboardingModal |
| M6 | 2026-09-18 | 安全加固: 配额管理 + 敏感词 + 灰度开关(FeatureFlagPanel) + JsonFormatter |
| 深度审查 | 2026-09-21 | P0~P3 约 49 项修复 (见第 5 节) |

---

## 5. 深度审查修复清单（P0~P3, 11 commit, ~49 项）

### P0 越权 (已修复)
- 后端所有业务路由强制 JWT 验证 (deps.py get_current_user)
- 前端路由守卫 admin 权限检查

### P2-A 安全
- 硬编码密钥从 .env.example 移除（已用占位符）
- CORS 限制允许域名
- Redis / API 连接池配置化

### P2-B1 鉴权
- logout 死锁修复（异步 session 清理顺序）
- refresh rotation 竞态（同一 token 不能被两次 refresh）
- 软删除用户从列表过滤

### P2-B2 性能/阻塞
- sync_service 异常分支写回 last_error
- generate LLM failed 补 final flush
- history 压缩阈值 `>` 改 `>=`
- retrieve rerank idx 范围检查防越界
- embedding/rerank/llm rstrip 改 removesuffix
- JsonFormatter 防 None 崩溃

### P2-B2-quick + P3 快修
- 前端 main.tsx 加 ErrorBoundary 防白屏
- ChatPage SSE 断网保留已流出内容
- AppLayout 去掉 nullish 兜底（直接报错比静默好）

### P3 资源/连接池
- arq_pool 全局单例复用（不要每次 new）
- _incr_usage 时序修复（先写 DB 再发事件）

### P3 配置安全
- .env.example 全替换占位符
- CORS origins 白名单化
- Redis password 在 prod 强制设置

### P3 业务 bug
- **audit LIKE 转义**: service 层对 doc%, doc_, a\b 字面量匹配正确转义
- **audit 时间边界**: HTTP 层无效日期返回 422
- **chat 消息占位**: DB 里 user/assistant 同时刻创建，流式结束无 generating 残留
- **embedding 条数校验**: 模型截断时抛 EmbeddingCountError

### P3 前端体验
- **uploadDocument fetch→XMLHttpRequest**: fetch 标准不支持 upload progress，XHR `upload.onprogress` 是唯一方案，加 onProgress 可选回调；保留 401 refresh 重试
- **DocumentsPage**: Dragger 下方 `<Progress percent />` 进度条；Table 加 rowSelection checkbox + 批量删除(Promise.allSettled 并行)；pagination 客户端分页 pageSize=10；轮询 effect 改 selectedKb 依赖 + documentsRef 镜像，避免定时器因数组引用反复重建
- **ChatPage SSE 断网**: catch 块区分 user abort vs 网络断开；断网保留 fullText，标记 networkError=true + done=true；MessageBubble 渲染橙色断网提示行 + 重试按钮；canOperate 加 && !msg.networkError 隐藏重复操作栏

### Migration 0008
- FK ondelete SET NULL
- gen_random_uuid 默认值
- CheckConstraint 加 NOT NULL
- 索引优化

---

## 6. 踩过的坑（经验教训）

| 坑 | 根因 | 解法 |
|---|---|---|
| antd TableRowSelection 导入失败 | antd/es/table 不导出这个类型 | 直接传对象，onChange 显式标类型 `(keys: React.Key[])` |
| fetch 不支持 upload progress | fetch 没有 upload.onprogress 等价物 | 必须用 XMLHttpRequest |
| SSE 断网追加 refused 气泡误导 | catch 块没区分 abort vs 网络错误 | 非 abort 时保留 fullText + 标记 networkError |
| Web crypto 在 LAN http 下 undefined | crypto.subtle 只在 https 或 localhost 可用 | 换 jsencrypt，RSA-OAEP 改 PKCS1-v1_5 |
| Worker 各自生成不同 RSA 密钥 | 内存生成不持久化，多 worker 解密失败 | Redis SETNX 单例持久化 |
| Docker mem_limit 2g reranker OOM | v2-m3 模型峰值 4GB+ | 换 base 版 200MB，或提 mem_limit 到 4g |
| 轮询 effect 因 documents 变化重建 | 依赖数组放了 documents[] | 依赖只放 selectedKb，documentsRef 镜像最新值 |
| Compress-Archive 压 Desktop 被拒 | Sandbox 限制 C:\Users\...\Desktop | 输出到工作区内路径 |
| PowerShell heredoc 不支持 | PowerShell 5 没有 bash heredoc | git commit 用多个 -m 参数 |
| PostgreSQL public schema | pgvector 扩展需在 public 创建 | alembic 0001 里 CREATE EXTENSION vector |
| reranker 超时 3s 后显示_score 乱 | 自动降级 vector_score 但名字没换 | display_score fallback 到 rerank_score → vector_score |
| 扫描版 PDF 无文本层 | pypdf 提取为空 | 抛错提示用户 |
| PDF 里的表格检索不到列含义 | pypdf 只出文本层，表格被拍平成字符流 | 用 pdfplumber 填 `is_table` / `table_header` |
| 同一份表格数据被 embedding 两次 | 表格块 + 拍平的文本块各存一份 | 同页正文用词坐标排除表格区域 |
| 分块任务卡死不报错 | 表格「表头+一行数据」超 MAX → `_accumulate` 死循环 | 见硬约束 6；已加单测钉住 |
| arq worker 3 次失败后指数退避 | 31499s 约 8.7 小时 | 清 Redis retry record + 手动 parse |
| 本地路径 hash 换电脑对不上 | Trae project_id 含绝对路径 hash | 手动读 memory 目录里的 topics.md |

---

## 7. 启动命令

```powershell
cd code
# .env 里已有 API key, Docker Desktop 至少分配 12GB 内存
docker compose up -d --build

# 验证
docker compose ps  # 8 容器 healthy
curl http://localhost:8000/api/health  # {"db":"ok", "redis":"ok", "vector":"ok", "llm":"ok", "embedding":"ok", "rerank":"ok"}

# 前端 http://localhost:5173
# 种子账号 admin / ChangeMe123!
```

---

## 8. 目录结构速查

```
code/
├── apps/
│   ├── api/              FastAPI 后端
│   │   ├── app/
│   │   │   ├── api/          路由 (auth, chat, docs, jobs, kbs, admin, groups)
│   │   │   ├── services/     业务层 (auth, chat, retrieve, generate, grounding, crypto, quota, sensitive, feature_flag)
│   │   │   ├── workers/      arq 任务 (parse_job)
│   │   │   ├── models/       SQLAlchemy ORM
│   │   │   ├── schemas/      Pydantic schema (严格遵循 §5.2)
│   │   │   ├── config/       settings.py, decisions.py
│   │   │   └── migrations/   alembic (0001~0008)
│   │   └── Dockerfile
│   └── web/              React18 前端
│       ├── src/
│       │   ├── pages/        ChatPage, DocumentsPage, KnowledgeBasesPage, AdminPage, LoginPage
│       │   ├── mocks/        data.ts (真实 fetch + SSE)
│       │   └── App.tsx
│       └── Dockerfile (nginx)
├── docker-compose.yml
├── .env
├── .env.example
├── .env.prod.example
├── README.md
└── AGENT_CONTEXT.md      ← 你正在读的文件
```

---

## 9. 给接手 Agent 的指令

读完本文件 + memory/projects/*/ 下的 topics.md 后，请按以下顺序行动：

1. **用当前文件 + topics.md 回答自己**: 这个项目的检索链是什么？为什么 reranker 用 base 版？SSE 断网怎么处理？
2. **启动环境验证**: `docker compose up -d --build`，确认 8 容器 healthy，跑一遍完整问答流程
3. **git init + 推 GitHub**: `git init` → `git add .` → `git commit -m "feat: init RAG Agent"` → `git remote add origin <你的仓库>` → `git push -u origin main`
4. **告诉我**: 你对项目的理解，以及你想接下来做什么（部署上线？还是加新功能？）

有任何代码细节不确定，直接搜文件或问我。
