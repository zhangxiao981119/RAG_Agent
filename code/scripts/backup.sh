#!/usr/bin/env bash
# 生产备份：PostgreSQL 全库 + MinIO 原始文档。
# 发布 / 迁移前必须先执行（scripts/release.sh 已内置调用）。
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE="docker compose -p kagent -f docker-compose.prod.yml"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT_DIR="${BACKUP_DIR:-backups}/${STAMP}"
mkdir -p "$OUT_DIR"
OUT_ABS="$(cd "$OUT_DIR" && pwd)"

echo "[备份] 目标目录：${OUT_ABS}"

# 1) PostgreSQL 逻辑备份（走容器内 env，避免与 .env 变量名耦合）
$COMPOSE exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  > "${OUT_ABS}/postgres.sql"
echo "[备份] PostgreSQL -> postgres.sql"

# 2) MinIO 原始文档（打包整个对象数据卷，只读挂载避免误写）
docker run --rm -v kagent_minio_data:/data:ro -v "${OUT_ABS}":/backup alpine \
  tar czf /backup/minio-data.tar.gz -C /data .
echo "[备份] MinIO -> minio-data.tar.gz"

echo "[备份] 完成：${OUT_ABS}"