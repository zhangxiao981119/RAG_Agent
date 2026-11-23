"""chunk 口径分布：可重算候选的统一判定函数 + 巡检视图

Revision ID: 0029
Revises: 0028
Create Date: 2026-11-22

★ 本迁移只建 1 个函数 + 1 个视图：不建表、不加列、不写数据、**不建任何约束**。
  零影响的数学根据：没有写入者、没有触发器、检索链路一行不改（I-4B-4）。
  ★ 因此 `tests/scenario/test_0028_migration.py::_PG_CONSTRAINT_SOURCES` **不需要**
    追加注册 —— 「新迁移必须登记约束名」这条纪律只针对**产生约束**的迁移。
    把它写到 docstring 里，是为了拦住下一个人「顺手也登记一下 0029」的自我怀疑。

★ 为什么"判定"必须落在 SQL 函数里，而不是在 Python 里写 predicate：
  判定的消费方有两个 —— 应用（视图 / 端点 / 发布快照）与重算脚本（`--check` 的
  CI 门禁）。两处各写一遍 predicate 的第一次分叉就会产生「视图说 0 条、脚本说
  300 条」，而这类分叉**没有任何单一行为用例能同时覆盖两边**（两边各自内部自洽）。
  函数是唯一能保证「同一份实现」的载体（I-4B-2）。

★ 为什么函数接收三个口径参数，而不是在函数里读当前配置：
  「当前口径」的唯一真源是 `app/config/index_profile.py::current_index_profile()`
  （它依赖 settings 与 decisions 两个 Python 层，DB 看不到）。让 DB 自己算等于造
  第二个真源 —— 那正是 `index_profile.py` 模块 docstring 反复警告的形态（I-4B-1）。
  故本函数是「**数据形状**的判据」，不是「当前口径的判据」：口径由调用方绑定进来。
  与之配套的守卫是 `tests/scenario/test_step4b_guards.py::T-4B-8`（本文件与
  `needs_reindex.py` 里不得出现口径**字面量**）。

★ 为什么要 `STABLE` 而不是 `VOLATILE`：同事务内多次调用结果一致，且 PG 可内联
  SQL 函数 → `WHERE f.stale_hard` 能下推到 `chunks` 上。不加 `SECURITY DEFINER`：
  权限跟随调用方，与既有「四闸门把授权下推到 SQL」的纪律一致。
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ── 判定函数（唯一实现；脚本 / 视图 / 端点 / 发布快照全部调它）──────────────
# 四个实现细节，每个都有理由，不许"顺手"改：
#  1. `WHERE c.is_latest = true` 是**分母（universe）的唯一定义**（I-4B-7）。
#     **不要**在这里加 `d.deleted_at IS NULL` / `d.status = 'indexed'` /
#     `c.embedding IS NOT NULL` —— 这三个都是**列**（document_deleted /
#     document_is_latest / embedding_missing），由调用方决定要不要排除。
#     分母一旦加条件就变成"判定的一部分"，而"排除谁"是需要独立裁决的（U-4B-1）。
#  2. `LEFT JOIN` 而非 `INNER JOIN`：`chunks.document_id` 现在是 `nullable=False` 外键，
#     理论上 inner join 不掉行；但用 LEFT + COALESCE 让"外键约束若将来被放松"这件事
#     **不改变行数** —— 分母不变是 I-4B-7 的硬要求。
#  3. 三处比较用 `<>` 而不是 `IS NOT DISTINCT FROM`：三列都是 NOT NULL
#     （`entities.py` 的 `chunks` 定义），NULL 不可能出现；用 `<>` 的好处是
#     "若将来某列被改成 nullable，函数会静默漏判而不是抛错"—— 那正是错的，
#     所以同批必须有一条**前置条件断言**（见下方 _ASSERT_NOT_NULL_SQL）。
#  4. 列名一律加 `chunk_` 前缀再返回：调用方用 `f.stale_hard` / `f.chunk_id` 读，
#     不与 `chunks` 的原始列名混淆（脚本里 `JOIN ... f ON f.chunk_id = c.id`）。
_FN_SQL = """
CREATE OR REPLACE FUNCTION fn_chunk_reindex_candidates(
    p_embedding_model        text,
    p_embedding_text_version text,
    p_chunker_version        text
)
RETURNS TABLE (
    chunk_id              uuid,
    tenant_id             uuid,
    document_id           uuid,
    kb_id                 uuid,
    chunk_embedding_model text,
    chunk_text_version    text,
    chunk_chunker_version text,
    embedding_missing     boolean,
    stale_hard            boolean,
    stale_chunker         boolean,
    document_deleted      boolean,
    document_is_latest    boolean,
    chunk_created_at      timestamptz
)
LANGUAGE sql
STABLE
AS $$
SELECT
    c.id,
    c.tenant_id,
    c.document_id,
    c.kb_id,
    c.embedding_model,
    c.embedding_text_version,
    c.chunker_version,
    (c.embedding IS NULL)                                      AS embedding_missing,
    (c.embedding_model        <> p_embedding_model
     OR c.embedding_text_version <> p_embedding_text_version)   AS stale_hard,
    (c.chunker_version        <> p_chunker_version)            AS stale_chunker,
    COALESCE(d.deleted_at IS NOT NULL, false)                  AS document_deleted,
    COALESCE(d.is_latest, true)                                AS document_is_latest,
    c.created_at
FROM chunks c
LEFT JOIN documents d ON d.id = c.document_id
WHERE c.is_latest = true
$$;
"""

# ── 巡检视图：**故意不含任何口径参数** ────────────────────────────────────
# 它回答的是「库里到底有几套口径」，不是「哪些过期」。分开的理由：
# "哪些过期"需要当前口径（Python 侧才有），"有几套口径"不需要。
# 混在一起会让视图的列语义随部署变化，而视图是要给人 psql 里看的（列语义必须稳定）。
_VIEW_SQL = """
CREATE OR REPLACE VIEW v_chunk_caliber_distribution AS
SELECT
    c.tenant_id,
    c.embedding_model,
    c.embedding_text_version,
    c.chunker_version,
    COALESCE(d.deleted_at IS NOT NULL, false) AS document_deleted,
    count(*)                                  AS chunk_count,
    count(*) FILTER (WHERE c.embedding IS NULL) AS embedding_missing,
    min(c.created_at)                         AS oldest_chunk_at,
    max(c.created_at)                         AS newest_chunk_at
FROM chunks c
LEFT JOIN documents d ON d.id = c.document_id
WHERE c.is_latest = true
GROUP BY 1, 2, 3, 4, 5;
"""

# ── 函数正确性的**前置条件断言**（不是约束，是迁移里必须失败的守卫）──────────
# 函数里用 `<>`：NULL 会让比较得到 NULL（既不 true 也不 false）→ 该行四个桶都不落
# → 对账恒等式失配。把前置条件写成迁移里的显式断言，比写在注释里强 —— 注释不会失败。
_ASSERT_NOT_NULL_SQL = """
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'chunks'
      AND column_name IN ('embedding_model','embedding_text_version','chunker_version')
      AND is_nullable <> 'NO'
  ) THEN
    RAISE EXCEPTION '0029: chunks 三列口径必须 NOT NULL，否则 stale_hard 的 <> 比较会漏判 NULL';
  END IF;
END $$;
"""


def upgrade() -> None:
    op.execute(_ASSERT_NOT_NULL_SQL)
    op.execute(_FN_SQL)
    op.execute(_VIEW_SQL)


def downgrade() -> None:
    """纯 DDL 回滚（函数与视图无数据、无写入者）。

    ★ 顺序陷阱：`scripts/reindex_embeddings.py` 会调本函数 → 若先 downgrade 再
      revert 脚本，脚本会 `UndefinedFunction`（而它可能是唯一的重算手段）。
      正确顺序是**先 revert 脚本、再 downgrade 迁移**（设计 §E-1）。
    """
    op.execute("DROP VIEW IF EXISTS v_chunk_caliber_distribution")
    op.execute("DROP FUNCTION IF EXISTS fn_chunk_reindex_candidates(text, text, text)")
