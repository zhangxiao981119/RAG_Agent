"""messages.meta 增加 GIN 索引（方案二：口径快照反查）。

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-12

用途：支持按口径快照做**反查** —— 如「哪些答案用的是提示词版本 3 / 某模型」：
    SELECT ... FROM messages WHERE meta @> '{"caliber":{"prompt_versions":{"qa_system":3}}}'
jsonb_path_ops 对 @> 包含查询最优；不含用户内容，仅索引 meta 结构。
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "idx_msg_meta_gin",
        "messages",
        ["meta"],
        postgresql_using="gin",
        postgresql_ops={"meta": "jsonb_path_ops"},
    )


def downgrade() -> None:
    op.drop_index("idx_msg_meta_gin", table_name="messages")