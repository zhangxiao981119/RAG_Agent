"""0005_add_audit_logs — 新增 audit_logs 表（手册 §4.2.10 / M5 任务 3 提前实现）。

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-18
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB, UUID


# revision identifiers, used by Alembic.
revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 表结构对齐 models/entities.py 中已有的 AuditLog 模型（项目骨架定义）
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), nullable=True),
        # action 命名约定：chat.ask / doc.upload / doc.delete / kb.create / acl.member.set / auth.login.*
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("object_type", sa.Text(), nullable=True),
        sa.Column("object_id", sa.Text(), nullable=True),
        sa.Column("detail", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        comment="审计日志（谁在何时做了什么）",
    )
    # 保留期 1 年（§8 D-15），归档任务后续实现
    op.create_index("idx_audit_time", "audit_logs", ["tenant_id", sa.text("created_at DESC")])
    op.create_index("idx_audit_user", "audit_logs", ["tenant_id", "user_id", sa.text("created_at DESC")])


def downgrade() -> None:
    op.drop_index("idx_audit_user", table_name="audit_logs")
    op.drop_index("idx_audit_time", table_name="audit_logs")
    op.drop_table("audit_logs")
