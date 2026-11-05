"""管理端「系统能力」接口（设计文档 §9.6 ①）—— GET /api/system/capabilities。

★ 数据源是 app/config/capabilities.py 的 `capability_snapshot()`：
  与启动横幅（log_capability_banner）共用同一份实现，避免「横幅说 A、页面说 B」
  —— 两处各拼一份 JSON，能力一开一关就会漂移，且不报错。
★ 只读、无副作用：本端点不触发补建、不改任何状态，页面统计是 SELECT。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_admin
from app.config.capabilities import (
    capability_snapshot,
    collect_graph_data_stats,
    collect_wiki_data_stats,
)
from app.config.config_profile import config_fingerprint
from app.config.settings import get_settings

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/capabilities")
async def system_capabilities(
    user: Any = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """能力快照（admin）。非 admin 403（require_admin）。

    ★ 能力关闭时不查库（collect_* 只在 enabled 时调用）：
      关闭态的 data 字段是 None（「未启用 / 未统计」），不是 0 ——
      0 会被读成「已就绪但没有内容」（见 capability_snapshot 的注释）。
    """
    settings = get_settings()
    fingerprint = config_fingerprint()

    stats: dict[str, int] | None = None
    if settings.graph_enabled:
        stats = await collect_graph_data_stats(session)
    wiki_stats: dict[str, int] | None = None
    if settings.wiki_enabled:
        wiki_stats = await collect_wiki_data_stats(session)

    return capability_snapshot(fingerprint, stats, wiki_stats)
