"""web 同步字段 + 检索加权列 —— M7 WU-M-04。

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-16

sync_sources 增补网页源所需字段（鉴权类型 / 密文凭据 / 入口 / 范围），
并放开 source_type 取值到 'web'；chunks 增补治理加权列，
避免检索期每次 join documents 取治理分（INV-M-13：加权在 rerank 之后叠加）。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── sync_sources：网页源字段 ──────────────────────────────
    op.add_column(
        "sync_sources",
        sa.Column("auth_type", sa.Text(), nullable=False, server_default="none"),
    )
    # 凭据 MUST 经 crypto 加密后落库（INV-M-07）；本列只存密文，禁止明文
    op.add_column("sync_sources", sa.Column("auth_payload", sa.Text(), nullable=True))
    op.add_column(
        "sync_sources",
        sa.Column("entry_urls", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.add_column(
        "sync_sources",
        sa.Column("scope_rules", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    # 放开 source_type 到 web：MUST 先 drop 旧 CHECK 再建新 CHECK
    op.drop_constraint("ck_sync_sources_type", "sync_sources", type_="check")
    op.create_check_constraint(
        "ck_sync_sources_type", "sync_sources", "source_type IN ('local','git','web')"
    )

    # ── chunks：治理加权（检索期在 rerank 之后叠加，不进 RRF）──
    op.add_column(
        "chunks",
        sa.Column("governance_weight", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("chunks", "governance_weight")
    op.drop_constraint("ck_sync_sources_type", "sync_sources", type_="check")
    op.create_check_constraint(
        "ck_sync_sources_type", "sync_sources", "source_type IN ('local','git')"
    )
    op.drop_column("sync_sources", "scope_rules")
    op.drop_column("sync_sources", "entry_urls")
    op.drop_column("sync_sources", "auth_payload")
    op.drop_column("sync_sources", "auth_type")
