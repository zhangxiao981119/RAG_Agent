"""parse_segments 分段表 + parse_jobs 进度列 —— 方案 C（扫描件分段 OCR）。

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-18

背景：扫描件 OCR 实测 22~180s/页，单份文档超 8 页即越过 parse_job_timeout
（1800s）被 arq 中途杀掉。方案 C 把扫描件解析切成若干「执行段」串行入队：
每段只渲染 + 逐页 OCR，产出每页原始文本落 parse_segments.page_texts，
全部段成功后由聚合任务按全局页序归并再走标题识别 / 分块 / 嵌入。

设计口径：
  · parse_segments 一行 = 一个执行段，(job_id, segment_index) 唯一
  · page_texts 为 JSONB 字符串数组（索引 i 对应 page_no = page_start + i）
  · 段是「已完成进度」：重试时已 succeeded 的段直接跳过 OCR，只重跑失败段
  · parse_jobs 增补 pages_done / pages_total 用于进度上报（非扫描件恒 0）
  · job_id 外键 CASCADE：parse_job 删除时段记录同步清理
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "parse_jobs",
        sa.Column("pages_done", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "parse_jobs",
        sa.Column("pages_total", sa.Integer(), nullable=False, server_default="0"),
    )

    op.create_table(
        "parse_segments",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "job_id",
            UUID(as_uuid=True),
            sa.ForeignKey("parse_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("segment_index", sa.Integer(), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=False),
        sa.Column("page_end", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("page_texts", JSONB(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','failed','dead')",
            name="ck_parse_segments_status",
        ),
        sa.UniqueConstraint("job_id", "segment_index", name="uq_parse_segments_job_index"),
        comment="扫描件 OCR 分段执行单元（方案 C）",
    )


def downgrade() -> None:
    op.drop_table("parse_segments")
    op.drop_column("parse_jobs", "pages_total")
    op.drop_column("parse_jobs", "pages_done")
