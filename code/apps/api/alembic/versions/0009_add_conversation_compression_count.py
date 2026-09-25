"""Conversation 压缩次数追踪 + 新开对话重置。

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-25
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # conversations 表加 compression_count 列（默认 0，新开对话自动重置）
    op.add_column(
        "conversations",
        op.Column(
            "compression_count",
            op.Integer(),
            nullable=False,
            server_default=op.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("conversations", "compression_count")
