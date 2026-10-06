# 部署手册

> **项目定位、技术栈、设计取舍、功能清单**见仓库根目录 [README.md](../README.md)。
> **生产环境**的完整步骤（一键脚本、配置清单、备份恢复命令、回滚执行细节、
> 验收清单 F1–F5）见 [docs/DEPLOY.md](docs/DEPLOY.md)。
>
> 本文负责回答一个问题：**我该走哪条路，以及怎么把它跑起来。**

| 场景 | 去哪节 | 耗时 |
|------|--------|------|
| 本地跑起来看看 | [二、本地启动](#二本地启动开发--体验) | 10-30 分钟（首次要下模型） |
| 给同事实地演示 | [三、局域网](#三同局域网演示) | 1 分钟 |
| 给远方的人演示 | [四、外网穿透](#四外网演示零服务器成本) | 5 分钟 |
| 真正上线 | [五、生产部署](#五生产部署云服务器) → [docs/DEPLOY.md](docs/DEPLOY.md) | 30-60 分钟 |

---

## 一、前置条件

| 项目 | 要求 | 说明 |
|------|------|------|
| Docker | Engine 24+ / Compose v2 | `docker compose version` 能输出版本号 |
| 内存 | **16 GB**（本地最少 8 GB） | embedding 稳态约 1.4GB、reranker 启动峰值超 3GB |
| 磁盘 | 30 GB | 模型权重约 5GB（仅首次下载）+ 数据 + 镜像 |
| 网络 | 可访问公网 | 拉镜像、装依赖、下载模型、调用 LLM API |
| LLM | OpenAI 兼容接口 | 默认 `deepseek-v4-flash`；私有化可指向内网网关 |

> **内存压到 8GB 的办法**：在 `.env` 设置 `EMBEDDING_BASE_URL` / `RERANK_BASE_URL`
> 指向外部模型端点，并移除编排里的 `embedding` / `reranker` 两个服务。
>
> **完全离线的服务器**：需提前在有网机器下载 `BAAI/bge-m3` 与
> `BAAI/bge-reranker-v2-m3`，再导入 `hf_cache` 数据卷。模型默认从
> `hf-mirror.com` 下载，慢的话在 `.env` 设 `HF_ENDPOINT`。

---

## 二、本地启动（开发 / 体验）

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

首次启动自动完成三件事：

1. 执行数据库迁移（`0001 ~ 0009`）
2. 初始化默认租户 + admin 角色 + admin 账号 + 公开知识库（幂等）
3. 下载 bge-m3（约 2GB）与 bge-reranker-v2-m3（约 1GB）到本地缓存

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

## 三、同局域网演示

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

注意：**不要把 pg / redis / minio 端口暴露出去** —— 开发编排里它们没有认证强度要求。

---

## 四、外网演示（零服务器成本）

别人不在同一局域网，但你又不想买云服务器 —— 用内网穿透：

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

穿透时你的本地服务**直接暴露到公网**。务必做这三件事：

1. 改 `.env` 里的 `SECRET_KEY`（`openssl rand -hex 32` 生成新值）
2. 用一个**没余额**的 LLM API Key（防止盗刷）
3. 演示完立刻 `Ctrl+C` 停掉穿透进程

---

## 五、生产部署（云服务器）

### 5.1 资源规划

| 场景 | vCPU | 内存 | 说明 |
|------|------|------|------|
| demo 展示 | 4 核 | 8 GB | 嵌入+重排走本地模型 |
| 小团队生产 | 8 核 | 16 GB | 推荐起步 |
| 正式生产 | 16 核 | 32 GB | 高峰并发 |

> 嵌入/重排改用**外部 API**（`.env` 设 `EMBEDDING_BASE_URL` / `RERANK_BASE_URL`）时，
> 内存可压到 4-8GB —— 本地 CPU 推理换的是"数据不出内网"，代价是单机吞吐。

### 5.2 配置：三项必须改

```bash
cd code
cp .env.prod.example .env
```

| 变量 | 怎么填 |
|------|--------|
| `SECRET_KEY` / `JWT_SECRET` | 各执行一次 `openssl rand -hex 32` |
| `ADMIN_PASSWORD` | admin 初始密码（≥10 位，含大小写与数字） |
| `LLM_API_KEY` | OpenAI 兼容服务的 Key；同时确认 `LLM_MODEL` 是**现役**模型 ID |

> `.env` 里的基础设施密码（`POSTGRES_PASSWORD` / `REDIS_PASSWORD` /
> `MINIO_ROOT_PASSWORD`）只在**首次初始化**生效，改值不会重置已有数据卷。

### 5.3 启动与验证

```bash
# 一键初始化（推荐）：自动生成密钥、收集必要输入、构建、启动、等待健康
bash scripts/init-prod.sh

# 或手动：
docker compose -p kagent -f docker-compose.prod.yml up -d --build
curl http://localhost/api/health          # 数据库 / Redis / 模型全部 ok 即正常
```

生产编排的对外入口**只有 web 容器的 `WEB_PORT`**（默认 80），
其余服务均不绑定宿主机端口。建议在宿主机前置 Nginx 终止 HTTPS。

**详细步骤（手动部署、观察启动、拓扑与安全说明、常见报错）见
[docs/DEPLOY.md](docs/DEPLOY.md)。**

---

## 六、升级与回滚

### 升级

```bash
git pull                                    # 或替换交付包
docker compose -p kagent -f docker-compose.prod.yml up -d --build api worker web
# 启动时自动执行 alembic upgrade head；迁移前请先备份
```

### 回滚（按影响由小到大，优先选小的）

| 方式 | 操作 | 影响 |
|------|------|------|
| **功能回滚**（零停服） | 管理后台 → 灰度开关 → 关闭对应规则 | 秒级生效，最多 60s 缓存过期 |
| **迁移回滚** | `alembic downgrade -1` | 需停 api + worker |
| **镜像回滚** | 切回上一稳定版本 → 重新 `--build` | 整版本退回 |

```
故障现象
  │
  ├─ 单一特性异常（配额误拒答 / 敏感词误拦截）
  │     → 关 feature_flag（秒级，零停服）
  │
  ├─ 多特性异常，怀疑新迁移引入
  │     → alembic downgrade -1（需停 api + worker）
  │
  └─ 整体不可用（启动失败 / 全量报错）
        → 退回上一交付镜像 + 回退迁移
```

**执行细节与注意事项见 [docs/DEPLOY.md §9](docs/DEPLOY.md)** ——
其中包含 `downgrade` 会 `DROP TABLE` 哪些表这类必须提前知道的信息。

---

## 七、日常运维速查

```bash
cd code
COMPOSE="docker compose -p kagent -f docker-compose.prod.yml"

$COMPOSE ps                       # 服务状态
$COMPOSE logs -f api              # 跟踪 api 日志
$COMPOSE logs -f worker           # 跟踪文档解析日志
$COMPOSE restart api              # 重启单个服务
$COMPOSE down                     # 停止全部（不删数据卷）
$COMPOSE up -d                    # 重新启动
```

**数据卷**：`kagent_postgres_data`（账号/权限/对话/审计）与 `kagent_minio_data`
（原始文档）**必须备份**；`kagent_redis_data` 与 `kagent_hf_cache` 可重建。

**备份与恢复的完整命令见 [docs/DEPLOY.md §5](docs/DEPLOY.md)。**

---

## 八、故障排查

| 现象 | 排查方向 |
|------|----------|
| `up` 时提示「必须在 .env 设置」 | `.env` 缺变量；从 `.env.prod.example` 复制并填写 |
| embedding / reranker 一直不健康 | 首次启动在下载模型，`logs embedding` 看进度；慢则设 `HF_ENDPOINT` |
| `/api/health` 数据库或 Redis 报错 | `$COMPOSE logs postgres redis`；确认密码未在首次初始化后被改 |
| api 反复重启 | `logs api` 看 traceback；常见是 `LLM_API_KEY` 未配置或迁移失败 |
| LLM 调用返回 400 | **检查 `LLM_MODEL` 是否为现役模型 ID**（模型 ID 会退役） |
| 登录提示密码错误 | `ADMIN_PASSWORD` 只在首次创建时生效，改值不会重置已有账号 |
| 上传文档后一直解析中 | `logs worker`；失败自动重试 3 次后进死信队列。也检查格式是否在白名单内 |
| 端口被占用 | 改 `.env` 的 `WEB_PORT` |

更多排查项见 [docs/DEPLOY.md §8](docs/DEPLOY.md)。

---

## 九、生产验收清单

| # | 操作 | 期望结果 |
|---|------|----------|
| F1 | 干净机器按本文档部署 | 30 分钟内起来，能登录、能提问、能看审计 |
| F2 | 连续两次创建公开知识库 | 第二次被唯一约束拒绝 |
| F3 | 提问含手机号/身份证的文档 | 回答中号码已打码，引用原文不打码 |
| F4 | 查审计日志 | 能看到谁、何时、问了什么、引用了哪些文档 |
| F5 | 备份后恢复 | 恢复后对话、文档、账号完整 |

逐项操作细节见 [docs/DEPLOY.md §4](docs/DEPLOY.md)。
