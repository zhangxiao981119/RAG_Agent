"""轻量指标收集 —— 进程内计数与耗时分布。

设计取舍：
- **不引入 prometheus_client 依赖**，保持私有化离线部署的零额外依赖。
- 进程内聚合，通过管理端接口暴露快照；多副本部署时各副本独立计数
  （不做全局聚合 —— 对「发现降级、发现错误」这类用途已经够用）。
- 只为**需要被发现的异常路径**打点，不做全链路埋点，避免噪音。

使用约定：
    from app.infra import metrics

    metrics.incr("rag.rerank.degraded", reason="timeout")
    metrics.observe_ms("rag.retrieve.duration", 123)

标签值 MUST 是低基数的枚举值（如 "timeout" / "error"），
MUST NOT 传用户 ID、查询文本等高频值，否则内存会被标签组合打爆。
"""
from __future__ import annotations

import threading
from collections import defaultdict

_lock = threading.Lock()

# 计数器：key = "name|k=v,k=v"
_counters: dict[str, int] = defaultdict(int)

# 直方图：key = "name|k=v,k=v" → [count, sum, min, max]
_histograms: dict[str, list[float]] = {}


def _key(name: str, tags: dict[str, str] | None) -> str:
    if not tags:
        return name
    parts = ",".join(f"{k}={v}" for k, v in sorted(tags.items()))
    return f"{name}|{parts}"


def incr(name: str, value: int = 1, **tags: str) -> None:
    """计数器 +1（或 +value）。tags 必须是低基数枚举值。"""
    k = _key(name, tags)
    with _lock:
        _counters[k] += value


def observe_ms(name: str, milliseconds: float, **tags: str) -> None:
    """记录一次耗时（毫秒），维护 count / sum / min / max。"""
    k = _key(name, tags)
    with _lock:
        slot = _histograms.get(k)
        if slot is None:
            _histograms[k] = [1.0, float(milliseconds), float(milliseconds), float(milliseconds)]
        else:
            slot[0] += 1
            slot[1] += milliseconds
            if milliseconds < slot[2]:
                slot[2] = milliseconds
            if milliseconds > slot[3]:
                slot[3] = milliseconds


def snapshot() -> dict[str, object]:
    """导出当前快照。供管理端接口 / 测试断言使用。"""
    with _lock:
        return {
            "counters": dict(_counters),
            "histograms": {
                k: {
                    "count": int(v[0]),
                    "sum_ms": round(v[1], 3),
                    "avg_ms": round(v[1] / v[0], 3) if v[0] else 0.0,
                    "min_ms": round(v[2], 3),
                    "max_ms": round(v[3], 3),
                }
                for k, v in _histograms.items()
            },
        }


def get(name: str, **tags: str) -> int:
    """读取单个计数器当前值（测试用）。"""
    with _lock:
        return _counters.get(_key(name, tags), 0)


def reset() -> None:
    """清空所有指标（**仅供测试**使用）。"""
    with _lock:
        _counters.clear()
        _histograms.clear()
