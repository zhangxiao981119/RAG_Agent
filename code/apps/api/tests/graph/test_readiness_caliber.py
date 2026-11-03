"""就绪度口径 = 已索引文档（`--check` 永远红的那次回归）。

背景（真实故障，2026-11）：`--all` 已经跑成「成功 20 · 跳过 0 · 失败 0」，
但 `--check` 仍然退出码 1。原因是选取条件与体检口径不是同一批文档：

  · `_base_select()` 带 `status = 'indexed'`（没解析完的文档抽不出东西）；
  · `_check()` 的统计却用「全部未删除文档」当分母 —— 一篇解析失败的
    `e2e_spec.png`（0 分片、一个月前的 e2e 夹具）永远 `graph_status='pending'`，
    永远不被选中，却永远被算进「未就绪」。

后果比红更糟：**一个永远不会绿的闸门，运维很快就学会无视它**。

本文件钉住三件事：
  1. 统计与退出码只看已索引文档；未索引的单独一行说明、不进 `未就绪合计`；
  2. 横幅 / 能力快照同口径，覆盖率分母是 `done / (total - skipped)`
     （skipped 是「明确不该抽」，算已处理，不留在分母里当欠账）；
  3. 「标 done 但零提及」的巡检有内容长度下限（短夹具不该反复触发人工排查）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import capabilities
from scripts import backfill_graph as bg


class _FakeCheckSession:
    """只实现 `_check()` 用到的两个方法，不需要数据库。

    `scalar()` 按 SQL 文本分派返回值 —— 比「按调用顺序」稳，实现里调整查询顺序
    不会让测试莫名其妙地失败。
    """

    def __init__(
        self,
        row: SimpleNamespace,
        *,
        not_indexed: int = 0,
        entities: int = 0,
        relations: int = 0,
        empty_done: int = 0,
    ) -> None:
        self._row = row
        self._values = {
            "not_indexed": not_indexed,
            "entities": entities,
            "relations": relations,
            "empty_done": empty_done,
        }
        self.sql: list[str] = []

    async def __aenter__(self) -> _FakeCheckSession:
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    async def execute(self, stmt, params=None):  # noqa: ANN001
        self.sql.append(str(stmt))
        row = self._row

        class _Result:
            def one(self_inner):
                return row

        return _Result()

    async def scalar(self, stmt, params=None):  # noqa: ANN001
        sql = str(stmt)
        self.sql.append(sql)
        if "status <> 'indexed'" in sql:
            return self._values["not_indexed"]
        if "graph_entity_mentions" in sql:
            return self._values["empty_done"]
        if "FROM graph_entities" in sql:
            return self._values["entities"]
        if "FROM graph_relations" in sql:
            return self._values["relations"]
        raise AssertionError(f"未预期的查询: {sql[:120]}")


def _install(monkeypatch, session: _FakeCheckSession) -> None:
    monkeypatch.setattr(bg, "SessionLocal", lambda: session)


def _row(**overrides) -> SimpleNamespace:
    base = {
        "total": 21,
        "done": 20,
        "pending": 0,
        "running": 0,
        "failed": 0,
        "skipped": 1,
        "caliber_mismatch": 0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_check_counts_only_indexed_docs_and_exits_zero(monkeypatch, capsys):
    """20 done + 1 skipped + 1 未索引 → 就绪（退出码 0），未索引单独成行。"""
    session = _FakeCheckSession(_row(), not_indexed=1, entities=432, relations=182)
    _install(monkeypatch, session)

    assert await bg._check() == 0

    out = capsys.readouterr().out
    assert "已索引文档总数 : 21" in out
    assert "已按当前口径完成: 20" in out
    assert "未索引(不参与) : 1 篇" in out
    assert "先修解析链路" in out
    assert "未就绪合计     : 0" in out
    # 统计 SQL 必须限定已索引（与 _base_select 同一批文档）
    assert any("status = 'indexed'" in sql for sql in session.sql)


@pytest.mark.asyncio
async def test_not_indexed_docs_never_block_readiness(monkeypatch, capsys):
    """未索引文档再多也不影响退出码 —— 那是解析链路的账，不是图抽取的。"""
    session = _FakeCheckSession(_row(done=19, skipped=1), not_indexed=5)
    _install(monkeypatch, session)

    assert await bg._check() == 0
    assert "未索引(不参与) : 5 篇" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_real_backlog_on_indexed_docs_still_fails(monkeypatch, capsys):
    """已索引文档里真有欠账（pending / failed / 口径不匹配）→ 退出码 1（门不能松）。"""
    session = _FakeCheckSession(
        _row(done=17, pending=1, failed=1, caliber_mismatch=1), not_indexed=1
    )
    _install(monkeypatch, session)

    assert await bg._check() == 1
    assert "未就绪合计     : 3" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_empty_mention_alarm_has_min_content_length_guard(monkeypatch):
    """零提及巡检带内容长度下限：短夹具（"hello p1 verify"）不会反复报警。"""
    session = _FakeCheckSession(_row(), not_indexed=0)
    _install(monkeypatch, session)

    await bg._check()

    guard_sql = next(sql for sql in session.sql if "graph_entity_mentions" in sql)
    assert "char_length" in guard_sql
    assert f">= {bg._EMPTY_DONE_MIN_CHARS}" in guard_sql
    assert bg._EMPTY_DONE_MIN_CHARS == 200


def test_banner_uses_indexed_caliber_and_skipped_adjusted_coverage():
    """横幅：分母只数已索引；覆盖率按 done/(total−skipped)；未索引单独一行。"""
    stats = {
        "documents_total": 21,
        "documents_done": 20,
        "documents_pending": 0,
        "documents_failed": 0,
        "documents_skipped": 1,
        "documents_not_indexed": 1,
        "entities": 432,
        "relations": 182,
    }
    text = capabilities._render_text(
        role="api",
        app_env="prod",
        fingerprint="a1b2c3d4e5f6",
        graph_enabled=True,
        wiki_enabled=False,
        stats=stats,
    )

    assert "20/21 篇已抽取 · 待补建 0 · 失败 0（100.0% 覆盖）" in text
    assert "另 1 篇未索引，不参与图抽取" in text
    assert "先修解析链路" in text

    snapshot = capabilities.capability_snapshot("fingerprint00", stats)
    graph = next(c for c in snapshot["capabilities"] if c["key"] == "graph")
    assert graph["data"]["documents_total"] == 21  # 未索引那篇不在分母里
    assert graph["data"]["documents_not_indexed"] == 1


def test_banner_omits_not_indexed_line_when_zero():
    """没有未索引文档时不多打一行（避免每天都看到一条无关提示）。"""
    stats = {
        "documents_total": 20,
        "documents_done": 20,
        "documents_pending": 0,
        "documents_failed": 0,
        "documents_skipped": 0,
        "documents_not_indexed": 0,
        "entities": 400,
        "relations": 150,
    }
    text = capabilities._render_text(
        role="api",
        app_env="prod",
        fingerprint="a1b2c3d4e5f6",
        graph_enabled=True,
        wiki_enabled=False,
        stats=stats,
    )

    assert "20/20 篇已抽取" in text
    assert "未索引" not in text
