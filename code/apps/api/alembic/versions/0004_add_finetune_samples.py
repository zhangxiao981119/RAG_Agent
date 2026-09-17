"""0004_add_finetune_samples — 新增 finetune_samples 表（采纳问答收集为微调数据）。

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-17
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID


# revision identifiers, used by Alembic.
revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "finetune_samples",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        # 不设 FK 级联：会话/消息清理不应连带丢失已收集的微调数据
        sa.Column("conversation_id", UUID(as_uuid=True), nullable=False),
        sa.Column("user_message_id", UUID(as_uuid=True), nullable=False),
        sa.Column("assistant_message_id", UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("citations", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("adopted_by", UUID(as_uuid=True), nullable=False),
        sa.Column("adopted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("exported_at", sa.DateTime(timezone=True), nullable=True),
        comment="采纳的问答对（微调数据收集）",
    )
    op.create_index("idx_ft_tenant_exported", "finetune_samples", ["tenant_id", "exported_at"])


def downgrade() -> None:
    op.drop_index("idx_ft_tenant_exported", table_name="finetune_samples")
    op.drop_table("finetune_samples")
