from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID
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
        Index("idx_doc_kb", "tenant_id", "kb_id", "is_latest"),
        Index("idx_doc_grp", "doc_group_id", text("version DESC")),
        Index("idx_doc_governance", "tenant_id", "kb_id", "governance_status"),
    )


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    kb_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
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
        Index("idx_chunk_fts", text("to_tsvector('simple', content)"), postgresql_using="gin"),
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
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (CheckConstraint("status IN ('queued','running','succeeded','failed','dead')", name="ck_parse_jobs_status"),)


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


@event.listens_for(Chunk, "before_update")
def _chunk_before_update(mapper, connection, target):
    _validate_and_normalize_acl_tags(target)
