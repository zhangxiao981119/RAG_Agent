"""删除旧的全文本索引 idx_chunk_fts。

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-25

idx_chunk_fts 建在 `to_tsvector('simple', content)` 上，'simple' 不切中文，中文查询
在它上面恒不命中；查询侧已改为读 chunks.fts_vec（走 idx_chunk_fts_vec），
fts_text 也已回填完毕。

旧索引留着只有成本没有收益：每次 chunk 写入/更新都要对整段 content 重算 tsvector
并写一份 GIN 索引。downgrade 按原表达式重建，便于回滚到 0020 之前的查询口径。
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("idx_chunk_fts", table_name="chunks")


def downgrade() -> None:
    op.create_index(
        "idx_chunk_fts",
        "chunks",
        [sa.text("to_tsvector('simple', content)")],
        postgresql_using="gin",
    )
