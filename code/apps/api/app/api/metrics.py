"""运行指标端点（admin）—— 暴露进程内计数与耗时分布快照。

用途：让「降级、拒答、错误」这类异常路径可被观察。
背景：此前重排降级只有日志、无任何可查询的计数，
导致重排服务长期不可用而没人发现。

多副本部署说明：各副本独立计数，本端点返回的是**当前副本**的快照。
如需全局视角，应在网关层聚合各副本结果。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import CurrentUser, require_admin
from app.infra import metrics

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/metrics")
async def read_metrics(
    user: CurrentUser = Depends(require_admin),
) -> dict[str, object]:
    """返回当前进程的指标快照。

    关键观察项：
      counters["rag.rerank.degraded|reason=timeout"]  > 0 → 重排服务不稳定
      counters["rag.gate.refused|source=vector"]      占比较高 → 检查阈值口径
      histograms["rag.rerank.duration|mode=degraded"]        → 降级时的耗时基线
    """
    return metrics.snapshot()
