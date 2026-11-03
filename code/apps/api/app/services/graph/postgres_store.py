"""PostgresGraphStore —— GraphStore 端口的唯一实现。

三条实现要点（读代码前先看清，否则容易「看起来对」）：

1. **权限在 SQL 里，不在应用层。** `neighbors()` 的取片 SQL **逐字内联**
   retrieve/__init__.py 的 `_WHERE_BASE`（四闸门 G1~G4 + is_latest）。不复写、
   不精简、不重排 —— 图路看到的 chunk 集合必须恰好等于向量路可能看到的集合。
2. **口径过滤无兜底分支。** 所有读都带 `extractor_model = :gm AND prompt_version = :pv`，
   写入都盖这两列。**没有** `OR prompt_version IS NULL` 这种兜底：旧口径 =
   查不到 = 图路对这篇文档暂时不生效（三路照常），这与 index_profile 的
   「宁可查不到，不可查错」是同一条纪律。
3. **幂等由 ON CONFLICT 保证，删除由调用方保证。** upsert_subgraph 是**合并**语义：
   同一 (document_id, 口径) 重复执行终态一致（关系行的 support_count 是**赋值**
   不是累加，因此在同一份抽取结果上重复跑不会翻倍）。但「重抽后不再出现的提及」
   只能靠先删 —— 所以 graph_job 固定顺序是 `delete_by_document()` → `upsert_subgraph()`。

★ 为什么没有 get_entity / count 之类的方法：见 base.py 的端口说明。少一个读方法
  就少一处忘加权限过滤的地方。
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import GraphEntity, GraphEntityMention, GraphRelation
from app.services.graph.base import (
    ExtractedEntity,
    ExtractionResult,
    GraphNeighbor,
)
from app.services.keyword_text import to_fts_text

logger = logging.getLogger(__name__)


class PostgresGraphStore:
    """图三表的读写实现（唯一实现，见端口说明）。"""

    # ── 写：一篇文档的抽取结果 ──────────────────────────────
    async def upsert_subgraph(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        kb_id: uuid.UUID,
        document_id: uuid.UUID,
        result: ExtractionResult,
    ) -> int:
        """写入实体 / 提及 / 关系。返回写入的关系行数。

        ★ 为什么实体用 ON CONFLICT 合并而不是「先插后补」：
          实体是**跨文档共享**的行（(kb_id, name_key) 唯一），A 文档先建、B 文档
          只补提及。合并时更新 name_fts（因为展示名可能被后出现的写法替换）——
          name_vec 是生成列，会跟着自动重算，应用侧 MUST NOT 写它。
        ★ 关系行的 support_count 用**赋值**（EXCLUDED）而非 `+`：同一份抽取结果
          重跑时不会翻倍；同文档内多次表述的累加在 Python 侧先聚合（见 _merge_relations）。
        """
        if not result.entities:
            return 0

        entity_ids = await self._upsert_entities(
            session, tenant_id=tenant_id, kb_id=kb_id, result=result
        )
        await self._insert_mentions(
            session,
            tenant_id=tenant_id,
            kb_id=kb_id,
            document_id=document_id,
            result=result,
            entity_ids=entity_ids,
        )
        relation_rows = self._merge_relations(result)
        written = await self._insert_relations(
            session,
            tenant_id=tenant_id,
            kb_id=kb_id,
            document_id=document_id,
            result=result,
            entity_ids=entity_ids,
            relation_rows=relation_rows,
        )
        return written

    async def _upsert_entities(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        kb_id: uuid.UUID,
        result: ExtractionResult,
    ) -> dict[str, uuid.UUID]:
        """按 (kb_id, name_key) 合并实体，返回 name_key → entity_id 映射。"""
        # 同一批里 name_key 重复 MUST 先去掉：PostgreSQL 对
        # "INSERT ... ON CONFLICT DO UPDATE" 的同一语句内重复冲突键会直接报
        # 「cannot affect row a second time」。extract 侧已经去重，这里再挡一次是因为
        # 端口是公开的 —— 换个调用方（脚本 / 将来的增量抽取）不保证遵守该约定。
        unique: dict[str, ExtractedEntity] = {}
        for entity in result.entities:
            unique.setdefault(entity.name_key, entity)
        values = [
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_id,
                "kb_id": kb_id,
                "name": entity.name,
                "name_key": entity.name_key,
                # 写入侧切分 MUST 用 keyword_text.to_fts_text（与查询侧 build_fts_query
                # 共用同一模块），否则两侧 token 对不上 → 静默 0 召回。
                "name_fts": to_fts_text(entity.name, ""),
                "entity_type": entity.entity_type,
                "aliases": list(entity.aliases),
            }
            for entity in unique.values()
        ]
        stmt = (
            pg_insert(GraphEntity)
            .values(values)
            .on_conflict_do_update(
                index_elements=["kb_id", "name_key"],
                set_={
                    # 展示名与类型以后出现者为准；name_fts 必须同步（生成列 name_vec 跟着变）
                    "name": text("excluded.name"),
                    "name_fts": text("excluded.name_fts"),
                    "entity_type": text("excluded.entity_type"),
                    "aliases": text("excluded.aliases"),
                    "updated_at": func.now(),
                },
            )
            .returning(GraphEntity.id, GraphEntity.name_key)
        )
        rows = (await session.execute(stmt)).all()
        return {name_key: entity_id for entity_id, name_key in rows}

    async def _insert_mentions(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        kb_id: uuid.UUID,
        document_id: uuid.UUID,
        result: ExtractionResult,
        entity_ids: dict[str, uuid.UUID],
    ) -> None:
        """写入提及。同一 (chunk, entity) 已存在则保持原样（DO NOTHING）。"""
        merged: dict[tuple[uuid.UUID, uuid.UUID], dict] = {}
        for mention in result.mentions:
            entity_id = entity_ids.get(mention.entity_key)
            if entity_id is None:
                # 提及指向一个已被丢弃的实体（超长名 / 类型非法）——跳过而不是造行
                continue
            key = (mention.chunk_id, entity_id)
            if key in merged:
                continue
            merged[key] = {
                "id": uuid.uuid4(),
                "tenant_id": tenant_id,
                "kb_id": kb_id,
                "document_id": document_id,
                "chunk_id": mention.chunk_id,
                "entity_id": entity_id,
                "surface": mention.surface,
                "confidence": mention.confidence,
                "extractor_model": result.extractor_model,
                "prompt_version": result.prompt_version,
            }
        if not merged:
            return
        stmt = (
            pg_insert(GraphEntityMention)
            .values(list(merged.values()))
            .on_conflict_do_nothing(index_elements=["chunk_id", "entity_id"])
        )
        await session.execute(stmt)

    @staticmethod
    def _merge_relations(
        result: ExtractionResult,
    ) -> dict[tuple[str, str, str], int]:
        """按 (src, dst, relation) 聚合 support_count（同文档内被表述多次）。

        ★ 为什么在 Python 侧聚合而不是 SQL 的 `support_count + 1`：
          唯一约束是 (src, dst, relation, document_id)，一次 INSERT 里同键出现两次
          会触发 "ON CONFLICT DO UPDATE command cannot affect row a second time"。
          先聚合再写，既避开该错误，又让 support_count 成为**确定值**（幂等前提）。
        """
        counts: dict[tuple[str, str, str], int] = {}
        for relation in result.relations:
            key = (relation.src_key, relation.dst_key, relation.relation)
            counts[key] = counts.get(key, 0) + 1
        return counts

    async def _insert_relations(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        kb_id: uuid.UUID,
        document_id: uuid.UUID,
        result: ExtractionResult,
        entity_ids: dict[str, uuid.UUID],
        relation_rows: dict[tuple[str, str, str], int],
    ) -> int:
        values: list[dict] = []
        for (src_key, dst_key, relation), support in relation_rows.items():
            src_id = entity_ids.get(src_key)
            dst_id = entity_ids.get(dst_key)
            if src_id is None or dst_id is None:
                continue
            if src_id == dst_id:
                # DDL 有 ck_graph_relations_not_self：自环在抽取侧就该被丢弃，
                # 这里再挡一次是为了不让一条脏数据把整篇文档的写入打回滚。
                continue
            values.append(
                {
                    "id": uuid.uuid4(),
                    "tenant_id": tenant_id,
                    "kb_id": kb_id,
                    "src_entity_id": src_id,
                    "dst_entity_id": dst_id,
                    "relation": relation,
                    "document_id": document_id,
                    "support_count": support,
                    "extractor_model": result.extractor_model,
                    "prompt_version": result.prompt_version,
                }
            )
        if not values:
            return 0
        stmt = (
            pg_insert(GraphRelation)
            .values(values)
            .on_conflict_do_update(
                index_elements=[
                    "src_entity_id", "dst_entity_id", "relation", "document_id"
                ],
                set_={
                    "support_count": text("excluded.support_count"),
                    # 口径列随写入更新：同一文档同一三元组在换模型重抽后，
                    # 旧口径行会被 DO UPDATE 改写为新口径（否则唯一键冲突导致
                    # 新口径写不进去、旧口径又永远查不到 → 该边静默消失）。
                    "extractor_model": text("excluded.extractor_model"),
                    "prompt_version": text("excluded.prompt_version"),
                },
            )
        )
        await session.execute(stmt)
        return len(values)

    # ── 删：一篇文档的图数据 + 孤儿实体 ─────────────────────
    async def delete_by_document(
        self, session: AsyncSession, *, document_id: uuid.UUID
    ) -> int:
        """清本档 mention / relation，清理无引用实体并重算 doc_freq。

        ★ 只清理「因本文档失去提及」的实体（affected），不扫全 kb：
          只有这些实体可能变成孤儿。等价性：一个实体若在本档没有提及，它的
          mention 集合不因本次删除而变 → 不可能由「非孤儿」变「孤儿」。
          全 kb 扫 `NOT EXISTS` 在 20~30 万 mention 的量级上会变成每篇文档一次
          全表反连接，而按 affected 收敛是索引点查。
        ★ doc_freq 一律**重算**（count(DISTINCT document_id)），不做 -1 增量：
          多文档共享实体时增量必然算错（同一文档删了两次提及，-2；实际只该 -0）。
        """
        affected_ids = (
            await session.execute(
                select(GraphEntityMention.entity_id)
                .where(GraphEntityMention.document_id == document_id)
                .distinct()
            )
        ).scalars().all()

        deleted_relations = (
            await session.execute(
                delete(GraphRelation).where(GraphRelation.document_id == document_id)
            )
        ).rowcount or 0
        await session.execute(
            delete(GraphEntityMention).where(
                GraphEntityMention.document_id == document_id
            )
        )

        if not affected_ids:
            return int(deleted_relations)

        # 孤儿实体：在受影响集合内，且已无任何提及
        await session.execute(
            text(
                "DELETE FROM graph_entities e "
                "WHERE e.id = ANY(:ids) "
                "  AND NOT EXISTS ("
                "      SELECT 1 FROM graph_entity_mentions m WHERE m.entity_id = e.id)"
            ),
            {"ids": list(affected_ids)},
        )
        # 幸存实体的 doc_freq 重算（孤儿已在上一步删除，这里只剩有提及的）
        await session.execute(
            text(
                "UPDATE graph_entities e SET doc_freq = sub.cnt, updated_at = now() "
                "FROM ("
                "  SELECT entity_id, count(DISTINCT document_id) AS cnt "
                "  FROM graph_entity_mentions WHERE entity_id = ANY(:ids) "
                "  GROUP BY entity_id"
                ") AS sub "
                "WHERE e.id = sub.entity_id"
            ),
            {"ids": list(affected_ids)},
        )
        return int(deleted_relations)

    # ── 读：图扩展（四闸门 + 口径过滤）───────────────────────
    async def neighbors(
        self,
        session: AsyncSession,
        *,
        pushdown: dict,
        seed_entity_ids: list[uuid.UUID],
        extractor_model: str,
        prompt_version: str,
        max_hops: int,
        limit: int,
    ) -> list[GraphNeighbor]:
        """从种子实体扩 max_hops 跳，返回已过四闸门的候选 chunk。"""
        if not seed_entity_ids or limit < 1:
            return []

        # ★ 延迟导入：第 2 步 retrieve 会 import 图服务（在线召回），
        #   模块级互相 import 会成环。这里取的是**同一个字符串对象**（不是复制的副本），
        #   所以四闸门永远只有一处真源 —— 复制一份到本文件必然漂移，
        #   而漂移的表现是越权或漏召回，都不报错。
        from app.services.retrieve import _WHERE_BASE

        sql = text(
            f"""
WITH RECURSIVE walk AS (
    -- 种子实体：hop=1，weight=0（种子自身没有「路径支撑」）
    SELECT e.id AS entity_id, 1 AS hop, 0 AS weight, ARRAY[e.id] AS path
    FROM graph_entities e
    WHERE e.id = ANY(:seed_entity_ids)
  UNION ALL
    SELECT r.dst_entity_id,
           w.hop + 1,
           w.weight + r.support_count,
           w.path || r.dst_entity_id
    FROM walk w
    JOIN graph_relations r ON r.src_entity_id = w.entity_id
    WHERE w.hop < :max_hops
      AND r.extractor_model = :extractor_model
      AND r.prompt_version  = :prompt_version
      AND r.tenant_id       = :tenant_id
      AND r.kb_id           = ANY(:authorized_kb_ids)
      -- 防环：同一路径上不重复经过同一实体（无此条件时 2 跳会指数膨胀）
      AND NOT (r.dst_entity_id = ANY(w.path))
),
agg AS (
    SELECT entity_id, MIN(hop) AS hop, SUM(weight) AS weight
    FROM walk GROUP BY entity_id
)
SELECT c.id AS chunk_id,
       MIN(a.hop)    AS hop,
       SUM(a.weight) AS weight
FROM agg a
JOIN graph_entity_mentions m
  ON m.entity_id = a.entity_id
 AND m.extractor_model = :extractor_model
 AND m.prompt_version  = :prompt_version
JOIN chunks c ON c.id = m.chunk_id
{_WHERE_BASE}
GROUP BY c.id
ORDER BY MIN(a.hop) ASC, SUM(a.weight) DESC, c.id ASC
LIMIT :limit
"""
        )
        params = {
            **{k: pushdown[k] for k in
               ("tenant_id", "clearance", "authorized_kb_ids", "user_subjects")},
            "seed_entity_ids": list(seed_entity_ids),
            "extractor_model": extractor_model,
            "prompt_version": prompt_version,
            "max_hops": int(max_hops),
            "limit": int(limit),
        }
        rows = (await session.execute(sql, params)).all()
        return [
            GraphNeighbor(chunk_id=row.chunk_id, hop=int(row.hop), weight=int(row.weight))
            for row in rows
        ]

    # ── 健康 ────────────────────────────────────────────────
    async def health(self) -> bool:
        """存储可用性（含「表是否存在」）。内部吞异常，只回 bool。

        ★ 为什么包含一次真实取数而不是 `SELECT 1`：
          `SELECT 1` 只证明「能连上 PG」，不证明「迁移跑过」。开了图但没迁移时，
          连接完全正常，而每次问答都会静默走 0 路 —— 正是要堵的那种静默降级。
        """
        from app.database import SessionLocal

        try:
            async with SessionLocal() as session:
                await session.execute(text("SELECT 1 FROM graph_entities LIMIT 1"))
            return True
        except Exception:
            logger.warning("图存储健康检查失败", exc_info=True)
            return False


# ── 端口实现的选择点（唯一一处）───────────────────────────
# 后端选择由 settings.graph_store_backend 决定，但**实现只注册了 postgres**。
# 非 postgres 的值在启动自检里就被拒绝启动（self_check_ext），不会走到这里 ——
# 因此本函数没有「未知后端 → 返回 None / 降级」的分支：静默降级是设计禁止的。
_STORE = PostgresGraphStore()


def get_graph_store() -> PostgresGraphStore:
    """当前图存储实现（进程内单例：无连接状态，可安全共享）。"""
    return _STORE
