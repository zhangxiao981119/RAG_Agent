"""第 4 步 · `load_wiki_nav_page()` 的在线失败语义（段 B，全部替身，不连库）。

钉住设计 §2.4 五条契约 + §2.5 的 statement 计数：
  1. ① 先读调用前的 `statement_timeout`（不硬编码 0），成功路径最后一条 SQL
     MUST 还原到**那个值**（不是 "0"）—— 否则 50ms 会溢出到紧接着的 ILIKE
     （顺序扫描，最易超时）/ FTS 上
  2. ③ 失败 MUST `rollback()`（QueryCanceled 让事务进入 aborted 状态），并且
     rollback 之后**不再发还原 SQL**（事务级设置已随事务消失）
  3. ④ 还原失败也 fail-open，绝不把异常抛回 `retrieve()`
  4. `no_match` 是正常态，不计 degraded
  5. `_classify` 沿 `.orig` / `__cause__` / `__context__` 展开异常链，DB 侧取消
     被 SQLAlchemy 包一层时仍归到 `timeout`，不会静默混进「代码 bug」桶
  6. 空 needles 连 1 条 SQL 都不发；`**pushdown` 原样展开、只多 4 个绑定值

★ 诚实边界：替身 session 能证明「发了哪些 SQL / 什么顺序 / 失败怎么收尾」，
  **不能**证明真库的 EXPLAIN 走索引、ACL 语义、P95 或超时真的被 PG 触发 ——
  那些在 `tests/wiki/test_nav_acl.py` 的真库探针与 §2.6 全量评测里。
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from asyncpg.exceptions import QueryCanceledError

from app.config.wiki_profile import WikiProfile
from app.services.retrieve import _WHERE_BASE
from app.services.wiki import navigate as nav_mod
from app.services.wiki.navigate import WikiNavPage, load_wiki_nav_page

TENANT = uuid.UUID(int=9)
KB = uuid.UUID(int=1)
DOC = uuid.UUID(int=2)
PROFILE = WikiProfile(generator_model="qwen-test", prompt_version="wprompt-v1")
PUSHDOWN = {
    "tenant_id": TENANT,
    "clearance": 40,
    "authorized_kb_ids": [KB],
    "user_subjects": ["dept:it"],
}


class _Wrapped(Exception):
    """模拟 SQLAlchemy 包一层：原始驱动异常挂在 `.orig`。"""

    def __init__(self, orig: BaseException) -> None:
        super().__init__("DBAPIError wrapper")
        self.orig = orig


class _Result:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def first(self):  # noqa: ANN201
        return self._rows[0] if self._rows else None


class FakeSession:
    """按 SQL 特征分发；未识别的 SQL 直接断言失败（= 没有多余 SQL 的证据）。"""

    def __init__(
        self,
        rows: list | None = None,
        *,
        current_timeout: str = "0",
        fail_on: str | None = None,
        select_exc: BaseException | None = None,
        restore_raises: bool = False,
        scalar_raises: bool = False,
        rollback_raises: bool = False,
    ) -> None:
        self._rows = rows or []
        self._current_timeout = current_timeout
        self._fail_on = fail_on
        self._select_exc = select_exc or RuntimeError("导航 SQL 抖动")
        self._restore_raises = restore_raises
        self._scalar_raises = scalar_raises
        self._rollback_raises = rollback_raises
        self.sql: list[str] = []
        self.params: list[dict] = []
        self.scalar_sql: list[str] = []
        self.rollback_calls = 0

    async def execute(self, stmt, params=None):  # noqa: ANN001, ANN201
        sql = stmt.text if hasattr(stmt, "text") else str(stmt)
        params = dict(params or {})
        self.sql.append(sql)
        self.params.append(params)
        if "set_config" in sql:
            if "v" in params:
                if self._restore_raises:
                    raise ConnectionError("连接已断（还原失败）")
            elif self._fail_on == "set":
                raise RuntimeError("设置预算失败")
            return _Result([])
        if "wiki_pages" in sql:
            if self._fail_on == "select":
                raise self._select_exc
            return _Result(self._rows)
        raise AssertionError(f"未识别的 SQL: {sql[:100]}")

    async def scalar(self, stmt, params=None):  # noqa: ANN001, ANN202
        sql = stmt.text if hasattr(stmt, "text") else str(stmt)
        assert "current_setting" in sql, f"scalar 只该用于读当前设置：{sql[:80]}"
        self.scalar_sql.append(sql)
        if self._scalar_raises:
            raise RuntimeError("读原值失败")
        return self._current_timeout

    async def rollback(self) -> None:
        self.rollback_calls += 1
        if self._rollback_raises:
            raise ConnectionError("连接已断")


def _page_row(terms: list[str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(document_id=DOC, key_terms=terms or ["访问令牌", "审计日志"])


async def _load(session: FakeSession, needles: list[str] | None = None) -> WikiNavPage:
    return await load_wiki_nav_page(
        session,
        needles=["访问"] if needles is None else needles,
        pushdown=PUSHDOWN,
        profile=PROFILE,
        timeout_ms=50,
    )


@pytest.fixture(autouse=True)
def _capture_metrics(monkeypatch: pytest.MonkeyPatch):
    """记录打点调用，避免污染进程级 prometheus 计数器。"""
    calls: list[tuple[str, int, dict]] = []

    def _incr(name: str, value: int = 1, **tags: str) -> None:
        calls.append((name, value, tags))

    monkeypatch.setattr(nav_mod.metrics, "incr", _incr)
    return calls


# ── 空输入 & 正常路径 ────────────────────────────────────────
@pytest.mark.asyncio
async def test_empty_needles_issues_zero_sql() -> None:
    session = FakeSession([_page_row()])
    out = await _load(session, needles=[])
    assert out == WikiNavPage(degraded_reason="no_match")
    assert session.sql == [] and session.scalar_sql == []
    assert session.rollback_calls == 0


@pytest.mark.asyncio
async def test_success_restores_previous_timeout_as_last_sql() -> None:
    """★ 还原成调用前的值，不是硬编码 0；且必须是最后一条 SQL。"""
    session = FakeSession([_page_row()], current_timeout="30s")
    out = await _load(session)

    assert out.degraded_reason == ""
    assert out.document_id == DOC
    assert out.key_terms == ("访问令牌", "审计日志")
    assert session.scalar_sql and "current_setting" in session.scalar_sql[0]
    assert "set_config" in session.sql[-1] and "statement_timeout" in session.sql[-1]
    assert session.params[-1] == {"v": "30s"}
    set_params = [p for p in session.params if "ms" in p]
    assert set_params == [{"ms": "50"}]
    assert session.rollback_calls == 0
    assert len(session.sql) == 3, "① 设预算 + ② 导航 SELECT + ③ 还原 = 3 条 statement"


@pytest.mark.asyncio
async def test_no_match_is_normal_not_degraded(_capture_metrics) -> None:
    session = FakeSession([])
    out = await _load(session)
    assert out.degraded_reason == "no_match"
    assert out.key_terms == () and out.document_id is None
    assert ("rag.wiki.nav.no_match", 1, {}) in _capture_metrics
    assert not [c for c in _capture_metrics if c[0] == "rag.wiki.nav.degraded"]
    assert "set_config" in session.sql[-1], "no_match 也是成功路径，照样要还原"


@pytest.mark.asyncio
async def test_scalar_failure_falls_back_to_zero_without_failing() -> None:
    """读原值失败不该让导航整体失败（fail-open），还原目标退回 PG 默认 0。"""
    session = FakeSession([_page_row()], scalar_raises=True)
    out = await _load(session)
    assert out.degraded_reason == ""
    assert session.params[-1] == {"v": "0"}


# ── 失败路径 ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_db_cancel_degrades_timeout_rolls_back_and_never_restores(_capture_metrics) -> None:
    session = FakeSession(
        fail_on="select",
        select_exc=QueryCanceledError("canceling statement due to statement timeout"),
    )
    out = await _load(session)

    assert out.degraded_reason == "timeout"
    assert out.key_terms == () and out.document_id is None
    assert session.rollback_calls == 1
    assert not [p for p in session.params if "v" in p], "rollback 后再发还原会开新事务"
    assert ("rag.wiki.nav.degraded", 1, {"reason": "timeout"}) in _capture_metrics


@pytest.mark.asyncio
async def test_wrapped_db_cancel_is_still_timeout(_capture_metrics) -> None:
    """SQLAlchemy 包一层时，沿 `.orig` 展开仍归 timeout，不混进 error 桶。"""
    wrapped = _Wrapped(QueryCanceledError("statement timeout"))
    session = FakeSession(fail_on="select", select_exc=wrapped)
    out = await _load(session)
    assert out.degraded_reason == "timeout"
    assert ("rag.wiki.nav.degraded", 1, {"reason": "timeout"}) in _capture_metrics


@pytest.mark.asyncio
async def test_generic_error_degrades_error(_capture_metrics) -> None:
    session = FakeSession(fail_on="select")
    out = await _load(session)
    assert out.degraded_reason == "error"
    assert session.rollback_calls == 1
    assert ("rag.wiki.nav.degraded", 1, {"reason": "error"}) in _capture_metrics


@pytest.mark.asyncio
async def test_rollback_failure_does_not_mask_reason() -> None:
    session = FakeSession(fail_on="select", rollback_raises=True)
    out = await _load(session)
    assert out.degraded_reason == "error"


@pytest.mark.asyncio
async def test_restore_failure_is_fail_open(_capture_metrics) -> None:
    """④ 还原失败：rollback + 「不扩展」，绝不抛回 retrieve()。"""
    session = FakeSession([_page_row()], restore_raises=True)
    out = await _load(session)
    assert out.degraded_reason == "error"
    assert out.key_terms == ()
    assert session.rollback_calls == 1
    assert ("rag.wiki.nav.degraded", 1, {"reason": "error"}) in _capture_metrics


@pytest.mark.asyncio
async def test_set_budget_failure_rolls_back() -> None:
    session = FakeSession([_page_row()], fail_on="set")
    out = await _load(session)
    assert out.degraded_reason == "error"
    assert session.rollback_calls == 1
    assert not any("wiki_pages" in s for s in session.sql), "预算没设上就不该发导航 SQL"


# ── 参数与 SQL 形状 ──────────────────────────────────────────
@pytest.mark.asyncio
async def test_params_are_pushdown_plus_four() -> None:
    """`**pushdown` 原样展开（不重算、不改名），额外只加 4 个绑定值。"""
    session = FakeSession([_page_row()])
    await _load(session, needles=["访问", "配置"])
    nav_sql = next(s for s in session.sql if "wiki_pages" in s)
    assert _WHERE_BASE in nav_sql
    nav_params = session.params[session.sql.index(nav_sql)]
    assert nav_params == {
        **PUSHDOWN,
        "needles": ["访问", "配置"],
        "gm": PROFILE.generator_model,
        "pv": PROFILE.prompt_version,
        "max_pages": 1,
    }


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (TimeoutError("wall clock"), "timeout"),
        (QueryCanceledError("canceled"), "timeout"),
        (RuntimeError("bug"), "error"),
        (ValueError("bug"), "error"),
    ],
)
def test_classify_never_invents_a_third_bucket(exc: BaseException, expected: str) -> None:
    assert nav_mod._classify(exc) == expected


def test_classify_walks_cause_and_context() -> None:
    outer = RuntimeError("outer")
    inner = TimeoutError("inner")
    outer.__cause__ = inner  # type: ignore[attr-defined]
    assert nav_mod._classify(outer) == "timeout"

    outer2 = RuntimeError("outer2")
    outer2.__context__ = QueryCanceledError("canceled")  # type: ignore[attr-defined]
    assert nav_mod._classify(outer2) == "timeout"
