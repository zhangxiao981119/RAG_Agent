"""数据契约的静态一致性测试（不连库）。

钉住三处「改了一处忘另一处」的经典漂移点：
  1. 迁移 0024 的索引名 ⇄ ORM 模型的 Index 名；
  2. 迁移 0025 的 documents 图状态四列 ⇄ ORM 列；
  3. decisions.GRAPH_ENTITY_TYPES（代码闭集）⇄ 迁移里的 DDL CHECK 字面量。

★ 为什么用静态比对而不是等 alembic autogenerate：
  autogenerate 需要连库（本机 Docker 不可用），而且它**不会**报「代码里的类型元组
  与 DDL CHECK 不一致」—— 那种不一致的表现是「抽取侧允许某类型、入库直接 CHECK 违例」，
  一篇文档整体 failed，而错误信息只是一句 constraint violation。
"""
from __future__ import annotations

import pathlib
import re

from app.config import decisions
from app.models import Document, GraphEntity, GraphEntityMention, GraphRelation

_MIGRATIONS = pathlib.Path(__file__).resolve().parents[2] / "alembic" / "versions"


def _read(name: str) -> str:
    return (_MIGRATIONS / name).read_text(encoding="utf-8")


def test_index_names_match_between_migration_and_orm():
    migration = set(re.findall(r'op\.create_index\(\s*"([^"]+)"', _read("0024_add_graph_tables.py")))
    assert migration, "未从迁移里解析到任何索引名（正则失效？）"
    orm = set()
    for model in (GraphEntity, GraphEntityMention, GraphRelation):
        orm |= {index.name for index in model.__table__.indexes}
    assert migration == orm, (
        "迁移与 ORM 的索引名不一致 —— 自动生成的对比测试看不到它，"
        f"差异：迁移多 {sorted(migration - orm)}，ORM 多 {sorted(orm - migration)}"
    )


def test_documents_graph_state_columns_exist():
    columns = set(Document.__table__.columns.keys())
    for name in (
        "graph_status",
        "graph_extractor_model",
        "graph_prompt_version",
        "graph_extracted_at",
    ):
        assert name in columns, f"documents 缺少图状态列 {name}（迁移 0025 与 ORM 不同步）"


def test_entity_type_closed_set_matches_ddl_check():
    ddl = _read("0024_add_graph_tables.py")
    match = re.search(r"entity_type IN \(([^)]*)\)", ddl)
    assert match, "未在迁移里找到 entity_type 的 CHECK 闭集"
    ddl_types = tuple(
        item.strip().strip("'") for item in match.group(1).split(",") if item.strip()
    )
    assert ddl_types == decisions.GRAPH_ENTITY_TYPES, (
        "GRAPH_ENTITY_TYPES 与 DDL CHECK 不一致 —— 抽取侧允许的类型会在入库时 "
        f"撞 CHECK 约束：代码 {decisions.GRAPH_ENTITY_TYPES} vs DDL {ddl_types}"
    )


def test_generated_vector_column_is_not_writable_from_app():
    """name_vec 是生成列：应用侧 MUST NOT 写入（写它 PG 会直接报错）。"""
    column = GraphEntity.__table__.columns["name_vec"]
    assert column.computed is not None
    assert column.computed.sqltext is not None
