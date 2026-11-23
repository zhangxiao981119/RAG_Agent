"""场景化配置：scenarios / scenario_versions / integration_applications.scenario_id

Revision ID: 0028
Revises: 0027
Create Date: 2026-11-20

★ 本迁移只建结构 + 种子行，不读取任何业务表，不改任何派生数据。
  零影响的数学根据：种子行的 policy 是空覆盖集（'{}'::jsonb），
  解析结果在定义上等于平台默认值，因此「没绑场景的应用」行为逐字段不变。

★ dna_declared 的 4 个代码常量刻意**硬编码**（纪律同 0011_add_chunk_index_profile.py:7-10）：
  迁移是历史快照，必须可重放、不随未来配置漂移。
  4 个环境绑定项（embedding_model / embedding_dim / 两个模型名）**不落值**，
  以 env_bound 声明 —— 否则等于把某个部署环境的配置写进所有部署。
  一致性由 tests/scenario/test_0028_seed.py 锁死：代码 bump 而未改本迁移 ⇒ CI 红。

★ db_binding 列本期建而不用（Step 6 起生效）。写入非 NULL 时由启动自检 WARN，
  避免「配了不生效」的静默失败。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ── 种子行字面量（唯一取值来源：设计文档 §0.5 取值台账） ──────────────────
# 全部为代码常量或其派生结果；环境绑定项以 env_bound 声明，不落值。
_SEED_DNA = {
    # index_profile.py:37
    "embedding_text_version": "etext-v1",
    # index_profile.py:45-51 派生自 decisions.py:439/440/441 → c{400}-{800}-{60}
    "chunker_version": "c400-800-60",
    # graph_profile.py:30
    "graph_prompt_version": "gprompt-v1",
    # wiki_profile.py:21
    "wiki_prompt_version": "wprompt-v1",
    # 由部署环境决定，迁移不落值（见文件头说明）
    "env_bound": [
        "embedding_model",        # settings.py:79（默认 bge-m3）
        "embedding_dim",          # settings.py:80（默认 1024）
        "graph_extractor_model",  # settings.py:163（默认空串 → 解析为 llm_model）
        "wiki_generator_model",   # settings.py:189（默认空串 → 解析为 llm_model）
    ],
}

_SEED_POLICY: dict = {}
_SEED_KB_IDS: list[str] = []
_SEED_BINDING_MODE = "unrestricted"

# policy 键闭集（Step 3 已接线的 4 个键；后续步骤的迁移在此扩展）
# ★ 与 app/services/scenario/policy_spec.py 的 WIRED_KEYS 必须相等，
#   由 tests/scenario/test_resolver_policy.py::test_policy_keyset_matches_migration 锁死。
_POLICY_KEYS = [
    "recall_top_k",
    "rerank_enabled",
    "relevance_threshold_vector",
    "relevance_threshold_rerank",
]


def canonical_payload(
    *, policy: dict, kb_ids: list[str], binding_mode: str, dna_declared: dict
) -> str:
    """版本行内容的规范序列化（排序键 + 紧凑分隔符 + 不转义非 ASCII）。"""
    return json.dumps(
        {
            "binding_mode": binding_mode,
            "dna_declared": dna_declared,
            "kb_ids": kb_ids,
            "policy": policy,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def compute_checksum(
    *, policy: dict, kb_ids: list[str], binding_mode: str, dna_declared: dict
) -> str:
    """与运行时 compute_checksum() 必须逐字节一致（tests 锁死）。"""
    raw = canonical_payload(
        policy=policy, kb_ids=kb_ids, binding_mode=binding_mode, dna_declared=dna_declared
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


_SEED_CHECKSUM = compute_checksum(
    policy=_SEED_POLICY,
    kb_ids=_SEED_KB_IDS,
    binding_mode=_SEED_BINDING_MODE,
    dna_declared=_SEED_DNA,
)


def upgrade() -> None:
    # ── 1. scenarios：只存身份 ────────────────────────────────────────
    op.create_table(
        "scenarios",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scenario_key", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("is_builtin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("db_binding", sa.Text()),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.CheckConstraint("status IN ('active','disabled')", name="ck_scenarios_status"),
        sa.CheckConstraint(
            "scenario_key ~ '^[a-z][a-z0-9_]{1,63}$'", name="ck_scenarios_key_format"
        ),
        sa.CheckConstraint("length(name) BETWEEN 1 AND 128", name="ck_scenarios_name_len"),
        sa.CheckConstraint(
            "db_binding IS NULL OR db_binding ~ '^kb_[a-z0-9_]{2,40}$'",
            name="ck_scenarios_db_binding_format",
        ),
        sa.UniqueConstraint("tenant_id", "scenario_key", name="uq_scenarios_tenant_key"),
    )
    # 每租户至多一个内置场景：用部分唯一索引表达（CHECK 无法表达聚合唯一）
    op.create_index(
        "uq_scenarios_one_builtin",
        "scenarios",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("is_builtin"),
    )

    # ── 2. scenario_versions：内容 + 版本 ─────────────────────────────
    op.create_table(
        "scenario_versions",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scenario_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="draft"),
        sa.Column(
            "policy", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "kb_ids", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("binding_mode", sa.Text(), nullable=False, server_default="explicit"),
        sa.Column("dna_declared", postgresql.JSONB(), nullable=False),
        sa.Column("checksum", sa.Text(), nullable=False),
        sa.Column("change_note", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("validated_at", sa.DateTime(timezone=True)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["scenario_id"], ["scenarios.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "state IN ('draft','validated','published','archived')",
            name="ck_scenario_versions_state",
        ),
        sa.CheckConstraint("version >= 1", name="ck_scenario_versions_version_positive"),
        sa.CheckConstraint(
            "binding_mode IN ('explicit','unrestricted')",
            name="ck_scenario_versions_binding_mode",
        ),
        # C-A1 policy 是对象
        sa.CheckConstraint(
            "jsonb_typeof(policy) = 'object'", name="ck_scenario_versions_policy_object"
        ),
        # C-A2 policy 键闭集（未接线的键写进来就是静默 no-op，必须在写入时挡住）
        sa.CheckConstraint(
            "policy - ARRAY[" + ",".join(f"'{k}'" for k in _POLICY_KEYS) + "]::text[] = '{}'::jsonb",
            name="ck_scenario_versions_policy_keyset",
        ),
        # C-A3 / C-A5 范围兜底；主校验在应用层（SQLSTATE 差异见设计文档 §A.3.1 末段）
        sa.CheckConstraint(
            "(policy->>'recall_top_k')::int BETWEEN 1 AND 1000",
            name="ck_scenario_versions_policy_recall_top_k",
        ),
        sa.CheckConstraint(
            "(policy->>'relevance_threshold_vector')::float8 BETWEEN 0.0 AND 1.0",
            name="ck_scenario_versions_policy_threshold_vector",
        ),
        sa.CheckConstraint(
            "(policy->>'relevance_threshold_rerank')::float8 BETWEEN 0.0 AND 1.0",
            name="ck_scenario_versions_policy_threshold_rerank",
        ),
        # C-A4 rerank_enabled 必须布尔
        sa.CheckConstraint(
            "policy->'rerank_enabled' IS NULL "
            "OR jsonb_typeof(policy->'rerank_enabled') = 'boolean'",
            name="ck_scenario_versions_policy_rerank_bool",
        ),
        # C-B1 binding_mode 与 kb_ids 联动（防「忘了绑定」= 静默放开全部范围）
        sa.CheckConstraint(
            "(binding_mode = 'explicit' AND jsonb_array_length(kb_ids) > 0) "
            "OR (binding_mode = 'unrestricted' AND kb_ids = '[]'::jsonb)",
            name="ck_scenario_versions_binding_consistent",
        ),
        # C-B2 kb_ids 元素必须全是字符串
        sa.CheckConstraint(
            "NOT jsonb_path_exists(kb_ids, '$[*] ? (@.type() != \"string\")')",
            name="ck_scenario_versions_kb_ids_strings",
        ),
        # dna_declared 键闭集 + 4 个代码常量必填
        sa.CheckConstraint(
            "jsonb_typeof(dna_declared) = 'object'",
            name="ck_scenario_versions_dna_object",
        ),
        sa.CheckConstraint(
            "dna_declared ? 'embedding_text_version' AND dna_declared ? 'chunker_version' "
            "AND dna_declared ? 'graph_prompt_version' AND dna_declared ? 'wiki_prompt_version' "
            "AND dna_declared ? 'env_bound'",
            name="ck_scenario_versions_dna_required_keys",
        ),
        sa.CheckConstraint(
            "dna_declared - ARRAY['embedding_text_version','chunker_version',"
            "'graph_prompt_version','wiki_prompt_version','env_bound']::text[] = '{}'::jsonb",
            name="ck_scenario_versions_dna_keyset",
        ),
        sa.CheckConstraint(
            "checksum ~ '^[0-9a-f]{16}$'", name="ck_scenario_versions_checksum_format"
        ),
        # C-V1 状态 → 时间戳一致
        sa.CheckConstraint(
            "(state <> 'published' OR published_at IS NOT NULL) "
            "AND (state <> 'validated' OR validated_at IS NOT NULL) "
            "AND (state <> 'archived' OR archived_at IS NOT NULL)",
            name="ck_scenario_versions_state_timestamps",
        ),
        sa.UniqueConstraint(
            "tenant_id", "scenario_id", "version", name="uq_scenario_versions_scenario_version"
        ),
    )
    # 「每场景至多一个已发布版本」= 数据库事实
    op.create_index(
        "uq_scenario_versions_one_published",
        "scenario_versions",
        ["scenario_id"],
        unique=True,
        postgresql_where=sa.text("state = 'published'"),
    )
    op.create_index(
        "idx_scenario_versions_lookup",
        "scenario_versions",
        ["tenant_id", "scenario_id", "state", "version"],
    )

    # ── 3. integration_applications.scenario_id ────────────────────────
    # 不回填：NULL 语义 = 内置默认场景，回填零收益且不可逆（见设计文档 §A.4）
    op.add_column(
        "integration_applications",
        sa.Column("scenario_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_integration_app_scenario",
        "integration_applications",
        "scenarios",
        ["scenario_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "idx_integration_app_scenario",
        "integration_applications",
        ["tenant_id", "scenario_id"],
    )

    # ── 4. 种子行：每租户一条内置场景（0 租户时插入 0 行，安全） ───────
    op.execute(
        sa.text(
            """
            WITH created AS (
                INSERT INTO scenarios
                    (tenant_id, scenario_key, name, status, is_builtin, db_binding, description)
                SELECT t.id, 'default', '内置默认场景', 'active', true, NULL,
                       '由迁移 0028 创建；policy 为空=完全等同平台默认口径，不可编辑'
                FROM tenants t
                ON CONFLICT (tenant_id, scenario_key) DO NOTHING
                RETURNING id, tenant_id
            )
            INSERT INTO scenario_versions
                (tenant_id, scenario_id, version, state,
                 policy, kb_ids, binding_mode, dna_declared, checksum, change_note, published_at)
            SELECT c.tenant_id, c.id, 1, 'published',
                   CAST(:policy AS jsonb), CAST(:kb_ids AS jsonb), :binding_mode,
                   CAST(:dna AS jsonb), :checksum,
                   '初始版本：空覆盖集，等价于平台默认', now()
            FROM created c
            """
        ).bindparams(
            policy=json.dumps(_SEED_POLICY, ensure_ascii=False),
            kb_ids=json.dumps(_SEED_KB_IDS, ensure_ascii=False),
            binding_mode=_SEED_BINDING_MODE,
            dna=json.dumps(_SEED_DNA, ensure_ascii=False, sort_keys=True),
            checksum=_SEED_CHECKSUM,
        )
    )


def downgrade() -> None:
    op.drop_index("idx_integration_app_scenario", table_name="integration_applications")
    op.drop_constraint(
        "fk_integration_app_scenario", "integration_applications", type_="foreignkey"
    )
    op.drop_column("integration_applications", "scenario_id")

    op.drop_index("idx_scenario_versions_lookup", table_name="scenario_versions")
    op.drop_index("uq_scenario_versions_one_published", table_name="scenario_versions")
    op.drop_table("scenario_versions")

    op.drop_index("uq_scenarios_one_builtin", table_name="scenarios")
    op.drop_table("scenarios")
