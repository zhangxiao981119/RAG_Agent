# 生产环境部署指南（M5 · 手册 F1/F5 验收）

本文档指导在一台干净机器上完成知识库问答 Agent 的私有化部署。
**目标：30 分钟内完成部署，能登录、能提问、能查看审计日志。**

---

## 1. 机器要求

| 项目 | 最低配置 | 说明 |
|------|----------|------|
| 操作系统 | Linux（Ubuntu 22.04 / CentOS 8+） | Windows/macOS 可用 Docker Desktop 做验证 |
| Docker | Engine 24+，Compose v2 | `docker compose version` 能输出版本号 |
| CPU | 4 核 | 嵌入与重排模型使用 CPU 推理（torch） |
| 内存 | 16 GB | embedding 稳态约 1.4GB、reranker 双引擎驻留约 2.4GB（启动峰值超 3GB），加基础服务与系统开销 |
| 磁盘 | 30 GB | 模型权重约 5GB（首次下载）、数据库、文档、镜像 |
| 网络 | 可访问公网 | 拉取镜像、pip/npm 依赖、HuggingFace 模型、LLM API |

> 若嵌入/重排改用**外部模型端点**（在 `.env` 设置 `EMBEDDING_BASE_URL` / `RERANK_BASE_URL`
> 并删除两个本地模型服务），内存最低可降到 8GB。

> 模型权重默认从国内镜像站 `https://hf-mirror.com` 下载；如服务器完全离线，
> 需提前在有网机器下载 `BAAI/bge-m3` 与 `BAAI/bge-reranker-v2-m3` 并导入 `hf_cache` 数据卷。

---

## 2. 快速部署（Linux，一键脚本）

```bash
# 1. 解压交付包，进入编排目录
cd code

# 2. 执行一键初始化（自动生成密钥、收集 LLM API Key 与管理员密码、构建、启动、健康等待）
bash scripts/init-prod.sh
```

脚本完成后访问 `http://<服务器IP>`，使用 `admin` + 初始化时设置的密码登录。

脚本是**幂等**的：重复执行不会覆盖已有的 `.env`，也不会删除数据。

---

## 3. 手动部署步骤

适用于 Windows / macOS（Docker Desktop）或需要逐步控制的场景。

### 3.1 准备配置

```bash
cd code
cp .env.prod.example .env
```

编辑 `.env`，**必须修改**的项：

| 变量 | 说明 |
|------|------|
| `SECRET_KEY` / `JWT_SECRET` | 各执行一次 `openssl rand -hex 32` 生成（Windows 可用 Git Bash） |
| `ADMIN_PASSWORD` | admin 初始密码，至少 10 位含大小写与数字 |
| `POSTGRES_PASSWORD` / `REDIS_PASSWORD` / `MINIO_ROOT_PASSWORD` | 基础设施强密码 |
| `LLM_API_KEY` | DeepSeek（或其他 OpenAI 兼容服务）的 API Key |

完整变量含义见 `.env.prod.example` 内的中文注释与手册附录 B。

### 3.2 构建并启动

```bash
# -p kagent 固定项目名，保证数据卷、容器名称在后续运维命令中一致
docker compose -p kagent -f docker-compose.prod.yml up -d --build
```

### 3.3 观察启动过程

```bash
# 查看全部服务状态（首次启动 embedding/reranker 需要下载模型，约 10-30 分钟）
docker compose -p kagent -f docker-compose.prod.yml ps

# 跟踪 api 日志：会依次执行 alembic 迁移 → seed_prod 初始化 → uvicorn 启动
docker compose -p kagent -f docker-compose.prod.yml logs -f api
```

`seed_prod` 会创建（均幂等）：默认租户 `default` → `admin` 角色 → 管理员账号 → 唯一公开知识库「公共知识库」。

### 3.4 验证健康

```bash
curl http://localhost/api/health
# 返回数据库、Redis、模型服务的可达状态，全部为 ok 即正常
```

---

## 4. 上线验收清单（对应手册 F1–F5）

| # | 操作 | 期望结果 |
|---|------|----------|
| F1 | 浏览器访问 `http://<服务器IP>`，用 admin 登录 | 进入知识问答界面，左侧自动加载最近对话 |
| F1 | 在「知识库」新建业务库 → 上传一份 pdf/docx/xlsx/md 文档 | 文档解析完成，状态变为可用 |
| F1 | 针对文档内容提问 | 流式返回答案，右侧引用栏显示来源文档与片段 |
| F3 | 提问含手机号/身份证/银行卡的文档 | **回答中号码已打码**（如 `138****5678`），引用原文保持不打码 |
| F4 | 管理后台 → 审计日志 | 可查"谁、什么时间、问了什么、引用了哪些文档" |
| F2 | （管理员）尝试创建第二个公开知识库 | 被系统拒绝（全租户唯一） |
| F5 | 执行第 5 节备份并恢复 | 恢复后对话、文档、账号完整 |

---

## 5. 备份与恢复（F5）

业务数据分布在两个数据卷：

| 数据卷 | 内容 | 是否必须备份 |
|--------|------|--------------|
| `kagent_postgres_data` | 账号、权限、对话、审计、文档元数据 | 必须 |
| `kagent_minio_data` | 上传的原始文档文件 | 必须 |
| `kagent_redis_data` | 缓存/异步任务队列 | 不需要（可重建） |
| `kagent_hf_cache` | 模型权重缓存 | 不需要（可重新下载） |

### 5.1 备份（逻辑备份，推荐）

```bash
cd code

# PostgreSQL：导出为带时间戳的 SQL 文件
docker compose -p kagent -f docker-compose.prod.yml exec -T postgres \
  pg_dump -U kagent -d kagent > "backup-pg-$(date +%Y%m%d-%H%M%S).sql"

# MinIO：打包整个对象数据目录（停服片刻可保证一致性；不停服建议用 mc mirror）
docker run --rm -v kagent_minio_data:/data -v "$PWD":/backup alpine \
  tar czf "/backup/backup-minio-$(date +%Y%m%d-%H%M%S).tar.gz" -C /data .
```

建议加入 crontab 每日执行，并将备份文件转存到服务器以外的存储。

### 5.2 恢复

```bash
cd code

# 1. 恢复 PostgreSQL（目标库需已初始化，即服务至少启动过一次）
cat backup-pg-XXXXXXXX-XXXXXX.sql | docker compose -p kagent -f docker-compose.prod.yml \
  exec -T postgres psql -U kagent -d kagent

# 2. 恢复 MinIO 文件
docker run --rm -v kagent_minio_data:/data -v "$PWD":/backup alpine \
  sh -c "cd /data && tar xzf /backup/backup-minio-XXXXXXXX-XXXXXX.tar.gz"

# 3. 重启应用让连接与缓存刷新
docker compose -p kagent -f docker-compose.prod.yml restart api worker
```

### 5.3 整卷级灾备（冷备）

停机状态下可直接打包数据卷目录，方式同 5.1 的 MinIO 打包，把卷名换成 `kagent_postgres_data` 即可。

---

## 6. 日常运维命令

均在 `code/` 目录下执行：

```bash
COMPOSE="docker compose -p kagent -f docker-compose.prod.yml"

$COMPOSE ps                      # 查看服务状态
$COMPOSE logs -f api             # 跟踪 api 日志
$COMPOSE restart api             # 重启单个服务
$COMPOSE down                    # 停止全部服务（不删除数据卷）
$COMPOSE up -d                   # 重新启动
$COMPOSE pull && $COMPOSE up -d  # 更新基础镜像后重启（应用镜像需重新 build）
```

**升级应用代码**：

```bash
git pull   # 或替换交付包
$COMPOSE up -d --build api worker web
```

启动时 `alembic upgrade head` 会自动执行数据库迁移；迁移前请先按第 5 节备份。

---

## 7. 生产拓扑与安全说明

- **唯一入口**是 web 容器的 `WEB_PORT`（默认 80）。PostgreSQL / Redis / MinIO / 模型服务 / api 均**不绑定宿主机端口**，只在容器内网通信。
- Redis 在生产编排中启用了 `requirepass`。
- 所有服务配置 `restart: unless-stopped`，宿主机重启后自动拉起。
- 容器日志统一轮转：单文件 50MB、最多 5 个，防止打满磁盘。
- 建议在宿主机前置 Nginx/负载均衡终止 HTTPS（证书由部署方提供）。
- `admin` 初始密码仅用于首次登录，**登录后请立即修改**。

---

## 8. 故障排查

| 现象 | 排查方向 |
|------|----------|
| `up` 时提示 `必须在 .env 设置` | `.env` 缺变量或未创建；按 3.1 从 `.env.prod.example` 复制并填写 |
| embedding/reranker 一直不健康 | 首次启动在下载模型权重，`logs embedding` 可见进度；下载失败可改 `HF_ENDPOINT` 或配置代理 |
| `/api/health` 数据库/Redis 报错 | `$COMPOSE logs postgres redis`；确认 `.env` 中密码未在首次初始化后被修改（改密码需同步重建数据卷） |
| api 反复重启 | `logs api` 查看 traceback；常见为 `LLM_API_KEY` 未配置或迁移失败 |
| 登录提示密码错误 | 确认 seed_prod 日志出现「创建管理员」；`ADMIN_PASSWORD` 只在首次创建时生效，改值不会重置已有账号 |
| 80 端口被占用 | 修改 `.env` 的 `WEB_PORT=8080`，访问时带端口 |
| 文档预览/上传失败 | 确认 `worker` 服务在线且 `minio` 健康；解析任务失败会自动重试 3 次 |

---

## 9. 回滚

按影响范围由小到大分三类：**功能回滚**（线上零停服，秒级生效）→ **迁移回滚**（需停服或维护窗口）→ **镜像回滚**（退回旧版本交付包）。

### 9.1 功能回滚（特性开关，推荐首选）

配额管理、敏感词过滤、重排等特性均接入 `feature_flags` 表，admin 在「系统管理 → 灰度开关」关闭对应规则即等价于回滚该功能，缓存 TTL 60s 内全租户生效。

| 特性 key | 关闭后的效果 |
|----------|--------------|
| `quota` | 不再执行配额检查与四级降级，所有提问按无配额策略放行 |
| `sensitive_filter` | 不再做输入/输出敏感词命中检测 |
| `rerank` | 检索结果不再重排，直接用向量分 + RRF 融合 |

操作步骤：

1. 管理员登录 → 系统管理 → 灰度开关
2. 找到对应 `feature_key` 的规则 → 关闭 Switch（或删除整条规则）
3. 等待最多 60 秒，Redis 缓存过期后该特性在全租户范围内关闭

如需更细粒度（按部门）回滚，可编辑规则的 `dept_path_pattern` 把命中范围缩小到空集，或把 `rollout_percent` 改为 0。

### 9.2 迁移回滚（数据库 schema 回退）

Alembic 迁移按版本号顺序执行，回退用 `downgrade` 命令。**迁移回滚前必须按第 5 节备份数据库**，且回退期间需停止 `api` 与 `worker` 避免写入冲突。

```bash
cd code
COMPOSE="docker compose -p kagent -f docker-compose.prod.yml"

# 1. 停止应用层（保留 postgres / redis）
$COMPOSE stop api worker

# 2. 查看当前迁移版本
$COMPOSE exec -T postgres psql -U kagent -d kagent -c "SELECT version_num FROM alembic_version;"

# 3. 回退一个版本（例如 0007 → 0006）
$COMPOSE exec -T api alembic downgrade -1

# 4. 或回退到指定版本
$COMPOSE exec -T api alembic downgrade 0006

# 5. 回退完成后重启应用层
$COMPOSE up -d api worker
```

> 注意：`downgrade` 会执行迁移文件中的 `downgrade()` 函数，对应 0007 会 `DROP TABLE` 三张表（tenant_quotas / sensitive_words / feature_flags）。如该表已有线上数据且可能复用，请先 `pg_dump` 备份对应表。

### 9.3 镜像回滚（退回上一交付版本）

适用于：代码级故障（如 chat 流式输出异常、迁移逻辑错误）导致整版本不可用，需快速退回上一个稳定版本。

```bash
cd code
COMPOSE="docker compose -p kagent -f docker-compose.prod.yml"

# 1. 切换交付包到上一稳定版本（保留 .env 不变）
git checkout <上一稳定 tag 或 commit>   # 或解压旧交付包覆盖 code/

# 2. 用旧代码重建并启动（迁移会自动 alembic upgrade head）
$COMPOSE up -d --build api worker web

# 3. 观察日志确认正常
$COMPOSE logs -f api
```

如已执行过新版迁移且新版表结构有破坏性变更（如 0007 建表后又在 0008 改了列类型），需先按 9.2 执行 `alembic downgrade <旧版本>` 再启动旧镜像，否则旧代码会因 schema 不匹配报错。

### 9.4 回滚决策树

```
故障现象
  │
  ├─ 单一特性异常（如配额误拒答 / 敏感词误拦截）
  │     → 9.1 关闭对应 feature_flag（秒级，零停服）
  │
  ├─ 多特性同时异常，怀疑是新迁移引入
  │     → 9.2 alembic downgrade -1（需停 api+worker）
  │
  └─ 整体不可用（启动失败 / 全量提问报错）
        → 9.3 退回上一交付镜像 + 9.2 回退迁移
```

---

## 附：交付物清单

```
code/
├── docker-compose.prod.yml     # 生产编排
├── .env.prod.example           # 生产配置模板（复制为 .env 使用）
├── scripts/
│   └── init-prod.sh            # 一键初始化脚本
├── docs/
│   └── DEPLOY.md               # 本文档
└── apps/api/scripts/
    └── seed_prod.py            # 生产最小种子（租户/管理员/公开库，幂等）
```
