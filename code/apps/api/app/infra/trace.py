"""trace_id 上下文 —— 贯穿一次请求的全部环节。

解决的问题：一次问答跨 7 个环节（改写 → 向量召回 → 关键词召回 → RRF → 重排 →
生成 → grounding），任一环变慢或答错，没有统一标识就无法把日志串起来。

用法：
    # 入口（中间件 / worker 任务开始处）
    token = trace.set_trace_id(trace.new_trace_id())
    ...
    trace.reset_trace_id(token)

    # 任意位置读取
    trace.get_trace_id()

日志自动带上：`install_log_filter()` 会给每条日志记录注入 trace_id 字段，
主进程的 JsonFormatter 会把它序列化进 JSON —— 无需在每个 logger 调用处手动传。
"""
from __future__ import annotations

import contextvars
import logging
import uuid

_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "trace_id", default=None
)


def new_trace_id() -> str:
    """生成新的 trace_id（32 位 hex，可安全用于日志与响应头）。"""
    return uuid.uuid4().hex


def set_trace_id(value: str) -> contextvars.Token:
    """设置当前上下文的 trace_id，返回用于还原的 token。"""
    return _trace_id.set(value)


def get_trace_id() -> str | None:
    """读取当前上下文的 trace_id；未设置时返回 None。"""
    return _trace_id.get()


def reset_trace_id(token: contextvars.Token) -> None:
    """还原 trace_id（请求结束时调用，避免污染复用协程的上下文）。"""
    _trace_id.reset(token)


class TraceIdFilter(logging.Filter):
    """把当前上下文的 trace_id 注入每条日志记录。"""

    def filter(self, record: logging.LogRecord) -> bool:
        # 显式传入的 trace_id 优先（如 worker 任务自己设置）
        if not getattr(record, "trace_id", None):
            record.trace_id = get_trace_id()
        return True


def install_log_filter() -> None:
    """给所有已注册的 handler 挂上过滤器。应用启动时调用一次即可。"""
    log_filter = TraceIdFilter()
    for handler in logging.root.handlers:
        if not any(isinstance(f, TraceIdFilter) for f in handler.filters):
            handler.addFilter(log_filter)
