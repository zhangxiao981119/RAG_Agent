"""GraphStore 端口 + 抽取/邻居产物数据结构。

★ 为什么是端口-适配器：图的存储形态（PostgreSQL 自连接 / Neo4j 遍历）是可替换的
  实现细节，而「四闸门下推 + 口径过滤」是**不可替换的契约**。契约钉在端口上，
  实现可换、契约不可绕。本版只提供 Postgres 实现（多一个常驻图库 = 多一份内存与
  排障面，而 5~10 万边这个量级在 PG 上走 B-tree + 2 跳自连接是毫秒级）。

★ 端口只有 4 个方法，**故意不提供** get_entity() / count() / 任意「不过滤」的取数
  方法：每多一个读方法就多一处可能忘加权限过滤的地方，而漏过滤的表现是越权，
  不会报错。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # pragma: no cover - 仅为类型标注，运行期不需要导入 ORM
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class ExtractedEntity:
    """抽取产出的一个实体（KB 内合并前的候选）。"""

    name: str                 # 归一前的展示名（≤ decisions.GRAPH_ENTITY_NAME_MAX_CHARS）
    name_key: str             # 归一化键（同一 KB 内唯一键，由 extract.normalize_name_key 产出）
    entity_type: str          # 必须 ∈ decisions.GRAPH_ENTITY_TYPES
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExtractedMention:
    """实体在某一片里出现过（图 → 原文的回链）。"""

    chunk_id: uuid.UUID
    entity_key: str           # 指向 ExtractedEntity.name_key
    surface: str              # 原文写法
    confidence: float | None = None


@dataclass(frozen=True)
class ExtractedRelation:
    """一条边。document_id 必带 —— 边只在文档维度记账，且唯一约束含它。"""

    src_key: str              # ExtractedEntity.name_key
    dst_key: str
    relation: str             # 归一后 ≤ decisions.GRAPH_RELATION_MAX_CHARS
    document_id: uuid.UUID


@dataclass(frozen=True)
class ExtractionResult:
    """一篇文档的抽取产物（单事务写入的单位）。"""

    entities: list[ExtractedEntity] = field(default_factory=list)
    mentions: list[ExtractedMention] = field(default_factory=list)
    relations: list[ExtractedRelation] = field(default_factory=list)
    extractor_model: str = ""
    prompt_version: str = ""
    chunks_processed: int = 0
    llm_calls: int = 0


@dataclass(frozen=True)
class GraphNeighbor:
    """图扩展得到的候选片。"""

    chunk_id: uuid.UUID
    hop: int                  # 1 = 种子实体的直接提及片；2 = 一跳邻居的提及片
    weight: int               # 路径支撑强度（沿途边 support_count 之和）


@dataclass(frozen=True)
class GraphRecallResult:
    """在线图召回的完整结果（第 2 步使用；此处先定义，避免端口与调用方各写一份）。"""

    neighbors: list[GraphNeighbor] = field(default_factory=list)
    seed_entity_count: int = 0
    expanded_entity_count: int = 0
    degraded_reason: str = ""    # "" = 正常；"timeout" / "error" / "no_entities" / "no_neighbors"
    elapsed_ms: int = 0


class GraphStore(Protocol):
    """图存储端口。**实现者必须满足四条契约**（缺一条即视为实现不合规）：

    契约 1（权限）：`neighbors()` 必须在存储层内完成四闸门过滤，逐字等价于
        app/services/retrieve/__init__.py 的 _WHERE_BASE。端口**不提供**任何
        「不过滤」的取数方法 —— 否则调用方一定会有人用错。
    契约 2（口径）：所有读取必须按 (extractor_model, prompt_version) 过滤；
        写入必须落这两个字段。无兜底分支。
    契约 3（幂等）：`upsert_subgraph()` 对同一 (document_id, 口径) 重复执行结果一致
        （先删后写，或 ON CONFLICT 合并，二者择一但必须确定）。
    契约 4（级联）：`delete_by_document()` 清本档 mention/relation，并清理无引用实体、
        重算受影响实体的 doc_freq。
    """

    async def upsert_subgraph(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        kb_id: uuid.UUID,
        document_id: uuid.UUID,
        result: ExtractionResult,
    ) -> int:
        """写入一篇文档的抽取结果（调用方事务内的一个单元）。返回写入的关系行数。"""

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
        """从种子实体出发扩 max_hops 跳，返回**已过四闸门**的候选 chunk。

        pushdown: tenant_id / clearance / authorized_kb_ids / user_subjects。
        结果按 (hop 升序, weight 降序, chunk_id 升序) 去重排序 —— 排序在存储层做，
        因为截断（limit）必须与排序一致，否则「取了哪 10 条」不可复现。
        """

    async def delete_by_document(self, session: AsyncSession, *, document_id: uuid.UUID) -> int:
        """清一篇文档的图数据 + 孤儿实体。返回删除的关系行数。"""

    async def health(self) -> bool:
        """存储可用性。内部吞异常，只回 bool（端口不向调用方抛存储层异常）。"""
