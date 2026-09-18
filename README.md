

# RAG Agent

## 简介

RAG Agent 是一个基于检索增强生成（Retrieval-Augmented Generation, RAG）的企业级问答系统平台。本项目专注于解决大模型在企业落地时的**数据安全**与**权限管控**问题，通过精细化的访问控制列表（ACL）和多租户隔离机制，确保数据“可用不可见”。

系统提供了完整的知识库管理、多格式文档解析、流式对话交互以及后台管理功能，支持私有化部署以满足极高的合规要求。

## 核心特性

*   **🛡️ 精细化权限控制 (ACL)**：
    *   基于部门、用户组、角色的多维权限体系。
    *   文档和用户设置**密级标签**（公开 < 内部 < 机密 < 绝密）。
    *   继承逻辑：下级部门默认继承上级权限，支持反向可见性控制。
*   **🔍 混合检索与排序**：
    *   支持向量检索与关键词检索的融合（RRF 算法）。
    *   集成 Rerank（重排序）模型，提升检索精度。
*   **📄 多格式文档解析**：
    *   原生支持 PDF, DOCX, Markdown, Excel (XLS/XLSX), 纯文本等格式解析。
    *   自动语义分块（Chunking），保留上下文关联。
*   **💬 智能对话体验**：
    *   流式输出 (SSE)，实时响应。
    *   溯源引用：生成的回答可精确追溯到原始文档片段。
    *   敏感词过滤与脱敏，保护隐私信息 (PII Masking)。
*   **⚙️ 强大的管理后台**：
    *   用户与组织架构管理。
    *   知识库成员与配额管理。
    *   审计日志 (Audit Log) 记录所有关键操作。
    *   特性开关 (Feature Flags) 与系统度量 (Metrics)。

## 技术栈

*   **后端**: Python 3.12, FastAPI, SQLAlchemy (Async), Alembic
*   **前端**: React 18, TypeScript, Ant Design, Vite
*   **数据库**: PostgreSQL (主数据), Redis (缓存/会话/限流)
*   **基础设施**: Docker, Docker Compose

## 快速开始

### 环境准备

*   Python >= 3.12
*   Node.js >= 18
*   PostgreSQL >= 14
*   Redis >= 6.2

### 1. 克隆与依赖安装

```bash
# 克隆仓库
git clone https://gitee.com/zhang_-xiao/rag_-agent.git
cd rag_-agent/code

# 安装后端依赖
cd apps/api
python -m pip install -r pyproject.txt

# 安装前端依赖
cd ../web
npm install
```

### 2. 配置环境变量

后端配置位于 `apps/api` 目录。

```bash
# 复制示例配置
cp apps/api/.env.example apps/api/.env
# 编辑 .env 文件，配置数据库连接、Redis、LLM API Key 等
```

### 3. 数据库初始化

确保 PostgreSQL 和 Redis 已启动。

```bash
cd apps/api

# 执行数据库迁移
alembic upgrade head

# 初始化基础数据（角色、部门、超级用户等）
python -m scripts.seed
```

### 4. 启动服务

**启动后端 API:**

```bash
cd apps/api
uvicorn app.main:app --reload --port 8000
```

**启动前端开发服务器:**

```bash
cd apps/web
npm run dev
```

访问 `http://localhost:5173` (默认端口) 即可使用。

## 项目结构

```
rag_-agent/
├── apps/
│   ├── api/                 # FastAPI 后端服务
│   │   ├── app/
│   │   │   ├── api/         # 路由接口定义
│   │   │   ├── models/      # 数据库实体 (SQLAlchemy Models)
│   │   │   ├── schemas/     # Pydantic 数据模型
│   │   │   ├── services/    # 核心业务逻辑 (ACL, RAG, LLM, Sync)
│   │   │   ├── workers/     # 异步任务 Worker
│   │   │   └── config/      # 配置管理
│   │   ├── alembic/         # 数据库迁移脚本
│   │   ├── scripts/         # 运维脚本 (seed, eval)
│   │   └── tests/           # 单元测试与 ACL 策略测试
│   │
│   └── web/                 # React 前端应用
│       ├── src/
│       │   ├── pages/       # 页面视图 (Chat, Admin, KB)
│       │   ├── components/  # 公共组件
│       │   ├── mocks/       # 数据模拟
│       │   └── store/       # 状态管理
│       └── ...
│
├── docs/                    # 文档
│   └── DEPLOY.md            # 生产环境部署指南
├── scripts/                 # 根目录工具脚本
└── docker-compose.yml       # 开发环境编排
```

## 功能模块详解

### 1. 权限系统 (ACL)

这是本系统的核心差异点，位于 `app/services/acl/`。
*   **可见性判断**：用户在提问或检索时，系统会根据其 `clearance`（ clearance 等级）与文档/部门的 `level`（密级）进行对比，拒绝低于密级的数据返回。
*   **隔离性**：实现了租户 (Tenant) 隔离、群组可见性以及部门继承逻辑。

### 2. 知识库与同步 (Sync)

*   支持手动上传文档，也支持配置 `SyncSource` 自动从 Git 仓库拉取文档更新。
*   文档解析服务在 `app/services/parse/`，支持多种格式的预处理。

### 3. RAG 流程

1.  **Retrieve**: `app/services/retrieve/` - 混合检索（向量+关键词）。
2.  **Rerank**: `app/services/rerank/` - 对检索结果进行精排。
3.  **Generate**: `app/services/generate/` - 构建 Prompt 调用 LLM，并构建引用 (Citation)。
4.  **Grounding**: `app/services/grounding/` - 校验 LLM 回复是否基于提供的上下文。

### 4. 评测 (Evaluation)

系统内置了评测脚本，位于 `scripts/run_eval.py` 和 `app/services/eval_service.py`，用于验证 RAG 流程的准确率和拒答率。

## 部署指南

请参考 `docs/DEPLOY.md` 获取详细的生产环境部署、备份恢复及运维命令。

## 测试

项目包含针对 ACL 权限逻辑的深度单元测试，确保权限控制的准确性。

```bash
cd apps/api
pytest tests/acl/ -v
```

## 许可

本项目在 Gitee 开源，具体许可协议请查看项目根目录 LICENSE 文件。