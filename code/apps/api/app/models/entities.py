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

    __table_args__ = (
        CheckConstraint("status IN ('pending','parsing','indexed','failed')", name="ck_documents_status"),
        Index("idx_doc_kb", "tenant_id", "kb_id", "is_latest"),
        Index("idx_doc_grp", "doc_group_id", text("version DESC")),
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
    )


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_audit_time", "tenant_id", text("created_at DESC")),
        Index("idx_audit_user", "tenant_id", "user_id", text("created_at DESC")),
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


class AuditLog(Base):
    """审计日志 —— 谁在何时做了什么（手册 §4.2.10）。

    user 不设 FK：用户删除后日志仍保留；保留期 1 年（§8 D-15），归档任务后续实现。
    """

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # action 命名：chat.ask / doc.upload / doc.delete / kb.create / acl.member.set / auth.login.*
    action: Mapped[str] = mapped_column(Text, nullable=False)
    object_type: Mapped[str | None] = mapped_column(Text)
    object_id: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    ip: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_audit_time", "tenant_id", created_at.desc()),
        Index("idx_audit_user", "tenant_id", "user_id", created_at.desc()),
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
from sqlalchemy import event as _sa_event


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


@_sa_event.listens_for(Document, "before_insert")
def _doc_before_insert(mapper, connection, target):
    _validate_and_normalize_acl_tags(target)


@_sa_event.listens_for(Document, "before_update")
def _doc_before_update(mapper, connection, target):
    _validate_and_normalize_acl_tags(target)


@_sa_event.listens_for(Chunk, "before_insert")
def _chunk_before_insert(mapper, connection, target):
    _validate_and_normalize_acl_tags(target)


@_sa_event.listens_for(Chunk, "before_update")
def _chunk_before_update(mapper, connection, target):
    _validate_and_normalize_acl_tags(target)
