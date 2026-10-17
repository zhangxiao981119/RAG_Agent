"""chunks 增补表格组列 —— 表格决策 A1'（限流版分片回捞）。

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-17

超大表按行组切分后检索只命中单片 —— 跨片行对比、表级总结类问题漏答。
chunks 增补 table_group_id：同一次行组切分的全部分片共享同一组 id，
检索命中任一分片时回捞同组其余分片（上限 TABLE_RECALL_MAX_CHUNKS、
预算 TABLE_RECALL_TOKEN_BUDGET），补全表格上下文。

设计口径：
  · 仅纯表格分片非空；整表单片 / 混合块（分片与其他文本混装）为 NULL ——
    混合块参与回捞会把无关文本卷进表格上下文
  · 部分索引（WHERE table_group_id IS NOT NULL）：组数远小于 chunk 总数
  · 旧 chunk 该列为 NULL：不参与回捞，向量同构不受影响，无需重算
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chunks", sa.Column("table_group_id", sa.Text(), nullable=True))
    op.create_index(
        "idx_chunk_table_group",
        "chunks",
        ["table_group_id"],
        postgresql_where=sa.text("table_group_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_chunk_table_group", table_name="chunks")
    op.drop_column("chunks", "table_group_id")
