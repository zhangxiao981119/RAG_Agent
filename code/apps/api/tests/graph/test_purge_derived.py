"""purge_derived 与 parse_job 同事务顺序的回归测试。

★ 这是第 1 步唯一被设计文档点名的风险（「purge_derived() 的位置放错」）：
  重解析是 `DELETE chunks` + `INSERT`（新 uuid）。提及挂在 chunk_id 上有级联删除，
  于是「先删 chunk 再 purge」会让 affected 实体集合变成空集 →
  孤儿实体留着、doc_freq 全错、状态还是 done → 图路静默 0 结果。

本文件用两种手段钉住它：
  1. 直接测 purge_derived 的**开关契约**（关闭时不碰表、开启时复位状态）；
  2. 用 AST 巡检 parse_job 的两个写入点：purge 调用 MUST 位于 `delete(Chunk)` 之前，
     且两者在同一个 `async with SessionLocal()` 块内（= 同一事务）。
     这比「跑集成测试」更适合当前环境（本机 Docker 不可用，连不上 PG），
     而且它拦的正是「有人后来把 purge 挪出事务 / 挪到删 chunk 之后」。
"""
from __future__ import annotations

import ast
import pathlib
import uuid
from types import SimpleNamespace

import pytest

from app.config.settings import get_settings
from app.services.graph import purge as purge_module


@pytest.fixture
def env(monkeypatch):
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()
    monkeypatch.undo()


class _FakeDocument:
    def __init__(self) -> None:
        self.graph_status = "done"
        self.graph_extractor_model = "old-model"
        self.graph_prompt_version = "old-prompt"
        self.graph_extracted_at = "2026-01-01T00:00:00Z"


class _FakeSession:
    """替身 AsyncSession：记录 execute 的 SQL，模拟 to_regclass 探测结果。

    wiki_table_exists 模拟 `to_regclass('wiki_pages')` —— purge 第 3 步起
    用它守卫 wiki 页删除（迁移 0026 可能未跑，缺表必须跳过而不是炸）。
    """

    def __init__(self, document: _FakeDocument | None = None,
                 wiki_table_exists: bool = True) -> None:
        self._document = document
        self.commits = 0
        self.wiki_table_exists = wiki_table_exists
        self.executed: list[str] = []
        self.params: list[dict] = []

    async def scalar(self, _stmt):
        return self.wiki_table_exists

    async def get(self, _model, _pk):
        return self._document

    async def execute(self, stmt, _params=None):
        self.executed.append(str(stmt))
        self.params.append(dict(_params or {}))
        return SimpleNamespace(rowcount=1)

    def executed_sql(self) -> str:
        return "\n".join(self.executed)

    def deleted_wiki_pages(self) -> bool:
        return "DELETE FROM wiki_pages" in self.executed_sql()


class _FakeStore:
    def __init__(self) -> None:
        self.calls: list[uuid.UUID] = []

    async def delete_by_document(self, _session, *, document_id):
        self.calls.append(document_id)
        return 3


@pytest.mark.asyncio
async def test_purge_is_noop_when_graph_disabled(env, monkeypatch):
    """关闭图能力时 MUST NOT 碰图表：迁移可能没跑，碰了就是解析主路径炸掉。

    ★ 注意 wiki 页清理**不受**图开关管辖（见 test_purge_deletes_wiki_pages_
      even_when_graph_disabled）—— 本测试只断言图侧的 no-op。
    """
    env.setenv("GRAPH_ENABLED", "false")
    get_settings.cache_clear()

    store = _FakeStore()
    monkeypatch.setattr(purge_module, "get_graph_store", lambda: store)
    document = _FakeDocument()
    session = _FakeSession(document)

    deleted = await purge_module.purge_derived(session, document_id=uuid.uuid4())

    assert deleted == 0
    assert store.calls == []          # 存储层一次都没被调用
    assert document.graph_status == "done"  # 状态未被改写


@pytest.mark.asyncio
async def test_purge_deletes_wiki_pages_even_when_graph_disabled(env, monkeypatch):
    """§8.5：重索引同事务内 DELETE wiki 页 —— 守卫是**表存在**，不是 wiki_enabled。

    ★ 「历史上开过 wiki、后来关掉」的部署页还在库里；关图（或关 wiki）期间
      重解析若不删页，页的 source_chunk_ids 会指向死 chunk id —— 数组列没有
      外键，数据库不会替我们清。
    """
    env.setenv("GRAPH_ENABLED", "false")
    get_settings.cache_clear()

    store = _FakeStore()
    monkeypatch.setattr(purge_module, "get_graph_store", lambda: store)
    session = _FakeSession(document=None, wiki_table_exists=True)
    document_id = uuid.uuid4()

    deleted = await purge_module.purge_derived(session, document_id=document_id)

    assert deleted == 0                      # 图侧 no-op
    assert store.calls == []
    assert session.deleted_wiki_pages(), "wiki 页没有被同事务删除"
    assert {"id": document_id} in session.params, "DELETE 没有按 document_id 收敛"


@pytest.mark.asyncio
async def test_purge_skips_wiki_pages_when_table_missing(env, monkeypatch):
    """迁移 0026 未跑（缺表）时 MUST 跳过 wiki 清理 —— to_regclass 探测，
    绝不能让 UndefinedTable 炸掉解析主路径。"""
    env.setenv("GRAPH_ENABLED", "false")
    get_settings.cache_clear()

    store = _FakeStore()
    monkeypatch.setattr(purge_module, "get_graph_store", lambda: store)
    session = _FakeSession(document=None, wiki_table_exists=False)

    deleted = await purge_module.purge_derived(session, document_id=uuid.uuid4())

    assert deleted == 0
    assert not session.deleted_wiki_pages(), "缺表时不应发出 wiki DELETE"


@pytest.mark.asyncio
async def test_purge_clears_graph_and_resets_state(env, monkeypatch):
    env.setenv("GRAPH_ENABLED", "true")
    get_settings.cache_clear()

    store = _FakeStore()
    monkeypatch.setattr(purge_module, "get_graph_store", lambda: store)
    document = _FakeDocument()
    session = _FakeSession(document)
    document_id = uuid.uuid4()

    deleted = await purge_module.purge_derived(session, document_id=document_id)

    assert deleted == 3
    assert store.calls == [document_id]
    # 状态复位 + 口径列清空：否则补建脚本会认为这篇「已按旧口径完成」
    assert document.graph_status == "pending"
    assert document.graph_extractor_model is None
    assert document.graph_prompt_version is None
    assert document.graph_extracted_at is None


def _function_source(tree: ast.Module, name: str) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"未找到函数 {name}（改名后 MUST 同步更新本测试）")


def _line_of_call(node: ast.AST, func_name: str) -> int | None:
    """函数体内第一次调用 func_name 的行号（多处以最小行号为准，不依赖 walk 顺序）。"""
    lines = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            target = sub.func
            name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
            if name == func_name:
                lines.append(sub.lineno)
    return min(lines) if lines else None


@pytest.mark.parametrize(
    ("func_name", "docstring"),
    [
        ("_index_document", "普通文档路径（解析 → 分块 → 嵌入 → 写 chunks）"),
        ("run_parse_finalize", "扫描件聚合路径（归并 → 分块 → 嵌入 → 写 chunks）"),
    ],
)
def test_purge_runs_before_chunk_delete_in_same_transaction(func_name, docstring):
    """purge_derived MUST 在 DELETE chunks 之前、且同一事务内调用。"""
    source = (
        pathlib.Path(__file__).resolve().parents[2] / "app" / "workers" / "parse_job.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = _function_source(tree, func_name)

    purge_line = _line_of_call(function, "purge_derived")
    delete_line = _line_of_call(function, "delete")
    assert purge_line is not None, f"{func_name}：未调用 purge_derived（{docstring}）"
    assert delete_line is not None, f"{func_name}：未找到 delete(Chunk) 调用"
    assert purge_line < delete_line, (
        f"{func_name}：purge_derived 必须在 DELETE chunks 之前调用 —— "
        "否则提及已被级联删除，孤儿实体与 doc_freq 再也算不出来"
    )

    # 两者都必须位于某个 `async with SessionLocal() as session:` 块内（同事务）。
    # 判据：找最近的 AsyncWith 祖先，其上下文表达式是 SessionLocal()。
    def _inside_session_local(target_line: int) -> bool:
        for node in ast.walk(function):
            if isinstance(node, ast.AsyncWith) and node.lineno <= target_line <= getattr(
                node, "end_lineno", node.lineno
            ):
                for item in node.items:
                    expr = item.context_expr
                    if isinstance(expr, ast.Call) and getattr(expr.func, "id", "") == "SessionLocal":
                        return True
        return False

    assert _inside_session_local(purge_line), (
        f"{func_name}：purge_derived 不在 SessionLocal 事务块内 —— "
        "清理与 chunks 写入分属两个事务会让「chunks 已换、图数据还是旧的」成为可能"
    )
    assert _inside_session_local(delete_line), f"{func_name}：DELETE chunks 不在 SessionLocal 事务块内"
