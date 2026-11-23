#!/usr/bin/env bash
# 生产备份：PostgreSQL 全库（控制库 + 所有场景库）+ MinIO 原始文档。
# 温档下所有库都在同一实例，因此按 pg_database 动态枚举，不依赖配置清单：
# 只要库还存在且允许连接，就必须进备份，避免"新增场景库忘改配置就不备份"。
# 备份格式为 custom（-Fc）：压缩、可 pg_restore 选择性恢复，RTO 演练走同一格式。
set -euo pipefail
cd "$(dirname "$0")/.."

# 允许测试替换 docker 命令（不在生产文档中宣传）
DOCKER="${BACKUP_DOCKER:-docker}"
COMPOSE="$DOCKER compose -p kagent -f docker-compose.prod.yml"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT_DIR="${BACKUP_DIR:-backups}/${STAMP}"
mkdir -p "$OUT_DIR"
OUT_ABS="$(cd "$OUT_DIR" && pwd)"

# RPO 政策值：每日一次全库 dump（政策值，非实测；窗口与 RTO 数字待标定）
RPO_POLICY="每日一次全库 dump（政策值，非实测）"

echo "[备份] 目标目录：${OUT_ABS}"

# 0) 枚举库：排除模板库、禁止连接的库；维护库 postgres 由宿主侧过滤掉。
#    口令与账号留在容器 env 里，不出现在宿主命令行。
DATABASES="$(
  $COMPOSE exec -T postgres sh -c \
    'psql -U "$POSTGRES_USER" -d postgres -Atc "SELECT datname FROM pg_database WHERE datistemplate = false AND datallowconn = true ORDER BY datname"' \
    | grep -vx 'postgres' || true
)"
if [ -z "$DATABASES" ]; then
  echo "[备份] 失败：未枚举到任何业务数据库（检查 postgres 容器是否在运行）" >&2
  exit 1
fi

START_EPOCH="$(date +%s)"
START_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

db_names=()
db_seconds=()
db_bytes=()

# 1) 逐个库逻辑备份：任一库失败即整次备份失败（release.sh 会因此中止发布）。
while IFS= read -r db; do
  [ -n "$db" ] || continue
  t0="$(date +%s)"
  if ! $COMPOSE exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -Fc -d "$1"' sh "$db" \
      > "${OUT_ABS}/db_${db}.dump"; then
    echo "[备份] 失败：数据库 ${db} 导出失败" >&2
    exit 1
  fi
  t1="$(date +%s)"
  size="$(wc -c < "${OUT_ABS}/db_${db}.dump" | tr -d ' ')"
  db_names+=("$db")
  db_seconds+=("$((t1 - t0))")
  db_bytes+=("$size")
  echo "[备份] PostgreSQL ${db} -> db_${db}.dump（$((t1 - t0))s, ${size}B）"
done <<< "$DATABASES"

# 2) MinIO 原始文档（打包整个对象数据卷，只读挂载避免误写）
m0="$(date +%s)"
$DOCKER run --rm -v kagent_minio_data:/data:ro -v "${OUT_ABS}":/backup alpine \
  tar czf /backup/minio-data.tar.gz -C /data .
m1="$(date +%s)"
echo "[备份] MinIO -> minio-data.tar.gz（$((m1 - m0))s）"

END_EPOCH="$(date +%s)"
END_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# 3) 清单：记录本次窗口（总耗时 + 每库耗时/体积）与 RPO 政策值。
MANIFEST="${OUT_ABS}/manifest.txt"
{
  echo "backup_stamp=${STAMP}"
  echo "started_at=${START_UTC}"
  echo "finished_at=${END_UTC}"
  echo "window_seconds=$((END_EPOCH - START_EPOCH))"
  echo "database_count=${#db_names[@]}"
  echo "rpo_policy=${RPO_POLICY}"
  echo "minio_seconds=$((m1 - m0))"
  echo "# database<TAB>seconds<TAB>bytes<TAB>file"
  for i in "${!db_names[@]}"; do
    printf 'database\t%s\t%s\t%s\tdb_%s.dump\n' \
      "${db_names[$i]}" "${db_seconds[$i]}" "${db_bytes[$i]}" "${db_names[$i]}"
  done
} > "$MANIFEST"

echo "[备份] 清单 -> manifest.txt（窗口 $((END_EPOCH - START_EPOCH))s，${#db_names[@]} 个库，RPO 政策值：${RPO_POLICY}）"
echo "[备份] 完成：${OUT_ABS}"
