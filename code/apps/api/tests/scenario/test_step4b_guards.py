"""Step 4B-1 的**无库守卫**（不连库，任何机器必须跑）。

★ 两类内容，各自的判据都是「不依赖被测函数自己算出来的值」：
  1. **静态守卫**（设计 §D.3 的 T-4B-8…T-4B-11）：源码文本 / AST / 常量哈希。
  2. **启动横幅的降级契约**：用 session 替身触发「统计超时」「统计失败」两条分支，
     断言横幅只打提示不打数字、且**绝不抛**。它属于启动路径，
     所以在没有 PG 的机器上同样必须成立 —— 这就是它放在本文件而不是
     `test_step4b_reindex_pg.py`（无库即整体 skip）的原因。

★ 每条的「破坏方式」写在各用例 docstring 里 —— 没有那一句的用例在本项目里算
  不合格（设计 §D.2 的要求）。
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import pathlib

import pytest

_API_ROOT = pathlib.Path(__file__).resolve().parents[2]
_MIGRATION_0029 = _API_ROOT / "alembic" / "versions" / "0029_add_chunk_reindex_views.py"
_NEEDS_REINDEX = _API_ROOT / "app" / "services" / "scenario" / "needs_reindex.py"
_REINDEX_SCRIPT = _API_ROOT / "scripts" / "reindex_embeddings.py"

# 口径**字面量**黑名单：这五个值是"当前部署长什么样"的答案，一旦写进 SQL 或
# 判定模块，改 `.env` 之后视图与检索就会各说各话，而且不会有任何报错（I-4B-1）。
_FORBIDDEN_CALIBER_LITERALS = (
    "bge-m3",
    "etext-v",
    "c400-800-60",
    "wprompt-v",
    "gprompt-v",
)


def _string_constants(path: pathlib.Path) -> list[tuple[int, str]]:
    """模块内所有字符串字面量（含函数内联 SQL），带行号便于报错定位。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


# ══════════════════════════════════════════════════════════════════════
# T-4B-8：口径不得以字面量出现（I-4B-1）
# ══════════════════════════════════════════════════════════════════════


def test_t_4b_8_no_caliber_literals_in_migration_or_judge():
    """T-4B-8：`0029` 与 `needs_reindex.py` 的字符串常量里**不得**出现口径值。

    ★ 破坏方式（必须变红）：在 `0029` 里把函数写成
      `WHERE c.embedding_model = '<某个模型名>'`（"反正默认值就是它"）→ 红。
      这是「第二真源」最自然的入侵方式：它看起来只是"把默认值内联一下"，
      而后果是改 `.env` 之后视图仍按旧模型算，且没有任何信号。
    ★ 为什么连 `needs_reindex.py` 一起扫：判定模块比迁移更容易被写进字面量
      （"反正我这里是 Python，import 一下更麻烦"）。两处都必须只用
      `current_index_profile()`。
    """
    offenders: list[tuple[str, int, str]] = []
    for path in (_MIGRATION_0029, _NEEDS_REINDEX):
        for lineno, text in _string_constants(path):
            for literal in _FORBIDDEN_CALIBER_LITERALS:
                if literal in text:
                    offenders.append((path.name, lineno, literal))
    assert not offenders, (
        "口径值不得以字面量出现在判定实现里（I-4B-1：唯一真源是 "
        f"current_index_profile()）。命中：{offenders}"
    )


# ══════════════════════════════════════════════════════════════════════
# T-4B-9：三段检索 SQL 的 golden 哈希（I-4B-4，4B-1 的核心承诺）
# ══════════════════════════════════════════════════════════════════════

# ★ 这三个值是**实现 4B-1 之前**实测得到的（`sha256(<常量文本>)`）。
#   它们存在的唯一目的：把「4B-1 没改读路径」从一句承诺变成一个可执行断言。
#   *「没改」无法用行为断言证明* —— 行为断言只能覆盖它跑过的那组输入，而召回范围
#   取决于 SQL 的每一个字（谓词顺序、LIMIT 下推、索引选择都会影响结果）。
#
# ★ 正当变更流程（不写清这条，下一个改动者只会照抄数字）：
#     改 SQL → 跑本用例拿到新哈希 → 在本文件更新三个字面量 →
#     commit message 里引用 DQ4B-1 与设计文档 §C.2 → 附上召回范围变化的 A/B 证据。
#   golden 变红不是阻碍，是"提醒你这里有一道门"。
_GOLDEN_SQL_SHA256 = {
    "_VECTOR_SQL_TEMPLATE": "7d41c206dbdf04933ed72d424744a425dc5b0e526f796d2352f0dadf18345485",
    "_KEYWORD_SQL": "9d7196996de2330c89225fd8e7d18cf1f4aabc7fc77908d62c4d90160cd87a7b",
    "_KEYWORD_FTS_SQL": "3b91278bfb25adca63bc4bdae52e346254af8a7afc739713ea2c2fe9eaffe08a",
}


def test_t_4b_9_retrieval_sql_text_is_byte_identical():
    """T-4B-9：向量 / 关键词 ILIKE / 关键词 tsquery 三段 SQL 的 sha256 逐字相等。

    ★ 破坏方式（必须变红）：给关键词路加
      `AND c.embedding_model = :embedding_model`（哪怕加**一行注释**也会变）→ 红。
    ★ 为什么是 golden 而不是行为断言：见上方注释与设计 §D.3 的推导。
    """
    from app.services.retrieve import _KEYWORD_FTS_SQL, _KEYWORD_SQL, _VECTOR_SQL_TEMPLATE

    actual = {
        "_VECTOR_SQL_TEMPLATE": _VECTOR_SQL_TEMPLATE,
        "_KEYWORD_SQL": _KEYWORD_SQL.text,
        "_KEYWORD_FTS_SQL": _KEYWORD_FTS_SQL.text,
    }
    drifted = {
        name: hashlib.sha256(text.encode("utf-8")).hexdigest()
        for name, text in actual.items()
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != _GOLDEN_SQL_SHA256[name]
    }
    assert not drifted, (
        "检索期 SQL 文本变了 —— 4B-1 的承诺是「读路径召回范围零变化」。"
        f"实测与 golden 不符：{drifted}；期望：{_GOLDEN_SQL_SHA256}。"
        "若这是有意的读路径改动，按文件头的「正当变更流程」显式更新这三个字面量。"
    )


# ══════════════════════════════════════════════════════════════════════
# T-4B-10：判定谓词只有一份（I-4B-2）
# ══════════════════════════════════════════════════════════════════════


def test_t_4b_10_reindex_script_does_not_own_the_predicate():
    """T-4B-10：`reindex_embeddings.py` 不得自己重写口径比较，且必须调那个函数。

    ★ 破坏方式（必须变红）：把 `_load_stale()` 改回
      `or_(Chunk.embedding_model != ..., Chunk.embedding_text_version != ...)` → 红。
    ★ 为什么这一条能防住真正的风险：两处 predicate 的分叉**没有任何单一行为用例
      能同时覆盖两边**（视图用例绿、脚本用例也绿，两边各自内部自洽，只有
      "两边不一致"这件事没人测）。所以必须在**结构上**让它不可能。
    """
    source = _REINDEX_SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source)

    forbidden: list[str] = []
    for node in ast.walk(tree):
        # `Chunk.embedding_model != ...` / `Chunk.embedding_text_version != ...`
        if not isinstance(node, ast.Attribute):
            continue
        if not isinstance(node.value, ast.Name) or node.value.id != "Chunk":
            continue
        if node.attr in {"embedding_model", "embedding_text_version", "chunker_version"}:
            forbidden.append(node.attr)
    assert not forbidden, (
        "脚本自己重写了口径比较（I-4B-2：判定谓词唯一实现是 "
        f"fn_chunk_reindex_candidates）。命中：{sorted(set(forbidden))}"
    )
    assert "fn_chunk_reindex_candidates" in source, (
        "脚本没有调用统一判定函数 —— 它的「过期」口径会与视图/端点分叉"
    )
    # `--check` 的 CI 门禁语义（有过期即非 0）是既有契约，一行都不能改。
    assert "return 1 if total else 0" in source, (
        "`--check` 的退出码语义被改了 —— 这是 CI 门禁的契约（4B 只换谓词来源，不换语义）"
    )


# ══════════════════════════════════════════════════════════════════════
# T-4B-11：新增错误码必须有 HTTP 映射
# ══════════════════════════════════════════════════════════════════════


def test_t_4b_11_new_write_code_has_status_and_message():
    """T-4B-11：`SCENARIO_KB_EMPTY_BUT_INDEXED` 必须有 `_HTTP` 与 `_MSG` 映射。

    ★ 破坏方式（必须变红）：把它加进 `WriteCode` 枚举但忘了 `_HTTP` → 红。
      缺映射时 `_HTTP[code]` 抛 `KeyError` → 落到全局兜底 → **500**，而这个码
      本该是 409（"世界状态冲突，别重试，去查数据管道"）。500 会让客户端重试，
      重试永远不成功。
    ★ 与 4A 的 `test_every_write_code_has_status_and_message` 的关系：那条断言
      **全体**成员都有映射，本条只钉**新码的状态码具体值**（409 不是 400）。
      两条都在，前者防漏、后者防"映射到错误的状态"。
    """
    from app.services.scenario import write_errors

    assert hasattr(write_errors.WriteCode, "KB_EMPTY_BUT_INDEXED")
    code = write_errors.WriteCode.KB_EMPTY_BUT_INDEXED
    assert code.value == "SCENARIO_KB_EMPTY_BUT_INDEXED", code.value
    assert write_errors.http_status(code) == 409, (
        "必须是 409：请求体完全合法，是世界的状态与这个请求冲突。"
        "400 会让客户端以为改请求能解决，而它必须去查解析/迁移链路"
    )
    assert write_errors.message(code), "缺默认文案"


# ══════════════════════════════════════════════════════════════════════
# 启动横幅的降级契约（不连库：用 session 替身触发超时 / 失败）
# ══════════════════════════════════════════════════════════════════════
#
# ★ 为什么这两条必须存在：设计 §B.5.2 把统计放进了**启动路径**，而 U-4B-7 用
#   「过期扫描是离线动作」论证不必给口径三列加索引 —— 两句原本对不上。
#   本实现的处置是「异步 + 超时护栏 + 异常降级」，那是这套设计能站住的唯一前提。
#   一个没有用例守着的"绝不阻塞启动"承诺，在第一次改动后就会悄悄变成
#   "库慢的时候 api 起不来"，而那种故障只在生产的大库上出现。


class _HangingSession:
    """`async with` 进去就挂住的 session 替身（模拟大表上的慢统计）。"""

    async def __aenter__(self):
        await asyncio.sleep(30)
        raise AssertionError("超时路径不该走到这里")

    async def __aexit__(self, *exc_info):
        return False


class _BrokenSession:
    """`async with` 直接抛错的 session 替身（模拟库不可达 / 迁移没跑到 0029）。"""

    async def __aenter__(self):
        raise RuntimeError("库不可达（替身）")

    async def __aexit__(self, *exc_info):
        return False


@pytest.mark.asyncio
async def test_banner_degrades_on_timeout(capsys, monkeypatch):
    """横幅超时：只打「统计超时」不打数字，打点 + 绝不抛。

    破坏方式（必须变红）：删掉 `log_needs_reindex_banner` 的
    `except TimeoutError` 分支 → `asyncio.wait_for` 的 TimeoutError 冒泡出
    启动路径 → 本用例红（而线上表现是「库一大，api 起不来」）。

    ★ 断言「不打数字」是另一半：只打提示意味着运维看到的是"这次没测出来"，
      而不是一个**假的 0**（假 0 会被读成"全库都对上了"）。
    """
    import app.database as database
    from app.infra import metrics
    from app.services.scenario import needs_reindex

    monkeypatch.setattr(database, "SessionLocal", _HangingSession)
    before = metrics.get("rag.caliber.banner_timeout")

    await needs_reindex.log_needs_reindex_banner(timeout=0.05)

    out = capsys.readouterr().out
    assert needs_reindex.BANNER_TIMEOUT_HINT in out, f"横幅没有打超时提示：{out!r}"
    assert "条对齐" not in out, f"超时时不该打出数字（会读成假的 0）：{out!r}"
    assert metrics.get("rag.caliber.banner_timeout") == before + 1
    # 站位值那一行三个分支都必须打（运维每次部署都要看到"它们是站位值"）
    assert "CALIBER_BANNER_TIMEOUT_SECONDS" in out, out
    assert "CALIBER_STALE_PROGRESS_HOURS" in out, out


@pytest.mark.asyncio
async def test_banner_degrades_on_error(capsys, monkeypatch):
    """横幅统计失败（库抖动 / 只跑到 0028）：打「统计失败」，启动继续。

    破坏方式（必须变红）：把 `except Exception` 分支删掉（只留 TimeoutError）→
    `RuntimeError` 冒泡 → 本用例红。这正是"迁移没跑就发版"的真实现场：
    0029 的函数不存在会让统计抛 `UndefinedFunctionError`，而它**不该**让 api 起不来。
    """
    import app.database as database
    from app.infra import metrics
    from app.services.scenario import needs_reindex

    monkeypatch.setattr(database, "SessionLocal", _BrokenSession)
    before = metrics.get("rag.caliber.banner_failed")

    await needs_reindex.log_needs_reindex_banner(timeout=1.0)

    out = capsys.readouterr().out
    assert needs_reindex.BANNER_FAILED_HINT in out, f"横幅没有打失败提示：{out!r}"
    assert "条对齐" not in out, f"失败时不该打出数字：{out!r}"
    assert metrics.get("rag.caliber.banner_failed") == before + 1
