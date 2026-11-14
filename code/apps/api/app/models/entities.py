from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    REAL,
    Text,
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    # §3.2.6 权限缓存版本号；任何权限变更都 +1 使旧缓存 key 失效
    acl_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (CheckConstraint("status IN ('active','suspended')", name="ck_tenants_status"),)


class Department(Base):
    __tablename__ = "departments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("departments.id", ondelete="RESTRICT"))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    depth: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    visible_to_parent: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("tenant_id", "path", name="uq_departments_tenant_path"),
        Index("idx_dept_parent", "tenant_id", "parent_id"),
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    username: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(Text)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    dept_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("departments.id"))
    clearance: Mapped[int] = mapped_column(Integer, nullable=False, server_default="20")
    role_names: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    memory: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("status IN ('active','disabled')", name="ck_users_status"),
        UniqueConstraint("tenant_id", "username", name="uq_users_tenant_username"),
        Index("idx_users_dept", "tenant_id", "dept_id"),
    )


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default="normal")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("kind IN ('normal','external')", name="ck_groups_kind"),
        UniqueConstraint("tenant_id", "name", name="uq_groups_tenant_name"),
    )


class UserGroup(Base):
    __tablename__ = "user_groups"

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True)


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_roles_tenant_name"),)


class UserRole(Base):
    __tablename__ = "user_roles"

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True)


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(Text, nullable=False, server_default="restricted")
    is_public: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    acl_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("visibility IN ('restricted','public')", name="ck_kb_visibility"),
        UniqueConstraint("tenant_id", "name", name="uq_kb_tenant_name"),
        Index("uq_kb_single_public", "is_public", unique=True, postgresql_where=text("is_public")),
    )


class KnowledgeBaseMember(Base):
    __tablename__ = "kb_members"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    kb_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False)
    subject_type: Mapped[str] = mapped_column(Text, nullable=False)
    subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("subject_type IN ('user','group','dept','role')", name="ck_kb_members_subject_type"),
        UniqueConstraint("kb_id", "subject_type", "subject_id", name="uq_kb_members_subject"),
        Index("idx_kbmem_subject", "tenant_id", "subject_type", "subject_id"),
    )


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    kb_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False)
    doc_group_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    is_latest: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    ext: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    level: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    level_rank: Mapped[int] = mapped_column(Integer, nullable=False, server_default="20")
    owner_dept_path: Mapped[str | None] = mapped_column(Text)
    acl_tags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    deny_subjects: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # ── M7 知识治理（四维审查结论 + 治理元数据，迁移 0015）──
    # 四维分数 NULL = 未审查；审查结论只写这些字段与 governance_report，
    # MUST NOT 改写 filename/content 等原文（INV-M-11）。
    freshness_score: Mapped[int | None] = mapped_column(Integer)
    authority_score: Mapped[int | None] = mapped_column(Integer)
    consistency_score: Mapped[int | None] = mapped_column(Integer)
    completeness_score: Mapped[int | None] = mapped_column(Integer)
    governance_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    governance_report: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    source_kind: Mapped[str] = mapped_column(Text, nullable=False, server_default="upload")
    effective_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str | None] = mapped_column(Text)
    # ── 图抽取状态（迁移 0025，GraphRAG）──
    # 「哪篇没抽、按什么口径抽的、什么时候抽的」三问的答案都在这里，
    # 使图就绪度退化成 documents 上的一个 WHERE（无需 JOIN 第四张状态表）。
    # ★ 检索侧 MUST NOT 读这几列：文档可检索性与图抽取解耦，抽取失败只影响图路。
    graph_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    # 口径列：与 graph_entities 的 (extractor_model, prompt_version) 同源（graph_profile）。
    # 换模型 / 改 prompt 后这两列与当前口径不符 → backfill --recaliber 选中它重抽。
    graph_extractor_model: Mapped[str | None] = mapped_column(Text)
    graph_prompt_version: Mapped[str | None] = mapped_column(Text)
    graph_extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("status IN ('pending','parsing','indexed','failed')", name="ck_documents_status"),
        CheckConstraint(
            "governance_status IN ('pending','running','done','failed')",
            name="ck_documents_governance_status",
        ),
        CheckConstraint(
            "source_kind IN ('upload','sync_local','sync_git','sync_web')",
            name="ck_documents_source_kind",
        ),
        CheckConstraint(
            "graph_status IN ('pending','running','done','failed','skipped')",
            name="ck_documents_graph_status",
        ),
        Index("idx_doc_kb", "tenant_id", "kb_id", "is_latest"),
        Index("idx_doc_grp", "doc_group_id", text("version DESC")),
        Index("idx_doc_governance", "tenant_id", "kb_id", "governance_status"),
        Index("idx_doc_graph_status", "tenant_id", "is_latest", "graph_status"),
    )


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    kb_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # M8 关键词召回（迁移 0020）：中文重叠 2-gram + ASCII 整词的预切分文本。
    # 由 keyword_text.to_fts_text 生成；'simple' 配置不能切中文，故必须在入库侧切好，
    # 查询侧再用同一模块生成 tsquery。为 NULL 表示该行尚未回填。
    fts_text: Mapped[str | None] = mapped_column(Text)
    # 生成列：fts_text 落库后由 PG 自动算出 tsvector（应用侧 MUST NOT 写入）。
    # ★ 不存 tsvector 而是每次 to_tsvector 的代价：ts_rank 对每条命中行重算并
    #   detoast 原文，实测 142ms（命中数百行）；用存储列后 1.9ms。
    fts_vec = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', fts_text)", persisted=True)
    )
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    heading_path: Mapped[str | None] = mapped_column(Text)
    page_no: Mapped[int | None] = mapped_column(Integer)
    level_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    acl_tags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    deny_subjects: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    is_latest: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1024))
    # ── 索引口径（方案一：索引/模型版本）──
    # 记录本 chunk 的向量由什么口径产出，供检索隔离与「过期重算」判定。
    # 取值来自 app/config/index_profile.py 的 current_index_profile()，写入时落到行上。
    embedding_model: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_text_version: Mapped[str] = mapped_column(Text, nullable=False)
    chunker_version: Mapped[str] = mapped_column(Text, nullable=False)
    # M7 治理加权（迁移 0017）：在 rerank 之后叠加，MUST NOT 进入 RRF（INV-M-13）。
    # 落列缓存避免检索期每次 join documents。
    governance_weight: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # 表格决策 A1'（迁移 0018）：超大表行组切分的组 id，同组分片可被检索期回捞。
    # 仅纯表格分片非空；整表单片 / 混合块为 NULL。
    table_group_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("cardinality(acl_tags) > 0", name="ck_chunks_acl_tags_not_empty"),
        Index("idx_chunk_doc", "document_id"),
        Index("idx_chunk_filter", "tenant_id", "kb_id", "is_latest", "level_rank"),
        Index("idx_chunk_acl", "acl_tags", postgresql_using="gin"),
        Index("idx_chunk_deny", "deny_subjects", postgresql_using="gin"),
        # 关键词召回（迁移 0020 替代旧的 idx_chunk_fts）：索引建在预切分文本生成的
        # tsvector 存储列上，中文 2-gram 也因此可命中。
        # ★ 不用表达式索引：ts_rank 拿不到索引里算好的 tsvector，仍会逐行重算。
        Index("idx_chunk_fts_vec", "fts_vec", postgresql_using="gin"),
        Index(
            "idx_chunk_emb",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        # 组回捞查询走部分索引：仅组 id 非空的行参与（组数远小于 chunk 总数）
        Index(
            "idx_chunk_table_group",
            "table_group_id",
            postgresql_where=text("table_group_id IS NOT NULL"),
        ),
        # 章节补全（迁移 0022）：按 (document_id, heading_path) 取同节分片。
        # 同样走部分索引 —— 无标题分片永远不会被章节补全选中。
        Index(
            "idx_chunk_section",
            "document_id",
            "heading_path",
            "chunk_index",
            postgresql_where=text("heading_path IS NOT NULL"),
        ),
    )


class KbTerm(Base):
    """知识库领域词表（迁移 0023）—— 查询扩展的依据。

    由 scripts/build_kb_terms.py 离线从 chunks 抽取，不依赖大模型：
      · model     型号/料号（XYZ-1000），aliases 是它的写法变体
      · acronym   文档里成对出现的缩写与全称（`访问令牌（AccessToken）`）
    检索期只做「term / aliases 命中查询词 → 补充另一侧写法」，词元总量仍受
    ILIKE_MAX_TERMS 与 FTS_QUERY_MAX_TERMS 约束。
    """

    __tablename__ = "kb_terms"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    kb_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False
    )
    # 归一后的词条（NFKC + 小写），查询侧按同一规则归一后精确匹配
    term: Mapped[str] = mapped_column(Text, nullable=False)
    # 写法变体 / 对照词（JSONB 数组），如型号的三种分隔写法、缩写与全称互指
    aliases: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    # 在知识库原文中的出现次数：抽取时用于过滤低频噪声
    freq: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("kb_id", "term", name="uq_kb_terms_kb_term"),
        Index("idx_kb_terms_lookup", "kb_id"),
    )


class GraphEntity(Base):
    """图实体（迁移 0024）—— KB 内合一。

    「跨文档合并」是图的全部意义：同一实体在 A 文档与 B 文档的提及指向**同一行**，
    于是「从实体出发找相邻 chunk」才能跨越文档边界（这正是向量路做不到的事）。
    name_key（NFKC + 折叠空白 + lower）是合并键，MUST 由
    app/services/graph/extract.py::normalize_name_key 的同一函数产出 ——
    抽取侧（写实体）与链接侧（查询 → 种子实体）各写一份必然漂移，
    而漂移的表现是「实体明明在库里，却一条也链不上」，不报错。
    """

    __tablename__ = "graph_entities"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    kb_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    name_key: Mapped[str] = mapped_column(Text, nullable=False)
    # 关键词式实体链接的检索列：写入侧由 keyword_text.to_fts_text(name, "") 生成，
    # 查询侧由同一模块的 build_fts_query 生成（与 chunks.fts_text 同款单一真源）。
    name_fts: Mapped[str] = mapped_column(Text, nullable=False)
    # 生成列，应用侧 MUST NOT 写入
    name_vec = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', name_fts)", persisted=True)
    )
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    aliases: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    doc_freq: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "entity_type IN ('PERSON','ORG','PRODUCT','TERM','POLICY','PROCESS','SYSTEM','OTHER')",
            name="ck_graph_entities_type",
        ),
        UniqueConstraint("kb_id", "name_key", name="uq_graph_entities_kb_key"),
        Index("idx_graph_entities_kb", "tenant_id", "kb_id"),
        Index("idx_graph_entities_name_vec", "name_vec", postgresql_using="gin"),
    )


class GraphEntityMention(Base):
    """实体提及（迁移 0024）—— 图 → 原文的唯一回链，也是 ACL 的落点。

    ★ 提及表里**没有** acl_tags / level_rank（设计文档 §8.4）：可见性一律 JOIN 回
      chunks 用四闸门判定。冗余存一份权限副本，文档改密级时副本不会跟着变，
      而漂移的表现是「越权或漏召回」，都不报错。
    """

    __tablename__ = "graph_entity_mentions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    kb_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    chunk_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chunks.id", ondelete="CASCADE"), nullable=False
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("graph_entities.id", ondelete="CASCADE"), nullable=False
    )
    surface: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float | None] = mapped_column(REAL)
    # 口径章：检索 MUST 按这两列过滤（旧口径 = 查不到，不混算）
    extractor_model: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("chunk_id", "entity_id", name="uq_graph_mentions_chunk_entity"),
        Index("idx_graph_mentions_entity", "entity_id"),
        Index("idx_graph_mentions_document", "document_id"),
        Index("idx_graph_mentions_chunk", "chunk_id"),
        Index("idx_graph_mentions_caliber", "extractor_model", "prompt_version"),
    )


class GraphRelation(Base):
    """实体关系（迁移 0024）—— 边只在**文档**维度记账。

    边永不被引用，只是遍历线索，所以不需要 chunk 级证据；support_count 是
    「同文档内该三元组被表述的次数」，用作扩展时的路径强度权重。
    """

    __tablename__ = "graph_relations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    kb_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False
    )
    src_entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("graph_entities.id", ondelete="CASCADE"), nullable=False
    )
    dst_entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("graph_entities.id", ondelete="CASCADE"), nullable=False
    )
    relation: Mapped[str] = mapped_column(Text, nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    support_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    extractor_model: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("src_entity_id <> dst_entity_id", name="ck_graph_relations_not_self"),
        UniqueConstraint(
            "src_entity_id", "dst_entity_id", "relation", "document_id",
            name="uq_graph_relations_triple",
        ),
        Index("idx_graph_rel_src", "src_entity_id"),
        Index("idx_graph_rel_dst", "dst_entity_id"),
        Index("idx_graph_rel_doc", "document_id"),
        Index("idx_graph_rel_caliber", "tenant_id", "kb_id", "extractor_model", "prompt_version"),
    )


class IntegrationApplication(Base):
    """企业应用凭证与可访问知识库范围。"""

    __tablename__ = "integration_applications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # 高熵随机密钥只保存 SHA-256 哈希，明文仅在创建响应中出现一次。
    key_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    kb_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    daily_token_limit: Mapped[int] = mapped_column(BigInteger, nullable=False)
    daily_message_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("status IN ('active','disabled')", name="ck_integration_app_status"),
        CheckConstraint("rate_limit_per_minute > 0", name="ck_integration_app_rate_positive"),
        CheckConstraint("daily_token_limit > 0", name="ck_integration_app_token_positive"),
        CheckConstraint("daily_message_limit > 0", name="ck_integration_app_message_positive"),
        UniqueConstraint("tenant_id", "name", name="uq_integration_app_tenant_name"),
        Index("idx_integration_app_tenant", "tenant_id", "status"),
    )


class IntegrationConversation(Base):
    """集成会话与调用应用、真实用户的绑定关系。"""

    __tablename__ = "integration_conversations"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("integration_applications.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index(
            "idx_integration_conversation_subject",
            "tenant_id",
            "application_id",
            "user_id",
            "created_at",
        ),
    )


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # context 压缩次数（新开对话自动重置为 0）
    compression_count: Mapped[int] = mapped_column(default=0, server_default="0")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    citations: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, server_default="[]")
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("role IN ('user','assistant')", name="ck_messages_role"),
        Index("idx_msg_conv", "conversation_id", "created_at"),
        # 口径快照（方案二）反查用：meta @> '{"caliber":{...}}'（jsonb_path_ops 对包含查询最优）
        Index(
            "idx_msg_meta_gin",
            "meta",
            postgresql_using="gin",
            postgresql_ops={"meta": "jsonb_path_ops"},
        ),
    )


class ParseJob(Base):
    __tablename__ = "parse_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="3")
    last_error: Mapped[str | None] = mapped_column(Text)
    # 方案 C 分段进度：非扫描件恒为 0/0；扫描件 pages_total=文档页数、
    # pages_done=已完成 OCR 的页数（段成功时累加），经 GET /jobs 上报给前端。
    pages_done: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    pages_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (CheckConstraint("status IN ('queued','running','succeeded','failed','dead')", name="ck_parse_jobs_status"),)


class ParseSegment(Base):
    """方案 C：扫描件 OCR 的分段执行单元（执行单元，不是语义边界）。

    大扫描件在单个 arq 任务里 OCR 会越过 parse_job_timeout 被杀；改为按
    OCR_MAX_SCAN_PAGES 切段串行执行 —— 每段只做「渲染 + 逐页 OCR」，
    产出**每页原始文本**落本表 page_texts，不做标题识别（标题跨页累积推迟到
    聚合任务 run_parse_finalize，保证 heading 口径与整份一次解析时一致）。

    ★ 段结果是**已完成进度**：重试时若本段已 succeeded 直接跳过 OCR，
      只重跑失败段（这是把 segment 落库的唯一理由）。
    ★ page_texts 为 JSONB 字符串数组，索引 i 对应 page_no = page_start + i，
      聚合期按 segment_index 升序拼接即得全局页序（INV-M-05 可回链）。
    """

    __tablename__ = "parse_segments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("parse_jobs.id", ondelete="CASCADE"), nullable=False
    )
    segment_index: Mapped[int] = mapped_column(Integer, nullable=False)
    page_start: Mapped[int] = mapped_column(Integer, nullable=False)  # 1-based，含
    page_end: Mapped[int] = mapped_column(Integer, nullable=False)    # 1-based，含
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    page_texts: Mapped[list[str] | None] = mapped_column(JSONB)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed','dead')",
            name="ck_parse_segments_status",
        ),
        UniqueConstraint("job_id", "segment_index", name="uq_parse_segments_job_index"),
    )


class GovernanceJob(Base):
    """M7 WU-M-03：四维审查异步任务（与 ParseJob 同构，迁移 0016）。

    审查 MUST 独立于文档解析主路径（INV-M-10）；失败降级为"未审查"，
    知识 MUST 仍可被检索（INV-M-12）。
    """

    __tablename__ = "governance_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="3")
    last_error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("status IN ('queued','running','succeeded','failed','dead')", name="ck_governance_jobs_status"),
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(Text, nullable=False)
    object_type: Mapped[str | None] = mapped_column(Text)
    object_id: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    ip: Mapped[str | None] = mapped_column(INET)
    # 链路追踪 ID：与该次请求的日志同值，可按 trace 把审计与容器日志串成一条链
    trace_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_audit_time", "tenant_id", text("created_at DESC")),
        Index("idx_audit_user", "tenant_id", "user_id", text("created_at DESC")),
        Index("idx_audit_trace", "tenant_id", "trace_id"),
    )


class SyncSource(Base):
    """M6 任务 1：知识源同步 — 配置表（§8 D-07）；M7 WU-M-04 增补 web 源。

    支持三种源类型：
    - local：扫描宿主机挂载目录
    - git：定时 git pull 后扫描工作目录
    - web：内部网址抓取（鉴权 → 发现页面 → 清洗 → 切片 → 增量）

    增量指纹存在 synced_files（JSONB），格式：
    { "relative/path.md": {"mtime": float, "size": int, "doc_id": "uuid"} }

    web 源的凭据 MUST 经 app/services/crypto.py 加密后存 auth_payload（INV-M-07），
    MUST NOT 明文落库 / 写日志。
    """

    __tablename__ = "sync_sources"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    kb_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)  # local | git | web
    path: Mapped[str] = mapped_column(Text, nullable=False)  # 本地目录 或 Git URL
    branch: Mapped[str | None] = mapped_column(Text)  # Git 分支，仅 git 类型
    file_patterns: Mapped[str] = mapped_column(Text, nullable=False, server_default="*.md,*.pdf,*.txt,*.docx")
    level_rank: Mapped[int] = mapped_column(Integer, nullable=False, server_default="20")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")  # active | paused
    synced_files: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    # ── M7 web 源字段（迁移 0017）──
    auth_type: Mapped[str] = mapped_column(Text, nullable=False, server_default="none")  # none | basic | bearer | cookie
    auth_payload: Mapped[str | None] = mapped_column(Text)  # crypto 加密后的凭据密文
    entry_urls: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    scope_rules: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("source_type IN ('local','git','web')", name="ck_sync_sources_type"),
        CheckConstraint("status IN ('active','paused')", name="ck_sync_sources_status"),
    )


class EvalCase(Base):
    __tablename__ = "eval_cases"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    kb_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"))
    question: Mapped[str] = mapped_column(Text, nullable=False)
    expected_answerable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    expected_doc_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), server_default="{}")
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EvalRun(Base):
    """评估基线运行记录 —— 手册 §7.3 / M6 G5 门禁的版本化留痕（项2：评估基线版本）。

    每跑一次 scripts/run_eval.py 落一条，记录「这一次评估的指标 + 用例集指纹 + 当时的运行口径」。
    用途：① 留痕，可回溯「某次发布前评估是什么成绩」；
         ② 基线对比，--baseline 与最近一次**同用例集**的运行比对，指标回退超过
            --max-regression 即以非零退出码阻断发布。

    ★ 用例集指纹（case_fingerprint）是**可比性前提**：用例增删改后指标天然会变，
      与旧基线比毫无意义。指纹不一致时基线对比 MUST 跳过而非强行比对。
    """

    __tablename__ = "eval_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    # 本次指标：refusal_rate / miss_rate / hit_rate / answerable / unanswerable / judged
    metrics: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    # 参与评估的用例条数（与 case_fingerprint 一起界定「比的是哪一批」）
    case_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # 用例集指纹 = sha1(排序后的用例身份集合)[:12]，用例增删改即变
    case_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    # 运行口径快照（contract / 模型 / 提示词版本 / 阈值 / 配置指纹），用于解释指标差异来源
    caliber: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    # 本次是否通过门禁（--assert-* 与 --baseline 全部满足才算 True）
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_eval_runs_tenant_fp_created", "tenant_id", "case_fingerprint", "created_at"),
    )


class FinetuneSample(Base):
    """采纳的问答对 —— 微调数据收集（用户采纳后入库，微调时按 exported_at 取出未导出样本）。"""

    __tablename__ = "finetune_samples"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    # 不设 FK 级联：会话/消息清理不应连带丢失已收集的微调数据
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # 唯一约束：同一条回答只能被采纳一次（幂等）
    assistant_message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, unique=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    citations: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, server_default="[]")
    adopted_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    adopted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # 导出打标：微调取出后写时间戳，下次只取未导出的增量
    exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("idx_ft_tenant_exported", "tenant_id", "exported_at"),)


# ── M6 续篇：配额 / 敏感词 / 灰度开关 ───────────────────────────


class TenantQuota(Base):
    """租户级配额（一租户一行）。M6 续篇 — 单/租户双层 token 限额。

    表无记录时 chat.py / services/quota.py 走 settings 默认值。
    """

    __tablename__ = "tenant_quotas"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False, unique=True)
    # 每日 token 上限（输入+输出合计）
    daily_token_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    # 每日问答次数上限
    daily_message_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    # 月度全局 token 上限（防止单租户刷爆整月预算）
    monthly_token_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SensitiveWord(Base):
    """敏感词表（admin 管理，命中即拒答）。

    word 小写存储；匹配时先对输入做 NFKC 归一化 + 去零宽 + 小写。
    """

    __tablename__ = "sensitive_words"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    word: Mapped[str] = mapped_column(Text, nullable=False)
    # 分类：政治/广告/违法/辱骂，仅展示用
    category: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("tenant_id", "word", name="uq_sensitive_tenant_word"),
        Index("idx_sensitive_tenant_word", "tenant_id", "word"),
    )


class FeatureFlag(Base):
    """特性开关（按部门灰度，关闭即回滚功能）。

    dept_path_pattern 支持 SQL LIKE：'公司/研发中心/%'、'%' 表示全量。
    rollout_percent 命中部门内再按 user_id hash 取模放量（0-100）。
    """

    __tablename__ = "feature_flags"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    feature_key: Mapped[str] = mapped_column(Text, nullable=False)
    dept_path_pattern: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    rollout_percent: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("tenant_id", "feature_key", "dept_path_pattern", name="uq_feature_flags_tenant_key_pattern"),
        Index("idx_feature_flags_tenant_key", "tenant_id", "feature_key", "enabled"),
    )


class PromptVersion(Base):
    """提示词版本（DB 存版本，约束段仍锁在代码里，不可在线改）。

    设计要点：
      · 同一 prompt_key 可存多版本，version 单调递增（0 号保留给代码内置默认，不落库）
      · status: draft（草稿）/ active（生效）/ archived（归档）
      · 每个 key 同一时刻只允许一条 active，由部分唯一索引 uq_prompt_versions_active 兜底
      · content 只放「可编辑区」文本；约束段与拒答文案由 app/config/prompts.py 在 build
        时拼回，激活前 MUST 通过 build 校验，防止把约束改没了
    """

    __tablename__ = "prompt_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    prompt_key: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("tenant_id", "prompt_key", "version", name="uq_prompt_versions_tenant_key_version"),
        Index("idx_prompt_versions_tenant_key_status", "tenant_id", "prompt_key", "status"),
        # 每个 key 同一时刻只允许一条 active —— 并发激活时由数据库保证唯一
        Index(
            "uq_prompt_versions_active",
            "tenant_id",
            "prompt_key",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )


# ─────────────────────────────────────────────────────────────
# M4 任务 3：acl_tags 写库断言（模型层 event listener，不可绕过）
#
# 覆盖两条约束（手册 §3.2.4 + §4.2.8）：
#   A1  public MUST 单独存在（acl_tags = ["public"]，不能混 dept/group/...）
#   A2  acl_tags MUST NOT 含 level: 标签（放进来会让 G4 绕过 G1 密级闸门）
#   空  acl_tags 为空数组 → 自动规范化为 ["public"]
#
# 实现方式：SQLAlchemy before_insert / before_update event，所有写库路径
# （上传 API、解析 worker、未来手动标签覆盖）都会走这里。
# 抛 AclTagError → 转 ValueError（API 层已有 ValueError → 400 的映射）。
# ─────────────────────────────────────────────────────────────


def _validate_and_normalize_acl_tags(target: object) -> None:
    """对 Document / Chunk 的 acl_tags 做断言 + 空数组规范化。

    lazy import validate_acl_tags 避免 models → services 的循环依赖。
    """
    tags = getattr(target, "acl_tags", None)
    if tags is None:
        return  # 列有 server_default，允许 None 在 DB 层处理

    # 空数组规范化为 ["public"]（约束 1）
    if not tags:
        target.acl_tags = ["public"]
        tags = ["public"]

    # 断言（lazy import）
    from app.services.acl import validate_acl_tags
    from app.services.acl.errors import AclTagError
    try:
        validate_acl_tags(tags)
    except AclTagError as exc:
        raise ValueError(str(exc)) from exc


@event.listens_for(Document, "before_insert")
def _doc_before_insert(mapper, connection, target):
    _validate_and_normalize_acl_tags(target)


@event.listens_for(Document, "before_update")
def _doc_before_update(mapper, connection, target):
    _validate_and_normalize_acl_tags(target)


@event.listens_for(Chunk, "before_insert")
def _chunk_before_insert(mapper, connection, target):
    _validate_and_normalize_acl_tags(target)


class WikiPage(Base):
    """LLM wiki 导航页（迁移 0026，第 3 步）—— 纯派生数据的读模型。

    ★ 一文档一页（uq_wiki_pages_document）：重复生成的幂等语义是
      「content_hash 相同跳过，不同 DELETE 旧页 + INSERT 新页」（§9.4）。
    ★ 本表**不进检索**（术语扩展是第 4 步）：检索侧一行不读它，
      WIKI_ENABLED=false 时整张表无人读写。
    ★ 不冗余存 ACL 字段（§8.4）：页的可见性由读接口判定 —— 列表按 kb 成员，
      详情按「该文档存在至少一条过四闸门的 chunk」（EXISTS 内联 _WHERE_BASE）。
      冗余一份权限副本 = 文档改密级时副本不会跟着变，而漂移不报错。
    ★ source_chunk_ids 是 UUID[]（数组不能建 FK）：重索引换 chunk 行后页会指向
      死 id —— 对策是 purge_derived() 在重索引**同一事务**内 DELETE 页（§8.5），
      不是外键。
    """

    __tablename__ = "wiki_pages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    kb_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False
    )
    scope: Mapped[str] = mapped_column(Text, nullable=False, server_default="doc")
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    # 200~400 字导航摘要（LLM 产物，**不可引用** —— 读接口带固定 disclaimer）
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    # sections = [{"heading_path": str, "summary": str, "key_terms": [str], "chunk_ids": [uuid]}]
    sections: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    # 全页术语（已 sorted 去重），供第 4 步查询扩展
    key_terms: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    # 生成依据（仅供审计；数组不能建 FK，靠 purge_derived 同事务删页）
    source_chunk_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default=text("'{}'")
    )
    generator_model: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    # sha1(输入文本 + 模型 + prompt 版本)[:12]（幂等键，§9.4）
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="ready")
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("scope IN ('doc')", name="ck_wiki_pages_scope"),
        CheckConstraint("status IN ('ready','stale','failed')", name="ck_wiki_pages_status"),
        UniqueConstraint("document_id", name="uq_wiki_pages_document"),
        Index("idx_wiki_pages_kb", "tenant_id", "kb_id", "status"),
    )



@event.listens_for(Chunk, "before_update")
def _chunk_before_update(mapper, connection, target):
    _validate_and_normalize_acl_tags(target)
