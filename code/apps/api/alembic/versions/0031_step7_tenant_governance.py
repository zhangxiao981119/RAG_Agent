"""租户层收敛：库可见性上限、版本钉定、策略收紧、分级字段

Revision ID: 0031
Revises: 0030
Create Date: 2026-11-23

★ 本迁移只加 **7 个 nullable/可推导列** 与对应约束、索引，不建新表、不改检索拓扑。
  关闭 `TENANT_GOVERNANCE_ENABLED` 时这些列一列都不读，行为回到 0030。

★ 为什么不建 `scenarios(pinned_version) → scenario_versions` 外键：
  `scenario_versions.scenario_id` 已经反向引用 `scenarios.id`，再加一条正向复合外键
  会形成循环外键，删场景时 RESTRICT/CASCADE 会互相顶住。版本没有删除路径，
  写入时校验 + 解析期 fail-closed 已经足够，不给回滚制造额外障碍。

★ `allowed_db_keys` 用 `text[]` 而不是 JSONB：
  它就是一个字符串集合，`NULL / 空数组 / 数组` 三种语义用数组表达最直接；
  元素格式由应用层校验（`default` 或 `^kb_[a-z0-9_]{2,40}$`），
  DB 只守「一维、元素非 NULL」这两条能写进 CHECK 的事实。

★ `tier` 与 `db_binding` 的关系用 CHECK 绑死：
  `cold ⇔ db_binding IS NULL`，`warm/hot ⇒ db_binding IS NOT NULL`。
  部署搬库时必须在同一条语句里改 `tier`，否则 CHECK 拒绝，
  不会出现「库搬了、等级没搬」的第二真源。

★ 回填口径：`db_binding IS NULL → cold`，非 NULL → `warm`。
  现有默认场景都在控制库，基线是 cold；框架写的「回滚到全部温」与本部署事实不符，
  这里按事实回填，并在设计文档里写明。
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_UPGRADE_STATEMENTS = (
    # 租户级：库可见性上限
    "ALTER TABLE tenants ADD COLUMN allowed_db_keys text[]",
    "ALTER TABLE tenants ADD CONSTRAINT ck_tenants_allowed_db_keys_dims "
    "CHECK (allowed_db_keys IS NULL OR array_ndims(allowed_db_keys) = 1)",
    "ALTER TABLE tenants ADD CONSTRAINT ck_tenants_allowed_db_keys_no_null "
    "CHECK (allowed_db_keys IS NULL OR array_position(allowed_db_keys, NULL) IS NULL)",
    # 场景级：版本钉定
    "ALTER TABLE scenarios ADD COLUMN pinned_version integer",
    "ALTER TABLE scenarios ADD CONSTRAINT ck_scenarios_pinned_version "
    "CHECK (pinned_version IS NULL OR pinned_version >= 1)",
    # 场景级：L1 收紧（NULL = 不收紧）
    "ALTER TABLE scenarios ADD COLUMN recall_top_k_max integer",
    "ALTER TABLE scenarios ADD CONSTRAINT ck_scenarios_recall_cap "
    "CHECK (recall_top_k_max IS NULL OR recall_top_k_max > 0)",
    "ALTER TABLE scenarios ADD COLUMN threshold_vector_min double precision",
    "ALTER TABLE scenarios ADD CONSTRAINT ck_scenarios_threshold_vector_min "
    "CHECK (threshold_vector_min IS NULL OR threshold_vector_min BETWEEN 0 AND 1)",
    "ALTER TABLE scenarios ADD COLUMN threshold_rerank_min double precision",
    "ALTER TABLE scenarios ADD CONSTRAINT ck_scenarios_threshold_rerank_min "
    "CHECK (threshold_rerank_min IS NULL OR threshold_rerank_min BETWEEN 0 AND 1)",
    "ALTER TABLE scenarios ADD COLUMN rerank_allowed boolean",
    # 场景级：分级（先可空，回填后再收紧）
    "ALTER TABLE scenarios ADD COLUMN tier text",
    "UPDATE scenarios SET tier = CASE WHEN db_binding IS NULL THEN 'cold' ELSE 'warm' END",
    "ALTER TABLE scenarios ALTER COLUMN tier SET NOT NULL",
    "ALTER TABLE scenarios ALTER COLUMN tier SET DEFAULT 'cold'",
    "ALTER TABLE scenarios ADD CONSTRAINT ck_scenarios_tier "
    "CHECK (tier IN ('hot','warm','cold'))",
    "ALTER TABLE scenarios ADD CONSTRAINT ck_scenarios_tier_binding "
    "CHECK ((tier = 'cold' AND db_binding IS NULL) "
    "OR (tier IN ('warm','hot') AND db_binding IS NOT NULL))",
    "CREATE INDEX idx_scenarios_tenant_tier ON scenarios (tenant_id, tier)",
)

_DOWNGRADE_STATEMENTS = (
    "DROP INDEX IF EXISTS idx_scenarios_tenant_tier",
    "ALTER TABLE scenarios DROP CONSTRAINT IF EXISTS ck_scenarios_tier_binding",
    "ALTER TABLE scenarios DROP CONSTRAINT IF EXISTS ck_scenarios_tier",
    "ALTER TABLE scenarios DROP COLUMN IF EXISTS tier",
    "ALTER TABLE scenarios DROP COLUMN IF EXISTS rerank_allowed",
    "ALTER TABLE scenarios DROP CONSTRAINT IF EXISTS ck_scenarios_threshold_rerank_min",
    "ALTER TABLE scenarios DROP COLUMN IF EXISTS threshold_rerank_min",
    "ALTER TABLE scenarios DROP CONSTRAINT IF EXISTS ck_scenarios_threshold_vector_min",
    "ALTER TABLE scenarios DROP COLUMN IF EXISTS threshold_vector_min",
    "ALTER TABLE scenarios DROP CONSTRAINT IF EXISTS ck_scenarios_recall_cap",
    "ALTER TABLE scenarios DROP COLUMN IF EXISTS recall_top_k_max",
    "ALTER TABLE scenarios DROP CONSTRAINT IF EXISTS ck_scenarios_pinned_version",
    "ALTER TABLE scenarios DROP COLUMN IF EXISTS pinned_version",
    "ALTER TABLE tenants DROP CONSTRAINT IF EXISTS ck_tenants_allowed_db_keys_no_null",
    "ALTER TABLE tenants DROP CONSTRAINT IF EXISTS ck_tenants_allowed_db_keys_dims",
    "ALTER TABLE tenants DROP COLUMN IF EXISTS allowed_db_keys",
)


def upgrade() -> None:
    for statement in _UPGRADE_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for statement in _DOWNGRADE_STATEMENTS:
        op.execute(statement)
