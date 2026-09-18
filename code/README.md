# 企业知识库问答 AI Agent

私有化部署的企业知识库 RAG 问答系统。上传 PDF / Word / Excel / Markdown / 纯文本，自动解析分块向量化，支持多知识库、多轮对话、权限隔离、密级脱敏、审计追溯、配额限流、敏感词拦截与灰度开关。

---

## 一、快速启动（本地 · 10 分钟）

**前提**：装了 Docker Desktop（Windows/macOS）或 Docker + Docker Compose（Linux），机器内存 **16GB+**。

```bash
# 1. 进入项目目录
cd code

# 2. 可选：编辑 .env（改 LLM API Key、改管理员密码、换端口）
#    不编辑也行，默认值能跑起来

# 3. 一键启动（首次约 10-30 分钟，取决于模型下载速度）
docker compose up -d --build

# 4. 观察进度
docker compose logs -f api          # 看到 "Uvicorn running on ...:8000" 即启动完成

# 5. 打开浏览器
#    前端: http://localhost:5173
#    后端 API: http://localhost:8000/api/health  （全绿=正常）
#    MinIO 控制台: http://localhost:9001  （存储原始文档）
```

首次启动会自动：
- 执行数据库迁移（0001 ~ 0007）
- 初始化默认租户 + admin 角色 + admin 账号 + 公开知识库
- 下载 bge-m3（embedding，~2GB）和 bge-reranker-v2-m3（重排，~1GB）到本地缓存

### 内置账号

| 账号 | 密码 | 角色 |
|------|------|------|
| admin | `ChangeMe123!` | 超级管理员（密级 40，全权限） |

登录后请立即在「系统管理 → 用户管理」修改密码。

### 演示流程

1. 用 admin 登录 → 系统管理 → 新建一个业务知识库
2. 上传 PDF 或 Markdown 文档 → 等待状态变为「已索引」
3. 回到首页 → 勾选刚建的知识库 → 提问 → 流式返回答案 + 右侧引用原文
4. 切换到「公开知识库」提问 → 对比权限差异

---

## 二、同局域网演示

同事/家人在同一 WiFi 下，直接用你的电脑内网 IP 访问：

```
# Windows PowerShell 查你的内网 IP
ipconfig | findstr "IPv4"
# 输出类似：192.168.1.100

# 别人的浏览器输入
http://192.168.1.100:5173
```

放行防火墙端口（管理员 PowerShell 执行一次）：

```powershell
netsh advfirewall firewall add rule name="KAgent Web" dir=in action=allow protocol=TCP localport=5173
netsh advfirewall firewall add rule name="KAgent API" dir=in action=allow protocol=TCP localport=8000
```

注意：不要把 pg/redis/minio 端口暴露到公网。

---

## 三、外网演示（零服务器成本）

别人不在同一局域网，但你又不想买云服务器 — 用内网穿透：

### 方案 A：cpolar（国内友好，免费够用）

```bash
# 1. 下载安装 cpolar（https://www.cpolar.com/）
# 2. 一条命令把本地 5173 端口暴露到公网
cpolar http 5173

# 3. 输出里会有一个公网域名，类似：
#    Forwarding  http://xxxx.cpolar.io -> localhost:5173
# 4. 把 http://xxxx.cpolar.io 发给别人，直接打开
```

### 方案 B：ngrok（国际版）

```bash
# 1. 下载安装 ngrok（https://ngrok.com/），注册拿免费 token
# 2. 暴露端口
ngrok http 5173

# 3. 输出里的 Forwarding 链接发给别人
```

### 安全警告

穿透时你的本地服务直接暴露到公网。**务必做这三件事**：

1. 改 `.env` 里的 `SECRET_KEY`（`openssl rand -hex 32` 生成新值）
2. 用一个没余额的 LLM API Key（防止盗刷）
3. 演示完立刻 Ctrl+C 停掉穿透进程

---

## 四、阿里云服务器部署

### 推荐规格

| 场景 | ECS 规格 | vCPU | 内存 | 月费参考 |
|------|----------|------|------|----------|
| demo 展示（嵌入+重排走本地） | `ecs.c7.share.xlarge` | 4 核 | 8 GB | ~200 元 |
| 小团队生产 | `ecs.c7.2xlarge` | 8 核 | 16 GB | ~600 元 |
| 正式生产 | `ecs.c7.4xlarge` | 16 核 | 32 GB | ~1200 元 |

> 如果嵌入/重排改用外部 API（`.env` 设 `EMBEDDING_BASE_URL` / `RERANK_BASE_URL`），内存可以压到 4-8GB。

### 一键部署脚本

```bash
# 1. 装 Docker（Ubuntu 22.04）
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker

# 2. 下载代码（或 git clone）
git clone https://gitee.com/zhang_-xiao/rag_-agent.git
cd rag_-agent/code

# 3. 编辑 .env（必须改：SECRET_KEY、LLM_API_KEY、ADMIN_PASSWORD）
#    cp .env.prod.example .env  # 生产模板
#    nano .env

# 4. 一键启动
docker compose up -d --build

# 5. 等待启动完成（首次下载模型 5-30 分钟）
docker compose logs -f api

# 6. 打开浏览器
#    http://<服务器公网 IP>    （80 端口映射）
```

详细生产部署见 [docs/DEPLOY.md](docs/DEPLOY.md)。

---

## 五、项目架构

### 容器拓扑

```
                    [外部 LLM API]
                         │
                    ┌────▼────┐
                    │  api     │  ── Python/FastAPI/uvicorn
                    │  :8000   │
                    └────┬────┘
           ┌─────────────┼─────────────┐
           │             │             │
      ┌────▼────┐  ┌─────▼────┐  ┌─────▼────┐
      │ postgres│  │  redis   │  │  minio   │
      │ pgvector│  │          │  │          │
      └─────────┘  └──────────┘  └──────────┘
           │             │
      ┌────▼─────────────▼────┐
      │      worker (arq)      │  ── 异步文档解析任务
      └───────────────────────┘
           │             │
      ┌────▼────┐  ┌─────▼────┐
      │embedding│  │ reranker │   ── 本地 CPU 推理
      │ bge-m3  │  │ bge-rer  │
      └─────────┘  └──────────┘

      ┌─────────┐
      │  web    │  ── React 18 + Nginx
      │  :5173  │
      └─────────┘
```

### 技术栈

| 层 | 技术 |
|----|------|
| 后端框架 | FastAPI + Python 3.12 + asyncpg |
| 数据库 | PostgreSQL 16 + pgvector 0.7 |
| 缓存/队列 | Redis 7 + arq 异步任务 |
| 对象存储 | MinIO（兼容 S3） |
| 嵌入模型 | bge-m3（本地 CPU 推理，1024 维） |
| 重排模型 | bge-reranker-v2-m3（本地 CPU 推理） |
| LLM | OpenAI 兼容接口（DeepSeek / 通义千问等） |
| 前端 | React 18 + TypeScript + Vite + Ant Design |
| 部署 | Docker Compose |

### 目录结构

```
code/
├── apps/
│   ├── api/                     # 后端
│   │   ├── app/
│   │   │   ├── api/             # REST 路由层（21 个模块）
│   │   │   ├── services/        # 业务层（检索/生成/配额/敏感词/特性开关等）
│   │   │   ├── models/          # SQLAlchemy ORM（PostgreSQL + pgvector）
│   │   │   ├── schemas/         # Pydantic 请求/响应模型
│   │   │   └── config/          # 配置 + 全局口径常量
│   │   ├── alembic/versions/    # 7 版数据库迁移（0001~0007）
│   │   └── Dockerfile
│   └── web/                     # 前端
│       ├── src/
│       │   ├── pages/           # ChatPage / AdminPage / KnowledgeBasesPage
│       │   ├── pages/admin/     # 8 个管理面板（组织/用户/权限/审计/配额...）
│       │   └── mocks/           # API 客户端 + 类型定义
│       └── Dockerfile           # 多阶段构建：Vite → Nginx
├── docker-compose.yml           # 开发环境（8 服务）
├── docker-compose.prod.yml      # 生产环境（4 服务 + 2 个 API worker）
├── .env.prod.example            # 生产配置模板
└── docs/
    └── DEPLOY.md                # 生产部署指南（F1-F5 验收 + §9 回滚指南）
```

---

## 六、功能清单

| 编号 | 功能 | 说明 |
|------|------|------|
| F01 | 文档上传 | PDF / md / txt / xls / xlsx / docx，单文件 ≤100MB，**不支持扫描件 OCR** |
| F02 | 自动管道 | 解析 → 分块 → 向量化 → 入库，arq 异步任务 + 进度轮询 |
| F03 | 混合检索 | 向量相似度 + ILIKE 关键词 → RRF 融合 → bge-reranker 重排 → 0.35 阈值闸门 |
| F04 | 生成回答 | DeepSeek / 通义千问 SSE 流式输出，**带引用溯源**（点击 [n] 打开原文片段） |
| F05 | 回答边界 | 只依据检索到的知识库作答，检索为空 → 拒答（三段式 grounding check） |
| F06 | 账号登录 | RSA 加密密码 + JWT access/refresh token + Redis 黑名单 |
| F07 | 组织架构 | 部门树（物化路径）+ 用户组 + 角色，admin 可增删改移 |
| F08 | 知识库成员 | 四种主体（用户/部门/组/角色），支持加/删/替换 |
| F09 | chunk 级权限 | 四闸门判定（KB 成员 + 密级 + 拒绝标签 + 部门可见），检索时 SQL 下推过滤 |
| F10 | 公开知识库 | 全租户唯一，所有登录用户可见 |
| F11 | 权限缓存 | 变更 → 立即失效 Redis + 重新计算 acl_tags |
| F12 | 密级脱敏 | 4 档（公开/内部/机密/绝密），回答中手机号/身份证/银行卡自动打码 |
| F13 | 审计日志 | 谁、什么时候、问了什么、引用了哪些文档、拒答原因，可按动作/日期过滤 |
| F14 | 多租户 | tenants 表隔离，Docker Compose 一键起全套 |
| F15 | 数据同步 | 本地目录 + Git 源，定时增量，手动触发 |
| F16 | 多轮对话 | 会话列表 + 消息历史 + 最近 3 轮上下文拼接 + 记忆压缩 |
| F17 | 安全加固 | 提示注入防护（系统提示约束）+ Redis 令牌桶限流（20次/分钟）+ 配额四级降级 + 敏感词双向拦截 + 按部门灰度特性开关 |

### 管理面板（admin 登录可见）

| Tab | 功能 |
|-----|------|
| 组织架构 | 部门树增删改移 |
| 用户管理 | 用户 CRUD + 重置密码 |
| 用户组 | 组 CRUD + 成员管理 |
| 角色管理 | 角色 CRUD + 重命名自动同步 |
| 审计日志 | 动作/日期筛选 + 详情 |
| 系统监控 | P50/P95/P99 延迟 + 拒答率 |
| 评估门禁 | 跑 eval_cases 用例，检查可答/拒答准确率 |
| 配额管理 | 用户/租户双层 token 限额 + 用量进度条 |
| 敏感词 | 敏感词增删查 + 批量录入 + 命中即拒答 |
| 灰度开关 | 按部门特性开关 + 百分比放量 + 关闭即回滚 |

---

## 七、常见问题

### Q: 启动后 embedding/reranker 一直不健康？

首次启动在下载模型权重（各 ~1-2GB），`docker compose logs embedding` 能看到下载进度。如果网络慢，设 `HF_ENDPOINT=https://hf-mirror.com` 加速。

### Q: 启动后 pg_isready / redis 报错？

`.env` 里的密码只在**首次初始化**时生效。改了密码需要：停服务 → 删 `postgres_data` 卷 → 重启。

### Q: 上传文档后一直解析中？

看 `docker compose logs worker`。解析任务失败会自动重试 3 次，再失败进死信队列。常见原因：文档损坏、格式不支持、MinIO 不可达。

### Q: 提问拒答「未找到相关内容」？

- 知识库选对了吗？
- 文档解析完成了吗（状态「已索引」）？
- 密级够吗（高密级文档低密级用户看不到）？
- 问题里加几个文档里的关键词再试

### Q: 配额/敏感词/灰度开关不起作用？

M6 安全加固功能默认关闭。需要到「灰度开关」tab 手动创建规则并启用：
- `quota / % / 100% / 启用` → 开启配额管理
- `sensitive_filter / % / 100% / 启用` → 开启敏感词拦截
- `rerank / % / 100% / 启用` → 开启重排

### Q: 想改管理员密码？

admin 密码只在 `seed_prod.py` 首次创建时读取 `ADMIN_PASSWORD` 环境变量。后续改 `.env` 里的密码不会重置已有账号。进入系统后在「用户管理」里重置，或手动调用 `POST /api/users/{id}/reset-password`。

### Q: 想临时停掉？

```bash
docker compose down        # 停所有服务（不删数据）
docker compose up -d       # 再启动
```

数据卷（postgres_data / minio_data / redis_data / hf_cache）独立于容器生命周期，down 不会丢数据。

---

## 八、升级与回滚

升级到新版本：

```bash
git pull
docker compose up -d --build api worker web
# 启动时自动执行 alembic upgrade head
```

M6 回滚方式（按影响从小到大）：

| 方式 | 操作 | 影响 |
|------|------|------|
| **功能回滚（零停服）** | 灰度开关 tab 关闭对应规则 | 秒级，60 秒缓存过期生效 |
| **迁移回滚** | `docker compose exec api alembic downgrade -1` | 需停 api+worker |
| **镜像回滚** | `git checkout <旧commit>` → `docker compose up -d --build` | 退回上一版本 |

详细回滚决策树见 [docs/DEPLOY.md §9](docs/DEPLOY.md)。

---

## 九、生产验收清单

| # | 操作 | 期望 |
|---|------|------|
| F1 | 干净机器按 DEPLOY.md 部署 | 30 分钟内起来，能登录、能问、能看审计 |
| F2 | 连续两次创建公开知识库 | 第二次被唯一索引拒绝 |
| F3 | 文档含手机号/身份证提问 | 回答中打码，引用原文不打码 |
| F4 | 查审计日志 | 能看到谁、什么时候、问了什么、引用到哪些文档 |
| F5 | 备份 + 恢复 | 恢复后对话、文档、账号完整 |
