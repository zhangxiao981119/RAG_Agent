"""`GET /api/admin/needs-reindex`（R-4B-1）—— chunk 口径分布的**实时**读入口。

★ 与启动横幅的分工（为什么两个都要，而不是只留一个）：
  · 横幅 → **部署者**（改 `.env` 后必然看第一屏），但只代表**启动那一刻**，多副本
    各说各话（U-4B-5）；
  · 本端点 → **运维排障时**，给**此刻**的数，且能按租户切（横幅做不到）。
  没有一处依赖"有人主动盯着"，这是对 R10（"闸门永远红 → 学会无视"）的正面回答。

★ 为什么不提供**写** / 触发重算：重算要调 embedding 服务、要按批提交，属离线动作。
  把它做成 HTTP 端点会让"一个 GET 顺手把事情做了"变成可能，而它需要十几分钟。

★ 租户边界（必须写死，`T-4B-7` 守）：
  · `scope=tenant`（默认）→ 四桶只统计 `c.tenant_id = CurrentUser.tenant_id`；
  · `scope=deployment` → **只给五个总数**，不返回 `newest_*_at`、不返回任何
    `tenant_id` / `document_id` / `kb_id`。理由：`--check` 的退出码是**全局**的
    （脚本无租户条件），运维必须能对上那个数；但**明细**跨租户是越权。
    与 `self_check.py:210-223` 的口径一致（全仓唯一不带 `tenant_id` 的场景读取，
    靠"只在启动期执行 / 只用于计数 / 日志只允许特定字段"三条约束让它安全）。
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_db, require_admin
from app.config.index_profile import current_index_profile
from app.schemas.needs_reindex import NeedsReindexOut, ProfileOut, VerdictOut
from app.services.scenario import needs_reindex

router = APIRouter(prefix="/admin", tags=["admin"])

NOTE = (
    "chunker_only 是软 DNA：不拒绝检索、不进任何就绪度分母、不告警；"
    "它的处置是「下次全量重建时用新参数」，所以这个数字读作"
    "「待办规模」而不是「当前缺陷数」。"
    "in_dead_documents 属已软删/旧版文档的 chunk 仍可能被检出，"
    "是否排除它们是运维决策（U-4B-1），本端点只并列暴露。"
    "kb_readability 里的「可检」只指口径维度，不含授权维度。"
)


@router.get("/needs-reindex", response_model=NeedsReindexOut, response_model_exclude_none=True)
async def read_needs_reindex(
    scope: Literal["tenant", "deployment"] = Query(default="tenant"),
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> NeedsReindexOut:
    """返回当前进程观测到的 chunk 口径分布。

    ★ **不接受口径参数**：口径从 `current_index_profile()` 取。允许传口径等于允许
      "用别的口径问这个部署"，而这类问题（"如果换成 X 模型有多少过期"）的正确
      做法是改配置后重启，不是给端点开一个参数。
    """
    tenant_id = user.tenant_id if scope == "tenant" else None
    report = await needs_reindex.collect(session, tenant_id=tenant_id)
    profile = current_index_profile()
    decision = needs_reindex.verdict(report, now=datetime.now(UTC))
    # ★ 部署级 MUST NOT 回显时间戳（T-4B-7 守着这条）：
    #   `newest_ok_at` / `newest_stale_at` 是**该租户某条数据的时刻**，跨租户聚合后
    #   它在语义上已经不属于任何人，但它仍然泄露"某个租户最近一次写入在哪一刻"。
    #   ★ 只把 `response_model_exclude_none=True` 当成防线是错的：那个开关只在
    #     值为 None 时省键 —— 只要实现塞进一个非 None 的时间戳，模型照样通过校验，
    #     于是"部署级不给明细"这条纪律就静默失效了。所以这里必须显式置 None。
    expose_times = scope == "tenant"
    return NeedsReindexOut(
        profile=ProfileOut(
            embedding_model=profile.embedding_model,
            embedding_text_version=profile.embedding_text_version,
            chunker_version=profile.chunker_version,
        ),
        scope=scope,
        universe=report.universe,
        ok=report.ok,
        hard_stale=report.hard_stale,
        chunker_only=report.chunker_only,
        embedding_missing=report.embedding_missing,
        in_dead_documents=report.in_dead_documents,
        generation_conflicts=report.generation_conflicts,
        newest_ok_at=report.newest_ok_at if expose_times else None,
        newest_stale_at=report.newest_stale_at if expose_times else None,
        reconciles=report.reconciles,
        verdict=VerdictOut(
            level=decision.level, reason=decision.reason, detail=decision.detail
        ),
        note=NOTE,
    )
