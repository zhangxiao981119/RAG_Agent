"""新增知识库领域词表 kb_terms。

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-25

查询扩展的依据：离线从 chunks 抽取型号、缩写-全称对、高频标识符，
检索期按「命中词 → 补充另一侧写法」扩词元。不建表也能靠正则归一化型号，
但没有这张表，同义词/缩写这类「字面完全不同」的长尾问法永远召不回来。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "kb_terms",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False,
                  comment="租户"),
        sa.Column("kb_id", postgresql.UUID(as_uuid=True), nullable=False, comment="所属知识库"),
        sa.Column("term", sa.Text(), nullable=False, comment="归一后的词条"),
        sa.Column("aliases", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'[]'::jsonb"), comment="写法变体 / 对照词"),
        sa.Column("kind", sa.Text(), nullable=False,
                  comment="model / acronym"),
        sa.Column("freq", sa.Integer(), nullable=False, server_default="0",
                  comment="在知识库原文中的出现次数"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["kb_id"], ["knowledge_bases.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("kb_id", "term", name="uq_kb_terms_kb_term"),
    )
    op.create_index("idx_kb_terms_lookup", "kb_terms", ["kb_id"])


def downgrade() -> None:
    op.drop_index("idx_kb_terms_lookup", table_name="kb_terms")
    op.drop_table("kb_terms")
