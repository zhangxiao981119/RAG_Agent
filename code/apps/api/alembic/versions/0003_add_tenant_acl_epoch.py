"""0003_add_tenant_acl_epoch — tenants 表加 acl_epoch 字段（§3.2.6 权限缓存失效版本号）。

缓存 key 形如 `acl:{tenant_id}:{user_id}:{acl_epoch}`；任何权限变更都让 epoch +1，
使整个 tenant 的旧缓存 key 自然失效（key 对不上）。比精确失效简单且不易写错。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "acl_epoch",
            sa.BigInteger,
            nullable=False,
            server_default=sa.text("0"),
            comment="权限缓存版本号；任何权限变更都 +1 使旧缓存 key 失效（§3.2.6）",
        ),
    )


def downgrade() -> None:
    op.drop_column("tenants", "acl_epoch")
