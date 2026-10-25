"""新增章节补全查询索引 idx_chunk_section。

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-25

检索命中后按 (document_id, heading_path) 取同节其余分片（章节上下文补全）。
没有这个索引时该查询要走 idx_chunk_doc 取整篇再按 heading_path 过滤 —— 长文档
一篇几百片，每个命中小节都重复扫一遍。

部分索引只覆盖有标题的行：无标题分片永远不会被章节补全选中，不进索引可省体积。
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "idx_chunk_section",
        "chunks",
        ["document_id", "heading_path", "chunk_index"],
        postgresql_where=sa.text("heading_path IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_chunk_section", table_name="chunks")
