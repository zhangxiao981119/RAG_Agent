"""M6 任务 1：知识源同步服务（§8 D-07）。

定时扫描配置的同步源（本地目录 / Git 仓库），
发现新增或变更的文档后自动上传到 MinIO + 入队解析，
无需人工干预。

增量指纹：文件 mtime + size，存在 sync_sources.synced_files（JSONB）。
"""
from __future__ import annotations

import asyncio
import fnmatch
import logging
import os
import shutil
import subprocess
import uuid
from datetime import datetime, timezone

from arq.connections import RedisSettings, create_pool
from sqlalchemy import select

from app.config.settings import get_settings
from app.database import SessionLocal
from app.models import Document, ParseJob, SyncSource
from app.services.storage import get_storage

logger = logging.getLogger(__name__)

# 同步源在容器内的挂载根目录
_SYNC_ROOT = "/data/sync"


def _parse_patterns(patterns_str: str) -> list[str]:
    """解析文件模式字符串，逗号分隔。"""
    return [p.strip() for p in patterns_str.split(",") if p.strip()]


def _scan_directory(root: str, patterns: list[str]) -> dict[str, dict]:
    """递归扫描目录，返回 {relative_path: {mtime, size, full_path}}。

    relative_path 用正斜杠（跨平台一致），作为 Document.filename。
    """
    result: dict[str, dict] = {}
    if not os.path.isdir(root):
        logger.warning("同步源目录不存在: %s", root)
        return result

    for dirpath, _dirs, filenames in os.walk(root):
        # 跳过 .git 目录
        if ".git" in dirpath.split(os.sep):
            continue
        for filename in filenames:
            if not any(fnmatch.fnmatch(filename, pat) for pat in patterns):
                continue
            full_path = os.path.join(dirpath, filename)
            rel_path = os.path.relpath(full_path, root).replace("\\", "/")
            stat = os.stat(full_path)
            result[rel_path] = {
                "mtime": stat.st_mtime,
                "size": stat.st_size,
                "full_path": full_path,
            }
    return result


def _git_pull_or_clone(git_url: str, branch: str | None, work_dir: str) -> None:
    """Git 仓库同步：首次 clone，后续 pull。"""
    if os.path.isdir(os.path.join(work_dir, ".git")):
        # 已存在，pull
        cmd = ["git", "pull", "--ff-only"]
        if branch:
            cmd.extend(["origin", branch])
        subprocess.run(cmd, cwd=work_dir, check=True, capture_output=True, timeout=120)
    else:
        # 首次 clone
        cmd = ["git", "clone", "--depth", "1"]
        if branch:
            cmd.extend(["--branch", branch])
        cmd.extend([git_url, work_dir])
        os.makedirs(os.path.dirname(work_dir), exist_ok=True)
        subprocess.run(cmd, check=True, capture_output=True, timeout=300)


async def _sync_one_file(
    source: SyncSource,
    rel_path: str,
    file_info: dict,
) -> uuid.UUID:
    """将单个文件同步到目标知识库（存 MinIO -> 建 Document -> 建 ParseJob -> 入队 arq）。

    每个文件用独立 session + 事务：enqueue_job 失败时整体回滚，避免 zombie job。
    """
    settings = get_settings()

    full_path = file_info["full_path"]
    # open().read() 是同步阻塞，to_thread 放到线程池
    data = await asyncio.to_thread(lambda: open(full_path, "rb").read())

    size_bytes = len(data)
    filename = os.path.basename(rel_path)
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    # 存 MinIO
    doc_group_id = uuid.uuid4()
    storage_key = f"{source.tenant_id}/{source.kb_id}/{doc_group_id}/v1/{filename}"
    storage = await get_storage()
    await storage.put_object(storage_key, data, "application/octet-stream")

    # 独立 session + 事务：enqueue_job 必须在 commit 之前成功，否则整体回滚
    async with SessionLocal() as session:
        # 建 Document
        document = Document(
            tenant_id=source.tenant_id,
            kb_id=source.kb_id,
            doc_group_id=doc_group_id,
            version=1,
            is_latest=True,
            filename=rel_path,  # 用相对路径，方便追溯来源
            ext=ext,
            size_bytes=size_bytes,
            storage_key=storage_key,
            status="pending",
            level=1,
            level_rank=source.level_rank,
            owner_dept_path=None,
            acl_tags=[],  # 同步源文档无部门归属，公开可读
            deny_subjects=[],
            uploaded_by=None,  # 系统同步
        )
        session.add(document)
        await session.flush()

        # 建 ParseJob
        parse_job = ParseJob(
            tenant_id=source.tenant_id,
            document_id=document.id,
            status="queued",
            max_attempts=settings.arq_max_attempts,
        )
        session.add(parse_job)
        await session.flush()

        # 入队 arq —— 必须在 commit 之前执行
        redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        try:
            await redis.enqueue_job(
                "run_parse_job", str(parse_job.id), _queue_name=settings.arq_queue_name
            )
        except Exception:
            await redis.close()
            # enqueue 失败 → 事务回滚，Document/ParseJob 不入库
            raise
        await redis.close()

        # 全部成功才 commit
        await session.commit()

    logger.info(
        "sync.file.indexed",
        extra={
            "sync_source_id": str(source.id),
            "file_path": rel_path,
            "doc_id": str(document.id),
        },
    )
    return document.id


async def sync_source(source: SyncSource) -> int:
    """同步一个源，返回新增/更新的文件数。"""
    patterns = _parse_patterns(source.file_patterns)

    # 1. 获取文件列表
    try:
        if source.source_type == "local":
            # 本地目录：path 是 /data/sync 下的子目录名
            root = os.path.join(_SYNC_ROOT, source.path) if not os.path.isabs(source.path) else source.path
            files = _scan_directory(root, patterns)
        elif source.source_type == "git":
            # Git：工作目录固定在 /data/sync/git/{source.id}
            work_dir = os.path.join(_SYNC_ROOT, "git", str(source.id))
            # subprocess.run 阻塞，用 to_thread 放到线程池
            await asyncio.to_thread(_git_pull_or_clone, source.path, source.branch, work_dir)
            files = _scan_directory(work_dir, patterns)
        else:
            logger.error("未知同步源类型: %s", source.source_type)
            return 0
    except Exception as exc:
        # 异常分支必须开 session 持久化 last_error，否则失败原因永久丢失
        async with SessionLocal() as err_session:
            source = await err_session.merge(source)
            source.last_error = f"{type(exc).__name__}: {exc}"
            await err_session.commit()
        logger.error("sync.source.failed", extra={"source_id": str(source.id), "error": str(exc)})
        return 0

    # 2. 增量对比
    synced: dict = dict(source.synced_files or {})
    new_count = 0

    async with SessionLocal() as session:
        # source 可能来自外部 session，merge 到当前 session 以保证持久化
        source = await session.merge(source)
        for rel_path, file_info in files.items():
            existing = synced.get(rel_path)
            # 指纹对比：mtime + size 都没变就跳过
            if existing and existing.get("mtime") == file_info["mtime"] and existing.get("size") == file_info["size"]:
                continue

            # 新文件或变更文件 —— _sync_one_file 内部用独立 session，enqueue_job 失败整体回滚
            try:
                doc_id = await _sync_one_file(source, rel_path, file_info)
                synced[rel_path] = {
                    "mtime": file_info["mtime"],
                    "size": file_info["size"],
                    "doc_id": str(doc_id),
                }
                new_count += 1
            except Exception as exc:
                logger.error(
                    "sync.file.failed",
                    extra={"file_path": rel_path, "error": str(exc)},
                )

        # 更新 sync_source
        source.synced_files = synced
        source.last_synced_at = datetime.now(timezone.utc)
        source.last_sync_count = new_count
        source.last_error = None
        await session.commit()

    logger.info(
        "sync.source.complete",
        extra={
            "source_id": str(source.id),
            "new_count": new_count,
            "total_files": len(files),
        },
    )
    return new_count


async def sync_all_active_sources() -> int:
    """扫描所有 active 状态的同步源并同步。供 arq cron job 调用。"""
    async with SessionLocal() as session:
        result = await session.execute(
            select(SyncSource).where(SyncSource.status == "active")
        )
        sources = list(result.scalars().all())

    total_new = 0
    for source in sources:
        try:
            total_new += await sync_source(source)
        except Exception as exc:
            logger.error(
                "sync.source.error",
                extra={"source_id": str(source.id), "error": str(exc)},
            )
    return total_new
