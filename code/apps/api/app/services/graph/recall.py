"""在线图召回（side channel）—— 实体图 → 邻居 chunk。

★ 这是**第 4 个上下文来源**，但语义与三路召回完全不同（设计文档 §9.3）：
  产物单独放在 `RetrievalResult.graph_recall_chunks`，分数恒 0，**不进 RRF、不参与
  排序、不参与阈值闸门、不改变拒答判定**。它只是「顺带多给模型几片相关原文」。
  塞进 `chunks` 会同时污染排序与 `retrieved[0]` 的阈值语义，而且 TOP_K_RERANK
  截断之后根本进不了上下文 —— 白算。

★ 永不抛异常（这是契约，不是防御性编程）：
  图是增强项。图库抖动 / 超时 / 语法问题都不该让一次问答失败 —— 三路召回照常返回，
  用户无感。代价是失败必须**可见**：`degraded_reason` + 调用方打点，而不是静默吞掉。

★ 失败时 MUST 在返回前 rollback：
  DB 侧超时设置（`set_config('statement_timeout', ..., true)`）与被取消的查询会把连接
  留在失败事务里，后续表格回捞 / 章节补全的 SQL 会连锁报 `current transaction is
  aborted` —— 图召回失败会连累主链路。rollback 自身也可能失败（连接已断），再包一层
  try：它的失败不该盖住原始原因。

★ 成功时 MUST 在返回前还原 statement_timeout：
  `is_local=true` 的作用域是**整个事务**，而本函数返回后同一事务里还要跑大表分片回捞
  与章节补全。不还原 = 把 800ms 的图召回预算套给它们 → 回捞 QueryCanceled、章节补全
  （无 try 包裹）冒泡成 VECTOR_STORE_DOWN：**图路把本来会成功的请求变成失败**，
  而 dev 数据量小、回捞几毫秒，看不出来。
  还原点是**唯一一处**（`_steps()` 之后、return 之前），空种子/空邻居这两个最常见
  的成功态也走这里 —— 它们恰恰是最需要还原的（否定路径不该留下最长的影响）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import decisions
from app.config.graph_profile import GraphProfile
from app.services.graph.base import GraphRecallResult
from app.services.graph.linking import link_entities
from app.services.graph.postgres_store import get_graph_store
from app.services.retrieve.base import RetrievedChunk

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GraphRecallOutcome(GraphRecallResult):
    """`GraphRecallResult` + 已组装好的上下文片。

    ★ 为什么用子类而不是改 `base.GraphRecallResult`：那个类型属于**端口契约**
      （`GraphStore` 的返回形状），而「取 chunk 行 + 组装 RetrievedChunk」是检索层
      的职责，不该反向污染存储端口的类型。子类让端口一行不动，调用方拿到的仍是
      `GraphRecallResult`（LSP 成立）。
    """

    chunks: list[RetrievedChunk] = field(default_factory=list)


# 取邻居片的 SQL：四闸门**逐字复用** _WHERE_BASE（函数内延迟 import —— 图包被
# retrieve 包导入，模块级反向 import 会成环）。取的是同一个字符串对象，不是副本：
# 复制一份到本文件必然漂移，而漂移的表现是越权或漏召回，都不报错。
_CHUNK_SQL_TEMPLATE = """
SELECT c.id, c.document_id, c.kb_id, c.content, c.heading_path, c.page_no,
       c.level_rank, c.token_count, d.filename
FROM chunks c
LEFT JOIN documents d ON d.id = c.document_id
{where_base}
  AND c.id = ANY(:chunk_ids)
"""


async def _rollback_quietly(session: AsyncSession) -> None:
    """吞掉 rollback 自身的异常：连接可能已断，它的失败不该盖住原始错因。"""
    try:
        await session.rollback()
    except Exception:  # noqa: BLE001 - 见 docstring
        logger.debug("图召回失败后的 rollback 也失败了（忽略）", exc_info=True)


async def _read_statement_timeout(session: AsyncSession) -> str:
    """读当前事务生效的 statement_timeout，作为稍后还原的目标值。

    读失败就退回 "0"：还原的目标值拿不到不该让图召回整体失败。
    （PG 的 statement_timeout 默认值就是 0 = 不限，本项目也不配全局值。）
    """
    try:
        value = await session.scalar(text("SELECT current_setting('statement_timeout')"))
    except Exception:  # noqa: BLE001 - 见 docstring
        logger.debug("读取 statement_timeout 失败，还原目标退回 0", exc_info=True)
        return "0"
    return "0" if value is None else str(value)


async def _restore_statement_timeout(session: AsyncSession, previous: str) -> None:
    """把事务级 statement_timeout 还原成调用前的值。

    ★ 为什么必须还原（这是真实缺陷，不是洁癖）：
      `set_config(..., is_local=true)` 的作用域是**整个事务**，不是「本函数」。
      而本函数返回后，**同一个事务**里还要跑大表分片回捞与章节补全 —— 800ms 的图
      召回预算会继续套在它们身上。原本「慢一点但能成功」的回捞变成 QueryCanceled，
      而章节补全那段没有 try 包裹，会直接冒泡成 VECTOR_STORE_DOWN。
      方向最坏：**图路把本来会成功的请求变成失败**，且 dev 数据量小不暴露。

    ★ 还原自身失败不许掩盖正常结果：吞异常 + 记日志。已拿到的召回结果仍然有效，
      不能因为收尾失败把一次成功召回判成 degraded。

    ★ 还原成**调用前的值**而不是硬编码 "0"：若服务端/上层配了全局 statement_timeout，
      写死 0 会把那条限制悄悄放宽 —— 放宽不报错，只是让后续 SQL 变慢而无人知晓。
    """
    try:
        await session.execute(
            text("SELECT set_config('statement_timeout', :prev, true)"),
            {"prev": previous},
        )
    except Exception:  # noqa: BLE001 - 见 docstring
        logger.warning(
            "图召回后还原 statement_timeout 失败：同一事务里的回捞/补全会继承较短超时",
            extra={"previous": previous},
            exc_info=True,
        )


def _build_chunk(row, *, filename: str) -> RetrievedChunk:  # noqa: ANN001 - Row
    """邻居行 → RetrievedChunk。分数与三路对齐为 0 / None（仅补上下文）。"""
    return RetrievedChunk(
        chunk_id=row.id,
        document_id=row.document_id,
        kb_id=row.kb_id,
        filename=filename,
        content=row.content,
        heading_path=row.heading_path or "",
        page_no=row.page_no,
        level_rank=row.level_rank,
        vector_score=0.0,
        keyword_score=None,
        rrf_score=0.0,
        rerank_score=None,
        # ★ 恒 0：图召回片不参与排序与阈值。给非 0 分数会改写拒答判定（见模块说明）。
        final_score=0.0,
        is_graph_recall=True,
    )


async def recall_by_graph(
    session: AsyncSession,
    *,
    query: str,
    pushdown: dict,
    timeout_ms: int,
    profile: GraphProfile,
) -> GraphRecallOutcome:
    """在线图召回。永不抛异常；超时/异常时返回空结果并给出 degraded_reason。

    ★ 成功路径的最后一条 SQL MUST 是 `statement_timeout` 还原（`_restore_statement_timeout`）：
      「空种子」「空邻居」也是成功路径，同样要还原 —— 否则最常见的两种情况（问句里
      没提到任何库内实体）反而把 800ms 的超时留给后面的回捞/补全。
      为此把 8 步收进内层 `_steps()`，让还原点只有一处、不可能漏。
    """
    started = time.perf_counter()

    def _outcome(reason: str = "", **kwargs) -> GraphRecallOutcome:
        return GraphRecallOutcome(
            degraded_reason=reason,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            **kwargs,
        )

    async def _steps() -> dict:
        # ② 查询 → 种子实体
        seeds = await link_entities(
            session,
            query=query,
            tenant_id=pushdown["tenant_id"],
            kb_ids=list(pushdown["authorized_kb_ids"]),
            extractor_model=profile.extractor_model,
            prompt_version=profile.prompt_version,
        )
        # ③ 空种子 = 正常态：这句话里没有库中已有的实体，不是故障、不告警
        if not seeds:
            return {"degraded_reason": "no_entities"}

        # ④ 图扩展（四闸门在存储层内下推，调用方不可能漏权限过滤）
        neighbors = await get_graph_store().neighbors(
            session,
            pushdown=pushdown,
            seed_entity_ids=[seed.entity_id for seed in seeds],
            extractor_model=profile.extractor_model,
            prompt_version=profile.prompt_version,
            max_hops=decisions.GRAPH_MAX_HOPS,
            limit=decisions.GRAPH_RETRIEVE_TOP_K,
        )
        # ⑤ 空邻居：种子实体没有被抽到任何关系上（只有孤立实体）
        if not neighbors:
            return {"degraded_reason": "no_neighbors", "seed_entity_count": len(seeds)}

        # ⑥ 排序 + 截断：键与存储层逐字一致（hop 升序 / weight 降序 / chunk_id 升序）。
        #    重排不是「不信任存储层」，而是同一把尺子两端都用：只让存储层排、
        #    这里不再校验，换一个 GraphStore 实现（Neo4j）时排序口径就无人守；
        #    用别的键重排则「取了哪 10 条」与存储层分叉，结果不可复现。
        #    UUID 的 Python 比较（.int）与 PG uuid 的字节序一致，故混合排序稳定。
        neighbors = sorted(neighbors, key=lambda n: (n.hop, -n.weight, n.chunk_id))[
            : decisions.GRAPH_RETRIEVE_TOP_K
        ]

        # ⑦ 取 chunk 行（逐字复用四闸门）并组装
        from app.services.retrieve import _WHERE_BASE

        rows = (
            await session.execute(
                text(_CHUNK_SQL_TEMPLATE.format(where_base=_WHERE_BASE)),
                {**pushdown, "chunk_ids": [n.chunk_id for n in neighbors]},
            )
        ).all()
        by_id = {row.id: row for row in rows}

        # ⑧ 预算裁剪：先条数、再 token（超预算即停，不跳片 —— 跳片会让上下文
        #    出现语义断裂的片段，长度省了但可读性更差）
        chunks: list[RetrievedChunk] = []
        used_tokens = 0
        for neighbor in neighbors:
            row = by_id.get(neighbor.chunk_id)
            if row is None:
                # 抽取之后被重解析 / 软删：图数据还在，chunk 行没了 —— 正常跳过
                continue
            if len(chunks) >= decisions.GRAPH_RECALL_MAX_CHUNKS:
                break
            row_tokens = int(row.token_count or 0)
            if used_tokens + row_tokens > decisions.GRAPH_RECALL_TOKEN_BUDGET:
                break
            chunks.append(_build_chunk(row, filename=getattr(row, "filename", "") or ""))
            used_tokens += row_tokens

        logger.debug(
            "图召回完成",
            extra={
                "seeds": len(seeds),
                "neighbors": len(neighbors),
                "chunks": len(chunks),
                "tokens": used_tokens,
            },
        )
        return {
            "degraded_reason": "",
            "seed_entity_count": len(seeds),
            "neighbors": list(neighbors),
            "chunks": chunks,
        }

    fields: dict = {"degraded_reason": "error"}
    previous_timeout = "0"
    try:
        # 墙钟预算两端都设：asyncio 侧取消 await，DB 侧也让 PG 自己停 ——
        # 只取消 await 时查询仍在库里跑（还会占着连接），下一个请求排队等它。
        async with asyncio.timeout(timeout_ms / 1000):
            # ① DB 侧超时（作用于当前事务，正是我们要的粒度；稍后还原）。
            #    ★ 必须走 set_config() 而不是 `SET LOCAL statement_timeout = :ms`：
            #      SET 是**工具语句**，不接受绑定参数（asyncpg 走扩展协议会发 $1，
            #      PG 报 syntax error at or near "$1"）。set_config 是普通函数，
            #      第三个参数 is_local=true 等价于 SET LOCAL（事务结束自动复原）。
            previous_timeout = await _read_statement_timeout(session)
            await session.execute(
                text("SELECT set_config('statement_timeout', :ms, true)"),
                {"ms": str(int(timeout_ms))},
            )
            fields = await _steps()

    except TimeoutError:
        # asyncio.timeout 触发（或语句被 PG 的 statement_timeout 取消后抛 TimeoutError）
        await _rollback_quietly(session)
        logger.info(
            "图召回超时，降级为 0 路（三路照常）",
            extra={"timeout_ms": timeout_ms},
        )
        return _outcome("timeout")

    except Exception:  # noqa: BLE001 - 契约：本函数永不抛（见模块 docstring）
        await _rollback_quietly(session)
        logger.warning("图召回失败，降级为 0 路（三路照常）", exc_info=True)
        return _outcome("error")

    # 成功路径（含空种子 / 空邻居）：最后一条 SQL 是还原，且只此一处 ——
    # 失败路径已经 rollback，事务级设置随之消失，不需要也不应该再发 SQL。
    await _restore_statement_timeout(session, previous_timeout)
    reason = fields.pop("degraded_reason", "")
    return _outcome(reason, **fields)
