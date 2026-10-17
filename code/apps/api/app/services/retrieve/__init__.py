"""检索服务 —— 混合召回 + RRF + 重排 + 阈值（手册 §3.3.2）。

M4 任务 5：权限下推过滤（H7）。向量 / 关键词 SQL 都接进四闸门：
  G1 level_rank <= clearance
  G2 kb_id = ANY(authorized_kb_ids)
  G3 NOT deny_subjects && user_subjects
  G4 acl_tags && user_subjects

删除 M2 "先取后过滤"临时逻辑。PUSHDOWN_WHERE_SQL 从 acl/visibility 导入
（手册 §3.3.2 唯一真源），pushdown_params 构造参数。
"""
from __future__ import annotations

import logging
import re
import time
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import decisions
from app.config.index_profile import current_index_profile
from app.infra import metrics, trace
from app.services.acl.visibility import pushdown_params  # noqa: F401  (外部引用，保留导出)
from app.services.embedding import get_embedding_service
from app.services.retrieve.base import RetrievalResult, RetrievedChunk
from app.services.rerank import RerankTimeout, get_rerank_service

logger = logging.getLogger(__name__)

# 下推 SQL 骨架（来自 acl/visibility.py：四闸门 G1~G4）
# 注意：chunks 表无 deleted_at 列，PUSHDOWN_WHERE_SQL 里的 deleted_at 条件要去掉
# （手册 §3.3.2 是通用模板，当前 chunks 只有 is_latest 表示版本）
# 我们自己拼 WHERE 子句，保留四闸门核心条件
_WHERE_BASE = """WHERE c.tenant_id = :tenant_id
  AND c.is_latest = true
  AND c.level_rank <= :clearance
  AND c.kb_id = ANY(:authorized_kb_ids)
  AND NOT (c.deny_subjects && :user_subjects)
  AND c.acl_tags && :user_subjects"""

# 向量召回 SQL（余弦距离 <=>，取 top K）
# ★ 索引口径过滤（方案一）：只比对「向量是否同构」的三要素（模型 + 文本公式），
#   与写入侧 parse_job 记录的口径对齐。改模型/改向量文本公式后，旧 chunk 向量
#   被判为不同构而**排除**——宁可在重算前查不到，也不能把不同构向量混进来算相似度
#   （那是"静默劣化"：不报错，只是答不准）。
#   ★ 该过滤**只加在向量路径**：关键词路（ILIKE / tsquery）与向量空间无关，加会误伤。
# 治理权重（M7 WU-M-17）：COALESCE 按 0 —— INV-M-12 治理失败/未审查不参与过滤
_VECTOR_SQL_TEMPLATE = f"""
SELECT c.id, c.document_id, c.kb_id, c.content, c.heading_path, c.page_no,
       c.level_rank, c.acl_tags, c.table_group_id,
       COALESCE(c.governance_weight, 0) AS governance_weight,
       1 - (c.embedding <=> '{{query_vec}}'::vector) AS vector_score
FROM chunks c
{_WHERE_BASE}
  AND c.embedding IS NOT NULL
  AND c.embedding_model = :embedding_model
  AND c.embedding_text_version = :embedding_text_version
ORDER BY c.embedding <=> '{{query_vec}}'::vector
LIMIT :limit
"""

# 关键词召回（ILIKE 子串匹配）
# ESCAPE '\' 让 pattern 中的 \% \_ 当字面量，避免用户输入的 %/_ 被当 SQL 通配符
_KEYWORD_SQL = text(f"""
SELECT c.id, c.document_id, c.kb_id, c.content, c.heading_path, c.page_no,
       c.level_rank, c.acl_tags, c.table_group_id,
       COALESCE(c.governance_weight, 0) AS governance_weight
FROM chunks c
{_WHERE_BASE}
  AND (c.content ILIKE :pattern ESCAPE '\\' OR c.heading_path ILIKE :pattern ESCAPE '\\')
LIMIT :limit
""")

# ★ 关键词召回（tsquery 路径）—— 走 entities.py 里已建但此前未使用的 idx_chunk_fts
#   收益：索引可用 + 词元级匹配（不再只靠子串），且 ts_rank 提供真实排序分。
#   限制：PostgreSQL 'simple' 配置不做中文分词，中文仍靠上面的 ILIKE 路径。
#   ★ 别名必须写成 AS q(query)：to_tsquery 是标量函数，不给列别名时它的输出列
#     名是函数名本身，`q.query` 会直接报 column does not exist。
_KEYWORD_FTS_SQL = text(f"""
SELECT c.id, c.document_id, c.kb_id, c.content, c.heading_path, c.page_no,
       c.level_rank, c.acl_tags, c.table_group_id,
       COALESCE(c.governance_weight, 0) AS governance_weight,
       ts_rank(to_tsvector('simple', c.content), q.query) AS keyword_score
FROM chunks c, to_tsquery('simple', :tsquery) AS q(query)
{_WHERE_BASE}
  AND to_tsvector('simple', c.content) @@ q.query
ORDER BY keyword_score DESC
LIMIT :limit
""")

# 取 chunk 的 filename（JOIN documents）
# 过滤 deleted_at IS NULL，避免已软删除文档的 filename 仍被带进引用展示
_FILENAME_SQL = text("""
SELECT id, filename FROM documents WHERE id = ANY(:doc_ids) AND deleted_at IS NULL
""")

# 大表分片回捞（表格决策 A1'）：按组取同组全部分片，ACL 四闸门与主召回同款下推。
# 已入选片 / 上限 / token 预算在 Python 侧过滤 —— 组内分片数小（= 表 tokens / 800），
# LIMIT 取上限 + 50 余量已足够；排除条件用 Python 过滤避免 asyncpg 空数组类型推断问题。
# ORDER BY chunk_index 保证回捞片按表内顺序拼接（跨片行对比需要行序连贯）。
_TABLE_RECALL_SQL = text(f"""
SELECT c.id, c.document_id, c.kb_id, c.content, c.heading_path, c.page_no,
       c.level_rank, c.token_count
FROM chunks c
{_WHERE_BASE}
  AND c.table_group_id = :group_id
ORDER BY c.chunk_index
LIMIT :recall_limit
""")

# 从查询中提取可安全用于 tsquery 的词元：仅 ASCII 字母/数字/下划线，长度 >= 2。
# 只提取这类词元有两个原因：
#   ① 它们是 tsquery 能正确处理的（无需转义操作符，也匹配得上 'simple' 分词结果）
#   ② 中文/日文等无法被 'simple' 配置分词，交给 ILIKE 路径处理
_TS_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{2,}")


def build_tsquery(query: str, max_tokens: int = 12) -> str | None:
    """把查询里的 ASCII/数字词元拼成 tsquery（OR 连接）。

    返回 None 表示无可用词元 —— 调用方应当跳过 FTS 路径。
    OR 而非 AND：关键词召回是**召回**路径，优先保召回率，精排交给 RRF 与重排。
    """
    tokens = _TS_TOKEN_RE.findall(query[:400])
    if not tokens:
        return None
    # 去重并保持出现顺序，避免重复词元膨胀 tsquery
    seen: list[str] = []
    for tk in tokens:
        low = tk.lower()
        if low not in seen:
            seen.append(low)
        if len(seen) >= max_tokens:
            break
    return " | ".join(seen)


def _build_rrf_scores(
    *rank_lists: list[uuid.UUID],
) -> dict[uuid.UUID, float]:
    """RRF 融合。k=60。分数 = Σ_r 1/(k + rank_r)。

    接受任意条召回路径 —— 当前是三条（向量 / tsquery / ILIKE），
    新增召回路径时直接多传一个 list，无需改本函数。
    """
    scores: dict[uuid.UUID, float] = {}
    for ranks in rank_lists:
        for rank, cid in enumerate(ranks):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (decisions.RRF_K + rank + 1)
    return scores


class RetrievalService:
    async def retrieve(
        self,
        session: AsyncSession,
        query: str,
        tenant_id: uuid.UUID,
        authorized_kb_ids: list[uuid.UUID],
        clearance: int,
        user_subjects: list[str],
    ) -> RetrievalResult:
        """混合召回 + RRF + rerank。

        参数是 pushdown_params 的直接展开（由 API 层从 CurrentUser 取，
        MUST NOT 信任前端传参）。若 authorized_kb_ids 为空直接拒答。
        """
        stages: dict[str, int] = {}

        if not authorized_kb_ids:
            return RetrievalResult(refused=True, refuse_reason="NO_RELEVANT_CONTENT", stage_ms=stages)

        # 下推参数（手册 §3.3.2：四闸门 G1~G4 的 SQL 绑定值）
        pushdown = {
            "tenant_id": tenant_id,
            "clearance": clearance,
            "authorized_kb_ids": authorized_kb_ids,
            "user_subjects": user_subjects,
        }

        # ① 向量召回
        t0 = time.perf_counter()
        embedding_service = get_embedding_service()
        query_vec = await embedding_service.embed_query(query)
        # pgvector 输入格式："[0.1,0.2,...]" 字符串，直接嵌入 SQL
        query_vec_str = "[" + ",".join(repr(float(x)) for x in query_vec) + "]"
        vector_sql = text(_VECTOR_SQL_TEMPLATE.format(query_vec=query_vec_str))
        # 按当前索引口径过滤：只召回与本查询向量同构（同模型 + 同文本公式）的 chunk
        profile = current_index_profile()
        vector_result = await session.execute(
            vector_sql,
            {
                **pushdown,
                "limit": decisions.TOP_K_RECALL,
                "embedding_model": profile.embedding_model,
                "embedding_text_version": profile.embedding_text_version,
            },
        )
        vector_rows = vector_result.fetchall()
        stages["retrieving"] = int((time.perf_counter() - t0) * 1000)

        # ② 关键词召回（双路径：tsquery 走全文索引 + ILIKE 兜中文）
        t0 = time.perf_counter()
        # 转义 ILIKE 通配符 % 和 _，避免用户输入改变匹配语义
        # （如查询 "100% 完成" 不应把 % 当任意字符通配）
        escaped = (
            query[:200]
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        pattern = f"%{escaped}%"
        keyword_result = await session.execute(
            _KEYWORD_SQL,
            {**pushdown, "pattern": pattern, "limit": decisions.TOP_K_RECALL},
        )
        keyword_rows = list(keyword_result.fetchall())

        # ★ tsquery 路径：走 idx_chunk_fts 索引，只用 ASCII/数字词元
        fts_rows = []
        tsquery = build_tsquery(query) if decisions.KEYWORD_TSQUERY_ENABLED else None
        if tsquery:
            try:
                fts_result = await session.execute(
                    _KEYWORD_FTS_SQL,
                    {**pushdown, "tsquery": tsquery, "limit": decisions.TOP_K_RECALL},
                )
                fts_rows = list(fts_result.fetchall())
            except Exception:
                # FTS 路径失败不应拖垮检索 —— 记录后继续用 ILIKE 结果
                logger.warning("tsquery 召回失败，仅用 ILIKE 路径", exc_info=True)
                metrics.incr("rag.keyword.fts_failed")
        stages["keyword"] = int((time.perf_counter() - t0) * 1000)

        # 合并所有 chunk（去重）
        # 注意：FTS 路径排在 ILIKE 之前入 map，让 keyword_score 字段来自 FTS（ILIRE 行无该字段）
        all_chunks: dict[uuid.UUID, dict] = {}
        vector_ranks: list[uuid.UUID] = []
        for row in vector_rows:
            all_chunks[row.id] = dict(row._mapping)
            vector_ranks.append(row.id)
        fts_ranks: list[uuid.UUID] = []
        for row in fts_rows:
            if row.id not in all_chunks:
                all_chunks[row.id] = dict(row._mapping)
            fts_ranks.append(row.id)
        keyword_ranks: list[uuid.UUID] = []
        for row in keyword_rows:
            if row.id not in all_chunks:
                all_chunks[row.id] = dict(row._mapping)
            keyword_ranks.append(row.id)

        if not all_chunks:
            return RetrievalResult(refused=True, refuse_reason="NO_RELEVANT_CONTENT", stage_ms=stages)

        # ③ RRF 融合（三路召回：向量 / tsquery / ILIKE 全部计入）
        rrf_scores = _build_rrf_scores(vector_ranks, fts_ranks, keyword_ranks)
        # 按 RRF 降序取 top 50（去重后的候选）
        sorted_ids = sorted(all_chunks.keys(), key=lambda cid: rrf_scores[cid], reverse=True)
        candidate_ids = sorted_ids[: decisions.TOP_K_RECALL]

        # 取 filename
        doc_ids = list({all_chunks[cid]["document_id"] for cid in candidate_ids})
        filename_result = await session.execute(_FILENAME_SQL, {"doc_ids": doc_ids})
        filenames = {row.id: row.filename for row in filename_result.fetchall()}

        # ④ rerank
        t0 = time.perf_counter()
        # rerank 在线时只保留被重排返回的候选（未返回即视为不相关，已淘汰）
        reranked_ids: list[uuid.UUID] = []
        rerank_scores: dict[uuid.UUID, float] = {}
        use_rerank = True
        try:
            rerank_service = get_rerank_service()
            documents = [all_chunks[cid]["content"] for cid in candidate_ids]
            results = await rerank_service.rerank(query, documents, decisions.TOP_K_RERANK)
            # results 按相关性绝对分降序，直接采用（relevance_score 已是 0~1 的 sigmoid 分）
            for idx, score in results:
                # 防御越界：异常 rerank 服务返回的 idx 可能超出 candidate_ids 范围
                if not (0 <= idx < len(candidate_ids)):
                    continue
                cid = candidate_ids[idx]
                reranked_ids.append(cid)
                rerank_scores[cid] = float(score)
        except RerankTimeout:
            # ★ 降级必须打点。此前这里只有 use_rerank=False，
            #   导致重排服务长期不可用而无人知晓（rollback 静默失败）。
            use_rerank = False
            metrics.incr("rag.rerank.degraded", reason="timeout")
            logger.warning(
                "rerank 降级到 vector_score",
                extra={"reason": "timeout", "trace_id": trace.get_trace_id()},
            )
        except Exception:
            # rerank 服务异常（JSON 解析错、HTTP 5xx 等）按降级处理，
            # 避免单点故障让整个检索失败（rerank 设计意图就是"失败即降级"）
            use_rerank = False
            metrics.incr("rag.rerank.degraded", reason="error")
            logger.warning(
                "rerank 异常，降级到 vector_score",
                extra={"reason": "error", "trace_id": trace.get_trace_id()},
                exc_info=True,
            )
        rerank_ms = (time.perf_counter() - t0) * 1000
        stages["rerank"] = int(rerank_ms)
        metrics.observe_ms("rag.rerank.duration", rerank_ms, mode="on" if use_rerank else "degraded")

        # ⑤ 构造 RetrievedChunk
        #    rerank 在线：候选集 = rerank 返回项，顺序即重排顺序，final=绝对 rerank 分
        #    rerank 降级：候选集 = 全部召回，final=向量余弦分，按分排序取 TOP_K
        #    ★ 治理加权（INV-M-13）：位于 rerank 之后叠加，MUST NOT 进 RRF ——
        #      否则已标定的 RELEVANCE_THRESHOLD_* 阈值口径与 eval 基线同时失效。
        #      偏移 = weight * GOVERNANCE_WEIGHT_SCALE（weight ∈ [-50,+50]，
        #      0 = 未审查/中性 —— INV-M-12：治理失败不使知识不可检索）。
        retrieved: list[RetrievedChunk] = []

        def _build_chunk(cid: uuid.UUID) -> RetrievedChunk:
            row = all_chunks[cid]
            vec_score = float(row.get("vector_score") or 0.0)
            r_score = rerank_scores.get(cid)
            base_score = r_score if r_score is not None else vec_score
            # COALESCE 在 SQL 层兜 NULL，这里再按缺省 0 兜底（INV-M-12 双保险）
            weight = float(row.get("governance_weight") or 0)
            return RetrievedChunk(
                chunk_id=cid,
                document_id=row["document_id"],
                kb_id=row["kb_id"],
                filename=filenames.get(row["document_id"], ""),
                content=row["content"],
                heading_path=row["heading_path"] or "",
                page_no=row["page_no"],
                level_rank=row["level_rank"],
                vector_score=vec_score,
                keyword_score=1.0 if cid in keyword_ranks else None,
                rrf_score=rrf_scores[cid],
                rerank_score=r_score,
                final_score=base_score + weight * decisions.GOVERNANCE_WEIGHT_SCALE,
            )

        if use_rerank:
            retrieved = [_build_chunk(cid) for cid in reranked_ids]
        else:
            retrieved = [_build_chunk(cid) for cid in candidate_ids]
            retrieved.sort(key=lambda c: c.final_score, reverse=True)
            retrieved = retrieved[: decisions.TOP_K_RERANK]

        # L1 阈值闸门
        # ★ 必须按当前打分口径取阈值：重排在线比 rerank 分，降级比向量余弦分。
        #   两者分布不同，用同一个阈值会在重排抖动时产生不可预测的拒答行为。
        threshold = decisions.active_threshold(use_rerank)
        if not retrieved or retrieved[0].final_score < threshold:
            metrics.incr(
                "rag.gate.refused",
                source="rerank" if use_rerank else "vector",
            )
            logger.info(
                "L1 闸门拒答",
                extra={
                    "threshold": threshold,
                    "top_score": retrieved[0].final_score if retrieved else None,
                    "score_source": "rerank" if use_rerank else "vector",
                    "trace_id": trace.get_trace_id(),
                },
            )
            return RetrievalResult(
                refused=True,
                refuse_reason="NO_RELEVANT_CONTENT",
                stage_ms=stages,
                score_source="rerank" if use_rerank else "vector",
                use_rerank=use_rerank,
            )

        metrics.incr("rag.gate.passed", source="rerank" if use_rerank else "vector")

        # ⑥ 大表分片回捞（表格决策 A1'）：命中纯表格分片时补捞同组其余分片。
        #    回捞片仅作上下文补充 —— 单独存放，不进排序 / 阈值 / 三路贡献统计；
        #    调用方（chat）把它追加进上下文拼装序列，编号与 citations 沿用既有机制。
        #    限流双保险：每组最多 TABLE_RECALL_MAX_CHUNKS 片、累计 token 不得超过
        #    TABLE_RECALL_TOKEN_BUDGET（超预算即停，保行序完整，不跳片拼残表）。
        recall_chunks: list[RetrievedChunk] = []
        hit_group_ids = {
            g
            for g in (
                all_chunks[c.chunk_id].get("table_group_id") for c in retrieved
            )
            if g
        }
        if hit_group_ids:
            t_recall = time.perf_counter()
            final_id_set = {c.chunk_id for c in retrieved}
            for gid in hit_group_ids:
                recall_result = await session.execute(
                    _TABLE_RECALL_SQL,
                    {
                        **pushdown,
                        "group_id": gid,
                        "recall_limit": decisions.TABLE_RECALL_MAX_CHUNKS + 50,
                    },
                )
                picked = 0
                picked_tokens = 0
                for row in recall_result.fetchall():
                    if row.id in final_id_set:
                        continue
                    if picked >= decisions.TABLE_RECALL_MAX_CHUNKS:
                        break
                    row_tokens = int(row.token_count or 0)
                    if picked_tokens + row_tokens > decisions.TABLE_RECALL_TOKEN_BUDGET:
                        break
                    recall_chunks.append(
                        RetrievedChunk(
                            chunk_id=row.id,
                            document_id=row.document_id,
                            kb_id=row.kb_id,
                            filename=filenames.get(row.document_id, ""),
                            content=row.content,
                            heading_path=row.heading_path or "",
                            page_no=row.page_no,
                            level_rank=row.level_rank,
                            vector_score=0.0,
                            keyword_score=None,
                            rrf_score=0.0,
                            rerank_score=None,
                            final_score=0.0,
                            is_table_recall=True,
                        )
                    )
                    picked += 1
                    picked_tokens += row_tokens
            if recall_chunks:
                metrics.incr(
                    "rag.table_recall.hit",
                    chunks=len(recall_chunks),
                    groups=len(hit_group_ids),
                )
            stages["table_recall"] = int((time.perf_counter() - t_recall) * 1000)

        # ⑦ 三路召回贡献打点
        #    covered = 最终 top-K 中该路召回到的条数（多路可重叠）
        #    unique  = 最终 top-K 中**只有**该路能召回到的条数
        #    口径以最终 top-K 为准，而不是各路原始候选数 —— 后者只说明"捞了多少"，
        #    前者才说明"真正被用上的有多少"。
        final_ids = {c.chunk_id for c in retrieved}
        path_sets = {
            "vector": set(vector_ranks),
            "tsquery": set(fts_ranks),
            "ilike": set(keyword_ranks),
        }
        path_stats: dict[str, int] = {}
        for name, ids in path_sets.items():
            covered = final_ids & ids
            others: set[uuid.UUID] = set()
            for other_name, other_ids in path_sets.items():
                if other_name != name:
                    others |= other_ids
            path_stats[f"{name}_covered"] = len(covered)
            path_stats[f"{name}_unique"] = len(covered - others)

        return RetrievalResult(
            chunks=retrieved,
            stage_ms=stages,
            score_source="rerank" if use_rerank else "vector",
            use_rerank=use_rerank,
            path_stats=path_stats,
            table_recall_chunks=recall_chunks,
        )


def get_retrieval_service() -> RetrievalService:
    return RetrievalService()
