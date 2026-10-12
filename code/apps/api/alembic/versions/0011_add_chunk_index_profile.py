"""chunks 表增加索引口径字段（方案一：索引/模型版本）。

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-12

★ 回填值刻意**硬编码**而非读取 app.config.index_profile：迁移是历史快照，
  必须可重放且不随未来配置漂移。写入的是「建表时全库 chunk 的口径」——
  即历史 chunk 均由当前 bge-m3 + etext-v1 + c400-800-60 产出。
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 建表时全库 chunk 的实际口径（与 index_profile.current_index_profile() 对齐）
_BACKFILL_MODEL = "bge-m3"
_BACKFILL_TEXT_VERSION = "etext-v1"
_BACKFILL_CHUNKER_VERSION = "c400-800-60"


def upgrade() -> None:
    # 先加 nullable 以便回填，再置 NOT NULL（表内已有数据）
    op.add_column("chunks", sa.Column("embedding_model", sa.Text(), nullable=True))
    op.add_column("chunks", sa.Column("embedding_text_version", sa.Text(), nullable=True))
    op.add_column("chunks", sa.Column("chunker_version", sa.Text(), nullable=True))

    op.execute(
        sa.text(
            "UPDATE chunks SET embedding_model = :m, embedding_text_version = :tv, "
            "chunker_version = :cv"
        ).bindparams(
            m=_BACKFILL_MODEL, tv=_BACKFILL_TEXT_VERSION, cv=_BACKFILL_CHUNKER_VERSION
        )
    )

    op.alter_column("chunks", "embedding_model", nullable=False)
    op.alter_column("chunks", "embedding_text_version", nullable=False)
    op.alter_column("chunks", "chunker_version", nullable=False)


def downgrade() -> None:
    op.drop_column("chunks", "chunker_version")
    op.drop_column("chunks", "embedding_text_version")
    op.drop_column("chunks", "embedding_model")