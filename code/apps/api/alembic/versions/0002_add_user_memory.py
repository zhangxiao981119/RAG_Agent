"""0002_add_user_memory — users 表加 memory JSONB 字段（用户画像 + 缓存重要信息）。

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-17
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "memory",
            sa.JSON,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="用户画像与缓存重要信息：{profile: {...}, facts: [...]}"
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "memory")
