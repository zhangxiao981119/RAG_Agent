#!/usr/bin/env bash
# ==========================================================================
# 生产环境一键初始化（M5 任务 6 · 手册 F1：干净机器 30 分钟内起来）
#
# 做的事：
#   1. 检查 docker / compose 环境
#   2. .env 不存在时从 .env.prod.example 复制，自动生成各项强密码
#   3. 交互式收集管理员初始密码与 LLM API Key
#   4. 构建镜像并启动全部服务
#   5. 轮询 /api/health 直到全绿，输出登录地址
#
# 用法：bash scripts/init-prod.sh
# 幂等：重复执行不会重建已存在的 .env，也不会丢数据
# ==========================================================================
set -euo pipefail

# 切到 compose 文件所在目录（脚本位于 code/scripts/ 下）
cd "$(dirname "$0")/.."

COMPOSE="docker compose -p kagent -f docker-compose.prod.yml"
HEALTH_URL="http://localhost:$(grep -E '^WEB_PORT=' .env 2>/dev/null | cut -d= -f2 || echo 80)/api/health"

echo "=============================================="
echo " 知识库问答 Agent · 生产环境初始化"
echo "=============================================="

# ── 1. 环境检查 ──────────────────────────────────────────
if ! command -v docker >/dev/null 2>&1; then
  echo "[错误] 未检测到 docker，请先安装 Docker Engine 24+ 与 Compose v2"
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "[错误] 未检测到 docker compose v2，请升级 Docker"
  exit 1
fi

# ── 2. .env 初始化 ───────────────────────────────────────
if [ ! -f .env ]; then
  cp .env.prod.example .env
  echo "[1/4] 已从 .env.prod.example 创建 .env，开始生成密钥..."

  # 自动生成全部基础设施密钥（hex 字符，无 sed 转义问题）
  SECRET_KEY=$(openssl rand -hex 32)
  JWT_SECRET=$(openssl rand -hex 32)
  DB_PASSWORD=$(openssl rand -hex 16)
  REDIS_PASSWORD=$(openssl rand -hex 16)
  MINIO_PASSWORD=$(openssl rand -hex 16)

  sed -i "s|SECRET_KEY=please-change-me-to-64-hex-chars|SECRET_KEY=${SECRET_KEY}|" .env
  sed -i "s|JWT_SECRET=please-change-me-to-64-hex-chars-too|JWT_SECRET=${JWT_SECRET}|" .env
  sed -i "s|POSTGRES_PASSWORD=please-change-this-db-password|POSTGRES_PASSWORD=${DB_PASSWORD}|" .env
  sed -i "s|REDIS_PASSWORD=please-change-this-redis-password|REDIS_PASSWORD=${REDIS_PASSWORD}|" .env
  sed -i "s|MINIO_ROOT_PASSWORD=please-change-this-minio-password|MINIO_ROOT_PASSWORD=${MINIO_PASSWORD}|" .env
else
  echo "[1/4] .env 已存在，跳过生成（如需重新生成请先删除 .env）"
fi

# ── 3. 交互收集：LLM API Key 与管理员密码 ───────────────
if grep -q "LLM_API_KEY=请填入" .env; then
  echo
  echo "[2/4] 需要配置 LLM API Key（DeepSeek 或其他 OpenAI 兼容服务）"
  read -r -p "请输入 LLM_API_KEY: " INPUT_LLM_KEY
  if [ -z "$INPUT_LLM_KEY" ]; then
    echo "[错误] LLM_API_KEY 不能为空"
    exit 1
  fi
  sed -i "s|LLM_API_KEY=请填入你的-API-KEY|LLM_API_KEY=${INPUT_LLM_KEY}|" .env
else
  echo "[2/4] LLM_API_KEY 已配置"
fi

# ADMIN_PASSWORD 仍是模板占位则交互收集（已改过则尊重现值）
if grep -q "ADMIN_PASSWORD=ChangeMe123!" .env; then
  echo
  echo "[3/4] 设置管理员 admin 的初始密码（至少 10 位，含大小写与数字）"
  read -r -s -p "请输入管理员密码: " INPUT_ADMIN_PWD
  echo
  read -r -s -p "请再次输入确认:   " INPUT_ADMIN_PWD2
  echo
  if [ "$INPUT_ADMIN_PWD" != "$INPUT_ADMIN_PWD2" ]; then
    echo "[错误] 两次输入不一致"
    exit 1
  fi
  if [ "${#INPUT_ADMIN_PWD}" -lt 10 ]; then
    echo "[错误] 密码长度至少 10 位"
    exit 1
  fi
  sed -i "s|ADMIN_PASSWORD=ChangeMe123!|ADMIN_PASSWORD=${INPUT_ADMIN_PWD}|" .env
else
  echo "[3/4] 管理员密码已配置"
fi

# ── 4. 构建并启动 ────────────────────────────────────────
echo
echo "[4/4] 构建镜像并启动服务（首次需下载模型权重，可能耗时 10-30 分钟）..."
$COMPOSE up -d --build

# ── 5. 等待健康 ──────────────────────────────────────────
WEB_PORT=$(grep -E '^WEB_PORT=' .env | cut -d= -f2)
HEALTH_URL="http://localhost:${WEB_PORT}/api/health"
echo
echo "等待服务健康（探针: ${HEALTH_URL}）..."

ATTEMPT=0
MAX_ATTEMPTS=180  # 180 次 × 10 秒 = 30 分钟上限
until curl -fsS "$HEALTH_URL" >/dev/null 2>&1; do
  ATTEMPT=$((ATTEMPT + 1))
  if [ "$ATTEMPT" -ge "$MAX_ATTEMPTS" ]; then
    echo "[错误] 等待健康超时，请查看日志: $COMPOSE logs api"
    exit 1
  fi
  if [ $((ATTEMPT % 6)) -eq 0 ]; then
    echo "  ...已等待 $((ATTEMPT * 10 / 60)) 分钟，embedding/reranker 首次下载模型较慢，属正常现象"
  fi
  sleep 10
done

echo
echo "=============================================="
echo " 部署完成"
echo "----------------------------------------------"
echo " 访问地址 : http://localhost:${WEB_PORT}"
echo " 管理员   : admin（密码为初始化时设置）"
echo " 健康检查 : ${HEALTH_URL}"
echo "----------------------------------------------"
echo " 首次登录后请："
echo "   1. 修改 admin 密码"
echo "   2. 在「知识库」页面创建第一个业务知识库"
echo "   3. 上传文档并提问验证"
echo " 常用命令见 docs/DEPLOY.md"
echo "=============================================="
