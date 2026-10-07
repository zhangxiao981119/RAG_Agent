"""审计日志查询 API（admin）—— 手册 §5.1 GET /api/admin/audit-logs。

action 支持前缀过滤（传 doc.* 可筛全部文档类动作）；
user_id 支持按操作人过滤。服务端分页。
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_db, require_admin
from app.services import audit

router = APIRouter(prefix="/admin", tags=["admin"])


class AuditLogItem(BaseModel):
    id: int
    user_id: str | None
    user_label: str
    action: str
    object_type: str | None
    object_id: str | None
    detail: dict
    ip: str | None
    created_at: str


class AuditLogPage(BaseModel):
    items: list[AuditLogItem]
    total: int
    page: int
    page_size: int


@router.get("/audit-logs", response_model=AuditLogPage)
async def list_audit_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    action: str | None = Query(None, max_length=50, description="动作前缀过滤，如 doc.* 传 doc"),
    user_id: uuid.UUID | None = Query(None, description="按操作人过滤"),
    start_date: date | None = Query(None, description="起始日期 YYYY-MM-DD"),
    end_date: date | None = Query(None, description="结束日期 YYYY-MM-DD（含当天）"),
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> AuditLogPage:
    items, total = await audit.list_logs(
        session, user.tenant_id, page, page_size,
        action=action, user_id=user_id, start_date=start_date, end_date=end_date,
    )
    return AuditLogPage(
        items=[AuditLogItem(**item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
    )


# ─────────────────────────────────────────────────────────────
# M6 可观测：P95 延迟聚合统计
# ─────────────────────────────────────────────────────────────

class RecallContribution(BaseModel):
    """三路召回贡献均值。

    口径：以**最终 top-K**为准，不是各路原始候选数。
    `covered` = top-K 中该路召回到的条数（多路可重叠）；
    `unique`  = 仅该路能召回到的条数 —— 直接量化"去掉这路会漏多少"，
    是"三路是否冗余"的唯一证据。
    只统计成功作答的请求（拒答请求没有最终 top-K）。
    """
    # 平均最终 top-K 条数（covered/unique 的分母参考）
    topk_avg: float
    vector_covered: float
    vector_unique: float
    tsquery_covered: float
    tsquery_unique: float
    ilike_covered: float
    ilike_unique: float


class MetricSummary(BaseModel):
    """聚合指标响应体。"""
    # 时间窗口描述
    window: str
    # 总请求数
    total_requests: int
    # 拒答数及拒答率
    refused_count: int
    refused_rate: float
    # 端到端延迟（毫秒）分位数
    total_p50: float
    total_p95: float
    total_p99: float
    total_avg: float
    # 检索延迟（毫秒）分位数
    retrieve_p50: float
    retrieve_p95: float
    retrieve_p99: float
    retrieve_avg: float
    # 首字延迟（毫秒）分位数：从请求起点到第一个非空 delta，仅成功作答路径有值
    ttft_p50: float
    ttft_p95: float
    ttft_p99: float
    ttft_avg: float
    # 三路召回贡献
    recall: RecallContribution


@router.get("/metrics", response_model=MetricSummary)
async def get_metrics(
    hours: int = Query(24, ge=1, le=720, description="查询最近 N 小时的数据"),
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> MetricSummary:
    """聚合 chat.ask.metric 指标，返回 P50/P95/P99 延迟和拒答率。"""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    # 用 PostgreSQL percentile_cont 原生聚合（detail 是 JSONB）
    sql = text("""
        SELECT
          count(*)                                           AS total_requests,
          count(*) FILTER (WHERE (detail->>'refused')::bool) AS refused_count,
          COALESCE(percentile_cont(0.50) WITHIN GROUP (ORDER BY (detail->>'total_ms')::int), 0)    AS total_p50,
          COALESCE(percentile_cont(0.95) WITHIN GROUP (ORDER BY (detail->>'total_ms')::int), 0)    AS total_p95,
          COALESCE(percentile_cont(0.99) WITHIN GROUP (ORDER BY (detail->>'total_ms')::int), 0)    AS total_p99,
          COALESCE(avg((detail->>'total_ms')::int), 0)                                                AS total_avg,
          COALESCE(percentile_cont(0.50) WITHIN GROUP (ORDER BY (detail->>'retrieve_ms')::int), 0) AS retrieve_p50,
          COALESCE(percentile_cont(0.95) WITHIN GROUP (ORDER BY (detail->>'retrieve_ms')::int), 0) AS retrieve_p95,
          COALESCE(percentile_cont(0.99) WITHIN GROUP (ORDER BY (detail->>'retrieve_ms')::int), 0) AS retrieve_p99,
          COALESCE(avg((detail->>'retrieve_ms')::int), 0)                                            AS retrieve_avg,
          COALESCE(percentile_cont(0.50) WITHIN GROUP (ORDER BY (detail->>'ttft_ms')::int), 0) AS ttft_p50,
          COALESCE(percentile_cont(0.95) WITHIN GROUP (ORDER BY (detail->>'ttft_ms')::int), 0) AS ttft_p95,
          COALESCE(percentile_cont(0.99) WITHIN GROUP (ORDER BY (detail->>'ttft_ms')::int), 0) AS ttft_p99,
          COALESCE(avg((detail->>'ttft_ms')::int), 0)                                          AS ttft_avg,
          COALESCE(avg((detail->>'chunks_count')::int)
                   FILTER (WHERE NOT (detail->>'refused')::bool), 0)                          AS topk_avg,
          COALESCE(avg((detail->>'vector_covered')::int), 0)                                    AS vector_covered,
          COALESCE(avg((detail->>'vector_unique')::int), 0)                                      AS vector_unique,
          COALESCE(avg((detail->>'tsquery_covered')::int), 0)                                    AS tsquery_covered,
          COALESCE(avg((detail->>'tsquery_unique')::int), 0)                                      AS tsquery_unique,
          COALESCE(avg((detail->>'ilike_covered')::int), 0)                                      AS ilike_covered,
          COALESCE(avg((detail->>'ilike_unique')::int), 0)                                        AS ilike_unique
        FROM audit_logs
        WHERE tenant_id = :tenant_id
          AND action = 'chat.ask.metric'
          AND created_at >= :since
    """)
    row = (await session.execute(sql, {"tenant_id": str(user.tenant_id), "since": since})).one()
    total = row.total_requests or 0
    refused = row.refused_count or 0
    return MetricSummary(
        window=f"最近 {hours} 小时",
        total_requests=total,
        refused_count=refused,
        refused_rate=round(refused / total * 100, 1) if total else 0.0,
        total_p50=round(float(row.total_p50), 1),
        total_p95=round(float(row.total_p95), 1),
        total_p99=round(float(row.total_p99), 1),
        total_avg=round(float(row.total_avg), 1),
        retrieve_p50=round(float(row.retrieve_p50), 1),
        retrieve_p95=round(float(row.retrieve_p95), 1),
        retrieve_p99=round(float(row.retrieve_p99), 1),
        retrieve_avg=round(float(row.retrieve_avg), 1),
        ttft_p50=round(float(row.ttft_p50), 1),
        ttft_p95=round(float(row.ttft_p95), 1),
        ttft_p99=round(float(row.ttft_p99), 1),
        ttft_avg=round(float(row.ttft_avg), 1),
        recall=RecallContribution(
            topk_avg=round(float(row.topk_avg), 1),
            vector_covered=round(float(row.vector_covered), 1),
            vector_unique=round(float(row.vector_unique), 1),
            tsquery_covered=round(float(row.tsquery_covered), 1),
            tsquery_unique=round(float(row.tsquery_unique), 1),
            ilike_covered=round(float(row.ilike_covered), 1),
            ilike_unique=round(float(row.ilike_unique), 1),
        ),
    )
