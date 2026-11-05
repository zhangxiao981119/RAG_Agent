"""新增 wiki_pages 一表（LLM wiki 导航页，第 3 步）。

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-04

DDL 逐字照架构设计文档 v1.2 §8.3（含两条 CHECK 与 uq_wiki_pages_document）。

★ 这张表是**纯派生数据**：生成输入 = 已落库 chunks（content + heading_path），
  删了随时可由 rebuild_wiki 重建。它**不进检索**（术语扩展是第 4 步）——
  本迁移不改变任何检索行为，WIKI_ENABLED=false（默认）时整张表无人读写。

★ source_chunk_ids 故意不建外键（Postgres 数组列不能引用）：重索引换 chunk 行后
  页会指向死 id。对策不是外键，而是 §8.5 的 purge_derived() —— 重索引**同一事务**
  内 DELETE 页（services/graph/purge.py 第 3 步已补）。

★ status 三态（ready/stale/failed）：导航页的可见性不做在 status 上 ——
  读接口的 404 三合一（不存在 / 生成失败 / 无可见 chunk）由「EXISTS 过四闸门的
  chunk」判定，status 只做运维可见性（failed 页留在库里让人看见生成失败过）。

纯新增表，不动既有表：downgrade drop 即可，回滚不影响检索（设计 §13 第 3 步
的回滚承诺：downgrade 0025 后服务仍可用 —— wiki 接口 404，检索本来就不读它）。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "wiki_pages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False, comment="租户"),
        sa.Column("kb_id", postgresql.UUID(as_uuid=True), nullable=False,
                  comment="所属知识库（kb 删除时级联清页）"),
        sa.Column("scope", sa.Text(), nullable=False, server_default="doc",
                  comment="生成范围：本版只允许 doc（§7.5 的可见性论证）"),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False,
                  comment="来源文档（一文档一页，uq 兜底）"),
        sa.Column("title", sa.Text(), nullable=False, comment="页标题（LLM 产物）"),
        sa.Column("summary", sa.Text(), nullable=False,
                  comment="200~400 字导航摘要（LLM 产物，不可引用）"),
        sa.Column("sections", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'[]'::jsonb"),
                  comment='[{"heading_path","summary","key_terms","chunk_ids"}]'),
        sa.Column("key_terms", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'[]'::jsonb"),
                  comment="全页术语（已 sorted 去重），供第 4 步查询扩展"),
        sa.Column("source_chunk_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
                  nullable=False, server_default=sa.text("'{}'"),
                  comment="生成依据（仅供审计；数组不能建 FK，靠 purge_derived 同事务删页）"),
        sa.Column("generator_model", sa.Text(), nullable=False, comment="口径：生成模型"),
        sa.Column("prompt_version", sa.Text(), nullable=False, comment="口径：prompt 版本"),
        sa.Column("content_hash", sa.Text(), nullable=False,
                  comment="sha1(输入文本 + 模型 + prompt 版本)[:12]（幂等键）"),
        sa.Column("status", sa.Text(), nullable=False, server_default="ready",
                  comment="ready / stale / failed"),
        sa.Column("generated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["kb_id"], ["knowledge_bases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.CheckConstraint("scope IN ('doc')", name="ck_wiki_pages_scope"),
        sa.CheckConstraint("status IN ('ready','stale','failed')", name="ck_wiki_pages_status"),
        # 一文档一页：重复生成 = DELETE 旧页 + INSERT 新页（§9.4 幂等键的落点）
        sa.UniqueConstraint("document_id", name="uq_wiki_pages_document"),
    )
    op.create_index("idx_wiki_pages_kb", "wiki_pages", ["tenant_id", "kb_id", "status"])


def downgrade() -> None:
    op.drop_index("idx_wiki_pages_kb", table_name="wiki_pages")
    op.drop_table("wiki_pages")
