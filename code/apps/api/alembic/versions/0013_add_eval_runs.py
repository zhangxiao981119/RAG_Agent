"""eval_runs 表 —— 评估基线版本化留痕（项2：评估基线版本）。

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-12

每跑一次 scripts/run_eval.py 落一条：指标 + 用例集指纹 + 运行口径快照 + 是否通过门禁。
用例集指纹不一致时基线不可比（见 scripts/run_eval.py 的 --baseline）。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "eval_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column("metrics", JSONB(), nullable=False, server_default="{}"),
        sa.Column("case_count", sa.Integer(), nullable=False),
        sa.Column("case_fingerprint", sa.Text(), nullable=False),
        sa.Column("caliber", JSONB(), nullable=False, server_default="{}"),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
    )
    # 基线取数：按 (租户, 用例集指纹) 定位，再按 created_at 取最近一条
    op.create_index(
        "idx_eval_runs_tenant_fp_created",
        "eval_runs",
        ["tenant_id", "case_fingerprint", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_eval_runs_tenant_fp_created", table_name="eval_runs")
    op.drop_table("eval_runs")