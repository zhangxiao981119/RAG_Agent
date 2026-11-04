"""图召回的 SQL 契约测试（静态比对，不连库）。

这组用例防的是同一类事故：**四处各写一份 WHERE，某天只改了三处**。
漂移的表现是越权（漏掉某个闸门）或静默 0 召回（多语一个条件），都不报错 ——
所以这里用「逐字包含」而不是「语义等价」来断言。

钉住五件事：
  1. 图取片 SQL 与主召回的四闸门是**同一个字符串**，不是副本
  2. `neighbors()` 的存储层 SQL 同样内联同一份 `_WHERE_BASE`
  3. 实体链接的每一层都带 tenant / kb / 口径三件套，且口径无兜底分支
  4. 图路**不产生分数**（SQL 里没有 ts_rank / 余弦距离 / 任何排序表达式）
  5. 取片 SQL 不自带 ORDER BY（顺序由存储层给定，两处各排一次必然分叉）
"""
from __future__ import annotations

import inspect
import pathlib
import re

from app.services.graph import linking as linking_mod
from app.services.graph import recall as recall_mod
from app.services.graph.postgres_store import PostgresGraphStore
from app.services.retrieve import _WHERE_BASE

_GATES = (
    "c.tenant_id = :tenant_id",
    "c.is_latest = true",
    "c.level_rank <= :clearance",
    "c.kb_id = ANY(:authorized_kb_ids)",
    "NOT (c.deny_subjects && :user_subjects)",
    "c.acl_tags && :user_subjects",
)


def _norm(sql: str) -> str:
    """折叠空白，避免「换行位置不同」造成假失败（条件文本本身仍逐字比对）。"""
    return re.sub(r"\s+", " ", sql).strip()


def test_chunk_fetch_sql_inlines_where_base():
    sql = _norm(recall_mod._CHUNK_SQL_TEMPLATE.format(where_base=_WHERE_BASE))
    for gate in _GATES:
        assert _norm(gate) in sql, f"图取片 SQL 少了闸门：{gate}"


def test_chunk_fetch_sql_references_same_string_object():
    """模板里用的是 `{where_base}` 占位 —— 调用点必须传 `_WHERE_BASE` 本体。

    若哪天有人图省事把四闸门复制进模板字面量，这里会立刻红：
    复制品没有占位符，`format()` 后与 `_WHERE_BASE` 仍"包含"关系成立，
    但源码里已经不出现 `{where_base}` 了。
    """
    assert "{where_base}" in recall_mod._CHUNK_SQL_TEMPLATE
    source = inspect.getsource(recall_mod.recall_by_graph)
    assert "text(_CHUNK_SQL_TEMPLATE.format(where_base=_WHERE_BASE))" in _norm(source)


def test_store_neighbors_sql_inlines_where_base():
    """存储层的图扩展 SQL 同样必须内联同一份四闸门（不是"另写一份等价条件"）。"""
    source = pathlib.Path(inspect.getfile(PostgresGraphStore)).read_text(encoding="utf-8")
    method = source.split("async def neighbors", 1)[1]
    assert "{_WHERE_BASE}" in method, "neighbors 的 SQL 必须用 f-string 内联 _WHERE_BASE"
    assert "from app.services.retrieve import _WHERE_BASE" in method


def test_store_neighbors_sql_filters_caliber_on_both_sides():
    """关系与提及两侧都要带口径：只过滤一侧时，旧口径的提及会把邻居片带进来。"""
    source = pathlib.Path(inspect.getfile(PostgresGraphStore)).read_text(encoding="utf-8")
    method = source.split("async def neighbors", 1)[1].split("async def ", 1)[0]
    normalized = _norm(method)
    assert "r.extractor_model = :extractor_model" in normalized
    assert "r.prompt_version = :prompt_version" in normalized
    assert "m.extractor_model = :extractor_model" in normalized
    assert "m.prompt_version = :prompt_version" in normalized


def test_linking_layers_share_one_scope_clause():
    """三层 SQL MUST 用同一个 `_SCOPE_WHERE` 常量拼装（各写一份 = 迟早少一条）。"""
    for sql in (linking_mod._EXACT_SQL, linking_mod._FTS_SQL, linking_mod._LIKE_SQL):
        assert _norm(linking_mod._SCOPE_WHERE) in _norm(sql.text)


def test_linking_scope_has_tenant_kb_and_caliber():
    scope = _norm(linking_mod._SCOPE_WHERE)
    assert "e.tenant_id = :tenant_id" in scope
    assert "e.kb_id = ANY(:kb_ids)" in scope
    assert "m.extractor_model = :extractor_model" in scope
    assert "m.prompt_version = :prompt_version" in scope


def test_linking_scope_has_no_caliber_fallback():
    scope = linking_mod._SCOPE_WHERE.upper()
    assert "IS NULL" not in scope
    assert " OR " not in scope


def test_graph_path_produces_no_scores():
    """图路不产生任何分数：不进 RRF、不参与阈值，SQL 里也不该算排序分。"""
    sql = _norm(recall_mod._CHUNK_SQL_TEMPLATE.format(where_base=_WHERE_BASE)).lower()
    for forbidden in ("ts_rank", "<=>", "<->", "similarity(", "score"):
        assert forbidden not in sql, f"图取片 SQL 不该出现 {forbidden}（图路无分数）"


def test_chunk_fetch_sql_has_no_order_by():
    """顺序由存储层给定；这里再排一次，两处口径迟早分叉。"""
    sql = _norm(recall_mod._CHUNK_SQL_TEMPLATE.format(where_base=_WHERE_BASE)).upper()
    assert "ORDER BY" not in sql


# ── statement_timeout 的还原点（事务级设置会溢出到后续 SQL）──────
def test_restore_timeout_is_single_call_site():
    """还原点 MUST 只有一处（成功路径出口），否则早晚有一支 return 忘了收尾。"""
    source = inspect.getsource(recall_mod.recall_by_graph)
    assert source.count("_restore_statement_timeout(") == 1


def test_restore_timeout_runs_after_last_recall_sql():
    """还原必须排在取片 SQL **之后**、return 之前 —— 顺序错了等于没还原。"""
    source = inspect.getsource(recall_mod.recall_by_graph)
    chunk_sql_at = source.index("_CHUNK_SQL_TEMPLATE.format")
    restore_at = source.index("_restore_statement_timeout(")
    assert restore_at > chunk_sql_at
    # 且它在 try/except 之外：失败路径已 rollback，不该再发 SQL 开新事务
    assert "await _restore_statement_timeout" in source.split("except Exception")[-1]


def test_restore_target_is_captured_not_hardcoded():
    """还原成**调用前的值**：写死 '0' 会悄悄放宽服务端配的全局 statement_timeout。"""
    source = inspect.getsource(recall_mod._restore_statement_timeout)
    assert ":prev" in source, "还原语句必须绑定调用前读到的值"
    body = inspect.getsource(recall_mod.recall_by_graph)
    assert "_read_statement_timeout" in body
