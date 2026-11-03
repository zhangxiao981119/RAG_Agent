"""documents 增加图抽取状态四列（graph_status / 模型 / prompt 版本 / 时间）。

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-03

★ 为什么不建第四张状态表（设计文档 §8.2）：
  三列 + 一个索引足以回答「哪篇没抽、按什么口径抽的、什么时候抽的」，且让
  「图就绪度」退化成 documents 上的一个 WHERE（无需 JOIN）。代价是 documents
  变宽 4 列 —— 与 M7 治理列（已有 9 列）同款权衡：状态与它描述的行同生命周期，
  单独建表只会多一个 JOIN 和一处可能不同步的写入点。

★ 为什么这几列与搜索无关却重要：
  抽取是离线异步任务，失败不能让文档不可检索（文档可检索性不依赖图）。
  所以状态落在 documents 上、由 graph_job 维护，检索侧只按 chunks.is_latest 工作，
  完全不必看这几列（「只写不读」的第 1 步就靠这个分工）。

纯加列 + 加约束：downgrade 逐个 drop，既有数据零影响。
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 默认 'pending' 而非 NULL：既有文档一律「待抽」，补建脚本按
    # graph_status != 'done' 选取即可，不需要 NULL 兜底分支（NULL 会让
    # `NOT IN ('done')` 静默漏掉这些行 —— 三值逻辑陷阱）。
    op.add_column(
        "documents",
        sa.Column("graph_status", sa.Text(), nullable=False, server_default="pending",
                  comment="图抽取状态：pending/running/done/failed/skipped"),
    )
    op.add_column(
        "documents",
        sa.Column("graph_extractor_model", sa.Text(), nullable=True,
                  comment="最近一次成功抽取的口径：模型（NULL = 从未抽取）"),
    )
    op.add_column(
        "documents",
        sa.Column("graph_prompt_version", sa.Text(), nullable=True,
                  comment="最近一次成功抽取的口径：prompt 版本"),
    )
    op.add_column(
        "documents",
        sa.Column("graph_extracted_at", sa.DateTime(timezone=True), nullable=True,
                  comment="最近一次成功抽取时间"),
    )
    op.create_check_constraint(
        "ck_documents_graph_status",
        "documents",
        "graph_status IN ('pending','running','done','failed','skipped')",
    )
    # 就绪度查询走这个索引：WHERE tenant_id=? AND is_latest AND graph_status<>'done'。
    # 不带 kb_id 是刻意的 —— 图就绪度是**部署级**指标（横幅与自检要的），
    # 按库统计的场景频率低，走 idx_doc_kb 再过滤即可。
    op.create_index(
        "idx_doc_graph_status", "documents", ["tenant_id", "is_latest", "graph_status"]
    )


def downgrade() -> None:
    op.drop_index("idx_doc_graph_status", table_name="documents")
    op.drop_constraint("ck_documents_graph_status", "documents", type_="check")
    op.drop_column("documents", "graph_extracted_at")
    op.drop_column("documents", "graph_prompt_version")
    op.drop_column("documents", "graph_extractor_model")
    op.drop_column("documents", "graph_status")
