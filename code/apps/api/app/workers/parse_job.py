"""异步解析 worker —— arq 实现（手册 §6 M2 任务 2）。

流程：
  parse_job 入队 → 拉文件 → 解析 → 分块 → 嵌入 → 写 chunks → 更新 status
失败重试 max_attempts 次后进死信（status=dead）。

M6 任务 1：cron job 定时扫描所有 active 同步源（每 30 分钟）。

启动：arq app.workers.parse_job.WorkerSettings
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from arq import Retry
from arq.connections import RedisSettings
from arq.cron import cron
from sqlalchemy import delete

from app.config.settings import get_settings
from app.database import SessionLocal
from app.models import Chunk, Document, ParseJob
from app.services.chunk import chunk_blocks
from app.services.embedding import get_embedding_service
from app.services.parse import ParseError, parse_bytes
from app.services.storage import get_storage

logger = logging.getLogger(__name__)


async def run_parse_job(
    ctx: dict[str, Any], job_id: str
) -> str:
    """arq 任务入口。job_id 是 parse_jobs.id 字符串。

    分 3 段 session：锁任务 → 网络调用（无 session）→ 写 chunks。
    网络调用（拉 MinIO 文件 + 调 embedding 服务）可能几十秒，
    放在 session 外避免长时间占用 DB 连接池。
    """
    parse_job_id = uuid.UUID(job_id)
    settings = get_settings()

    # ── 第一段：锁任务 + 校验 document ────────────────────
    async with SessionLocal() as session:
        job = await session.get(ParseJob, parse_job_id)
        if job is None:
            logger.warning("parse_job not found, mark as dead: %s", job_id)
            return "dead"
        if job.status in ("succeeded", "dead"):
            return job.status

        job.status = "running"
        job.started_at = datetime.now(timezone.utc)
        job.attempts += 1
        await session.commit()

        document = await session.get(Document, job.document_id)
        if document is None:
            job.status = "dead"
            job.last_error = "document not found"
            job.finished_at = datetime.now(timezone.utc)
            await session.commit()
            return "dead"

        # 从 session 提取后续需要的字段（session 外不能访问 document 对象）
        doc_ext = {
            "storage_key": document.storage_key,
            "ext": document.ext,
            "tenant_id": document.tenant_id,
            "kb_id": document.kb_id,
            "id": document.id,
            "acl_tags": list(document.acl_tags or []),
            "deny_subjects": list(document.deny_subjects or []),
            "level_rank": document.level_rank,
        }

    # ── 第二段：网络调用（不在 session 中，不占连接池）──────
    try:
        storage = await get_storage()
        data = await storage.get_object(doc_ext["storage_key"])

        blocks = parse_bytes(doc_ext["ext"], data)
        if not blocks:
            raise ParseError("解析结果为空（无文本内容）")

        chunks = chunk_blocks(blocks)
        if not chunks:
            raise ParseError("分块结果为空")

        embedding_service = get_embedding_service()
        texts = [c.content for c in chunks]
        vectors = await embedding_service.embed(texts)

    except Exception as exc:
        # 网络/解析/嵌入异常 —— 开新 session 写回失败状态 + 重试计数
        async with SessionLocal() as fail_session:
            retry_job = await fail_session.get(ParseJob, parse_job_id)
            if retry_job is None:
                raise
            retry_job.last_error = f"{type(exc).__name__}: {exc}"
            if retry_job.attempts >= settings.arq_max_attempts:
                retry_job.status = "dead"
                retry_job.finished_at = datetime.now(timezone.utc)
                doc = await fail_session.get(Document, retry_job.document_id)
                if doc is not None:
                    doc.status = "failed"
                await fail_session.commit()
                return "dead"
            else:
                retry_job.status = "queued"
                await fail_session.commit()
                raise Retry(defer=2 ** retry_job.attempts) from exc

    # ── 第三段：写 chunks + 更新状态 ──────────────────────
    async with SessionLocal() as session:
        # 删除旧版本 chunks（重新索引时清理）
        await session.execute(
            delete(Chunk).where(Chunk.document_id == doc_ext["id"])
        )

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
                    embedding=vec,
                )
            )

        job = await session.get(ParseJob, parse_job_id)
        document = await session.get(Document, doc_ext["id"])
        document.status = "indexed"
        job.status = "succeeded"
        job.finished_at = datetime.now(timezone.utc)
        await session.commit()
        return "succeeded"


async def run_sync_all(ctx: dict[str, Any]) -> int:
    """M6 任务 1：cron job 入口 — 扫描所有 active 同步源并增量同步。"""
    from app.services.sync_service import sync_all_active_sources

    new_count = await sync_all_active_sources()
    logger.info("sync.cron.complete", extra={"new_count": new_count})
    return new_count


async def run_sync_source(ctx: dict[str, Any], source_id: str) -> int:
    """手动触发单个同步源（HTTP trigger_sync 入队，避免阻塞 API 事件循环）。"""
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
    functions = [run_parse_job, run_sync_all, run_sync_source]
    queue_name = get_settings().arq_queue_name
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_tries = get_settings().arq_max_attempts
    # M6 任务 1：每 30 分钟扫描所有 active 同步源
    cron_jobs = [
        cron(run_sync_all, minute={0, 30}),
    ]
