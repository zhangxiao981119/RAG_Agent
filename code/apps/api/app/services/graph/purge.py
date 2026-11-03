"""派生数据清理 —— 重索引（重新解析）前必须做的事。

★ 为什么必须存在（这是第 1 步最大的风险点）：
  重新解析是 `DELETE chunks` + `INSERT`（**新 uuid**）。图数据的两条证据都挂在
  chunk_id 上：`graph_entity_mentions.chunk_id` 有 ON DELETE CASCADE →
  旧提及被数据库自动删掉，但：
    · `graph_entities` 留下**孤儿实体**（doc_freq 全是历史值）；
    · `graph_relations` 的行还在（它挂 document_id，不挂 chunk），指向已不存在的证据；
    · `documents.graph_status` 仍是 'done' → 补建脚本认为这篇抽完了，永远不会重抽。
  合起来的后果是「图里还有边、但一条提及都没有」——图路静默 0 结果，且没人知道。
  所以清理 MUST 与 chunk 写入在**同一个事务**内完成：两者要么一起生效，要么一起回滚。

★ 为什么由本模块（而不是 parse_job）判断开关：
  关闭能力时 MUST 连表都不碰（迁移可能未跑，碰了就是 UndefinedTable 炸掉解析主路径）。
  把「什么时候该清」的判断收在一处，避免调用方各写一份 if。

★ 本步（第 1 步）**不含** wiki 派生清理：`wiki_pages` 表属迁移 0026（第 3 步）。
  第 3 步 MUST 在本函数的同一个事务内补一段 `DELETE FROM wiki_pages WHERE
  document_id = :id`（页的 source_chunk_ids 是数组、无外键，不删就会指向死 id）。
  这行不强加在本步，是因为在 0026 尚未存在的部署上执行它会让重解析整体失败。
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.models import Document
from app.services.graph.postgres_store import get_graph_store

logger = logging.getLogger(__name__)


def graph_purge_needed() -> bool:
    """重索引时是否需要清理图派生数据。

    ★ 只在能力开启时为真：关闭时图数据**保留**（重新开启且口径未变即可复用），
      而且此时表可能尚未迁移 —— 清理动作本身会让解析主路径失败。
    """
    return get_settings().graph_enabled


async def purge_derived(
    session: AsyncSession, *, document_id: uuid.UUID, reason: str = "reindex"
) -> int:
    """清一篇文档的图派生数据，并把抽取状态复位为 pending。返回删除的关系行数。

    调用契约（MUST）：
      · 与「删除旧 chunk + 写入新 chunk」在**同一个事务**内调用（调用方不得自行 commit）；
      · 只在本事务的 `DELETE chunks` **之前**调用 —— 顺序反了会漏掉「本轮新增的提及」
        之外的一切判断依据（提及已随 chunk 级联消失，affected 集合变成空集）。

    reason 只用于日志：'reindex'（重解析）/ 运维手动触发。审计需要知道
    「是谁把状态打回 pending 的」，否则数据消失时无从追溯。
    """
    if not graph_purge_needed():
        return 0

    store = get_graph_store()
    deleted = await store.delete_by_document(session, document_id=document_id)

    document = await session.get(Document, document_id)
    if document is not None:
        # 状态复位 = 告诉补建脚本「这篇需要重抽」。口径列一并置空：
        # 保留旧口径值会让 --recaliber 的选中条件误判为「口径匹配且已完成」。
        document.graph_status = "pending"
        document.graph_extractor_model = None
        document.graph_prompt_version = None
        document.graph_extracted_at = None
        logger.info(
            "图派生数据已清理（同事务内）",
            extra={
                "document_id": str(document_id),
                "deleted_relations": deleted,
                "reason": reason,
            },
        )
    return deleted
