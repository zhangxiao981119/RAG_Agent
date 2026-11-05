"""wiki 导航页生成（离线）—— 已落库 chunks → 一页导航摘要。

★ 输入是**已落库的** chunks.content + heading_path（与图抽取同一依据）：
  解析/分块/嵌入与 wiki 生成是两条独立的派生链。换生成模型或改 prompt
  不需要重新解析与嵌入 ——「开 wiki 只需 rebuild_wiki」的全部依据。

★ 幂等键（§9.4）：`(document_id, generator_model, prompt_version, content_hash)`。
  content_hash = sha1(输入文本 + generator_model + prompt_version)[:12]。
  四元组相同 → 直接跳过（**不调 LLM** —— 重复投递 / 重试 / 补跑都不该重烧钱）；
  任一不同 → DELETE 旧页 + INSERT 新页（uq_wiki_pages_document 兜底并发）。

★ 与图抽取（services/graph/extract.py）同款的硬规则：
  · LLM 调用期间**不持 session**（网络几十秒的事务会长时间 idle in transaction，
    冻结连接池、阻塞 VACUUM）—— 输入在第一个 session 里读成纯 dataclass，
    rollback 后再调 LLM，写入在第二个 session 里完成。
  · 归一/去重/截断在**代码**里做，不信任模型自己保证：chunk_ids 必须属于本文档、
    key_terms sorted 去重、sections 超上限丢弃 —— 模型输出脏是常态，一篇文档
    不该因为一条脏小节整体失败。
★ 本模块不写 documents 的任何状态列（wiki 没有 graph_status 的对应物）：
  生成失败只留日志与 metrics，页缺失本身（详情 404）就是「失败可见」的形态。
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.config.wiki_profile import WikiProfile, current_wiki_profile
from app.models import Chunk, Document, WikiPage
from app.services.wiki.prompts import (
    WIKI_MAX_SECTIONS,
    WIKI_MAX_TERMS_PER_SECTION,
    WIKI_TERM_MAX_CHARS,
    render_wiki_prompt,
)
from app.services.llm import get_llm_service

logger = logging.getLogger(__name__)


class WikiBuildError(Exception):
    """生成失败（LLM 输出契约违反 / 文档缺失）—— 可重试，由 job 层处置。"""


class WikiBuildSkip(Exception):
    """明确跳过（文档无分片）—— 重试无意义。"""


@dataclass(frozen=True)
class _ChunkRow:
    """生成输入的一行（只取需要的列，不持有 ORM 对象跨 session —— 见 extract.py）。"""

    id: uuid.UUID
    heading_path: str
    content: str


@dataclass(frozen=True)
class WikiInput:
    """一篇文档的生成输入（已渲染、已截断、已算 hash）。"""

    text: str
    content_hash: str
    chunks: list[_ChunkRow] = field(default_factory=list)


@dataclass(frozen=True)
class GeneratedPage:
    """LLM 产物（已按代码侧规则归一 / 校验 / 截断）。"""

    title: str
    summary: str
    sections: list[dict]
    key_terms: list[str]
    source_chunk_ids: list[uuid.UUID]
    llm_calls: int


def compute_content_hash(input_text: str, generator_model: str, prompt_version: str) -> str:
    """幂等键的 hash（§9.4）：sha1(输入文本 + 模型 + prompt 版本)[:12]。

    ★ 口径三要素进 hash 是刻意的：换模型 / bump prompt 后，即使文本没变，
      hash 也不同 → 旧页被判定为「待重建」而不是被复用 —— 与图抽取的
      「口径不匹配 = 直接排除」同一条 fail-closed 纪律。
    """
    raw = f"{input_text}|{generator_model}|{prompt_version}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _truncate_chunks(chunks: list[_ChunkRow], max_chars: int) -> list[_ChunkRow]:
    """按顺序取片段直到输入预算用尽（WIKI_INPUT_MAX_CHARS）。

    ★ 截断是**整片粒度**而不是切半个片段：半个片段会让模型在小节里引用一个
      「看不到结尾的内容」，且被截断的 id 不进 chunk_ids，审计能对上「模型看见了什么」。
      截掉的片段不参与生成 —— 页的覆盖范围由 source_chunk_ids 如实记录。
    """
    kept: list[_ChunkRow] = []
    used = 0
    for chunk in chunks:
        size = len(chunk.content) + len(chunk.heading_path) + 16  # 16 = id + 分隔符余量
        if kept and used + size > max_chars:
            break
        kept.append(chunk)
        used += size
    return kept


async def prepare_wiki_input(
    session: AsyncSession, *, document_id: uuid.UUID
) -> tuple[WikiInput, bool]:
    """读 chunks + 渲染输入 + 算 hash + 幂等判定。

    返回 (输入, 是否幂等命中)。命中 = 已有页的 (generator_model, prompt_version,
    content_hash) 与本次完全一致 —— 调用方直接跳过，不进入 LLM 段。
    """
    profile = current_wiki_profile()
    settings = get_settings()

    document = await session.get(Document, document_id)
    if document is None:
        raise WikiBuildError("document not found")

    rows = (
        await session.execute(
            select(WikiPage.id, WikiPage.generator_model, WikiPage.prompt_version,
                   WikiPage.content_hash, WikiPage.status)
            .where(WikiPage.document_id == document_id)
        )
    ).first()

    chunks = [
        _ChunkRow(id=row.id, heading_path=row.heading_path or "", content=row.content)
        for row in (
            await session.execute(
                select(Chunk.id, Chunk.heading_path, Chunk.content)
                .where(Chunk.document_id == document_id, Chunk.is_latest.is_(True))
                .order_by(Chunk.chunk_index)
            )
        ).all()
    ]
    if not chunks:
        raise WikiBuildSkip("文档无已索引分片（内容为空或尚未解析）")

    kept = _truncate_chunks(chunks, settings.wiki_input_max_chars)
    text = render_wiki_prompt([(c.id, c.heading_path, c.content) for c in kept])
    content_hash = compute_content_hash(text, profile.generator_model, profile.prompt_version)

    # ★ status='ready' 必须参与判定：失败留痕（status='failed'）的行四元组虽然
    #   相同，但那代表「生成没成」—— 把它当幂等命中会让失败的页永远不被重试。
    idempotent = bool(
        rows
        and rows.status == "ready"
        and rows.generator_model == profile.generator_model
        and rows.prompt_version == profile.prompt_version
        and rows.content_hash == content_hash
    )
    return WikiInput(text=text, content_hash=content_hash, chunks=kept), idempotent


def _extract_json(raw: str) -> dict | None:
    """从 LLM 输出里取出 JSON 对象（容忍 ```json 围栏与前后废话）。"""
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(raw[start:end])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _as_str(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _clean_terms(raw_terms: object) -> list[str]:
    """小节术语清洗：字符串化、截长、去空、保序去重（排序由页级统一做）。"""
    terms: list[str] = []
    seen: set[str] = set()
    for term in raw_terms if isinstance(raw_terms, list) else []:
        text = _as_str(term)
        if not text or len(text) > WIKI_TERM_MAX_CHARS or text in seen:
            continue
        seen.add(text)
        terms.append(text)
        if len(terms) >= WIKI_MAX_TERMS_PER_SECTION:
            break
    return terms


def _normalize_sections(
    raw_sections: object, valid_chunk_ids: set[uuid.UUID]
) -> tuple[list[dict], list[uuid.UUID]]:
    """小节清洗（§9.4 的「归一在代码里做」）：

    · chunk_ids 逐个校验是否属于本文档的 chunk（模型会善意地编 id —— 写进去
      就是导航页指向不存在片段的垃圾证据）；
    · 小节没有一条合法 chunk_ids → 整节丢弃（导航指向空气，不如没有）；
    · 空 summary / 空 heading_path 的节丢弃（导航价值为零）；
    · 超过 WIKI_MAX_SECTIONS 按顺序丢弃（防止模型按每 200 字一节把输出撑爆）。
    """
    sections: list[dict] = []
    used_chunk_ids: list[uuid.UUID] = []
    for item in raw_sections if isinstance(raw_sections, list) else []:
        if len(sections) >= WIKI_MAX_SECTIONS:
            break
        if not isinstance(item, dict):
            continue
        heading_path = _as_str(item.get("heading_path"))
        summary = _as_str(item.get("summary"))
        if not heading_path or not summary:
            continue
        chunk_ids: list[uuid.UUID] = []
        for raw_id in item.get("chunk_ids") if isinstance(item.get("chunk_ids"), list) else []:
            try:
                chunk_id = uuid.UUID(_as_str(raw_id))
            except ValueError:
                continue
            if chunk_id in valid_chunk_ids and chunk_id not in chunk_ids:
                chunk_ids.append(chunk_id)
        if not chunk_ids:
            continue
        sections.append({
            "heading_path": heading_path,
            "summary": summary,
            "key_terms": _clean_terms(item.get("key_terms")),
            "chunk_ids": chunk_ids,
        })
        used_chunk_ids.extend(chunk_ids)
    return sections, used_chunk_ids


async def generate_page_content(wiki_input: WikiInput) -> GeneratedPage:
    """调 LLM 生成一页（**不写库、不持事务** —— 与 extract_document 同款纪律）。

    输出解析失败时原地重试一次（截断问题在 wiki 不存在 —— 输入已被
    WIKI_INPUT_MAX_CHARS 截断，输出是页不是全文，4K 预算足够；重试一次
    是给「模型偶发走神」留的余量，两次都失败才是真的没救）。
    """
    profile: WikiProfile = current_wiki_profile()
    settings = get_settings()
    llm = get_llm_service()

    parsed: dict | None = None
    llm_calls = 0
    last_raw = ""
    for _attempt in range(2):
        last_raw = await llm.chat(
            [{"role": "user", "content": wiki_input.text}],
            temperature=0.2,
            model=profile.generator_model,
            timeout_seconds=settings.wiki_generate_timeout_seconds,
        )
        llm_calls += 1
        parsed = _extract_json(last_raw)
        if parsed is not None:
            break
    if parsed is None:
        raise WikiBuildError(
            f"wiki 生成输出非法 JSON（输出 {len(last_raw)} 字符）：{last_raw[:200]}"
        )

    title = _as_str(parsed.get("title"))
    summary = _as_str(parsed.get("summary"))
    if not title or not summary:
        raise WikiBuildError(f"wiki 生成输出缺 title/summary：{last_raw[:200]}")

    valid_chunk_ids = {c.id for c in wiki_input.chunks}
    sections, used_chunk_ids = _normalize_sections(
        parsed.get("sections"), valid_chunk_ids
    )
    if not sections:
        raise WikiBuildError(
            "wiki 生成输出没有任何合法小节（chunk_ids 全部无效或字段为空）"
        )

    # 全页术语 = 各节合并 sorted 去重（§9.6 ③：响应里就是这份排序结果）
    key_terms = sorted({term for section in sections for term in section["key_terms"]})

    return GeneratedPage(
        title=title,
        summary=summary,
        sections=sections,
        key_terms=key_terms,
        source_chunk_ids=used_chunk_ids,
        llm_calls=llm_calls,
    )


async def write_wiki_page(
    session: AsyncSession,
    *,
    document: Document,
    wiki_input: WikiInput,
    generated: GeneratedPage,
) -> None:
    """落页：DELETE 旧页 + INSERT 新页（**同一事务**，调用方负责 commit）。

    ★ DELETE 的范围是 document_id 下的**任何**旧页（包括旧口径的）：§9.4 的
      幂等语义是「hash 不同 → 重建」，一文档一页约束下旧页必须让位。
    """
    profile = current_wiki_profile()
    existing = (
        await session.execute(select(WikiPage.id).where(WikiPage.document_id == document.id))
    ).first()
    if existing is not None:
        page = await session.get(WikiPage, existing.id)
        if page is not None:
            await session.delete(page)
            await session.flush()
    session.add(
        WikiPage(
            tenant_id=document.tenant_id,
            kb_id=document.kb_id,
            scope="doc",
            document_id=document.id,
            title=generated.title,
            summary=generated.summary,
            sections=generated.sections,
            key_terms=generated.key_terms,
            source_chunk_ids=generated.source_chunk_ids,
            generator_model=profile.generator_model,
            prompt_version=profile.prompt_version,
            content_hash=wiki_input.content_hash,
            status="ready",
        )
    )


async def record_failed_page(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    content_hash: str,
) -> None:
    """生成失败留痕：status='failed' 的行（仅当该文档还没有任何页时插入）。

    ★ 为什么留痕：页缺失本身也是 404，但「从没生成过」与「生成了 N 次全失败」
      在运维上是两件事 —— 后者要看日志才能发现，而 failed 行让
      rebuild_wiki --stale-only 与管理端统计（pages_failed）直接可见。
    ★ 为什么不动已有页：失败重建时若已有 ready 页（旧口径），把它删了换成
      failed 行 = 读者从「看得到旧页」变成「什么都看不到」。旧页按旧口径
      生成、内容仍然真实，保留它优于清零。
    """
    profile = current_wiki_profile()
    existing = (
        await session.execute(
            select(WikiPage.id).where(WikiPage.document_id == document_id)
        )
    ).first()
    if existing is not None:
        return
    document = await session.get(Document, document_id)
    if document is None:
        return
    session.add(
        WikiPage(
            tenant_id=document.tenant_id,
            kb_id=document.kb_id,
            scope="doc",
            document_id=document_id,
            title=document.filename,
            summary="",
            sections=[],
            key_terms=[],
            source_chunk_ids=[],
            generator_model=profile.generator_model,
            prompt_version=profile.prompt_version,
            content_hash=content_hash,
            status="failed",
        )
    )
    await session.commit()
