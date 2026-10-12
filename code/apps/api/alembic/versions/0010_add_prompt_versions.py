"""提示词版本表。

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-12
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "prompt_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("prompt_key", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'draft'")),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "prompt_key", "version", name="uq_prompt_versions_tenant_key_version"),
    )
    op.create_index(
        "idx_prompt_versions_tenant_key_status",
        "prompt_versions",
        ["tenant_id", "prompt_key", "status"],
    )
    # 每个 key 同一时刻只允许一条 active —— 并发激活时由数据库保证唯一
    op.create_index(
        "uq_prompt_versions_active",
        "prompt_versions",
        ["tenant_id", "prompt_key"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("uq_prompt_versions_active", table_name="prompt_versions")
    op.drop_index("idx_prompt_versions_tenant_key_status", table_name="prompt_versions")
    op.drop_table("prompt_versions")