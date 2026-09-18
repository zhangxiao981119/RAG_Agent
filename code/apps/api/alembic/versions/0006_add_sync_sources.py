"""0006_add_sync_sources — 新增 sync_sources 表（M6 任务 1：知识源同步 §8 D-07）。

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-18
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID


# revision identifiers, used by Alembic.
revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sync_sources",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("kb_id", UUID(as_uuid=True), sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False, server_default="local"),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("branch", sa.Text(), nullable=True),
        sa.Column("file_patterns", sa.Text(), nullable=False, server_default="*.md,*.pdf,*.txt,*.docx"),
        sa.Column("level_rank", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("synced_files", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("source_type IN ('local','git')", name="ck_sync_sources_type"),
        sa.CheckConstraint("status IN ('active','paused')", name="ck_sync_sources_status"),
        comment="知识源同步配置（M6 任务 1）",
    )
    op.create_index("idx_sync_tenant", "sync_sources", ["tenant_id", "status"])


def downgrade() -> None:
    op.drop_index("idx_sync_tenant", table_name="sync_sources")
    op.drop_table("sync_sources")
