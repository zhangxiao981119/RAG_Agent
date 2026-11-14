"""新增企业应用凭证与集成会话隔离表。

Revision ID: 0027
Revises: 0026
Create Date: 2026-11-14
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "integration_applications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("key_hash", sa.Text(), nullable=False),
        sa.Column("kb_ids", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("rate_limit_per_minute", sa.Integer(), nullable=False),
        sa.Column("daily_token_limit", sa.BigInteger(), nullable=False),
        sa.Column("daily_message_limit", sa.Integer(), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.CheckConstraint("status IN ('active','disabled')", name="ck_integration_app_status"),
        sa.CheckConstraint("rate_limit_per_minute > 0", name="ck_integration_app_rate_positive"),
        sa.CheckConstraint("daily_token_limit > 0", name="ck_integration_app_token_positive"),
        sa.CheckConstraint("daily_message_limit > 0", name="ck_integration_app_message_positive"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_integration_app_tenant_name"),
        sa.UniqueConstraint("key_hash", name="uq_integration_applications_key_hash"),
    )
    op.create_index(
        "idx_integration_app_tenant", "integration_applications", ["tenant_id", "status"],
    )

    op.create_table(
        "integration_conversations",
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("application_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(
            ["application_id"], ["integration_applications.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "idx_integration_conversation_subject",
        "integration_conversations",
        ["tenant_id", "application_id", "user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_integration_conversation_subject", table_name="integration_conversations",
    )
    op.drop_table("integration_conversations")
    op.drop_index("idx_integration_app_tenant", table_name="integration_applications")
    op.drop_table("integration_applications")
