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
import time
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import decisions
from app.config.index_profile import current_index_profile
from app.config.settings import get_settings
from app.infra import metrics, trace
from app.services.acl.visibility import pushdown_params  # noqa: F401  (外部引用，保留导出)
from app.services.embedding import get_embedding_service
from app.services.keyword_text import (
    build_fts_query,
    build_ilike_pattern,
    build_ilike_terms,
)
from app.services.lexicon import alias_index_tokens, load_alias_map
from app.services.retrieve.base import RetrievalResult, RetrievedChunk
from app.services.retrieve.chain import (
    ChainDecomposeError,
    decompose_question,
    merge_rounds,
)
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

# 关键词召回（ILIKE / 正则子串匹配）
# ★ 此前是「整句子串」：自然问句整句几乎不可能原样出现在 chunk 里，实测 206 条评测
#   用例 0 命中，还白花约 11ms 全表扫描。改为「合格词元的正则 OR」（keyword_text.
#   build_ilike_terms）：中文按疑问/功能词切成语义单元（>= 2 字，取最长 3 个）、
#   ASCII 标识符/长词，单次扫描同时覆盖 content 与 heading_path。
# ★ 用 ~* 而非 ILIKE：多词元 OR 若用多条 ILIKE 要写成 N 个分支（每支一次匹配），
#   正则一条即可；词元经 Python re.escape，不存在通配/正则注入。
# ★ ORDER BY：LIMIT 50 若不带排序，取的是堆表顺序 —— 命中集里哪 50 条进 RRF 完全
#   随机（评测不可复现）。按**最长词元**的首次出现位置升序；正文未命中用标题位置
#   兜底，两处都没有落 2147483647 排最后。
#   :needle 是最长词元的小写原文，专供 strpos 使用（strpos 不做通配语义）。
_KEYWORD_SQL = text(f"""
SELECT c.id, c.document_id, c.kb_id, c.content, c.heading_path, c.page_no,
       c.level_rank, c.acl_tags, c.table_group_id,
       COALESCE(c.governance_weight, 0) AS governance_weight
FROM chunks c
{_WHERE_BASE}
  AND (c.content ~* :pattern OR c.heading_path ~* :pattern)
ORDER BY LEAST(
    CASE WHEN strpos(lower(c.content), :needle) > 0
         THEN strpos(lower(c.content), :needle) ELSE 2147483647 END,
    CASE WHEN strpos(lower(c.heading_path), :needle) > 0
         THEN strpos(lower(c.heading_path), :needle) ELSE 2147483647 END
), c.id
LIMIT :limit
""")

# ★ 关键词召回（tsquery 路径）—— 走迁移 0020 新建的 idx_chunk_fts_vec
#   收益：索引可用 + 词元级匹配（不再只靠子串），且 ts_rank 提供真实排序分。
#   过滤与排序都读 fts_vec（生成列 = to_tsvector('simple', fts_text)）：
#   fts_text 是入库侧预切分的中文 2-gram + ASCII 整词，PostgreSQL 'simple'
#   配置本身不切中文，直接索引 content 时中文恒不命中。
#   ★ MUST 读 fts_vec 而不是每次 to_tsvector(fts_text)：ts_rank 需要 tsvector，
#     表达式索引不返回它，会退化成对每条命中行重算 + detoast 原文（实测 142ms，
#     命中数百行时）；读存储列后同一查询 1.9ms。
#   ★ to_tsquery 必须**内联**写两遍，不能写成 `FROM chunks c, to_tsquery(...) AS q(query)`：
#     @@ 右侧一旦是函数扫描产出的列变量，planner 无法把它当常量去匹配 GIN 索引，
#     索引条件会退化为每行 Filter。
_KEYWORD_FTS_SQL = text(f"""
SELECT c.id, c.document_id, c.kb_id, c.content, c.heading_path, c.page_no,
       c.level_rank, c.acl_tags, c.table_group_id,
       COALESCE(c.governance_weight, 0) AS governance_weight,
       ts_rank(c.fts_vec, to_tsquery('simple', :tsquery)) AS keyword_score
FROM chunks c
{_WHERE_BASE}
  AND c.fts_vec @@ to_tsquery('simple', :tsquery)
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

# 章节上下文补全（长文档 RAG）：按 (document_id, heading_path) 取同节全部分片。
# 定位精准靠细粒度分片，逻辑完整靠把命中片所在的整节补回上下文 —— 只切碎不补全，
# 模型看到的是"半句话条款"。
# ★ 一条 SQL 覆盖所有命中小节：用 VALUES 拼出 (document_id, heading_path) 集合，
#   行值 IN 子查询命中 idx_chunk_section；不按小节发 N 次查询（最多 8 条往返）。
# ★ 模板里 where_base 复用 _WHERE_BASE（别名同为 c），四闸门与主召回逐字一致 ——
#   补全片和命中片必须同权限，否则缺口子给越权内容开了后门。
# ★ 排序固定为 (document_id, heading_path, chunk_index) 只为分组稳定；
#   真正的选取顺序在 Python 侧按命中片分数顺序逐节分配预算。
_SECTION_RECALL_SQL_TEMPLATE = """
SELECT c.id, c.document_id, c.kb_id, c.content, c.heading_path, c.page_no,
       c.level_rank, c.chunk_index, c.token_count
FROM chunks c
{where_base}
  AND (c.document_id, c.heading_path) IN (
      SELECT v.document_id, v.heading_path
      FROM (VALUES {values}) AS v(document_id, heading_path)
  )
ORDER BY c.document_id, c.heading_path, c.chunk_index
LIMIT :recall_limit
"""

# 查询侧的 tsquery 构造在 keyword_text 模块 —— 与入库侧的 to_fts_text 共用切分规则，
# 中文 bigram 才能与索引里的 token 对上（各写一份必然静默 0 召回）。


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

        # ① 向量召回（embedding 调用与向量 SQL 分段计时：两者成本量级不同，
        #    合并成一个 retrieving 无法判断瓶颈究竟是模型往返还是 HNSW 查询）
        t0 = time.perf_counter()
        embedding_service = get_embedding_service()
        query_vec = await embedding_service.embed_query(query)
        embed_ms = (time.perf_counter() - t0) * 1000
        # pgvector 输入格式："[0.1,0.2,...]" 字符串，直接嵌入 SQL
        query_vec_str = "[" + ",".join(repr(float(x)) for x in query_vec) + "]"
        vector_sql = text(_VECTOR_SQL_TEMPLATE.format(query_vec=query_vec_str))
        # 按当前索引口径过滤：只召回与本查询向量同构（同模型 + 同文本公式）的 chunk
        profile = current_index_profile()
        t_vec = time.perf_counter()
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
        vector_ms = (time.perf_counter() - t_vec) * 1000
        stages["embed"] = int(embed_ms)
        stages["vector_sql"] = int(vector_ms)
        stages["retrieving"] = int(embed_ms + vector_ms)
        metrics.observe_ms("rag.retrieve.embed", embed_ms)
        metrics.observe_ms("rag.retrieve.vector_sql", vector_ms)

        # ② 关键词召回（双路径：ILIKE 子串 + tsquery 全文索引）
        #    两路分开计时：ILIKE 是顺序扫描（随数据量线性变差），
        #    tsquery 走 GIN 索引，混在一起会把两路的成本差异互相掩盖。
        t0 = time.perf_counter()
        ilike_terms = build_ilike_terms(query)
        # 领域词表扩展（kb_terms）：命中词元补另一侧写法，ILIKE 正则与 tsquery 共用。
        # 一次精确键查询（jsonb ?|），词表再大也只回命中行。
        alias_map: dict[str, list[str]] = {}
        if ilike_terms and decisions.ALIAS_EXPANSION_ENABLED:
            t_lex = time.perf_counter()
            alias_map = await load_alias_map(session, authorized_kb_ids, ilike_terms)
            lexicon_ms = (time.perf_counter() - t_lex) * 1000
            stages["lexicon"] = int(lexicon_ms)
            metrics.observe_ms("rag.retrieve.lexicon", lexicon_ms)
            if alias_map:
                metrics.incr("rag.lexicon.expand", len(alias_map))
        # 词表查询单独计时，ILIKE 段只算自身 SQL —— 否则词表往返会被算进子串扫
        t0 = time.perf_counter()
        keyword_rows: list = []
        if ilike_terms:
            # 词元已按长度降序，第一个即最长词元，用作排序 needle
            keyword_result = await session.execute(
                _KEYWORD_SQL,
                {
                    **pushdown,
                    # 型号词元由 build_ilike_pattern 展开可选分隔符
                    # （`xyz[-_ ]?1000`），其余词元只做 re.escape；
                    # 词表别名作为同一逻辑词元的分支并列 OR
                    "pattern": build_ilike_pattern(ilike_terms, alias_map),
                    "needle": ilike_terms[0].lower(),
                    "limit": decisions.TOP_K_RECALL,
                },
            )
            keyword_rows = list(keyword_result.fetchall())
        ilike_ms = (time.perf_counter() - t0) * 1000
        stages["keyword_ilike"] = int(ilike_ms)
        metrics.observe_ms("rag.retrieve.keyword_ilike", ilike_ms)

        # ★ tsquery 路径：走 idx_chunk_fts_vec，中文按 3 字窗口、ASCII 按整词
        fts_rows = []
        fts_ms = 0.0
        tsquery = (
            build_fts_query(query, alias_index_tokens(alias_map))
            if decisions.KEYWORD_TSQUERY_ENABLED
            else None
        )
        if tsquery:
            t_fts = time.perf_counter()
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
            finally:
                fts_ms = (time.perf_counter() - t_fts) * 1000
                stages["keyword_fts"] = int(fts_ms)
                # 采集到的失败耗时同样计入：失败路径的耗时也是真实成本
                metrics.observe_ms("rag.retrieve.keyword_fts", fts_ms)
        stages["keyword"] = int(ilike_ms + fts_ms)

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
        rerank_off = not get_settings().rerank_enabled
        if rerank_off:
            # ★ 重排显式关闭（RERANK_ENABLED=false）：不发起调用，直接走 vector 口径。
            #   关闭 ≠ 降级 —— 故不计 rag.rerank.degraded、不刷降级告警，消除误导性日志。
            #   依据：本机 CPU 实测 bge-reranker-v2-m3 单次 8 条 10.5s、50 条 66s，
            #   远超 rerank_timeout_seconds(3s)，开启也必然逐次降级（白等 3s 后才降级，
            #   链式多轮时按轮数放大到 9s 级）。换到 GPU / 更强算力后置 true 即可启用。
            use_rerank = False
        else:
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
        # mode 三态：off（配置关闭，无耗时） / on（重排生效） / degraded（发起后失败降级）
        metrics.observe_ms(
            "rag.rerank.duration", rerank_ms,
            mode="off" if rerank_off else ("on" if use_rerank else "degraded"),
        )

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
        # 已入选片集合：表格回捞与章节补全都用它排除重复（两处共用，故在块外构造）
        final_id_set = {c.chunk_id for c in retrieved}
        hit_group_ids = {
            g
            for g in (
                all_chunks[c.chunk_id].get("table_group_id") for c in retrieved
            )
            if g
        }
        if hit_group_ids:
            t_recall = time.perf_counter()
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
                    # 进集合：后面的章节补全不得再把同一片补一次
                    final_id_set.add(row.id)
                    picked += 1
                    picked_tokens += row_tokens
            if recall_chunks:
                metrics.incr(
                    "rag.table_recall.hit",
                    chunks=len(recall_chunks),
                    groups=len(hit_group_ids),
                )
            recall_ms = (time.perf_counter() - t_recall) * 1000
            stages["table_recall"] = int(recall_ms)
            metrics.observe_ms("rag.retrieve.table_recall", recall_ms)

        # ⑦ 章节上下文补全（长文档 RAG）：命中片所在小节的其他分片补进上下文，
        #    保证条款前后逻辑完整（只切片不补全 = 模型拿到半句话）。
        #    与表格回捞同款 —— 单独存放，不进排序 / 阈值 / 三路贡献统计；
        #    调用方把它追加进上下文拼装序列，编号与 citations 沿用既有机制。
        #    ★ 表格分片跳过：它已走组回捞，再按小节补一次会让同一片进上下文两遍。
        section_chunks: list[RetrievedChunk] = []
        hit_sections: list[tuple[uuid.UUID, str]] = []
        seen_sections: set[tuple[uuid.UUID, str]] = set()
        for chunk in retrieved:
            heading = (chunk.heading_path or "").strip()
            if len(heading) < decisions.SECTION_RECALL_MIN_HEADING_CHARS:
                continue
            if all_chunks[chunk.chunk_id].get("table_group_id"):
                continue
            key = (chunk.document_id, heading)
            if key in seen_sections:
                continue
            seen_sections.add(key)
            hit_sections.append(key)

        if hit_sections:
            t_section = time.perf_counter()
            recall_params: dict = {
                **pushdown,
                # 每节上限 + 余量：超限行由 Python 侧按预算截断
                "recall_limit": decisions.SECTION_RECALL_MAX_CHUNKS * len(hit_sections) + 50,
            }
            for i, (doc_id, heading) in enumerate(hit_sections):
                recall_params[f"doc_{i}"] = doc_id
                recall_params[f"heading_{i}"] = heading
            # ★ 用 CAST(...) 而不是 :param::type 写法：SQLAlchemy 解析绑定参数的正则
            #   带 (?!:) 负向断言，`:doc_0::uuid` 这种「参数名后紧跟 ::」不会被识别成
            #   绑定参数，会原样发给 asyncpg 报 syntax error at or near ":"。
            values_sql = ", ".join(
                f"(CAST(:doc_{i} AS uuid), CAST(:heading_{i} AS text))"
                for i in range(len(hit_sections))
            )
            section_result = await session.execute(
                text(
                    _SECTION_RECALL_SQL_TEMPLATE.format(
                        where_base=_WHERE_BASE, values=values_sql
                    )
                ),
                recall_params,
            )
            rows_by_section: dict[tuple[uuid.UUID, str], list] = {}
            for row in section_result.fetchall():
                rows_by_section.setdefault(
                    (row.document_id, row.heading_path), []
                ).append(row)

            # 按命中片分数顺序逐节分配预算：高分小节先补全，预算耗尽即停
            budget_left = decisions.SECTION_RECALL_TOKEN_BUDGET
            for key in hit_sections:
                picked = 0
                for row in rows_by_section.get(key, []):
                    if row.id in final_id_set:
                        continue
                    if picked >= decisions.SECTION_RECALL_MAX_CHUNKS:
                        break
                    row_tokens = int(row.token_count or 0)
                    if row_tokens > budget_left:
                        break
                    section_chunks.append(
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
                            is_section_recall=True,
                        )
                    )
                    final_id_set.add(row.id)
                    picked += 1
                    budget_left -= row_tokens
                if budget_left <= 0:
                    break

            if section_chunks:
                # 不带 tags：补回片数/小节数是连续量，做标签会让时序基数无界
                metrics.incr("rag.section_recall.hit")
            section_ms = (time.perf_counter() - t_section) * 1000
            stages["section_recall"] = int(section_ms)
            metrics.observe_ms("rag.retrieve.section_recall", section_ms)

        # ⑧ 三路召回贡献打点
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
            section_recall_chunks=section_chunks,
        )

    async def retrieve_chain(
        self,
        session: AsyncSession,
        query: str,
        tenant_id: uuid.UUID,
        authorized_kb_ids: list[uuid.UUID],
        clearance: int,
        user_subjects: list[str],
        rewrite_score: int | None = None,
    ) -> RetrievalResult:
        """链式检索：按复杂度信号决定是否拆解 → 多轮检索 → 证据合并。

        · 不拆解（信号不满足 / 开关关闭）→ 直接单趟 retrieve()，chain_mode=single/off
        · 拆解失败（ChainDecomposeError）→ 降级单趟并打点，不阻断主流程
        · 拆解后仍只有一个子问题 → 等价单趟，不发多轮（省成本）
        · 多轮 MUST **串行**复用同一 session（AsyncSession 非并发安全）

        参数是 pushdown_params 的直接展开（与 retrieve 同口径），额外接收
        rewrite_score 作为「问题复杂度」信号。
        """
        t0 = time.perf_counter()

        async def _single(chain_mode: str, chain_ms: int | None = None) -> RetrievalResult:
            # 单趟路径统一出口：复用既有 retrieve()，只补 chain_mode 与拆解耗时
            result = await self.retrieve(
                session, query, tenant_id, authorized_kb_ids, clearance, user_subjects,
            )
            result.chain_mode = chain_mode
            if chain_ms is not None:
                result.stage_ms["chain_decompose"] = chain_ms
            return result

        if not decisions.should_use_chain(rewrite_score):
            # 开关关闭 → "off"；信号不满足（分数够高 / 拿不到分数）→ "single"
            return await _single("single" if decisions.CHAIN_RETRIEVAL_ENABLED else "off")

        try:
            subquestions = await decompose_question(query, tenant_id)
        except ChainDecomposeError as exc:
            # 拆解不可用 → 降级单趟 + 打点（拆解是增强项，不得阻断问答主链路）
            metrics.incr("rag.chain.degraded", reason="decompose_error")
            logger.info(
                "检索链式推理: 拆解不可用，降级单趟 (%s)", exc,
                extra={"trace_id": trace.get_trace_id()},
            )
            return await _single("single")

        decompose_ms = int((time.perf_counter() - t0) * 1000)

        if len(subquestions) <= 1:
            # 拆解判定问题已足够聚焦 → 单趟即可，多轮是纯浪费
            metrics.incr("rag.chain.single")
            return await _single("single", decompose_ms)

        # 多轮 MUST 串行复用同一 session：AsyncSession 非并发安全 —— 同 session 并发
        # 会让连接被多协程交叉使用（await 点交错），结果不可预期。
        rounds: list[RetrievalResult] = []
        for sq in subquestions:
            rounds.append(await self.retrieve(
                session, sq, tenant_id, authorized_kb_ids, clearance, user_subjects,
            ))

        merged = merge_rounds(rounds)
        merged.chain_mode = f"multi:{len(subquestions)}"
        merged.subquestions = subquestions
        merged.stage_ms["chain_decompose"] = decompose_ms

        metrics.incr("rag.chain.enabled")
        metrics.incr("rag.chain.subquestions", len(subquestions))
        metrics.incr("rag.chain.rounds", len(rounds))
        metrics.incr("rag.chain.merged", len(merged.chunks))
        logger.info(
            "检索链式推理: %d 个子问题 / %d 轮 / 合并 %d 条 / 拒答=%s",
            len(subquestions), len(rounds), len(merged.chunks), merged.refused,
            extra={"trace_id": trace.get_trace_id()},
        )
        return merged


def get_retrieval_service() -> RetrievalService:
    return RetrievalService()
