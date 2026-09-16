"""异步解析 worker —— arq 实现（手册 §6 M2 任务 2）。

流程：
  parse_job 入队 → 拉文件 → 解析 → 分块 → 嵌入 → 写 chunks → 更新 status
失败重试 max_attempts 次后进死信（status=dead）。

启动：arq app.workers.parse_job.WorkerSettings
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from arq import Retry
from arq.connections import RedisSettings
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
    """arq 任务入口。job_id 是 parse_jobs.id 字符串。"""
    parse_job_id = uuid.UUID(job_id)
    settings = get_settings()

    async with SessionLocal() as session:
        # 锁任务
        job = await session.get(ParseJob, parse_job_id)
        if job is None:
            raise ValueError(f"parse_job not found: {job_id}")
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

        try:
            # 拉文件
            storage = await get_storage()
            data = await storage.get_object(document.storage_key)

            # 解析
            blocks = parse_bytes(document.ext, data)
            if not blocks:
                raise ParseError("解析结果为空（无文本内容）")

            # 分块
            chunks = chunk_blocks(blocks)
            if not chunks:
                raise ParseError("分块结果为空")

            # 嵌入
            embedding_service = get_embedding_service()
            texts = [c.content for c in chunks]
            vectors = await embedding_service.embed(texts)

            # 写 chunks
            tenant_id = document.tenant_id
            kb_id = document.kb_id
            document_id = document.id
            # M2 无权限版本：acl_tags = {public}，deny_subjects = []
            acl_tags = ["public"]
            deny_subjects: list[str] = []
            level_rank = document.level_rank

            # 删除旧版本 chunks（重新索引时清理）
            await session.execute(
                delete(Chunk).where(Chunk.document_id == document_id)
            )

            for index, (chunk, vec) in enumerate(zip(chunks, vectors, strict=True)):
                session.add(
                    Chunk(
                        tenant_id=tenant_id,
                        kb_id=kb_id,
                        document_id=document_id,
                        chunk_index=index,
                        content=chunk.content,
                        token_count=chunk.token_count,
                        heading_path=chunk.heading_path or None,
                        page_no=chunk.page_no,
                        level_rank=level_rank,
                        acl_tags=acl_tags,
                        deny_subjects=deny_subjects,
                        is_latest=True,
                        embedding=vec,
                    )
                )

            document.status = "indexed"
            job.status = "succeeded"
            job.finished_at = datetime.now(timezone.utc)
            await session.commit()
            return "succeeded"

        except Exception as exc:
            await session.rollback()
            # 重试或死信
            async with SessionLocal() as retry_session:
                retry_job = await retry_session.get(ParseJob, parse_job_id)
                if retry_job is None:
                    raise
                retry_job.last_error = f"{type(exc).__name__}: {exc}"
                if retry_job.attempts >= settings.arq_max_attempts:
                    retry_job.status = "dead"
                    retry_job.finished_at = datetime.now(timezone.utc)
                    # 同步标记 document 失败
                    doc = await retry_session.get(Document, retry_job.document_id)
                    if doc is not None:
                        doc.status = "failed"
                    await retry_session.commit()
                    return "dead"
                else:
                    retry_job.status = "queued"
                    await retry_session.commit()
                    # 用 retry_job.attempts，不要访问已 rollback 的旧 session 对象
                    raise Retry(defer=2 ** retry_job.attempts) from exc


class WorkerSettings:
    """arq Worker 配置。"""
    functions = [run_parse_job]
    queue_name = get_settings().arq_queue_name
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_tries = get_settings().arq_max_attempts
