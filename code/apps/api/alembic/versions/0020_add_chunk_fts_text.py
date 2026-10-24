"""chunks.fts_text / fts_vec —— 中文关键词召回（2-gram 预切分）。

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-24

背景：idx_chunk_fts 建在 `to_tsvector('simple', content)` 上，但 'simple' 不做中文
分词，整句中文会变成一个 token，`@@` 永远匹配不上较短的查询 —— 评测集里 65.5%
的问题（无任何 ASCII 词元）因此完全绕过全文索引，只剩 ILIKE 顺序扫描。

做法：入库时把 content / heading_path 预切成「中文重叠 2-gram + ASCII 整词」写入
fts_text；fts_vec 是由它生成的 tsvector 存储列，索引建在 fts_vec 上。
切分口径的唯一真源是 app/services/keyword_text.py —— 本迁移只加列与索引；回填必须
用 scripts/backfill_fts_text.py（在 SQL 里另写一份 bigram 逻辑必然与 Python 侧不一致，
表现为静默 0 召回）。

★ 为什么落 tsvector 列而不是表达式索引：表达式索引只能加速 `@@` 过滤，ts_rank
  仍要对每条命中行重新 to_tsvector 并 detoast 原文（实测命中数百行时 142ms）；
  而且 590 行的小表上 planner 会因估算成本直接放弃该索引走 Seq Scan。
  换成生成列后同一查询 1.9ms（Bitmap Index Scan + 直接读已存 tsvector）。

旧索引 idx_chunk_fts 保留到「回填完成 + 查询切换」之后，由后续迁移删除。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TSVECTOR

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chunks", sa.Column("fts_text", sa.Text(), nullable=True))
    # 生成列：写入/更新 fts_text 时自动重算，应用侧无需关心
    op.add_column(
        "chunks",
        sa.Column(
            "fts_vec",
            TSVECTOR,
            sa.Computed("to_tsvector('simple', fts_text)", persisted=True),
        ),
    )
    op.create_index("idx_chunk_fts_vec", "chunks", ["fts_vec"], postgresql_using="gin")


def downgrade() -> None:
    op.drop_index("idx_chunk_fts_vec", table_name="chunks")
    op.drop_column("chunks", "fts_vec")
    op.drop_column("chunks", "fts_text")
