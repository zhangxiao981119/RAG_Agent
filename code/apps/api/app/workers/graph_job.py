"""图抽取的 arq 任务入口。

★ 与解析任务的分工（INV：抽取是**独立任务**，不在解析主路径上）：
  parse_job 提交 chunks 后只负责「入队」，抽取的成败不影响文档可检索性 ——
  文档已 indexed，三路召回照常；抽取失败只是「图路对这篇暂时无结果」。

★ 三段 session（与 governance / parse_job 同款原因）：
  ① 锁状态（短事务）；② 抽取 + 落库（含 LLM 网络调用，几十秒，**不持 session**）；
  落库与状态变更在**同一个事务**里（图数据与状态必须同生共死：
  状态先变 done 而数据没写进去 = 图永久为空且补建脚本不再选中它）。

★ 幂等（设计文档 §9.4）：重复执行 = 先 delete_by_document 再 upsert，终态一致。
  另外在入口处短路：已 done 且口径与当前一致 → 直接返回，不白烧 LLM。

★ 重试语义：只重试抽取本身（不重新解析文档）。arq 的 job_try 达到
  settings.arq_max_attempts 后置 failed 并返回，不再抛 Retry。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from arq import Retry

from app.config.graph_profile import current_graph_profile
from app.config.settings import get_settings
from app.database import SessionLocal
from app.infra import metrics, trace
from app.infra.arq_pool import get_arq_pool
from app.models import Document
from app.services.graph.extract import (
    GraphExtractError,
    GraphExtractSkip,
    extract_document,
)
from app.services.graph.postgres_store import get_graph_store
from app.services.wiki.build import WikiBuildError, WikiBuildSkip

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def enqueue_graph_extract(
    document_id: uuid.UUID | str, trace_id: str | None = None
) -> bool:
    """把一篇文档的图抽取入队。返回是否真的入队（关闭能力时返回 False）。

    ★ 为什么入队失败**不能**让解析任务失败：文档已经 indexed 且可检索，
      图只是附加能力。入队失败打点 + 告警，由补建脚本兜底
      （graph_status 仍是 'pending' → backfill_graph 会选中它）。
    """
    if not get_settings().graph_enabled:
        return False
    try:
        pool = await get_arq_pool()
        await pool.enqueue_job(
            "run_graph_extract",
            str(document_id),
            trace_id or trace.get_trace_id(),
            _queue_name=get_settings().arq_queue_name,
        )
        return True
    except Exception:
        metrics.incr("graph.extract.enqueue_failed")
        logger.warning(
            "图抽取入队失败（文档仍可检索，将由补建脚本兜底）",
            extra={"document_id": str(document_id)},
            exc_info=True,
        )
        return False


async def extract_and_store(document_id: uuid.UUID) -> str:
    """抽取一篇文档并落库（**不含**状态机与重试口径）。返回 'succeeded' / 'dead'。

    单独抽出来的理由：arq 任务与补建脚本 MUST 走同一条写入路径。
    两处各写一遍「删 → 写三表 → 置 done」的话，补建脚本漏掉 delete_by_document
    就会在库里留下「重抽后不再出现的旧边」，而这种残留只在图召回的结果里才看得出来。

    ★ 本函数不处理 graph_status='running' 的前置写入与失败置 failed —— 那是调用方的
      状态机职责（arq 有重试语义，脚本是一次性执行）。
    """
    async with SessionLocal() as session:
        if await session.get(Document, document_id) is None:
            logger.warning(
                "graph extract: document not found", extra={"document_id": str(document_id)}
            )
            return "dead"
        result = await extract_document(session, document_id=document_id)

    # 同一事务写三表 + 文档状态：状态先变 done 而数据没写进去 = 图永久为空
    # 且补建脚本不再选中它（最坏的一种「静默」）。
    async with SessionLocal() as session:
        document = await session.get(Document, document_id)
        if document is None:
            logger.warning(
                "graph extract: document 在抽取期间被删除",
                extra={"document_id": str(document_id)},
            )
            return "dead"
        store = get_graph_store()
        # 先删后写：重抽后不再出现的提及/边必须消失（ON CONFLICT 合并不清旧行）
        await store.delete_by_document(session, document_id=document_id)
        written = await store.upsert_subgraph(
            session,
            tenant_id=document.tenant_id,
            kb_id=document.kb_id,
            document_id=document_id,
            result=result,
        )
        document.graph_status = "done"
        document.graph_extractor_model = result.extractor_model
        document.graph_prompt_version = result.prompt_version
        document.graph_extracted_at = _now()
        await session.commit()

    metrics.incr("graph.extract.done")
    logger.info(
        "graph.extract.done",
        extra={
            "document_id": str(document_id),
            "chunks": result.chunks_processed,
            "entities": len(result.entities),
            "mentions": len(result.mentions),
            "relations": written,
            "llm_calls": result.llm_calls,
        },
    )
    return "succeeded"


async def run_graph_extract(
    ctx: dict[str, Any], document_id: str, trace_id: str | None = None
) -> str:
    """arq 任务入口。document_id 是 documents.id 字符串。"""
    # ★ 关闭能力时**一行代码都不执行**（连 session 都不开）：
    #   关闭态必须与未引入本能力时逐字节等价（含连接池占用）。
    if not get_settings().graph_enabled:
        return "disabled"

    trace.set_trace_id(trace_id or trace.new_trace_id())
    doc_id = uuid.UUID(document_id)
    profile = current_graph_profile()
    settings = get_settings()

    # ── 第一段：锁状态 + 幂等短路 ─────────────────────────
    async with SessionLocal() as session:
        document = await session.get(Document, doc_id)
        if document is None:
            logger.warning("graph extract: document not found", extra={"document_id": document_id})
            return "dead"
        if (
            document.graph_status == "done"
            and document.graph_extractor_model == profile.extractor_model
            and document.graph_prompt_version == profile.prompt_version
        ):
            # 已按当前口径抽过 —— 重复投递（重试 / 手工重放）不该再烧一次 LLM
            return "succeeded"
        document.graph_status = "running"
        await session.commit()

    # ── 第二段：抽取 + 落库（LLM 调用期间不持 session）────
    try:
        return await extract_and_store(doc_id)
    except GraphExtractSkip as exc:
        # 明确跳过：重试无意义（内容为空 / 超护栏），且必须可见（不能静默当 done）
        async with SessionLocal() as session:
            document = await session.get(Document, doc_id)
            if document is not None:
                document.graph_status = "skipped"
                await session.commit()
        metrics.incr("graph.extract.skipped")
        logger.warning(
            "图抽取已跳过",
            extra={"document_id": document_id, "reason": str(exc)},
        )
        return "skipped"
    except Exception as exc:
        return await _mark_failed(doc_id, document_id, exc, ctx, settings)


async def _mark_failed(
    doc_id: uuid.UUID,
    document_id: str,
    exc: BaseException,
    ctx: dict[str, Any],
    settings: Any,
) -> str:
    """失败处理：置 documents.graph_status='failed'，未用尽重试则抛 Retry。

    ★ 「置 failed」与「抛 Retry」顺序不能反：抛 Retry 后本次运行就结束了，
      状态若还停在 running，看板上会永远显示「抽取中」（卡死的假象）。
    """
    async with SessionLocal() as session:
        document = await session.get(Document, doc_id)
        if document is not None:
            document.graph_status = "failed"
            await session.commit()

    job_try = int(ctx.get("job_try", 1) or 1)
    metrics.incr("graph.extract.failed")
    logger.warning(
        "图抽取失败",
        extra={
            "document_id": document_id,
            "attempt": job_try,
            "error": f"{type(exc).__name__}: {exc}",
        },
        exc_info=not isinstance(exc, GraphExtractError),
    )
    if job_try < int(settings.arq_max_attempts):
        raise Retry(defer=2**job_try) from exc
    logger.error(
        "图抽取重试用尽，转人工处理（文档仍可检索）",
        extra={"document_id": document_id},
    )
    return "failed"


# ══ wiki 导航页（第 3 步）══════════════════════════════════════════


async def enqueue_wiki_build(
    document_id: uuid.UUID | str, trace_id: str | None = None
) -> bool:
    """把一篇文档的 wiki 页生成入队。返回是否真的入队（关闭能力时返回 False）。

    ★ 与 enqueue_graph_extract 同款纪律：入队失败**不能**让解析任务失败 ——
      文档已经 indexed 且可检索，wiki 只是附加能力。入队失败打点 + 告警，
      由 rebuild_wiki 脚本兜底。
    ★ 注意 wiki 页清理在 purge_derived 里是**无条件**的（守卫是表存在）：
      重解析后页已被删掉，这里不入队 = 这篇文档的页消失直到 rebuild_wiki 重跑。
      所以 WIKI_ENABLED=true 时这条路径 MUST 被走到。
    """
    if not get_settings().wiki_enabled:
        return False
    try:
        pool = await get_arq_pool()
        await pool.enqueue_job(
            "run_wiki_build",
            str(document_id),
            trace_id or trace.get_trace_id(),
            _queue_name=get_settings().arq_queue_name,
        )
        return True
    except Exception:
        metrics.incr("wiki.build.enqueue_failed")
        logger.warning(
            "wiki 生成入队失败（文档仍可检索，将由 rebuild_wiki 兜底）",
            extra={"document_id": str(document_id)},
            exc_info=True,
        )
        return False


async def build_and_store(document_id: uuid.UUID) -> str:
    """生成一篇文档的 wiki 页并落库（**不含**幂等判定与重试口径）。

    单独抽出来的理由与 extract_and_store 相同：arq 任务与 rebuild_wiki 脚本
    MUST 走同一条写入路径 —— 两处各写一遍「DELETE + INSERT」必然漂移。

    ★ 三段 session（与图抽取同款原因）：① 读输入 + 幂等判定（短事务）；
      ② LLM 调用（**不持 session**）；③ DELETE 旧页 + INSERT 新页（单事务提交）。
    """
    from app.services.wiki.build import (
        prepare_wiki_input,
        generate_page_content,
        record_failed_page,
        write_wiki_page,
    )

    # ① 读输入 + 幂等判定（§9.4：四元组相同 → 跳过，不调 LLM）
    async with SessionLocal() as session:
        document = await session.get(Document, document_id)
        if document is None:
            logger.warning(
                "wiki build: document not found", extra={"document_id": str(document_id)}
            )
            return "dead"
        wiki_input, idempotent = await prepare_wiki_input(session, document_id=document_id)

    if idempotent:
        metrics.incr("wiki.build.skipped_idempotent")
        logger.info(
            "wiki build: 幂等命中，跳过（不调 LLM）",
            extra={"document_id": str(document_id), "content_hash": wiki_input.content_hash},
        )
        # ★ 返回可区分值（不是 "succeeded"）：rebuild_wiki 的汇总会把它计入
        #   「跳过」—— 运维看到 成功 N / 跳过 M 才能知道哪些文档没烧 LLM。
        return "skipped_idempotent"

    # ② LLM 调用（不持 session —— 见模块 docstring 的 idle-in-transaction 教训）
    try:
        generated = await generate_page_content(wiki_input)
    except WikiBuildError:
        # 失败留痕（status='failed'）：rebuild_wiki --stale-only 与管理端统计
        # 由此看见「生成了但没成」；已有 ready 页不动（旧页仍真实，优于清零）
        async with SessionLocal() as session:
            await record_failed_page(session, document_id=document_id,
                                     content_hash=wiki_input.content_hash)
        raise

    # ③ 落页：DELETE 旧页 + INSERT 新页，同一事务
    async with SessionLocal() as session:
        document = await session.get(Document, document_id)
        if document is None:
            logger.warning(
                "wiki build: document 在生成期间被删除",
                extra={"document_id": str(document_id)},
            )
            return "dead"
        await write_wiki_page(session, document=document, wiki_input=wiki_input,
                              generated=generated)
        await session.commit()

    metrics.incr("wiki.build.done")
    logger.info(
        "wiki.build.done",
        extra={
            "document_id": str(document_id),
            "sections": len(generated.sections),
            "key_terms": len(generated.key_terms),
            "llm_calls": generated.llm_calls,
        },
    )
    return "succeeded"


async def run_wiki_build(
    ctx: dict[str, Any], document_id: str, trace_id: str | None = None
) -> str:
    """arq 任务入口。document_id 是 documents.id 字符串。

    ★ 关闭能力时**一行代码都不执行**（连 session 都不开）：
      关闭态必须与未引入本能力时逐字节等价。
    ★ 重试语义与图抽取对齐：LLM 瞬时失败按 arq 重试（只重试生成本身，
      文档已 indexed，与解析解耦）；无分片（skip）重试无意义，直接返回。
    """
    if not get_settings().wiki_enabled:
        return "disabled"

    trace.set_trace_id(trace_id or trace.new_trace_id())
    doc_id = uuid.UUID(document_id)
    settings = get_settings()

    try:
        return await build_and_store(doc_id)
    except WikiBuildSkip as exc:
        metrics.incr("wiki.build.skipped")
        logger.warning(
            "wiki 生成已跳过",
            extra={"document_id": document_id, "reason": str(exc)},
        )
        return "skipped"
    except Exception as exc:
        job_try = int(ctx.get("job_try", 1) or 1)
        metrics.incr("wiki.build.failed")
        logger.warning(
            "wiki 生成失败",
            extra={
                "document_id": document_id,
                "attempt": job_try,
                "error": f"{type(exc).__name__}: {exc}",
            },
            exc_info=not isinstance(exc, WikiBuildError),
        )
        if job_try < int(settings.arq_max_attempts):
            raise Retry(defer=2**job_try) from exc
        logger.error(
            "wiki 生成重试用尽，转人工处理（rebuild_wiki 可重跑；文档仍可检索）",
            extra={"document_id": document_id},
        )
        return "failed"
