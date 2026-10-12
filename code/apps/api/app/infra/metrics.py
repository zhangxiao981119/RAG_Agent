"""指标收集 —— 基于 prometheus_client 的进程内指标，暴露标准 Prometheus 文本格式。

设计取舍（P0-4 起）：
- 内部实现由「自研内存计数」改为 prometheus_client（Counter / Histogram），
  由 GET /metrics 暴露标准文本，供 Prometheus 抓取、Grafana 出图与告警。
  原自研实现只有 count/sum/min/max、没有分桶，算不出分位数，也无现成看板，故废弃。
- 对外签名保持 incr() / observe_ms() 不变，46 处调用点零改动。
- 进程内计数，**要求 api 固定单 worker**（见 docker-compose.prod.yml）：
  多 worker 时各进程独立计数，Prometheus 从同一端口抓到的是随机一个副本，
  rate() 会随副本切换跳变 —— 因此不启用多 worker，也不引入 multiprocess 目录。
- 指标名中的 `.` / `-` 会被规范化为 `_`（Prometheus 名称只允许 [a-zA-Z0-9_:]）。
- 耗时统一以**秒**为基准单位（Prometheus 约定）：observe_ms 传入毫秒、内部换算，
  指标名自动补 `_seconds` 后缀，可直接套用社区 Grafana 看板。

使用约定：
    from app.infra import metrics

    metrics.incr("rag.rerank.degraded", reason="timeout")
    metrics.observe_ms("rag.retrieve.duration", 123)

标签值 MUST 是低基数的枚举值（如 "timeout" / "error" / 路由模板 path），
MUST NOT 传用户 ID、查询文本、原始 URL 等高频值，否则时间序列会被标签组合打爆。
"""
from __future__ import annotations

import re

from prometheus_client import (
    REGISTRY,
    Counter,
    Histogram,
    generate_latest,
)
from prometheus_client import CONTENT_TYPE_LATEST  # noqa: F401 —— 供 /metrics 端点复用

# 耗时默认分桶：覆盖 5ms ~ 10s，兼顾检索链路（毫秒级）与 LLM 生成（秒级）。
# 分位数由桶边界插值得到，故桶需覆盖真实分布区间 —— 超出 10s 的落入 +Inf 桶。
_DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

_counters: dict[str, Counter] = {}
_histograms: dict[str, Histogram] = {}
_name_re = re.compile(r"[^a-zA-Z0-9_]")


def _normalize(name: str) -> str:
    """把业务名规范化为合法的 Prometheus 指标名（非法字符 → 下划线）。"""
    return _name_re.sub("_", name)


def _counter(name: str, tags: dict[str, str]) -> Counter:
    """按需创建并缓存计数器；同一业务名的标签集必须稳定。"""
    key = _normalize(name)
    collector = _counters.get(key)
    if collector is None:
        collector = Counter(key, f"{name} 计数", sorted(tags))
        _counters[key] = collector
    return collector


def _histogram(name: str, tags: dict[str, str]) -> Histogram:
    """按需创建并缓存直方图；名称自动补 `_seconds` 后缀（基准单位为秒）。"""
    key = _normalize(name)
    if not key.endswith("_seconds"):
        key = f"{key}_seconds"
    collector = _histograms.get(key)
    if collector is None:
        collector = Histogram(
            key, f"{name} 耗时（秒）", sorted(tags), buckets=_DURATION_BUCKETS
        )
        _histograms[key] = collector
    return collector


def incr(name: str, value: int = 1, **tags: str) -> None:
    """计数器 +1（或 +value）。tags 必须是低基数枚举值。"""
    collector = _counter(name, tags)
    if tags:
        collector.labels(**tags).inc(value)
    else:
        collector.inc(value)


def observe_ms(name: str, milliseconds: float, **tags: str) -> None:
    """记录一次耗时（毫秒入参，内部按秒观测）。"""
    collector = _histogram(name, tags)
    seconds = milliseconds / 1000.0
    if tags:
        collector.labels(**tags).observe(seconds)
    else:
        collector.observe(seconds)


def _read_counter(collector: Counter, tags: dict[str, str]) -> float:
    """读取计数器指定标签组合的累计值；不存在该组合时返回 0。"""
    for metric in collector.collect():
        for sample in metric.samples:
            # Counter 会额外产出 `_created` 样本，只取 `_total`
            if sample.name.endswith("_total") and sample.labels == tags:
                return sample.value
    return 0.0


def get(name: str, **tags: str) -> int:
    """读取单个计数器当前值（测试用）。无标签名读取带标签的序列时返回 0。"""
    collector = _counters.get(_normalize(name))
    if collector is None:
        return 0
    return int(_read_counter(collector, tags))


def _series_key(sample_name: str, labels: dict[str, str]) -> str:
    """把样本还原成 `name|k=v,...` 形式的键，保持 admin 端点的既有结构。"""
    label_part = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
    return f"{sample_name}|{label_part}" if label_part else sample_name


def snapshot() -> dict[str, object]:
    """导出当前快照（JSON 友好）。供管理端接口 / 测试断言使用。

    直方图给出 count / sum_ms / avg_ms —— prometheus_client 不保留 min/max，
    极值与分位数改由 /metrics 的桶序列在查询侧计算。
    """
    counters: dict[str, int] = {}
    for collector in _counters.values():
        for metric in collector.collect():
            for sample in metric.samples:
                if not sample.name.endswith("_total"):
                    continue
                name = sample.name[: -len("_total")]
                counters[_series_key(name, sample.labels)] = int(sample.value)

    histograms: dict[str, dict[str, float]] = {}
    for collector in _histograms.values():
        for metric in collector.collect():
            for sample in metric.samples:
                if sample.name.endswith("_count") or sample.name.endswith("_sum"):
                    base = sample.name.rsplit("_", 1)[0]
                else:
                    continue
                slot = histograms.setdefault(
                    _series_key(base, sample.labels), {"count": 0, "sum_ms": 0.0}
                )
                if sample.name.endswith("_count"):
                    slot["count"] = int(sample.value)
                else:
                    # 内部以秒累计，还原为毫秒供 admin 端点展示
                    slot["sum_ms"] = round(sample.value * 1000.0, 3)
    for slot in histograms.values():
        count = slot["count"]
        slot["avg_ms"] = round(slot["sum_ms"] / count, 3) if count else 0.0

    return {"counters": counters, "histograms": histograms}


def render() -> bytes:
    """输出 Prometheus 文本格式的当前指标（含进程/GC 等默认采集器）。"""
    return generate_latest(REGISTRY)


def reset() -> None:
    """清空所有自定义指标（**仅供测试**使用）。默认采集器不受影响。"""
    for collector in (*_counters.values(), *_histograms.values()):
        REGISTRY.unregister(collector)
    _counters.clear()
    _histograms.clear()