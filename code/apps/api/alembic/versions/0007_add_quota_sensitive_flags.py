"""0007_add_quota_sensitive_flags — 配额 / 敏感词 / 灰度开关三张表（M6 续篇）。

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-18
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID


# revision identifiers, used by Alembic.
revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 租户级配额 ──────────────────────────────────────────
    op.create_table(
        "tenant_quotas",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        # 每日 token 上限（输入+输出合计）
        sa.Column("daily_token_limit", sa.Integer(), nullable=False),
        # 每日问答次数上限
        sa.Column("daily_message_limit", sa.Integer(), nullable=False),
        # 月度全局 token 上限（防止单租户刷爆整月预算）
        sa.Column("monthly_token_limit", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        comment="租户级配额（一租户一行）",
    )
    op.create_unique_constraint("uq_tenant_quotas_tenant", "tenant_quotas", ["tenant_id"])

    # ── 敏感词表（admin 管理）──────────────────────────────────
    op.create_table(
        "sensitive_words",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        # 敏感词原文，小写存储；匹配时先归一化（NFKC + 去零宽 + 小写）
        sa.Column("word", sa.Text(), nullable=False),
        # 分类：政治/广告/违法/辱骂，仅展示用
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        comment="敏感词表（admin 管理，命中即拒答）",
    )
    op.create_index("idx_sensitive_tenant_word", "sensitive_words", ["tenant_id", "word"])
    # 同租户内词不重复（小写存储保证幂等）
    op.create_unique_constraint("uq_sensitive_tenant_word", "sensitive_words", ["tenant_id", "word"])

    # ── 特性开关（按部门灰度）──────────────────────────────────
    op.create_table(
        "feature_flags",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        # 功能名：quota / sensitive_filter / memory_compress ...
        sa.Column("feature_key", sa.Text(), nullable=False),
        # 部门路径前缀，支持 SQL LIKE：'公司/研发中心/%'、'%' 表示全量
        sa.Column("dept_path_pattern", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        # 命中部门内再按 user_id hash 取模放量，0-100
        sa.Column("rollout_percent", sa.Integer(), nullable=False, server_default=sa.text("100")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        comment="特性开关（按部门灰度，关闭即回滚功能）",
    )
    # 同租户同功能同模式唯一（一个功能对一个部门模式只能配一行）
    op.create_unique_constraint(
        "uq_feature_flags_tenant_key_pattern",
        "feature_flags",
        ["tenant_id", "feature_key", "dept_path_pattern"],
    )
    op.create_index("idx_feature_flags_tenant_key", "feature_flags", ["tenant_id", "feature_key", "enabled"])


def downgrade() -> None:
    op.drop_index("idx_feature_flags_tenant_key", table_name="feature_flags")
    op.drop_table("feature_flags")

    op.drop_index("idx_sensitive_tenant_word", table_name="sensitive_words")
    op.drop_table("sensitive_words")

    op.drop_table("tenant_quotas")
