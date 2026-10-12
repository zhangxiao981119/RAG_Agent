"""audit_logs.trace_id —— 审计日志关联链路追踪（P0-5 日志与链路追踪）。

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-12

一次问答的审计落库带上请求 trace_id，用同一个值即可把容器日志（api / worker）
与审计记录串成一条链，便于按 trace 复盘某次问答。
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("audit_logs", sa.Column("trace_id", sa.Text(), nullable=True))
    # 按 trace_id 反查本次请求产生的全部审计记录
    op.create_index("idx_audit_trace", "audit_logs", ["tenant_id", "trace_id"])


def downgrade() -> None:
    op.drop_index("idx_audit_trace", table_name="audit_logs")
    op.drop_column("audit_logs", "trace_id")