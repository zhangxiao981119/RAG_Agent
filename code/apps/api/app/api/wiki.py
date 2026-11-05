"""wiki 导航页读接口（设计文档 §9.6 ②③）—— 只读，不进检索（术语扩展是第 4 步）。

★ 两个端点的权限口径刻意不同（§8.4 / §9.6）：
  · 列表 = **kb 成员**（复用既有 kb 可见性判定，与 GET /api/kbs/{id} 同款）；
  · 详情 = 该页对应文档**存在至少一条过四闸门的 chunk**（EXISTS 子查询，
    内联 retrieve 的 `_WHERE_BASE` —— 四闸门单一真源，不复写一份 ACL 条件）。
★ 404 三合一（§9.6 ③）：页面不存在 / 生成失败 / 用户对该文档无可见 chunk，
  **三者返回同一 404** —— 用 404/403 的差异探测「某密级文档是否存在」会泄漏存在性。
★ WIKI_ENABLED=false → 两端点 404（关闭的能力不假装存在，且不是 403 ——
  关闭态对任何人都一样，不提供「能力存在与否」的探测面）。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user, get_db
from app.config.settings import get_settings

router = APIRouter(tags=["wiki"])

# §9.6 ③ 的固定免责声明（逐字）：summary / sections 都是 LLM 产物，
# 不是原文，不构成引用依据 —— 这句话 MUST 随每个详情响应返回，不能只在文档里。
WIKI_PAGE_DISCLAIMER: str = (
    "本页为系统自动生成的导航摘要（LLM 产物），不是原始文档内容，"
    "不构成引用依据；请以引用的原文片段为准。"
)

# 详情可见性判定（§9.6 ③）：EXISTS 过四闸门的 chunk。
# `_WHERE_BASE` 延迟导入（与 postgres_store.py 同款）：retrieve 是检索主路径模块，
# wiki 关闭时不应在 import 期把它拉进来。占位符与 _WHERE_BASE 的绑定参数一一对应。
_PAGE_VISIBLE_SQL = """
SELECT p.id, p.document_id, p.title, p.summary, p.sections, p.key_terms,
       p.generator_model, p.prompt_version, p.generated_at
FROM wiki_pages p
WHERE p.id = :page_id
  AND p.status = 'ready'
  AND EXISTS (
    SELECT 1 FROM chunks c
    {where_base}
      AND c.document_id = p.document_id
  )
"""


class WikiPageListItem(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    title: str
    generated_at: datetime


class WikiPageListResponse(BaseModel):
    items: list[WikiPageListItem]


class WikiPageDetail(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    title: str
    summary: str
    sections: list[dict]
    key_terms: list[str]
    caliber: dict[str, str]
    disclaimer: str


async def _tenant_kb_ids(session: AsyncSession, tenant_id: uuid.UUID) -> list[uuid.UUID]:

    rows = await session.execute(
        text("SELECT id FROM knowledge_bases WHERE tenant_id = :tid"),
        {"tid": tenant_id},
    )
    return [r[0] for r in rows]


@router.get("/kbs/{kb_id}/wiki/pages", response_model=WikiPageListResponse)
async def list_wiki_pages(
    kb_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> WikiPageListResponse:
    """某知识库的 wiki 页列表（导航 UI 数据源）。

    权限 = kb 成员（复用 GET /api/kbs/{kb_id} 的判定：admin 豁免，非成员 403）。
    列表只含「该用户有可见 chunk」的文档的页 —— 不可见文档的页**跳过**而不是 404
    （列表的语义是过滤，不是探测）。
    """
    settings = get_settings()
    if not settings.wiki_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="NOT_FOUND")

    # 与 kbs.get_kb 同款判定（复用既有 kb 可见性判定，不新造一套）
    if user.clearance < 40 and kb_id not in user.authorized_kb_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="FORBIDDEN")

    # kb 成员可见性里的租户校验：kb 必须属于本租户（跨租户 id 一律 404）
    from app.models import KnowledgeBase

    kb = await session.get(KnowledgeBase, kb_id)
    if kb is None or kb.tenant_id != user.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="NOT_FOUND")

    # 列表可见性 = 文档存在过四闸门的 chunk（与详情同一份 EXISTS，只是从
    # 「按 page_id」换成「按 p.kb_id 收集」）
    from app.services.retrieve import _WHERE_BASE

    kb_ids = (
        await _tenant_kb_ids(session, user.tenant_id)
        if user.clearance >= 40
        else list(user.authorized_kb_ids)
    )
    sql = f"""
    SELECT p.id, p.document_id, p.title, p.generated_at
    FROM wiki_pages p
    WHERE p.kb_id = :kb_id
      AND p.tenant_id = :tenant_id
      AND p.status = 'ready'
      AND EXISTS (
        SELECT 1 FROM chunks c
        {_WHERE_BASE}
          AND c.document_id = p.document_id
      )
    ORDER BY p.generated_at DESC
    """
    rows = (
        await session.execute(
            text(sql),
            {
                "kb_id": kb_id,
                "tenant_id": user.tenant_id,
                "clearance": user.clearance,
                "authorized_kb_ids": kb_ids,
                "user_subjects": user.subjects,
            },
        )
    ).all()
    return WikiPageListResponse(
        items=[
            WikiPageListItem(
                id=row.id, document_id=row.document_id,
                title=row.title, generated_at=row.generated_at,
            )
            for row in rows
        ]
    )


@router.get("/wiki/pages/{page_id}", response_model=WikiPageDetail)
async def get_wiki_page(
    page_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> WikiPageDetail:
    """wiki 页详情。

    权限 = 该页对应文档存在至少一条通过四闸门的 chunk（EXISTS 内联 _WHERE_BASE）。
    404 三合一：不存在 / 生成失败（status != ready）/ 无可见 chunk —— 同一个 404。
    """
    settings = get_settings()
    if not settings.wiki_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="NOT_FOUND")

    from app.services.retrieve import _WHERE_BASE

    if user.clearance >= 40:
        kb_ids = await _tenant_kb_ids(session, user.tenant_id)
    else:
        kb_ids = list(user.authorized_kb_ids)

    row = (
        await session.execute(
            text(_PAGE_VISIBLE_SQL.format(where_base=_WHERE_BASE)),
            {
                "page_id": page_id,
                "tenant_id": user.tenant_id,
                "clearance": user.clearance,
                "authorized_kb_ids": kb_ids,
                "user_subjects": user.subjects,
            },
        )
    ).first()
    # 404 三合一：不做「页在但你没权」与「页不在」的区分（§9.6 ③）
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="NOT_FOUND")

    return WikiPageDetail(
        id=row.id,
        document_id=row.document_id,
        title=row.title,
        summary=row.summary,
        sections=list(row.sections or []),
        key_terms=list(row.key_terms or []),
        caliber={
            "generator_model": row.generator_model,
            "prompt_version": row.prompt_version,
        },
        disclaimer=WIKI_PAGE_DISCLAIMER,
    )
