"""初始化 M0 数据结构。

Revision ID: 0001
Revises:
Create Date: 2026-09-16
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 所有建表语句拆成单条执行 —— asyncpg 不支持单次 execute 多语句
_STATEMENTS = [
    "CREATE EXTENSION IF NOT EXISTS vector",
    """CREATE TABLE tenants (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        name TEXT NOT NULL,
        code TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT 'active'
            CHECK (status IN ('active','suspended')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE departments (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id UUID NOT NULL REFERENCES tenants(id),
        parent_id UUID REFERENCES departments(id) ON DELETE RESTRICT,
        name TEXT NOT NULL,
        path TEXT NOT NULL,
        depth INT NOT NULL DEFAULT 0,
        sort_order INT NOT NULL DEFAULT 0,
        visible_to_parent BOOLEAN NOT NULL DEFAULT true,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (tenant_id, path)
    )""",
    "CREATE INDEX idx_dept_parent ON departments(tenant_id, parent_id)",
    """CREATE TABLE users (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id UUID NOT NULL REFERENCES tenants(id),
        username TEXT NOT NULL,
        email TEXT,
        password_hash TEXT NOT NULL,
        display_name TEXT NOT NULL,
        dept_id UUID REFERENCES departments(id),
        clearance INT NOT NULL DEFAULT 20,
        role_names TEXT[] NOT NULL DEFAULT '{}'::TEXT[],
        status TEXT NOT NULL DEFAULT 'active'
            CHECK (status IN ('active','disabled')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (tenant_id, username)
    )""",
    "CREATE INDEX idx_users_dept ON users(tenant_id, dept_id)",
    """CREATE TABLE groups (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id UUID NOT NULL REFERENCES tenants(id),
        name TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'normal' CHECK (kind IN ('normal','external')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (tenant_id, name)
    )""",
    """CREATE TABLE user_groups (
        tenant_id UUID NOT NULL REFERENCES tenants(id),
        user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        group_id UUID NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
        PRIMARY KEY (tenant_id, user_id, group_id)
    )""",
    """CREATE TABLE roles (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id UUID NOT NULL REFERENCES tenants(id),
        name TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (tenant_id, name)
    )""",
    """CREATE TABLE user_roles (
        tenant_id UUID NOT NULL REFERENCES tenants(id),
        user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        role_id UUID NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
        PRIMARY KEY (tenant_id, user_id, role_id)
    )""",
    """CREATE TABLE knowledge_bases (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id UUID NOT NULL REFERENCES tenants(id),
        name TEXT NOT NULL,
        description TEXT,
        visibility TEXT NOT NULL DEFAULT 'restricted'
            CHECK (visibility IN ('restricted','public')),
        is_public BOOLEAN NOT NULL DEFAULT false,
        owner_id UUID REFERENCES users(id),
        acl_version INT NOT NULL DEFAULT 1,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (tenant_id, name)
    )""",
    "CREATE UNIQUE INDEX uq_kb_single_public ON knowledge_bases ((is_public)) WHERE is_public",
    """CREATE TABLE kb_members (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id UUID NOT NULL REFERENCES tenants(id),
        kb_id UUID NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
        subject_type TEXT NOT NULL CHECK (subject_type IN ('user','group','dept','role')),
        subject_id TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (kb_id, subject_type, subject_id)
    )""",
    "CREATE INDEX idx_kbmem_subject ON kb_members(tenant_id, subject_type, subject_id)",
    """CREATE TABLE documents (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id UUID NOT NULL REFERENCES tenants(id),
        kb_id UUID NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
        doc_group_id UUID NOT NULL,
        version INT NOT NULL DEFAULT 1,
        is_latest BOOLEAN NOT NULL DEFAULT true,
        filename TEXT NOT NULL,
        ext TEXT NOT NULL,
        size_bytes BIGINT NOT NULL,
        storage_key TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','parsing','indexed','failed')),
        level INT NOT NULL DEFAULT 1,
        level_rank INT NOT NULL DEFAULT 20,
        owner_dept_path TEXT,
        acl_tags TEXT[] NOT NULL DEFAULT '{}'::TEXT[],
        deny_subjects TEXT[] NOT NULL DEFAULT '{}'::TEXT[],
        uploaded_by UUID REFERENCES users(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        deleted_at TIMESTAMPTZ
    )""",
    "CREATE INDEX idx_doc_kb ON documents(tenant_id, kb_id, is_latest)",
    "CREATE INDEX idx_doc_grp ON documents(doc_group_id, version DESC)",
    """CREATE TABLE chunks (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id UUID NOT NULL REFERENCES tenants(id),
        kb_id UUID NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
        document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        chunk_index INT NOT NULL,
        content TEXT NOT NULL,
        token_count INT NOT NULL,
        heading_path TEXT,
        page_no INT,
        level_rank INT NOT NULL,
        acl_tags TEXT[] NOT NULL CHECK (cardinality(acl_tags) > 0),
        deny_subjects TEXT[] NOT NULL,
        is_latest BOOLEAN NOT NULL DEFAULT true,
        embedding vector(1024),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX idx_chunk_doc ON chunks(document_id)",
    "CREATE INDEX idx_chunk_filter ON chunks(tenant_id, kb_id, is_latest, level_rank)",
    "CREATE INDEX idx_chunk_acl ON chunks USING GIN (acl_tags)",
    "CREATE INDEX idx_chunk_deny ON chunks USING GIN (deny_subjects)",
    "CREATE INDEX idx_chunk_fts ON chunks USING GIN (to_tsvector('simple', content))",
    "CREATE INDEX idx_chunk_emb ON chunks USING hnsw (embedding vector_cosine_ops)",
    """CREATE TABLE conversations (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id UUID NOT NULL REFERENCES tenants(id),
        user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        title TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE messages (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id UUID NOT NULL REFERENCES tenants(id),
        conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        role TEXT NOT NULL CHECK (role IN ('user','assistant')),
        content TEXT NOT NULL,
        citations JSONB NOT NULL DEFAULT '[]'::JSONB,
        meta JSONB NOT NULL DEFAULT '{}'::JSONB,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX idx_msg_conv ON messages(conversation_id, created_at)",
    """CREATE TABLE parse_jobs (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id UUID NOT NULL REFERENCES tenants(id),
        document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','succeeded','failed','dead')),
        attempts INT NOT NULL DEFAULT 0,
        max_attempts INT NOT NULL DEFAULT 3,
        last_error TEXT,
        started_at TIMESTAMPTZ,
        finished_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    # audit_logs 表与索引由 0005 迁移统一创建，0001 MUST NOT 重复建表（全新库会 DuplicateTable）
    """CREATE TABLE eval_cases (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id UUID NOT NULL REFERENCES tenants(id),
        kb_id UUID REFERENCES knowledge_bases(id) ON DELETE CASCADE,
        question TEXT NOT NULL,
        expected_answerable BOOLEAN NOT NULL,
        expected_doc_ids UUID[] DEFAULT '{}',
        note TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
]

_DROP_STATEMENTS = [
    "DROP TABLE IF EXISTS eval_cases",
    "DROP TABLE IF EXISTS parse_jobs",
    "DROP TABLE IF EXISTS messages",
    "DROP TABLE IF EXISTS conversations",
    "DROP TABLE IF EXISTS chunks",
    "DROP TABLE IF EXISTS documents",
    "DROP TABLE IF EXISTS kb_members",
    "DROP TABLE IF EXISTS knowledge_bases",
    "DROP TABLE IF EXISTS user_roles",
    "DROP TABLE IF EXISTS roles",
    "DROP TABLE IF EXISTS user_groups",
    "DROP TABLE IF EXISTS groups",
    "DROP TABLE IF EXISTS users",
    "DROP TABLE IF EXISTS departments",
    "DROP TABLE IF EXISTS tenants",
]


def upgrade() -> None:
    for stmt in _STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in _DROP_STATEMENTS:
        op.execute(stmt)
