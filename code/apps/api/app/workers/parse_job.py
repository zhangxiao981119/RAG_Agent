"""异步解析 worker —— arq 实现（手册 §6 M2 任务 2 / 方案 C 扫描件分段）。

入队统一走 run_parse_segment（首段 index=0），两条路径：
  · 普通文档（文本层 PDF / md / txt / xlsx / xls / docx / html / 图片）：
    首段直接「拉文件 → 解析 → 分块 → 嵌入 → 写 chunks → 成功」，
    与旧 run_parse_job 逐字节等价，不分段。
  · 纯扫描件 PDF：按 OCR_MAX_SCAN_PAGES 切段**串行** OCR —— 每段只渲染 +
    逐页转录，产出的**每页原始文本**落 parse_segments；全部段成功后由
    run_parse_finalize 按全局页序归并（跨页标题累积）→ 分块 → 嵌入 → 写 chunks。
    分段的原因：单页实测 22~180s，整份 OCR 会越过 parse_job_timeout（1800s）被杀。

失败重试 max_attempts 次后进死信（status=dead）。段结果是**已完成进度**：
重试时已 succeeded 的段直接跳过 OCR，只重跑失败的那一段（这是段落库的唯一理由）。

M6 任务 1：cron job 定时扫描所有 active 同步源（每 30 分钟）。

启动：arq app.workers.parse_job.WorkerSettings
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from math import ceil
from typing import Any

from arq import Retry
from arq.connections import RedisSettings
from arq.cron import cron
from sqlalchemy import delete, select

from app.config import decisions
from app.config.index_profile import current_index_profile
from app.config.settings import get_settings
from app.database import SessionLocal
from app.infra import trace
from app.infra.arq_pool import get_arq_pool
from app.models import Chunk, Document, ParseJob, ParseSegment
from app.services.chunk import chunk_blocks, to_embedding_text
from app.services.embedding import get_embedding_service
from app.services.governance import execute_governance_job, scan_and_enqueue
from app.services.parse import ParseError, parse_bytes_async
from app.services.parse.pdf_parser import (
    blocks_from_page_texts,
    ensure_ocr_ready,
    ensure_scanned_page_count,
    extract_pdf_page_texts,
    is_scanned_text_layer,
    ocr_scanned_pages,
)
from app.services.storage import get_storage

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _doc_ext(document: Document) -> dict[str, Any]:
    """从 session 提取后续需要的文档字段（session 外不能访问 ORM 对象）。"""
    return {
        "storage_key": document.storage_key,
        "ext": document.ext,
        "tenant_id": document.tenant_id,
        "kb_id": document.kb_id,
        "id": document.id,
        "acl_tags": list(document.acl_tags or []),
        "deny_subjects": list(document.deny_subjects or []),
        "level_rank": document.level_rank,
    }


def _segment_bounds(segment_index: int, pages_total: int) -> tuple[int, int]:
    """段序号 → 本段页区间 [start, end]（1-based 闭区间）。段长 = OCR_MAX_SCAN_PAGES。"""
    span = decisions.OCR_MAX_SCAN_PAGES
    start = segment_index * span + 1
    end = min(start + span - 1, pages_total)
    return start, end


def _segment_select(parse_job_id: uuid.UUID, segment_index: int):
    return select(ParseSegment).where(
        ParseSegment.job_id == parse_job_id,
        ParseSegment.segment_index == segment_index,
    )


async def _mark_document_failure(
    parse_job_id: uuid.UUID, exc: BaseException, settings: Any
) -> str:
    """普通路径/聚合路径的失败处理：写 last_error；重试次数用尽置 dead，
    否则置 queued 并抛 Retry（arq 按 2**attempts 秒退避）。"""
    async with SessionLocal() as fail_session:
        retry_job = await fail_session.get(ParseJob, parse_job_id)
        if retry_job is None:
            raise exc
        attempts = retry_job.attempts
        retry_job.last_error = f"{type(exc).__name__}: {exc}"
        if attempts >= settings.arq_max_attempts:
            retry_job.status = "dead"
            retry_job.finished_at = _now()
            doc = await fail_session.get(Document, retry_job.document_id)
            if doc is not None:
                doc.status = "failed"
            await fail_session.commit()
            return "dead"
        retry_job.status = "queued"
        await fail_session.commit()
        raise Retry(defer=2 ** attempts) from exc


async def _mark_segment_failure(
    parse_job_id: uuid.UUID, segment_index: int, exc: BaseException, settings: Any
) -> str:
    """段失败处理：重试计数用**段**的 attempts（段才是重试单元）。

    用尽 → 段置 dead + job 置 dead + document 置 failed（不再重试该段）。
    否则 → 段置 failed + job 置 queued，抛 Retry（arq 重跑同一段）。
    ★ 段记录一定已在本段开始前创建（见 run_parse_segment），故 attempts 总有值。
    """
    async with SessionLocal() as session:
        job = await session.get(ParseJob, parse_job_id)
        if job is None:
            raise exc
        segment = await session.scalar(_segment_select(parse_job_id, segment_index))
        attempts = segment.attempts if segment is not None else job.attempts
        err = f"{type(exc).__name__}: {exc}"
        job.last_error = err
        if segment is not None:
            segment.last_error = err
        exhausted = attempts >= settings.arq_max_attempts
        if segment is not None:
            segment.status = "dead" if exhausted else "failed"
            if exhausted:
                segment.finished_at = _now()
        if exhausted:
            job.status = "dead"
            job.finished_at = _now()
            doc = await session.get(Document, job.document_id)
            if doc is not None:
                doc.status = "failed"
            await session.commit()
            return "dead"
        job.status = "queued"
        await session.commit()
        raise Retry(defer=2 ** attempts) from exc


async def _index_document(
    parse_job_id: uuid.UUID, doc_ext: dict[str, Any], data: bytes
) -> str:
    """普通文档的完整路径：解析 → 分块 → 嵌入 → 写 chunks → 成功。

    与旧 run_parse_job 的第二、三段逐字节等价；data 由调用方下载后传入，
    避免路径判定之后重复拉取文件。
    """
    settings = get_settings()
    try:
        # ★ 统一走异步分派入口：图片扩展名转本地 OCR（WU-M-09），
        #   其余扩展名（含文本层 PDF）行为与原 parse_bytes 一致
        blocks = await parse_bytes_async(doc_ext["ext"], data)
        if not blocks:
            raise ParseError("解析结果为空（无文本内容）")

        chunks = chunk_blocks(blocks)
        if not chunks:
            raise ParseError("分块结果为空")

        embedding_service = get_embedding_service()
        # ★ 用 to_embedding_text 而非裸 content：补上章节标题前缀，
        #   否则只出现在标题里的词（如"报销标准"）向量路召不回。
        texts = [to_embedding_text(c) for c in chunks]
        vectors = await embedding_service.embed(texts)
    except Exception as exc:
        return await _mark_document_failure(parse_job_id, exc, settings)

    async with SessionLocal() as session:
        # 删除旧版本 chunks（重新索引时清理）
        await session.execute(delete(Chunk).where(Chunk.document_id == doc_ext["id"]))

        # 本次写入的索引口径（模型 / 维度 / 文本公式 / 分块参数）
        profile = current_index_profile()

        for index, (chunk, vec) in enumerate(zip(chunks, vectors, strict=True)):
            session.add(
                Chunk(
                    tenant_id=doc_ext["tenant_id"],
                    kb_id=doc_ext["kb_id"],
                    document_id=doc_ext["id"],
                    chunk_index=index,
                    content=chunk.content,
                    token_count=chunk.token_count,
                    heading_path=chunk.heading_path or None,
                    page_no=chunk.page_no,
                    level_rank=doc_ext["level_rank"],
                    acl_tags=doc_ext["acl_tags"],
                    deny_subjects=doc_ext["deny_subjects"],
                    is_latest=True,
                    table_group_id=chunk.table_group_id,
                    embedding=vec,
                    embedding_model=profile.embedding_model,
                    embedding_text_version=profile.embedding_text_version,
                    chunker_version=profile.chunker_version,
                )
            )

        job = await session.get(ParseJob, parse_job_id)
        document = await session.get(Document, doc_ext["id"])
        document.status = "indexed"
        job.status = "succeeded"
        job.finished_at = _now()
        await session.commit()
        return "succeeded"


async def _advance_segment_chain(
    parse_job_id: uuid.UUID, segment_index: int, pages_total: int, trace_id: str | None
) -> None:
    """本段成功后推进串行链：还有下一段则入队下一段，否则入队聚合任务。

    ★ 串行是硬要求：OCR 服务内部全局串行（apps/ocr 的 _infer_lock），
      若并发投递，排队等待会吃掉单页 180s 超时预算。
    """
    pool = await get_arq_pool()
    queue = get_settings().arq_queue_name
    next_index = segment_index + 1
    if next_index * decisions.OCR_MAX_SCAN_PAGES < pages_total:
        await pool.enqueue_job(
            "run_parse_segment", str(parse_job_id), next_index, trace_id,
            _queue_name=queue,
        )
    else:
        await pool.enqueue_job(
            "run_parse_finalize", str(parse_job_id), trace_id, _queue_name=queue
        )


async def run_parse_segment(
    ctx: dict[str, Any], job_id: str, segment_index: int = 0, trace_id: str | None = None
) -> str:
    """分段流水线入口。job_id 是 parse_jobs.id；trace_id 由入队方透传。

    首段（segment_index=0）先做**路径判定**：
      · 非扫描件 → 直接走完整路径（_index_document），不建段记录；
      · 扫描件 → 校验 OCR 就绪与文档级页数上限，落 pages_total，再 OCR 本段。
    后续段（segment_index>0）按 pages_total 计算本段页区间并 OCR。

    ★ 分 3 段 session：锁任务 → 网络调用（OCR，无 session）→ 写段结果。
    """
    trace.set_trace_id(trace_id or trace.new_trace_id())
    parse_job_id = uuid.UUID(job_id)
    settings = get_settings()

    # ── 第一段：锁任务 + 取文档字段 ────────────────────
    async with SessionLocal() as session:
        job = await session.get(ParseJob, parse_job_id)
        if job is None:
            logger.warning("parse_job not found, mark as dead: %s", job_id)
            return "dead"
        if job.status in ("succeeded", "dead"):
            return job.status

        job.status = "running"
        if job.started_at is None:
            job.started_at = _now()
        # job.attempts 记「整份文档解析尝试次数」：首段每次进入 +1，
        # 供 API 展示与死信口径；段级重试另由 parse_segments.attempts 计。
        if segment_index == 0:
            job.attempts += 1
        await session.commit()

        document = await session.get(Document, job.document_id)
        if document is None:
            job.status = "dead"
            job.last_error = "document not found"
            job.finished_at = _now()
            await session.commit()
            return "dead"
        doc_ext = _doc_ext(document)
        pages_total = job.pages_total

    # ── 第二段：拉文件（不在 session 中，不占连接池）──────
    try:
        storage = await get_storage()
        data = await storage.get_object(doc_ext["storage_key"])
    except Exception as exc:
        return await _mark_segment_failure(parse_job_id, segment_index, exc, settings)

    ext = str(doc_ext["ext"]).lower()

    # ── 路径判定（仅首段）─────────────────────────────
    if segment_index == 0:
        scanned = False
        page_count = 0
        if ext == "pdf":
            page_texts = extract_pdf_page_texts(data)
            scanned = is_scanned_text_layer(page_texts)
            page_count = len(page_texts)
        if not scanned:
            # 普通文档：完整路径一次跑完，不分段
            return await _index_document(parse_job_id, doc_ext, data)
        # 扫描件：落 pages_total（页区间计算依据）
        if pages_total != page_count:
            async with SessionLocal() as session:
                job = await session.get(ParseJob, parse_job_id)
                job.pages_total = page_count
                await session.commit()
        pages_total = page_count

    # ── 扫描件分段：先建/取段记录（重试计数单元），再做永久性校验 ──
    page_start, page_end = _segment_bounds(segment_index, pages_total)

    async with SessionLocal() as session:
        segment = await session.scalar(_segment_select(parse_job_id, segment_index))
        if segment is not None and segment.status == "succeeded":
            # 段已完成（上次「入队下一段」失败导致重入）：跳过 OCR，只推进链条
            already_done = True
        else:
            already_done = False
            if segment is None:
                session.add(
                    ParseSegment(
                        job_id=parse_job_id,
                        segment_index=segment_index,
                        page_start=page_start,
                        page_end=page_end,
                        status="running",
                        attempts=1,
                    )
                )
            else:
                segment.status = "running"
                segment.attempts += 1
            await session.commit()

    # ── OCR 就绪与文档级页数是**永久性**约束：不满足即 dead（重试无法自愈）──
    try:
        ensure_ocr_ready()
        ensure_scanned_page_count(pages_total)
    except ParseError as exc:
        async with SessionLocal() as session:
            job = await session.get(ParseJob, parse_job_id)
            job.status = "dead"
            job.last_error = f"{type(exc).__name__}: {exc}"
            job.finished_at = _now()
            segment = await session.scalar(_segment_select(parse_job_id, segment_index))
            if segment is not None:
                segment.status = "dead"
                segment.last_error = f"{type(exc).__name__}: {exc}"
            doc = await session.get(Document, job.document_id)
            if doc is not None:
                doc.status = "failed"
            await session.commit()
        return "dead"

    if not already_done:
        try:
            page_texts = await ocr_scanned_pages(data, page_start, page_end)
        except Exception as exc:
            return await _mark_segment_failure(
                parse_job_id, segment_index, exc, settings
            )

        # ── 第三段：写段结果 + 累加进度 ────────────────────
        async with SessionLocal() as session:
            segment = await session.scalar(_segment_select(parse_job_id, segment_index))
            segment.page_texts = page_texts
            segment.status = "succeeded"
            segment.finished_at = _now()
            job = await session.get(ParseJob, parse_job_id)
            job.pages_done += len(page_texts)
            await session.commit()

    # ── 推进串行链：下一段 / 聚合 ──────────────────────
    try:
        await _advance_segment_chain(parse_job_id, segment_index, pages_total, trace_id)
    except Exception as exc:
        return await _mark_segment_failure(parse_job_id, segment_index, exc, settings)
    return "succeeded"


async def run_parse_finalize(
    ctx: dict[str, Any], job_id: str, trace_id: str | None = None
) -> str:
    """聚合任务：全部段成功后，按全局页序归并 → 标题识别 → 分块 → 嵌入 → 写 chunks。

    ★ fail-closed：任一段缺失 / 段序号不连续 / 未 succeeded → ParseError，
      MUST NOT 拿残缺页序去入库（缺页知识会冒充完整知识）。
    ★ 标题累积只在聚合期做（blocks_from_page_texts），保证跨段标题延续。
    ★ 段结果已落库：聚合重试只重跑嵌入/写库，不重复 OCR。
    """
    trace.set_trace_id(trace_id or trace.new_trace_id())
    parse_job_id = uuid.UUID(job_id)
    settings = get_settings()

    # ── 第一段：锁任务 + 读全部段（无 IO 的归并数据）──────
    async with SessionLocal() as session:
        job = await session.get(ParseJob, parse_job_id)
        if job is None:
            logger.warning("parse_job not found, mark as dead: %s", job_id)
            return "dead"
        if job.status in ("succeeded", "dead"):
            return job.status

        job.status = "running"
        if job.started_at is None:
            job.started_at = _now()
        job.attempts += 1
        await session.commit()

        document = await session.get(Document, job.document_id)
        if document is None:
            job.status = "dead"
            job.last_error = "document not found"
            job.finished_at = _now()
            await session.commit()
            return "dead"
        doc_ext = _doc_ext(document)
        pages_total = job.pages_total
        segments = (
            await session.scalars(
                select(ParseSegment)
                .where(ParseSegment.job_id == parse_job_id)
                .order_by(ParseSegment.segment_index)
            )
        ).all()
        segment_rows = [
            (s.segment_index, s.status, list(s.page_texts or [])) for s in segments
        ]

    # ── fail-closed 校验：段数、连续性、成功态 ────────────
    expected = ceil(pages_total / decisions.OCR_MAX_SCAN_PAGES) if pages_total else 0
    problems: list[str] = []
    if len(segment_rows) != expected:
        problems.append(f"段数 {len(segment_rows)} != 期望 {expected}")
    elif [r[0] for r in segment_rows] != list(range(expected)):
        problems.append("段序号不连续")
    else:
        not_ok = [r[0] for r in segment_rows if r[1] != "succeeded"]
        if not_ok:
            problems.append(f"段 {not_ok} 未成功")
        got_pages = sum(len(r[2]) for r in segment_rows)
        if got_pages != pages_total:
            problems.append(f"归并页数 {got_pages} != pages_total {pages_total}")
    if problems:
        # 段数据不一致属**不可自愈**的状态损坏（重试读到的还是同样的段），直接 dead
        exc = ParseError("分段聚合校验失败：" + "；".join(problems))
        async with SessionLocal() as session:
            job = await session.get(ParseJob, parse_job_id)
            job.status = "dead"
            job.last_error = f"{type(exc).__name__}: {exc}"
            job.finished_at = _now()
            doc = await session.get(Document, job.document_id)
            if doc is not None:
                doc.status = "failed"
            await session.commit()
        return "dead"

    # 全局页序归并（segment_index 升序 → 段内页序即为全局页序）
    merged_texts: list[str] = [t for _, _, page_texts in segment_rows for t in page_texts]

    # ── 第二段：归并 → 标题识别 → 分块 → 嵌入（无 session）──
    try:
        blocks = blocks_from_page_texts(merged_texts)
        if not blocks:
            raise ParseError("聚合结果为空（无文本内容）")
        chunks = chunk_blocks(blocks)
        if not chunks:
            raise ParseError("分块结果为空")
        embedding_service = get_embedding_service()
        texts = [to_embedding_text(c) for c in chunks]
        vectors = await embedding_service.embed(texts)
    except Exception as exc:
        return await _mark_document_failure(parse_job_id, exc, settings)

    # ── 第三段：写 chunks + 更新状态 ──────────────────────
    async with SessionLocal() as session:
        await session.execute(delete(Chunk).where(Chunk.document_id == doc_ext["id"]))
        profile = current_index_profile()
        for index, (chunk, vec) in enumerate(zip(chunks, vectors, strict=True)):
            session.add(
                Chunk(
                    tenant_id=doc_ext["tenant_id"],
                    kb_id=doc_ext["kb_id"],
                    document_id=doc_ext["id"],
                    chunk_index=index,
                    content=chunk.content,
                    token_count=chunk.token_count,
                    heading_path=chunk.heading_path or None,
                    page_no=chunk.page_no,
                    level_rank=doc_ext["level_rank"],
                    acl_tags=doc_ext["acl_tags"],
                    deny_subjects=doc_ext["deny_subjects"],
                    is_latest=True,
                    table_group_id=chunk.table_group_id,
                    embedding=vec,
                    embedding_model=profile.embedding_model,
                    embedding_text_version=profile.embedding_text_version,
                    chunker_version=profile.chunker_version,
                )
            )
        job = await session.get(ParseJob, parse_job_id)
        document = await session.get(Document, doc_ext["id"])
        document.status = "indexed"
        job.status = "succeeded"
        job.finished_at = _now()
        await session.commit()
        return "succeeded"


async def run_sync_all(ctx: dict[str, Any]) -> int:
    """M6 任务 1：cron job 入口 — 扫描所有 active 同步源并增量同步。"""
    # cron 无上游请求，生成本轮 trace_id，贯穿本轮所有文件解析入队
    trace.set_trace_id(trace.new_trace_id())
    from app.services.sync_service import sync_all_active_sources

    new_count = await sync_all_active_sources()
    logger.info("sync.cron.complete", extra={"new_count": new_count})
    return new_count


async def run_sync_source(
    ctx: dict[str, Any], source_id: str, trace_id: str | None = None
) -> int:
    """手动触发单个同步源（HTTP trigger_sync 入队，避免阻塞 API 事件循环）。"""
    # 绑定入队方透传的 trace_id（缺失则新生成），worker 日志与触发请求同链
    trace.set_trace_id(trace_id or trace.new_trace_id())
    from app.services.sync_service import sync_source
    from app.models import SyncSource

    sync_source_id = uuid.UUID(source_id)
    async with SessionLocal() as session:
        source = await session.get(SyncSource, sync_source_id)
        if source is None:
            logger.warning("sync_source not found: %s", source_id)
            return 0
        # detach 交给 sync_source 内部自己管理 session
        session.expunge(source)

    new_count = await sync_source(source)
    logger.info("sync.manual.complete", extra={
        "source_id": source_id,
        "new_count": new_count,
    })
    return new_count


class WorkerSettings:
    """arq Worker 配置。"""
    # 方案 C：run_parse_segment（首段判定 + 段 OCR）/ run_parse_finalize（聚合入库）
    # 取代旧的整份 run_parse_job。治理任务（INV-M-10）：execute_governance_job /
    # scan_and_enqueue 独立注册，逻辑全在 services/governance，
    # 解析主路径零治理调用。
    functions = [
        run_parse_segment,
        run_parse_finalize,
        run_sync_all,
        run_sync_source,
        execute_governance_job,
        scan_and_enqueue,
    ]
    queue_name = get_settings().arq_queue_name
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_tries = get_settings().arq_max_attempts
    # ★ 不设此值时 arq 用自带默认 300s，长文档会在中途被杀
    job_timeout = get_settings().parse_job_timeout_seconds
    # M6 任务 1：每 30 分钟扫描所有 active 同步源
    cron_jobs = [
        cron(run_sync_all, minute={0, 30}),
        # M7 WU-M-15：治理审查 cron 扫描（INV-M-10 —— 审查是独立任务，
        # 解析主路径零治理调用；此处仅调度注册，逻辑全在 services/governance）
        cron(
            scan_and_enqueue,
            minute=set(range(0, 60, decisions.GOVERNANCE_SCAN_CRON_MINUTES)),
        ),
    ]
