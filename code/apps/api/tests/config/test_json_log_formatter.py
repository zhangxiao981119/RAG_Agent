"""修 5 回归：`JsonFormatter` 的时间戳格式化 MUST 在任何 3.11+ 上都不抛异常。

缺陷现场（`app/main.py`，第三方流水线扫出的真实报错）：
    {"ts": ...} 由 `formatter.formatTime(record, "%Y-%m-%dT%H:%M:%S.%03d")` 生成，
    在 Python 3.12+ 上直接 `ValueError: Invalid format string` —— `%03d` 是
    C `strftime` 的**宽度修饰**，Python 的 `time.strftime` 不支持。

为什么这条不能只当成"一行小修"：
  · 它是**格式化器自己**抛异常 ⇒ 不是某条日志丢了，而是**每条日志都炸**；
  · 炸掉的不只是普通日志：场景解析的 `diagnostic`、trace_id、审计字段、
    `builtin_row_missing` 这类计数之外的排障线索全部走同一个格式化器；
  · 失败是静默的 —— logging 内部会 `handleError` 把异常打到 stderr，
    进程照跑，于是「日志里什么都没有」看起来像「什么都没发生」。
    排障通道被自己的格式化器掐断，是最难发现的一类故障。

本文件钉三件事：
  1. `format()` 不抛异常，且 `ts` 形状严格是 `YYYY-MM-DDTHH:MM:SS.mmm`；
  2. 毫秒真的来自 `record.msecs`，时区语义真的来自 `formatter.converter`
     （后者可被改成 UTC —— 换掉它就等于悄悄改了日志时区）；
  3. `taskName`（Python 3.12 新增的 LogRecord 字段）不出现在业务字段里。
"""

from __future__ import annotations

import json
import logging
import re
import time

from app.main import JsonFormatter

_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}$")


def _record(*, created: float | None = None, msecs: float | None = None) -> logging.LogRecord:
    """构造一条 LogRecord（不经 logger，避免受 root handler 配置影响）。"""
    record = logging.LogRecord(
        name="app.services.scenario.resolver",
        level=logging.WARNING,
        pathname=__file__,
        lineno=42,
        msg="场景解析失败 code=%s",
        args=("SCENARIO_UNRESOLVABLE",),
        exc_info=None,
    )
    if created is not None:
        record.created = created
    if msecs is not None:
        record.msecs = msecs
    return record


def test_format_does_not_raise():
    """修复前这一行就抛 `ValueError: Invalid format string`（3.12+）。"""
    out = JsonFormatter().format(_record())
    payload = json.loads(out)
    assert payload["level"] == "WARNING"
    assert payload["logger"] == "app.services.scenario.resolver"
    assert payload["msg"] == "场景解析失败 code=SCENARIO_UNRESOLVABLE"


def test_ts_shape_is_iso_with_millis():
    """`ts` MUST 形如 `2026-06-11T04:04:46.007`（毫秒定宽 3 位，可被日志系统排序/解析）。"""
    payload = json.loads(JsonFormatter().format(_record()))
    assert _TS_RE.match(payload["ts"]), payload["ts"]


def test_ts_takes_millis_from_record_msecs():
    """毫秒取自 `record.msecs`，且 UTC converter 被尊重（时区语义不丢）。

    固定 `created` + `converter=time.gmtime` 让断言与时区无关：
    1700000000.5 = 2023-11-14T22:13:20.500Z。若实现改成
    `time.localtime` 或自己拼 `created % 1`，这里会红。
    """
    record = _record(created=1_700_000_000.5, msecs=500.0)
    formatter = JsonFormatter()
    formatter.converter = time.gmtime  # type: ignore[method-assign]

    payload = json.loads(formatter.format(record))
    assert payload["ts"] == "2023-11-14T22:13:20.500"


def test_ts_millis_are_zero_padded():
    """毫秒 7 必须渲染成 `.007`，不是 `.7` / `.7000`。"""
    record = _record(created=1_700_000_000.0, msecs=7.0)
    formatter = JsonFormatter()
    formatter.converter = time.gmtime  # type: ignore[method-assign]
    assert json.loads(formatter.format(record))["ts"].endswith(".007")


def test_business_extra_is_kept():
    """业务字段（场景 diagnostic / trace_id 等）照旧透传 —— 修时间戳不许弄丢这个。"""
    record = _record()
    record.tenant_id = "11111111-1111-1111-1111-111111111111"
    record.trace_id = "abc123"
    payload = json.loads(JsonFormatter().format(record))
    assert payload["tenant_id"] == "11111111-1111-1111-1111-111111111111"
    assert payload["trace_id"] == "abc123"


def test_taskname_is_not_emitted():
    """`taskName` 是 Python 3.12 给 LogRecord 新增的字段，必须进 `_RESERVED`。

    此前"不可见"是因为格式化器本身在抛异常 —— 修好时间戳之后它就会开始出现在
    **每一条**日志里（值多为 null），把真正的业务字段挤出视野。
    """
    record = _record()
    record.taskName = "asyncio-task-1"
    payload = json.loads(JsonFormatter().format(record))
    assert "taskName" not in payload


def test_exc_info_still_rendered():
    """异常栈仍走 `traceback` 字段（时间戳修复不该影响异常日志）。"""
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        import sys

        record = _record()
        record.exc_info = sys.exc_info()
    payload = json.loads(JsonFormatter().format(record))
    assert "RuntimeError: boom" in payload["traceback"]


def test_formatter_works_through_handler():
    """端到端：经 Handler 落盘那一层也不炸（`formatTime` 只在 handler 内被调用）。"""
    import io

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("tests.scenario.formatter.e2e")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    logger.warning("场景解析失败", extra={"reason": "non_builtin_unrestricted"})

    payload = json.loads(stream.getvalue().strip())
    assert payload["reason"] == "non_builtin_unrestricted"
    assert _TS_RE.match(payload["ts"])
