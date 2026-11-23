"""数据层目录：场景 → 库位 的可盘点视图 + kb 级冲突检测

Revision ID: 0030
Revises: 0029
Create Date: 2026-11-22

★ 本迁移**不加列、不加约束、不加触发器、不写数据**，只建 2 个视图 + 1 个函数。
  零影响的数学根据：没有写入者（全是 STABLE 只读对象）、检索链路一行不改、
  没有任何代码在关闭态下读它们（I-5-3 / I-5-5）。
  ★ 因此 `tests/scenario/test_0028_migration.py::_PG_CONSTRAINT_SOURCES` **不需要**
    追加注册 —— 「新迁移必须登记约束名」这条纪律只针对**产生约束**的迁移。
    本迁移一条 CHECK / UNIQUE / FK / 索引都不建，登记表里没有它该有的行。

★ 为什么**不给 knowledge_bases 加库位列**（与框架稿 §3A.8 D1 的偏移，DQ5-1）：
  · 库位是**场景**的属性：温档隔离单位是场景（框架 §8 Q11），一次检索只连一个库；
    而一个 kb 可出现在多个场景的 kb_ids 集合里（0028:166 是 JSONB 数组）。
  · 给 kb 加可写库位列 = 两个可写真相 → 必然分叉，而分叉不报错。
  · 框架 §3A.9 原文：「库位由知识集合所属场景推出，**不单独配置**」。
  · 权威列 scenarios.db_binding 早在 0028 就存在（:120），本迁移只是让它**可盘点**。

★ 为什么默认库位用 NULL 而不是哨兵 'default'：
  ck_scenarios_db_binding_format = "db_binding IS NULL OR db_binding ~ '^kb_[a-z0-9_]{2,40}$'"
  → 'default' 会被 DB 直接拒绝（0028:137-138）。展示层用 COALESCE(db_binding,'default')
    得到虚拟键 'default'（本迁移的两个视图负责这件事）。
  ★ 这条前提由 upgrade() 里的只读断言守住：CHECK 一旦被放宽，断言先失败。

★ 为什么 kb 冲突只能"检测"不能"约束"：
  冲突由"两个各自合法的场景"组合而成（同一 kb 被两个场景绑定到两个不同库位），
  这是**跨行聚合**条件，PG 的 CHECK/UNIQUE 表达不了。故落成函数 + 视图：
  健康态 = 0 行。
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ── 场景级路由视图：行数恒等于 `scenarios` 行数 ─────────────────────────────
# 四条实现细节，每条都有理由，不许"顺手"改：
#  1. `LEFT JOIN` 而非 `INNER JOIN`：无已发布版本的场景（刚建、或全归档）必须
#     仍在视图里，否则「这个场景路由到哪」在盘点里直接消失。能成立的前提是
#     `uq_scenario_versions_one_published`（每场景至多 1 个 published）→ 不放大行数，
#     「视图行数 == scenarios 行数」因此成为一条可断言的恒等式。
#  2. `v.tenant_id = s.tenant_id` 必须显式：与 resolver.LOOKUP_SQL 的加严同款，
#     漏掉它就会造出「视图说有已发布版本、resolver 说没有」的第二真源。
#  3. `state = 'published'` 是唯一版本口径：draft 会随写接口频繁变，
#     放进「数据住哪」的盘点会让视图每秒都可能变。
#  4. `db_key_source` 用两个离散值（default / bound），不用 NULL 表达 default ——
#     否则「为什么是默认」与「查不到」在同一个列上混淆。
_VIEW_SCENARIO_ROUTING_SQL = """
CREATE OR REPLACE VIEW v_scenario_db_routing AS
SELECT
    s.tenant_id,
    s.id                                                  AS scenario_id,
    s.scenario_key,
    s.status,
    s.is_builtin,
    v.version                                             AS published_version,
    s.db_binding                                          AS db_binding_raw,
    COALESCE(s.db_binding, 'default')::text                AS db_key,
    CASE WHEN s.db_binding IS NULL THEN 'default' ELSE 'bound' END AS db_key_source,
    (s.db_binding IS NULL)                                AS is_default
FROM scenarios s
LEFT JOIN scenario_versions v
       ON v.scenario_id = s.id
      AND v.tenant_id   = s.tenant_id
      AND v.state       = 'published';
"""


# ── kb 级冲突检测：健康态 = 0 行 ───────────────────────────────────────────
# 四个细节：
#  1. `jsonb_array_elements_text`：`scenario_versions.kb_ids` 是 JSONB 数组，
#     用 _text 变体直接得 text，避免 `::uuid` 在历史脏数据上转换失败（盘点不该 500）。
#  2. `WHERE s.db_binding IS NOT NULL`：NULL = 默认库，默认库与任何库都不冲突
#     （否则"全部默认"这个健康态会被误判成冲突）。
#  3. `HAVING count(DISTINCT db_key) > 1`：判据是「≥2 个不同的库位」，
#     不是「≥2 个场景」—— 同一 kb 被两个场景绑到同一个非默认库位是合法的。
#  4. 视图包一层函数：函数是唯一实现，视图只为 psql \d+ / ORM 反射友好。
_FN_KB_CONFLICTS_SQL = """
CREATE OR REPLACE FUNCTION fn_kb_db_binding_conflicts()
RETURNS TABLE (
    tenant_id   uuid,
    kb_id       text,
    db_keys     text[],
    scene_count bigint
)
LANGUAGE sql
STABLE
AS $$
WITH bound AS (
    SELECT
        s.tenant_id,
        kb_id.value            AS kb_id,
        s.db_binding           AS db_key,
        s.id                   AS scenario_id
    FROM scenarios s
    JOIN scenario_versions v
      ON v.scenario_id = s.id
     AND v.tenant_id   = s.tenant_id
     AND v.state       = 'published'
    CROSS JOIN LATERAL jsonb_array_elements_text(v.kb_ids) AS kb_id(value)
    WHERE s.db_binding IS NOT NULL
)
SELECT
    tenant_id,
    kb_id,
    array_agg(DISTINCT db_key ORDER BY db_key) AS db_keys,
    count(DISTINCT scenario_id)                AS scene_count
FROM bound
GROUP BY tenant_id, kb_id
HAVING count(DISTINCT db_key) > 1;
$$;
"""

_VIEW_KB_CONFLICTS_SQL = """
CREATE OR REPLACE VIEW v_kb_db_binding_conflicts AS
SELECT * FROM fn_kb_db_binding_conflicts();
"""


# ── 只读前置断言（不是约束，是迁移期必须失败的守卫）────────────────────────
# 本迁移的 COALESCE(..., 'default') 依赖「'default' 写不进 db_binding」这条口径。
# 若将来有人把 ck_scenarios_db_binding_format 放宽到允许 'default'，存储层与
# 展示层就会同时存在两个"默认"表示法 —— 而注释不会失败，断言会。
_ASSERT_DB_BINDING_CHECK_SQL = """
DO $$
DECLARE v_def text;
BEGIN
  SELECT pg_get_constraintdef(oid) INTO v_def
  FROM pg_constraint WHERE conname = 'ck_scenarios_db_binding_format';
  IF v_def IS NULL OR v_def NOT LIKE '%kb_%' THEN
    RAISE EXCEPTION '0030: ck_scenarios_db_binding_format 缺失或已改口径；默认库位统一用 NULL 表示，禁止把 default 写入该列（I-5-2）';
  END IF;
END $$;
"""


def upgrade() -> None:
    op.execute(_ASSERT_DB_BINDING_CHECK_SQL)
    op.execute(_VIEW_SCENARIO_ROUTING_SQL)
    op.execute(_FN_KB_CONFLICTS_SQL)
    op.execute(_VIEW_KB_CONFLICTS_SQL)


def downgrade() -> None:
    """纯 DDL 回滚（无数据、无写入者）。

    ★ 顺序：先删依赖函数的视图，再删另一个视图，最后删函数。
      `scripts/db_routing_report.py` 会查这两个对象 → 必须先 revert 脚本、
      再 downgrade 迁移，否则脚本 `UndefinedTable`（设计 §E-1）。
    """
    op.execute("DROP VIEW IF EXISTS v_kb_db_binding_conflicts")
    op.execute("DROP VIEW IF EXISTS v_scenario_db_routing")
    op.execute("DROP FUNCTION IF EXISTS fn_kb_db_binding_conflicts()")
