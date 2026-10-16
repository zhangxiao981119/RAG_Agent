"""governance_jobs 表 —— M7 WU-M-03（四维审查异步任务，与 parse_jobs 同构）。

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-16

审查 MUST 为独立 arq 任务（INV-M-10），不混入 run_parse_job 主路径；
本表记录其状态机与重试口径，与 parse_jobs 结构一致便于运维复用。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "governance_jobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "document_id",
            UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','failed','dead')",
            name="ck_governance_jobs_status",
        ),
        comment="四维审查异步任务（M7 WU-M-03）",
    )


def downgrade() -> None:
    op.drop_table("governance_jobs")
