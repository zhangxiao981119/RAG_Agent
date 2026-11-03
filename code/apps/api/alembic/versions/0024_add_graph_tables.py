"""新增实体图三表：graph_entities / graph_entity_mentions / graph_relations。

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-03

图的全部意义是「跨文档合并」：实体在**知识库**内合一（uq_graph_entities_kb_key），
提及把实体钉回具体 chunk（图 → 原文的唯一回链），关系只在**文档**维度记账
（边永不被引用，只是遍历线索，因此不需要 chunk 级证据）。

★ 为什么不冗余存 ACL 字段（设计文档 §8.4）：
  冗余 = 给权限数据造第二份拷贝。文档改密级 / 加 deny / 移出 kb 时副本不会自动跟，
  这正是 decisions 里「长期记忆 MUST NOT 含文档内容，那是权限副本」的同一个病。
  取片时 JOIN 回 chunks 并内联 retrieve 的四闸门，权限永远只有 chunks 一处真源。

★ name_fts / name_vec 的存在理由：实体链接（查询 → 种子实体）必须是词元级的 SQL，
  不花 LLM token；中文 2-gram 由 keyword_text.to_fts_text 在**写入侧**切好
  （与 chunks.fts_text 同一函数），查询侧用同一模块的 build_fts_query 生成 tsquery ——
  两侧各写一份切分逻辑必然漂移，表现为静默 0 召回。

纯新增表，不动既有表：downgrade 逆序 drop 即可，回滚不影响检索。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "graph_entities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False, comment="租户"),
        sa.Column("kb_id", postgresql.UUID(as_uuid=True), nullable=False, comment="所属知识库"),
        sa.Column("name", sa.Text(), nullable=False, comment="展示名：抽取结果首次出现的写法"),
        sa.Column("name_key", sa.Text(), nullable=False,
                  comment="归一化键：NFKC + 折叠空白 + lower（KB 内唯一）"),
        sa.Column("name_fts", sa.Text(), nullable=False,
                  comment="keyword_text.to_fts_text(name, \"\") 产物（中文 2-gram + ASCII 整词）"),
        # 生成列：name_fts 落库后由 PG 自动算出 tsvector（应用侧 MUST NOT 写入）。
        # 不用表达式索引的原因：ts_rank 拿不到索引里算好的 tsvector，会逐行重算
        # （与 chunks.fts_vec 同款取舍，见迁移 0020）。
        sa.Column("name_vec", postgresql.TSVECTOR(),
                  sa.Computed("to_tsvector('simple', name_fts)", persisted=True)),
        sa.Column("entity_type", sa.Text(), nullable=False, comment="类型闭集，见 CHECK"),
        sa.Column("aliases", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'[]'::jsonb"), comment="同一实体的其它出现写法"),
        sa.Column("doc_freq", sa.Integer(), nullable=False, server_default="1",
                  comment="提及它的文档数（并集去重，删除时按剩余 mention 重算）"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["kb_id"], ["knowledge_bases.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "entity_type IN ('PERSON','ORG','PRODUCT','TERM','POLICY','PROCESS','SYSTEM','OTHER')",
            name="ck_graph_entities_type",
        ),
        # KB 内同名即同一实体（本版不做消歧：同名合并是「跨文档合并」这个目标的直接推论，
        # 做类型区分会让同一实体在不同文档被抽成不同类型时分裂成两个节点）
        sa.UniqueConstraint("kb_id", "name_key", name="uq_graph_entities_kb_key"),
    )
    op.create_index("idx_graph_entities_kb", "graph_entities", ["tenant_id", "kb_id"])
    op.create_index(
        "idx_graph_entities_name_vec", "graph_entities", ["name_vec"],
        postgresql_using="gin",
    )

    op.create_table(
        "graph_entity_mentions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False, comment="租户"),
        sa.Column("kb_id", postgresql.UUID(as_uuid=True), nullable=False, comment="所属知识库"),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False, comment="来源文档"),
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True), nullable=False,
                  comment="实体出现的分片（图 → 原文的唯一回链）"),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False, comment="实体"),
        sa.Column("surface", sa.Text(), nullable=False, comment="原文中的写法（未归一）"),
        sa.Column("confidence", sa.REAL(), nullable=True,
                  comment="抽取自报置信度；NULL = 模型未提供"),
        sa.Column("extractor_model", sa.Text(), nullable=False, comment="口径：抽取模型"),
        sa.Column("prompt_version", sa.Text(), nullable=False, comment="口径：prompt 版本"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["kb_id"], ["knowledge_bases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        # chunk 级联是重解析的安全网：重解析 DELETE chunks 会带走提及。
        # ★ 但级联**不等于**清理干净 —— 会留下孤儿实体（doc_freq 需重算）与指向
        #   死 chunk 的 wiki 页，所以重索引路径 MUST 在同一事务内先 purge_derived()。
        sa.ForeignKeyConstraint(["chunk_id"], ["chunks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["entity_id"], ["graph_entities.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("chunk_id", "entity_id", name="uq_graph_mentions_chunk_entity"),
    )
    op.create_index("idx_graph_mentions_entity", "graph_entity_mentions", ["entity_id"])
    op.create_index("idx_graph_mentions_document", "graph_entity_mentions", ["document_id"])
    op.create_index("idx_graph_mentions_chunk", "graph_entity_mentions", ["chunk_id"])
    # 口径过滤索引：检索与就绪度统计都按 (extractor_model, prompt_version) 过滤
    op.create_index(
        "idx_graph_mentions_caliber", "graph_entity_mentions",
        ["extractor_model", "prompt_version"],
    )

    op.create_table(
        "graph_relations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False, comment="租户"),
        sa.Column("kb_id", postgresql.UUID(as_uuid=True), nullable=False, comment="所属知识库"),
        sa.Column("src_entity_id", postgresql.UUID(as_uuid=True), nullable=False, comment="头实体"),
        sa.Column("dst_entity_id", postgresql.UUID(as_uuid=True), nullable=False, comment="尾实体"),
        sa.Column("relation", sa.Text(), nullable=False,
                  comment="谓词短语，归一后 <= GRAPH_RELATION_MAX_CHARS"),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False,
                  comment="边只在文档维度记账（不被引用，只是遍历线索）"),
        sa.Column("support_count", sa.Integer(), nullable=False, server_default="1",
                  comment="同文档内该三元组被表述的次数（合并写入时累加）"),
        sa.Column("extractor_model", sa.Text(), nullable=False, comment="口径：抽取模型"),
        sa.Column("prompt_version", sa.Text(), nullable=False, comment="口径：prompt 版本"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["kb_id"], ["knowledge_bases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["src_entity_id"], ["graph_entities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["dst_entity_id"], ["graph_entities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.CheckConstraint("src_entity_id <> dst_entity_id", name="ck_graph_relations_not_self"),
        sa.UniqueConstraint(
            "src_entity_id", "dst_entity_id", "relation", "document_id",
            name="uq_graph_relations_triple",
        ),
    )
    op.create_index("idx_graph_rel_src", "graph_relations", ["src_entity_id"])
    op.create_index("idx_graph_rel_dst", "graph_relations", ["dst_entity_id"])
    op.create_index("idx_graph_rel_doc", "graph_relations", ["document_id"])
    op.create_index(
        "idx_graph_rel_caliber", "graph_relations",
        ["tenant_id", "kb_id", "extractor_model", "prompt_version"],
    )


def downgrade() -> None:
    # 逆序：relations / mentions 依赖 entities，且都依赖 chunks / documents / kbs
    op.drop_index("idx_graph_rel_caliber", table_name="graph_relations")
    op.drop_index("idx_graph_rel_doc", table_name="graph_relations")
    op.drop_index("idx_graph_rel_dst", table_name="graph_relations")
    op.drop_index("idx_graph_rel_src", table_name="graph_relations")
    op.drop_table("graph_relations")

    op.drop_index("idx_graph_mentions_caliber", table_name="graph_entity_mentions")
    op.drop_index("idx_graph_mentions_chunk", table_name="graph_entity_mentions")
    op.drop_index("idx_graph_mentions_document", table_name="graph_entity_mentions")
    op.drop_index("idx_graph_mentions_entity", table_name="graph_entity_mentions")
    op.drop_table("graph_entity_mentions")

    op.drop_index("idx_graph_entities_name_vec", table_name="graph_entities")
    op.drop_index("idx_graph_entities_kb", table_name="graph_entities")
    op.drop_table("graph_entities")
