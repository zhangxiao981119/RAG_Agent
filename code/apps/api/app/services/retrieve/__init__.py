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

import time
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import decisions
from app.services.acl.visibility import pushdown_params  # noqa: F401  (外部引用，保留导出)
from app.services.embedding import get_embedding_service
from app.services.retrieve.base import RetrievalResult, RetrievedChunk
from app.services.rerank import RerankTimeout, get_rerank_service

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
_VECTOR_SQL_TEMPLATE = f"""
SELECT c.id, c.document_id, c.kb_id, c.content, c.heading_path, c.page_no,
       c.level_rank, c.acl_tags,
       1 - (c.embedding <=> '{{query_vec}}'::vector) AS vector_score
FROM chunks c
{_WHERE_BASE}
  AND c.embedding IS NOT NULL
ORDER BY c.embedding <=> '{{query_vec}}'::vector
LIMIT :limit
"""

# 关键词召回（ILIKE 降级版，D-06）
_KEYWORD_SQL = text(f"""
SELECT c.id, c.document_id, c.kb_id, c.content, c.heading_path, c.page_no,
       c.level_rank, c.acl_tags
FROM chunks c
{_WHERE_BASE}
  AND (c.content ILIKE :pattern OR c.heading_path ILIKE :pattern)
LIMIT :limit
""")

# 取 chunk 的 filename（JOIN documents）
_FILENAME_SQL = text("""
SELECT id, filename FROM documents WHERE id = ANY(:doc_ids)
""")


def _build_rrf_scores(
    vector_ranks: list[uuid.UUID],
    keyword_ranks: list[uuid.UUID],
) -> dict[uuid.UUID, float]:
    """RRF 融合。k=60。分数 = 1/(k+rank)。"""
    scores: dict[uuid.UUID, float] = {}
    for rank, cid in enumerate(vector_ranks):
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (decisions.RRF_K + rank + 1)
    for rank, cid in enumerate(keyword_ranks):
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (decisions.RRF_K + rank + 1)
    return scores


def _normalize_rerank_scores(scores: list[float]) -> list[float]:
    """重排分归一化到 0~1（min-max）。"""
    if not scores:
        return []
    lo, hi = min(scores), max(scores)
    if hi - lo < 1e-9:
        return [1.0 for _ in scores]
    return [(s - lo) / (hi - lo) for s in scores]


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
        vector_result = await session.execute(
            vector_sql,
            {**pushdown, "limit": decisions.TOP_K_RECALL},
        )
        vector_rows = vector_result.fetchall()
        stages["retrieving"] = int((time.perf_counter() - t0) * 1000)

        # ② 关键词召回
        t0 = time.perf_counter()
        pattern = f"%{query[:200]}%"
        keyword_result = await session.execute(
            _KEYWORD_SQL,
            {**pushdown, "pattern": pattern, "limit": decisions.TOP_K_RECALL},
        )
        keyword_rows = keyword_result.fetchall()
        stages["keyword"] = int((time.perf_counter() - t0) * 1000)

        # 合并所有 chunk（去重）
        all_chunks: dict[uuid.UUID, dict] = {}
        vector_ranks: list[uuid.UUID] = []
        for row in vector_rows:
            all_chunks[row.id] = dict(row._mapping)
            vector_ranks.append(row.id)
        keyword_ranks: list[uuid.UUID] = []
        for row in keyword_rows:
            if row.id not in all_chunks:
                all_chunks[row.id] = dict(row._mapping)
            keyword_ranks.append(row.id)

        if not all_chunks:
            return RetrievalResult(refused=True, refuse_reason="NO_RELEVANT_CONTENT", stage_ms=stages)

        # ③ RRF 融合
        rrf_scores = _build_rrf_scores(vector_ranks, keyword_ranks)
        # 按 RRF 降序取 top 50（去重后的候选）
        sorted_ids = sorted(all_chunks.keys(), key=lambda cid: rrf_scores[cid], reverse=True)
        candidate_ids = sorted_ids[: decisions.TOP_K_RECALL]

        # 取 filename
        doc_ids = list({all_chunks[cid]["document_id"] for cid in candidate_ids})
        filename_result = await session.execute(_FILENAME_SQL, {"doc_ids": doc_ids})
        filenames = {row.id: row.filename for row in filename_result.fetchall()}

        # ④ rerank
        t0 = time.perf_counter()
        rerank_scores: dict[uuid.UUID, float] = {}
        use_rerank = True
        try:
            rerank_service = get_rerank_service()
            documents = [all_chunks[cid]["content"] for cid in candidate_ids]
            results = await rerank_service.rerank(query, documents, decisions.TOP_K_RERANK)
            # results 是 [(原 index, 分数)]，按 index 升序对齐到 candidate_ids
            results_sorted = sorted(results, key=lambda x: x[0])
            raw_scores = [s for _, s in results_sorted]
            norm_scores = _normalize_rerank_scores(raw_scores)
            for (idx, _), norm in zip(results_sorted, norm_scores, strict=False):
                rerank_scores[candidate_ids[idx]] = norm
        except RerankTimeout:
            use_rerank = False
        stages["rerank"] = int((time.perf_counter() - t0) * 1000)

        # ⑤ 构造 RetrievedChunk 并按 final_score 排序
        #    final_score 优先级：rerank 分 > vector_score（rerank 超时降级）
        retrieved: list[RetrievedChunk] = []
        for cid in candidate_ids:
            row = all_chunks[cid]
            vec_score = float(row.get("vector_score") or 0.0)
            rrf_score = rrf_scores[cid]
            r_score = rerank_scores.get(cid) if use_rerank else None
            # rerank 超时降级：用向量余弦分（0~1），RRF 分只是排名融合不是相似度
            final = r_score if r_score is not None else vec_score
            retrieved.append(
                RetrievedChunk(
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
                    rrf_score=rrf_score,
                    rerank_score=r_score,
                    final_score=final,
                )
            )
        retrieved.sort(key=lambda c: c.final_score, reverse=True)
        retrieved = retrieved[: decisions.TOP_K_RERANK]

        # L1 阈值闸门
        if not retrieved or retrieved[0].final_score < decisions.RELEVANCE_THRESHOLD:
            return RetrievalResult(refused=True, refuse_reason="NO_RELEVANT_CONTENT", stage_ms=stages)

        return RetrievalResult(chunks=retrieved, stage_ms=stages)


def get_retrieval_service() -> RetrievalService:
    return RetrievalService()
