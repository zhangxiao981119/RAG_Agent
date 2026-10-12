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

# 2. 执行一键初始化（自动生成密钥、收集 LLM API Key / 管理员密码 / 访问地址、构建、启动、健康等待）
bash scripts/init-prod.sh
```

脚本完成后访问 `https://<站点地址>`（自签证书，浏览器首次访问有告警，详见 §7），使用 `admin` + 初始化时设置的密码登录。

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
| `JWT_SECRET` | `openssl rand -hex 32` 生成（Windows 可用 Git Bash） |
| `SITE_ADDRESS` | 对外访问地址：内网 IP 或内部主机名（如 `192.168.1.100` / `rag.corp.local`） |
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

启动顺序由编排保证：**postgres 健康 → migrate 一次性服务执行 `alembic upgrade head` → migrate 成功后 api/worker 才启动**。migrate 失败则下游服务不启动（fail-fast），避免用旧 schema 跑新代码。

```bash
# 查看全部服务状态（首次启动 embedding/reranker 需要下载模型，约 10-30 分钟）
docker compose -p kagent -f docker-compose.prod.yml ps

# 观察迁移（一次性服务）：Exited (0) 表示迁移成功
docker compose -p kagent -f docker-compose.prod.yml logs migrate

# 跟踪 api 日志：seed_prod 初始化 → uvicorn 启动
docker compose -p kagent -f docker-compose.prod.yml logs -f api
```

`seed_prod` 会创建（均幂等）：默认租户 `default` → `admin` 角色 → 管理员账号 → 唯一公开知识库「公共知识库」。

> 日常发布升级请用 `bash scripts/release.sh`（先备份再 `up -d --build`），不要单独 `restart api` —— 迁移只在编排启动时由 migrate 服务执行。

### 3.4 验证健康

```bash
# 推荐用 /api/health 探针（自签证书加 -k 跳过校验）
curl -fsSk https://<站点地址>/api/health
# 返回数据库、Redis、模型服务的可达状态，全部为 ok 即正常
```

---

## 4. 上线验收清单（对应手册 F1–F5）

| # | 操作 | 期望结果 |
|---|------|----------|
| F1 | 浏览器访问 `https://<站点地址>`，用 admin 登录 | 进入知识问答界面，左侧自动加载最近对话 |
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
| `kagent_caddy_data` | Caddy 内置 CA 与站点证书 | 建议（丢失后 CA 重新生成，客户端需重新信任根证书） |

### 5.1 备份（逻辑备份，推荐）

**推荐：一键备份脚本**（PostgreSQL + MinIO 一起打包到 `backups/<时间戳>/`）：

```bash
cd code
bash scripts/backup.sh
# 可用 BACKUP_DIR=/mnt/backup bash scripts/backup.sh 指定输出目录
```

`release.sh` 发布前会自动调用该脚本，因此每次发布都留有发布前备份。

如需手工执行：

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
$COMPOSE logs migrate            # 查看最近一次迁移结果（一次性服务）
$COMPOSE restart api             # 重启单个服务（不触发迁移）
$COMPOSE down                    # 停止全部服务（不删除数据卷）
$COMPOSE up -d                   # 重新启动（按依赖顺序重跑 migrate → api/worker）
$COMPOSE pull && $COMPOSE up -d  # 更新基础镜像后重启（应用镜像需重新 build）
```

**升级应用代码（发布流程）**：

```bash
# 推荐：走发布脚本（先备份，再构建并启动；migrate 服务会自动执行迁移）
bash scripts/release.sh

# 等价手工流程：
git pull                              # 或替换交付包
bash scripts/backup.sh                # 迁移前先备份（第 5 节）
$COMPOSE up -d --build                # migrate → api/worker → web/gateway 按顺序拉起
$COMPOSE logs migrate                 # 确认迁移 Exited (0)
```

> 迁移只在 `up` 时由 **migrate 一次性服务**执行，`restart api` 不会触发迁移。若需单独执行迁移/回退，见 9.2。

---

## 7. 生产拓扑与安全说明

- **唯一入口**是 gateway（Caddy）容器的宿主机 `80` / `443`，负责 TLS 终止与安全响应头（HSTS / nosniff / X-Frame-Options / Referrer-Policy），并反代到 web。web / api / PostgreSQL / Redis / MinIO / 模型服务均**不绑定宿主机端口**，只在容器内网通信（web 仅 `expose`）。
- **TLS 证书**：内网自签，由 Caddy 内置 CA 自动签发与续期（`tls internal`），无需公网域名与 ACME；HTTP（80）自动 301 跳转到 HTTPS（443）。
- Redis 在生产编排中启用了 `requirepass`。
- 所有服务配置 `restart: unless-stopped`，宿主机重启后自动拉起。
- 容器日志统一轮转：单文件 50MB、最多 5 个，防止打满磁盘。
- `admin` 初始密码仅用于首次登录，**登录后请立即修改**。

### 7.1 信任自签根证书（消除浏览器告警）

```bash
# 导出 Caddy 内置 CA 的根证书到当前目录
docker compose -p kagent -f docker-compose.prod.yml exec gateway \
  cat /data/caddy/pki/authorities/local/root.crt > kagent-root.crt
```

将 `kagent-root.crt` 分发到各客户端并导入「受信任的根证书颁发机构」：

- Windows：双击证书 → 安装到「本地计算机」→ 放入「受信任的根证书颁发机构」
- macOS：钥匙串访问 → 系统 → 导入后设为「始终信任」
- Linux：导入系统或浏览器信任库

导入后重开页面即无告警。根证书由 `kagent_caddy_data` 卷持久化，**若删除该数据卷，CA 会重新生成，客户端需重新导入**。

---

## 8. 故障排查

| 现象 | 排查方向 |
|------|----------|
| `up` 时提示 `必须在 .env 设置` | `.env` 缺变量或未创建；按 3.1 从 `.env.prod.example` 复制并填写 |
| embedding/reranker 一直不健康 | 首次启动在下载模型权重，`logs embedding` 可见进度；下载失败可改 `HF_ENDPOINT` 或配置代理 |
| `/api/health` 数据库/Redis 报错 | `$COMPOSE logs postgres redis`；确认 `.env` 中密码未在首次初始化后被修改（改密码需同步重建数据卷） |
| api 反复重启 | `logs api` 查看 traceback；常见为 `LLM_API_KEY` 未配置或迁移失败 |
| 登录提示密码错误 | 确认 seed_prod 日志出现「创建管理员」；`ADMIN_PASSWORD` 只在首次创建时生效，改值不会重置已有账号 |
| 80 / 443 端口被占用 | gateway 固定监听 80/443，如被占用需停掉占用进程（Linux `ss -ltnp`，Windows `netstat -ano | findstr :443`） |
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
$COMPOSE run --rm migrate alembic downgrade -1

# 4. 或回退到指定版本
$COMPOSE run --rm migrate alembic downgrade 0006

# 5. 回退完成后重启应用层
$COMPOSE up -d api worker
```

> 回滚复用编排中的 `migrate` 一次性服务（`run --rm migrate` 等价于临时启动一个迁移容器），
> 与发布时执行迁移用的是同一镜像与同一 `DATABASE_URL`，避免手工 `exec api` 时依赖 api 容器状态。

> 注意：`downgrade` 会执行迁移文件中的 `downgrade()` 函数，对应 0007 会 `DROP TABLE` 三张表（tenant_quotas / sensitive_words / feature_flags）。如该表已有线上数据且可能复用，请先 `pg_dump` 备份对应表。

### 9.3 镜像回滚（退回上一交付版本）

适用于：代码级故障（如 chat 流式输出异常、迁移逻辑错误）导致整版本不可用，需快速退回上一个稳定版本。

```bash
cd code
COMPOSE="docker compose -p kagent -f docker-compose.prod.yml"

# 1. 【仅当本次发布引入了新迁移】先用【当前（新）代码】的 migrate 服务把 schema 退回旧版本。
#    必须在切换代码之前执行 —— 旧交付包不含新迁移文件，切回去后就无法再 downgrade。
$COMPOSE stop api worker
$COMPOSE run --rm migrate alembic downgrade <旧版本，如 0006>

# 2. 切换交付包到上一稳定版本（保留 .env 不变）
git checkout <上一稳定 tag 或 commit>   # 或解压旧交付包覆盖 code/

# 3. 用旧代码重建并启动；migrate 服务执行 `alembic upgrade head`，此时 head 即旧版本
$COMPOSE up -d --build

# 4. 观察日志确认迁移与启动正常
$COMPOSE logs migrate
$COMPOSE logs -f api
```

> - 若本次发布**未引入新迁移**（纯代码回滚），跳过第 1 步，直接切换代码后 `up -d --build` 即可；
>   旧代码的 `alembic upgrade head` 在 schema 已是该版本时为空操作。
> - 顺序不可颠倒：`downgrade` 依赖新代码里的迁移文件，先切旧包会导致无迁移可回退。

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

## 10. 可观测性与告警（Grafana / Prometheus）

生产编排内置 `prometheus` + `grafana` 两个服务，均为**内网**（不发布宿主机端口）：

- `prometheus` 每 15s 抓取一次 `api:8000/metrics`（Prometheus 文本格式），指标留存 30 天（卷 `kagent_prometheus_data`）。
- `grafana` 经 gateway 的同一 TLS 入口以子路径访问：**https://&lt;SITE_ADDRESS&gt;/grafana/**，登录账号为 `.env` 的 `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD`（数据落卷 `kagent_grafana_data`）。

### 10.1 指标来源

- `api` 进程暴露 `GET /metrics`（Prometheus 文本）：业务计数器与耗时直方图，另含进程级默认指标（`process_*` / `python_*`）。
- 业务指标口径：名称中的点号被规范化为下划线，计数器带 `_total` 后缀，耗时直方图带 `_seconds` 后缀。例：`rag_rerank_degraded_total`、`rag_gate_refused_total`、`rag_llm_first_token_timeout_total`、`http_request_duration_seconds_bucket`。
- `http_request_duration_seconds` 的 `path` 标签取**路由模板**（如 `/api/documents/{document_id}`）而非原始 URL，避免时间序列基数膨胀。
- api 固定单 worker（`--workers 1`）：指标在进程内聚合，多 worker 会导致同一端口只抓到随机副本、`rate()` 跳变。

### 10.2 内置看板

provisioning 自动加载看板 **KAgent RAG 可观测**（文件夹 `KAgent`），含：API 抓取状态、请求 QPS、5xx 错误率、延迟 P95/P50、重排降级速率、L1 闸门拒答率、LLM 首 token 超时、质量与安全异常计数、进程内存 RSS。

### 10.3 内置告警规则

文件 `ops/grafana/provisioning/alerting/rules.yml` 预置 7 条 Grafana 内置告警（`critical`：API 不可用 / 健康检查 503 / LLM 首 token 超时；`warning`：5xx 错误率、重排持续降级、拒答率异常、P95 延迟过高）。

**通知渠道需自行配置**：私有化环境无法预知 SMTP / webhook 地址，故未预置 Contact point。在 Grafana → Alerting → Contact points 新建通知渠道（如邮件、Webhook 飞书机器人），再到 Notification policies 将默认策略指向该渠道；未配置前告警仍在 Alerting → Alert rules 中可见 Firing 状态。

### 10.4 排障与验证

```bash
# 容器内直接看指标（确认 /metrics 已输出业务指标）
docker compose -p kagent -f docker-compose.prod.yml exec api curl -s http://localhost:8000/metrics | head

# 看 Prometheus 抓取目标是否 UP（Prometheus 仅内网，临时映射端口排障后用 Ctrl+C 结束）
docker compose -p kagent -f docker-compose.prod.yml exec prometheus wget -qO- http://localhost:9090/api/v1/targets | head

# 健康检查（自签证书加 -k）
curl -k https://<SITE_ADDRESS>/api/health
```

> ⚠️ 若在 `.env` 中设置了 `TRUSTED_HOSTS`，必须把 `api:8000` 一并列入，否则 Prometheus 以容器名抓取时 Host 不匹配，会被 api 以 400 拒绝、导致抓取中断。

---

## 附：交付物清单

```
code/
├── docker-compose.prod.yml     # 生产编排
├── .env.prod.example           # 生产配置模板（复制为 .env 使用）
├── ops/
│   ├── Caddyfile               # TLS 入口配置（Caddy 自签 + 安全头 + 反代 web + /grafana 子路径）
│   ├── prometheus/
│   │   └── prometheus.yml      # 抓取配置（api:8000/metrics）
│   └── grafana/
│       ├── provisioning/       # 数据源 / 看板 / 告警规则（文件化 provisioning）
│       └── dashboards/         # 看板 JSON（KAgent RAG 可观测）
├── scripts/
│   ├── init-prod.sh            # 一键初始化脚本（生成密钥/收集配置/构建启动）
│   ├── backup.sh               # 备份脚本（PostgreSQL + MinIO → backups/<时间戳>/）
│   └── release.sh              # 发布脚本（先备份再 up -d --build，migrate 自动执行迁移）
├── docs/
│   └── DEPLOY.md               # 本文档
└── apps/api/scripts/
    └── seed_prod.py            # 生产最小种子（租户/管理员/公开库，幂等）
```
