"""documents 治理字段 —— M7 WU-M-03（四维审查结论与治理元数据）。

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-16

四维审查（时效性/权威性/一致性/完整度）的结论落在 documents 上，
审查报告单独存 JSONB 以便审计；source_kind / content_hash / effective_at / expire_at
供 web 增量同步与时效性判定使用。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 四维分数：NULL = 未审查（INV-M-12：未审查/治理失败时按 0 处理，不参与过滤）
    op.add_column("documents", sa.Column("freshness_score", sa.Integer(), nullable=True))
    op.add_column("documents", sa.Column("authority_score", sa.Integer(), nullable=True))
    op.add_column("documents", sa.Column("consistency_score", sa.Integer(), nullable=True))
    op.add_column("documents", sa.Column("completeness_score", sa.Integer(), nullable=True))
    op.add_column(
        "documents",
        sa.Column("governance_status", sa.Text(), nullable=False, server_default="pending"),
    )
    op.add_column(
        "documents",
        sa.Column(
            "governance_report",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "documents",
        sa.Column("source_kind", sa.Text(), nullable=False, server_default="upload"),
    )
    op.add_column("documents", sa.Column("effective_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("documents", sa.Column("expire_at", sa.DateTime(timezone=True), nullable=True))
    # 内容指纹：web 增量同步判定页面是否变更（与 etag/last-modified 并列）
    op.add_column("documents", sa.Column("content_hash", sa.Text(), nullable=True))

    op.create_check_constraint(
        "ck_documents_governance_status",
        "documents",
        "governance_status IN ('pending','running','done','failed')",
    )
    op.create_check_constraint(
        "ck_documents_source_kind",
        "documents",
        "source_kind IN ('upload','sync_local','sync_git','sync_web')",
    )
    # 治理面板按状态取数：租户 + 库 + 治理状态
    op.create_index(
        "idx_doc_governance", "documents", ["tenant_id", "kb_id", "governance_status"]
    )


def downgrade() -> None:
    op.drop_index("idx_doc_governance", table_name="documents")
    op.drop_constraint("ck_documents_source_kind", "documents", type_="check")
    op.drop_constraint("ck_documents_governance_status", "documents", type_="check")
    for column in (
        "content_hash",
        "expire_at",
        "effective_at",
        "source_kind",
        "governance_report",
        "governance_status",
        "completeness_score",
        "consistency_score",
        "authority_score",
        "freshness_score",
    ):
        op.drop_column("documents", column)
