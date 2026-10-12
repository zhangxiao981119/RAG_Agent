#!/usr/bin/env bash
# 生产发布：备份 -> 构建并启动。
# 启动顺序由编排保证：migrate（一次性）先成功完成，api/worker 才会启动；
# migrate 失败则下游不启动（fail-fast），不会带着旧 schema 上线新代码。
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE="docker compose -p kagent -f docker-compose.prod.yml"

echo "[发布] 1/2 备份（PostgreSQL + MinIO）"
bash scripts/backup.sh

echo "[发布] 2/2 构建并启动（migrate -> api/worker -> web/gateway）"
$COMPOSE up -d --build

echo "[发布] 完成。查看状态：$COMPOSE ps"