"""P3 迁移/约束：FK ondelete + gen_random_uuid + CheckConstraint + 索引。

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-21
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# asyncpg 不支持单次 execute 多语句，拆成单条
_UP = [
    # ── FK ondelete: 3 处悬空引用改 SET NULL ──────────────
    # users.dept_id → SET NULL（部门被删时用户 dept_id 自动清空，而非阻塞删除）
    "ALTER TABLE users DROP CONSTRAINT users_dept_id_fkey, "
    "ADD CONSTRAINT users_dept_id_fkey "
    "FOREIGN KEY (dept_id) REFERENCES departments(id) ON DELETE SET NULL",

    # knowledge_bases.owner_id → SET NULL（用户被删时 KB owner 自动清空）
    "ALTER TABLE knowledge_bases DROP CONSTRAINT knowledge_bases_owner_id_fkey, "
    "ADD CONSTRAINT knowledge_bases_owner_id_fkey "
    "FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE SET NULL",

    # documents.uploaded_by → SET NULL（用户被删时文档 uploaded_by 自动清空）
    "ALTER TABLE documents DROP CONSTRAINT documents_uploaded_by_fkey, "
    "ADD CONSTRAINT documents_uploaded_by_fkey "
    "FOREIGN KEY (uploaded_by) REFERENCES users(id) ON DELETE SET NULL",

    # ── 0004/0006/0007 新表 id 补 gen_random_uuid default ────
    # 跳过：audit_logs.id 是 BIGSERIAL，rate_limits/quota_usage 表不存在
    "ALTER TABLE finetune_samples ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE sync_sources ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE sensitive_words ALTER COLUMN id SET DEFAULT gen_random_uuid()",

    # ── CheckConstraint: clearance/level_rank 范围 0-100 ──
    # 实际有 clearance 列的只有 users（tenants 没有）
    "ALTER TABLE users ADD CONSTRAINT chk_users_clearance "
    "CHECK (clearance BETWEEN 0 AND 100)",
    "ALTER TABLE documents ADD CONSTRAINT chk_docs_level_rank "
    "CHECK (level_rank BETWEEN 0 AND 100)",
    "ALTER TABLE chunks ADD CONSTRAINT chk_chunks_level_rank "
    "CHECK (level_rank BETWEEN 0 AND 100)",

    # ── 性能索引 ───────────────────────────────────────────
    "CREATE INDEX IF NOT EXISTS idx_docs_status ON documents(tenant_id, status, is_latest)",
    "CREATE INDEX IF NOT EXISTS idx_parsejobs_status ON parse_jobs(tenant_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_sync_status ON sync_sources(tenant_id, status)",
]

_DOWN = [
    # FK ondelete 回滚（还原为无 ON DELETE）
    "ALTER TABLE users DROP CONSTRAINT users_dept_id_fkey, "
    "ADD CONSTRAINT users_dept_id_fkey "
    "FOREIGN KEY (dept_id) REFERENCES departments(id)",
    "ALTER TABLE knowledge_bases DROP CONSTRAINT knowledge_bases_owner_id_fkey, "
    "ADD CONSTRAINT knowledge_bases_owner_id_fkey "
    "FOREIGN KEY (owner_id) REFERENCES users(id)",
    "ALTER TABLE documents DROP CONSTRAINT documents_uploaded_by_fkey, "
    "ADD CONSTRAINT documents_uploaded_by_fkey "
    "FOREIGN KEY (uploaded_by) REFERENCES users(id)",

    # gen_random_uuid default 回滚
    "ALTER TABLE finetune_samples ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE sync_sources ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE sensitive_words ALTER COLUMN id DROP DEFAULT",

    # CheckConstraint 回滚
    "ALTER TABLE users DROP CONSTRAINT chk_users_clearance",
    "ALTER TABLE documents DROP CONSTRAINT chk_docs_level_rank",
    "ALTER TABLE chunks DROP CONSTRAINT chk_chunks_level_rank",

    # 索引回滚
    "DROP INDEX IF EXISTS idx_docs_status",
    "DROP INDEX IF EXISTS idx_parsejobs_status",
    "DROP INDEX IF EXISTS idx_conv_user",
    "DROP INDEX IF EXISTS idx_sync_status",
]


def upgrade() -> None:
    for stmt in _UP:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in reversed(_DOWN):
        op.execute(stmt)
