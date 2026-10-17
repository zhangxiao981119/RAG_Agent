"""M6 任务 1：知识源同步服务（§8 D-07）；M7 WU-M-14 增补 web 源分支。

定时同步配置的知识源，发现新增或变更的文档后自动上传到 MinIO + 入队解析，
无需人工干预：
  · local / git：扫描目录文件，增量指纹 = mtime + size
  · web：会话 → 探活 → 发现 → 抓取 → 三级指纹（ETag → Last-Modified →
    content_hash）→ 版本链入库 → 消失页软失效（INV-M-09）

两类源的指纹均持久化在 sync_sources.synced_files（JSONB），结构随源类型不同。
"""
from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import logging
import os
import subprocess
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

from sqlalchemy import select, update

from app.config.settings import get_settings
from app.database import SessionLocal
from app.infra import trace
from app.infra.arq_pool import get_arq_pool
from app.models import Document, ParseJob, SyncSource
from app.services.storage import get_storage
from app.services.sync.web_fetcher import (
    FetchResult,
    WebFetchError,
    build_allowed_hosts,
    discover_pages,
    fetch_page,
    open_web_session,
    probe_source,
)

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


# ── M7 WU-M-14：web 源辅助 ────────────────────────────────────────


def _sha256_hex(data: bytes) -> str:
    """内容字节 → sha256 十六进制指纹（与 documents.content_hash 同值）。"""
    return hashlib.sha256(data).hexdigest()


def _filename_from_url(url: str) -> str:
    """URL → MinIO 对象名尾部：取路径末段；目录型 URL（空末段）用 index.html。

    只取 path 不含 query；Document.filename 另记完整 URL（web 文档溯源主键）。
    """
    tail = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    return tail or "index.html"


def _fingerprint_unchanged(entry: dict, result: FetchResult, content_hash: str) -> bool:
    """三级指纹比对（WU-M-14）：ETag → Last-Modified → content_hash。

    语义是"任一级命中即未变更"而非"任一级失配即变更"：ETag 常因缓存层
    配置抖动、Last-Modified 可能被重置 —— 失配就建版本会让内容未动的页面
    反复生成新版本。逐级尝试，全部失配才落到 content_hash 兜底判定
    （内容字节是最终真源）。
    """
    if result.etag and entry.get("etag") and result.etag == entry["etag"]:
        return True
    if (
        result.last_modified
        and entry.get("last_modified")
        and result.last_modified == entry["last_modified"]
    ):
        return True
    return bool(entry.get("content_hash")) and entry["content_hash"] == content_hash


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
            source_kind=f"sync_{source.source_type}",  # sync_local / sync_git
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
        # ★ 用进程级共享池 —— 原先每次同步都 create_pool + close，
        #   批量同步 N 个文件就是 N 次建连（见 app/infra/arq_pool.py）
        pool = await get_arq_pool()
        # enqueue 失败 → 异常上抛，事务回滚，Document/ParseJob 不入库。
        # MUST NOT 在此关闭池 —— 池是进程级共享资源，关掉会让后续所有入队重新建连
        # 透传当前 trace_id：手动触发时=请求 trace，cron 触发时=该轮新 trace
        await pool.enqueue_job(
            "run_parse_job", str(parse_job.id), trace.get_trace_id(),
            _queue_name=settings.arq_queue_name,
        )

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


async def _sync_web_page(
    source: SyncSource,
    url: str,
    result: FetchResult,
    doc_group_id: uuid.UUID,
    version: int,
    content_hash: str,
) -> uuid.UUID:
    """将单个抓取页面入库（版本链）：MinIO → Document → ParseJob → arq 入队。

    与 _sync_one_file 同构（copy 后改写），差异点：
      · 数据来自内存字节（FetchResult.content），ext 固定 html（html 只来自
        同步抓取，parse 分派表已注册 parse_html）
      · filename 记完整 URL —— web 文档的溯源主键是 URL
      · version > 1 时同事务把旧版本 is_latest 置 false（版本链原子性）
      · source_kind=sync_web，content_hash 落列（0015 治理字段，增量指纹复用）
    独立 session + 事务：enqueue_job 失败整体回滚，避免 zombie job（口径同上）。
    """
    settings = get_settings()
    size_bytes = len(result.content)
    filename = _filename_from_url(url)

    storage_key = f"{source.tenant_id}/{source.kb_id}/{doc_group_id}/v{version}/{filename}"
    storage = await get_storage()
    await storage.put_object(storage_key, result.content, "text/html")

    async with SessionLocal() as session:
        if version > 1:
            # 版本链：旧版本全部软失效，与新版本插入同事务提交
            await session.execute(
                update(Document)
                .where(Document.doc_group_id == doc_group_id, Document.is_latest.is_(True))
                .values(is_latest=False)
            )

        document = Document(
            tenant_id=source.tenant_id,
            kb_id=source.kb_id,
            doc_group_id=doc_group_id,
            version=version,
            is_latest=True,
            filename=url,  # web 文档 filename 即来源 URL
            ext="html",
            size_bytes=size_bytes,
            storage_key=storage_key,
            status="pending",
            level=1,
            level_rank=source.level_rank,
            owner_dept_path=None,
            acl_tags=[],
            deny_subjects=[],
            uploaded_by=None,
            source_kind="sync_web",
            content_hash=content_hash,
        )
        session.add(document)
        await session.flush()

        parse_job = ParseJob(
            tenant_id=source.tenant_id,
            document_id=document.id,
            status="queued",
            max_attempts=settings.arq_max_attempts,
        )
        session.add(parse_job)
        await session.flush()

        pool = await get_arq_pool()
        await pool.enqueue_job(
            "run_parse_job", str(parse_job.id), trace.get_trace_id(),
            _queue_name=settings.arq_queue_name,
        )
        await session.commit()

    logger.info(
        "sync.web.indexed",
        extra={
            "sync_source_id": str(source.id),
            "url": url,
            "doc_id": str(document.id),
            "version": version,
        },
    )
    return document.id


async def _set_doc_latest(doc_id: uuid.UUID, is_latest: bool) -> None:
    """单文档 is_latest 翻转（消失页复活用，独立小事务）。"""
    async with SessionLocal() as session:
        await session.execute(
            update(Document).where(Document.id == doc_id).values(is_latest=is_latest)
        )
        await session.commit()


async def _retire_doc_group(doc_group_id: uuid.UUID) -> None:
    """整组软失效（INV-M-09：页面消失只置 is_latest=false，不物理 DELETE）。"""
    async with SessionLocal() as session:
        await session.execute(
            update(Document)
            .where(Document.doc_group_id == doc_group_id, Document.is_latest.is_(True))
            .values(is_latest=False)
        )
        await session.commit()


async def _sync_web_source(source: SyncSource) -> int:
    """web 源同步主流程（M7 WU-M-14，INV-M-08 / INV-M-09）。

    流程：会话 → 探活 → 发现 → 并发抓取 → 三级指纹增量 → 变更页版本链入库
    → 本轮未发现的已同步页软失效。探活 / 发现阶段抛出的 WebAuthError /
    WebFetchError 不在此捕获 —— 上抛 sync_source 既有 except 统一记 last_error
    并中断（DoD：鉴权失败不进入后续步骤）。

    synced_files[url] 结构（与 local/git 的 {mtime, size, doc_id} 不同源类型
    不同指纹，互不复用）：
      { doc_group_id, version, doc_id, etag, last_modified, content_hash, gone }
    gone=True 表示页面已从站点消失（软失效）；重现且内容未变时复活最新版。
    """
    settings = get_settings()
    allowed = build_allowed_hosts(list(source.entry_urls or []))
    synced: dict = dict(source.synced_files or {})

    client = await open_web_session(source)
    try:
        await probe_source(client, source)
        discovered = await discover_pages(client, source)

        # 并发抓取（INV-M-08：并发上限可配，信号量限流）。单页失败记日志不中断，
        # 也不参与消失判定 —— 抓取失败 ≠ 页面消失
        sem = asyncio.Semaphore(max(1, settings.web_fetch_concurrency))

        async def _fetch(url: str) -> tuple[str, FetchResult | None]:
            async with sem:
                try:
                    return url, await fetch_page(client, url, allowed)
                except WebFetchError as exc:
                    logger.error("sync.web.fetch_failed", extra={"url": url, "error": str(exc)})
                    return url, None

        pairs = await asyncio.gather(*[_fetch(u) for u in discovered])
        fetched: dict[str, FetchResult] = {url: r for url, r in pairs if r is not None}
    finally:
        await client.aclose()

    discovered_set = set(discovered)
    new_count = 0

    # ── 增量判定与入库（无需网络，client 已关）──────────────────
    for url in discovered:
        result = fetched.get(url)
        if result is None:
            continue
        content_hash = _sha256_hex(result.content)
        entry = synced.get(url)

        if entry and _fingerprint_unchanged(entry, result, content_hash):
            if entry.get("gone"):
                # 消失页重现且内容未变：复活最新版，不重复建版本
                await _set_doc_latest(uuid.UUID(entry["doc_id"]), True)
                entry["gone"] = False
            continue

        # 新页或变更页 → 版本链入库
        if entry:
            doc_group_id = uuid.UUID(entry["doc_group_id"])
            version = int(entry["version"]) + 1
        else:
            doc_group_id = uuid.uuid4()
            version = 1
        try:
            doc_id = await _sync_web_page(source, url, result, doc_group_id, version, content_hash)
            synced[url] = {
                "doc_group_id": str(doc_group_id),
                "version": version,
                "doc_id": str(doc_id),
                "etag": result.etag,
                "last_modified": result.last_modified,
                "content_hash": content_hash,
                "gone": False,
            }
            new_count += 1
        except Exception as exc:
            # 单页入库失败不影响其余页面；指纹不更新，下一轮重试
            logger.error("sync.web.page_failed", extra={"url": url, "error": str(exc)})

    # ── 消失页软失效（INV-M-09：无 DELETE）───────────────────────
    # 发现被 max_pages 硬截断时本轮判定不可信（未发现的页可能只是没轮到），
    # 跳过软失效，避免整站误下线
    if len(discovered) >= settings.web_max_pages_per_sync:
        logger.warning(
            "sync.web.discovery_truncated",
            extra={"source_id": str(source.id), "discovered": len(discovered)},
        )
    else:
        for url, entry in synced.items():
            if url in discovered_set or entry.get("gone"):
                continue
            try:
                await _retire_doc_group(uuid.UUID(entry["doc_group_id"]))
                entry["gone"] = True
            except Exception as exc:
                logger.error("sync.web.retire_failed", extra={"url": url, "error": str(exc)})

    # ── 持久化指纹状态 ─────────────────────────────────────────
    async with SessionLocal() as session:
        source = await session.merge(source)
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
            "total_pages": len(discovered),
        },
    )
    return new_count


async def sync_source(source: SyncSource) -> int:
    """同步一个源，返回新增/更新的文件数。"""
    # 1. 获取文件列表 / web 全流程（web 源为独立函数，见 _sync_web_source）
    try:
        if source.source_type == "web":
            # M7 WU-M-14：web 源抛出的 WebAuthError / WebFetchError 不在此捕获，
            # 上抛到本函数既有 except 统一持久化 last_error 并中断
            # （DoD：鉴权失败不进入后续步骤，口径与 local/git 异常分支一致）
            return await _sync_web_source(source)

        patterns = _parse_patterns(source.file_patterns)
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
