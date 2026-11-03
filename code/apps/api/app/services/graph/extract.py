"""离线实体/关系抽取 —— chunks → 实体 / 提及 / 关系。

★ 输入是**已落库的** chunks.content + heading_path（不是原始文件）：
  解析/分块/嵌入与图抽取是两条独立的派生链。只要 chunk 行不变（同一个 chunk_id），
  换抽取模型或改 prompt 就**不需要**重新解析与嵌入 —— 这是「开图只需补建抽取」
  这句话成立的全部依据。

★ 归一（normalize_name_key）在**代码**里做，不让模型做：
  大小写 / 全半角 / 空白折叠是很便宜的确定性操作，而「一致性」是实体合并的前提。
  让模型自己归一，同一实体在两次调用里必然写法不同 → 图里出现两个节点 →
  跨文档合并（图的全部价值）静默失效。

★ 为什么分批（GRAPH_EXTRACT_BATCH_CHUNKS=4）：
  太少 → 一篇 200 片就是 50 次调用（成本与失败面线性涨）；
  太多 → 单次输出过长撞超时，且实体跨段错配（同一实体在两段里被当成两个）。

★ 失败语义（离线 fail-visible，见设计文档 §2 的失败语义三档）：
  · 内容为空 / 超护栏 → GraphExtractSkip（job 置 graph_status='skipped'，不重试）
  · LLM 输出非法 → 二分重试（把批拆小再要一次）→ 单分片仍非法 → GraphExtractError
    （job 置 'failed' 并按 arq 重试，但**不重新解析文档** —— 文档已 indexed，
    抽取失败只影响图路）
  · 输出被 max_tokens 硬截断 → GraphExtractTruncated（GraphExtractError 的子类）。
    单独一个类型，是为了让日志与 message 直接指向「调大
    GRAPH_EXTRACT_MAX_OUTPUT_TOKENS」，而不是让人以为模型不听话去改 prompt。
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import decisions
from app.config.graph_profile import current_graph_profile
from app.config.settings import get_settings
from app.models import Chunk, Document
from app.services.graph.base import (
    ExtractedEntity,
    ExtractedMention,
    ExtractedRelation,
    ExtractionResult,
)
from app.services.graph.prompts import (
    EXTRACT_MAX_ENTITIES_PER_CALL,
    render_extract_prompt,
)
from app.services.llm import get_llm_service

logger = logging.getLogger(__name__)

# NFKC 之后仍可能是 C0 控制字符 / 全角空格，统一折叠成单空格
_WS_RE = re.compile(r"\s+")


class GraphExtractError(Exception):
    """抽取失败（LLM 输出契约违反 / 文档缺失）—— 可重试，由 job 层置 failed。"""


class GraphExtractTruncated(GraphExtractError):
    """输出长度撞到 max_tokens 上限（**不是**模型输出格式错误）。

    ★ 为什么必须与「非法 JSON」分开：
      截断后的输出是**合法 JSON 的前缀** —— `json.loads` 失败，症状与「模型乱写」
      一模一样。若不区分，运维会去改 prompt、换模型、加温度，而真正的修法是
      调大 GRAPH_EXTRACT_MAX_OUTPUT_TOKENS（一个环境变量）。
      继承 GraphExtractError：调用方的 `except GraphExtractError` 无需改动，
      重试/置 failed 的语义也不变（重试用尽仍是人工介入，但日志里能一眼看出方向）。
    """


class GraphExtractSkip(Exception):
    """抽取被**明确跳过**（内容为空 / 超过单文档分片上限）—— 重试无意义。

    与 GraphExtractError 分开的理由：前者是「这篇文档不该抽」，后者是「这次没抽成」。
    混成一个异常会让 skipped 的文档被反复重试到死信队列，而 failed 的文档
    又可能被当成「不用管」。
    """


def normalize_name_key(name: str) -> str:
    """实体名归一化：NFKC + 折叠空白 + lower。

    ★ 这是**唯一**的归一实现：抽取侧（建实体）与链接侧（查询 → 种子实体）都 MUST
      调本函数。两处各写一份，只要一个字符的处理不同，查询就永远匹配不上库里的键
      —— 表现为「实体明明抽出来了却一条也链不上」，不报错。
    """
    return _WS_RE.sub(" ", unicodedata.normalize("NFKC", name)).strip().lower()


def normalize_relation(relation: str) -> str:
    """关系谓词归一：NFKC + 折叠空白 + 去首尾标点，超过上限返回空串（=丢弃）。

    返回空串而不是截断：截断后的谓词会与另一条边的谓词撞车（唯一约束含 relation），
    让两条不同的边被合并成一条 —— 那是静默错图，比少一条边更糟。
    """
    text = _WS_RE.sub(" ", unicodedata.normalize("NFKC", relation)).strip()
    text = text.strip(" \t，。；：、,.;:!?！？")
    if not text or len(text) > decisions.GRAPH_RELATION_MAX_CHARS:
        return ""
    return text


@dataclass(frozen=True)
class _ChunkRow:
    """抽取输入的一行（只取需要的三列，不持有 ORM 对象跨 session）。"""

    id: uuid.UUID
    heading_path: str
    content: str


async def _load_chunks(
    session: AsyncSession, document_id: uuid.UUID
) -> list[_ChunkRow]:
    rows = (
        await session.execute(
            select(Chunk.id, Chunk.heading_path, Chunk.content)
            .where(Chunk.document_id == document_id, Chunk.is_latest.is_(True))
            .order_by(Chunk.chunk_index)
        )
    ).all()
    return [
        _ChunkRow(id=row.id, heading_path=row.heading_path or "", content=row.content)
        for row in rows
    ]


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


def _as_confidence(value: object) -> float | None:
    """置信度：接受数字或数字字符串；其它一律 None（DDL 允许 NULL = 模型未提供）。"""
    if isinstance(value, bool):  # bool 是 int 的子类，先挡掉
        return None
    if isinstance(value, (int, float)):
        return min(1.0, max(0.0, float(value)))
    if isinstance(value, str):
        try:
            return min(1.0, max(0.0, float(value.strip())))
        except ValueError:
            return None
    return None


def _normalize_type(raw_type: str) -> str:
    """类型闭集归一：大小写不敏感匹配，不认识的一律 OTHER。

    ★ 不丢弃「类型不认识」的实体：类型只是展示/过滤维度，丢掉实体等于丢掉
      这篇文档的一个可链接节点。而**非法类型绝不能进库**（DDL 有 CHECK）——
      归一成 OTHER 是唯一既能入库又不丢信息的选择。
    """
    candidate = raw_type.strip().upper()
    if candidate in decisions.GRAPH_ENTITY_TYPES:
        return candidate
    return "OTHER"


def _collect(
    parsed: dict,
    *,
    batch_ids: set[uuid.UUID],
) -> tuple[dict[str, ExtractedEntity], list[ExtractedMention], list[tuple[str, str, str]]]:
    """把一批 LLM 输出规范化为 (实体表, 提及表, 关系三元组表)。

    关系只回 (src_key, dst_key, relation)：document_id 由 extract_document 统一回填
    （模型不知道也不需要知道它，本模块也不该伪造一个占位 id）。

    所有丢弃都**有明确规则**（超长名 / 未知 chunk_id / 自环 / 空谓词），
    且不抛异常 —— 模型输出脏是常态，一篇 200 片的文档不该因为一条脏关系整体失败。
    """
    entities: dict[str, ExtractedEntity] = {}
    raw_entities = parsed.get("entities")
    if isinstance(raw_entities, list):
        for item in raw_entities:
            if not isinstance(item, dict):
                continue
            name = _as_str(item.get("name"))
            if not name or len(name) > decisions.GRAPH_ENTITY_NAME_MAX_CHARS:
                # 超长名 = 模型把整句当实体名（最常见的脏输出形态）
                continue
            key = normalize_name_key(name)
            if not key:
                continue
            aliases = item.get("aliases")
            alias_tuple = tuple(
                sorted(
                    {
                        _as_str(alias)
                        for alias in (aliases if isinstance(aliases, list) else [])
                        if _as_str(alias) and _as_str(alias) != name
                    }
                )
            )
            existing = entities.get(key)
            if existing is None:
                entities[key] = ExtractedEntity(
                    name=name,
                    name_key=key,
                    entity_type=_normalize_type(_as_str(item.get("type"))),
                    aliases=alias_tuple,
                )
            else:
                # 同一实体在一批里出现多次：合并别名，保留首次写法（展示名）
                merged = tuple(sorted(set(existing.aliases) | set(alias_tuple)))
                entities[key] = ExtractedEntity(
                    name=existing.name,
                    name_key=key,
                    entity_type=existing.entity_type,
                    aliases=merged,
                )
    entities = dict(list(entities.items())[:EXTRACT_MAX_ENTITIES_PER_CALL])

    mentions: list[ExtractedMention] = []
    raw_mentions = parsed.get("mentions")
    if isinstance(raw_mentions, list):
        seen: set[tuple[uuid.UUID, str]] = set()
        for item in raw_mentions:
            if not isinstance(item, dict):
                continue
            name = _as_str(item.get("name"))
            key = normalize_name_key(name)
            if key not in entities:
                continue  # 提及指向未登记的实体（被丢弃的超长名等）
            raw_id = _as_str(item.get("chunk_id"))
            try:
                chunk_id = uuid.UUID(raw_id)
            except ValueError:
                continue
            if chunk_id not in batch_ids:
                # ★ 只接受本批合法 id：模型会「善意地」编一个不存在的 id，
                #   写进去就是一条指向不存在 chunk 的垃圾提及（还会被 FK 拒绝）。
                continue
            if (chunk_id, key) in seen:
                continue
            seen.add((chunk_id, key))
            mentions.append(
                ExtractedMention(
                    chunk_id=chunk_id,
                    entity_key=key,
                    surface=_as_str(item.get("surface")) or name,
                    confidence=_as_confidence(item.get("confidence")),
                )
            )

    relations: list[tuple[str, str, str]] = []
    raw_relations = parsed.get("relations")
    if isinstance(raw_relations, list):
        seen_rel: set[tuple[str, str, str]] = set()
        for item in raw_relations:
            if not isinstance(item, dict):
                continue
            src_key = normalize_name_key(_as_str(item.get("src")))
            dst_key = normalize_name_key(_as_str(item.get("dst")))
            if src_key not in entities or dst_key not in entities:
                continue
            if src_key == dst_key:
                continue  # DDL 有 not_self 约束；自环对扩展无意义
            relation = normalize_relation(_as_str(item.get("relation")))
            if not relation:
                continue
            triple = (src_key, dst_key, relation)
            if triple in seen_rel:
                continue
            seen_rel.add(triple)
            relations.append(triple)
    relations = relations[: EXTRACT_MAX_ENTITIES_PER_CALL]

    return entities, mentions, relations


def _as_list(parsed: dict, key: str) -> list:
    """取合并用的列表字段；缺失或类型不对一律按空列表（二分后某侧可能返回 {}）。"""
    value = parsed.get(key)
    return value if isinstance(value, list) else []


async def _extract_batch(
    batch: list[_ChunkRow],
    *,
    model: str,
    timeout_seconds: float,
    max_tokens: int,
) -> dict:
    """调 LLM 抽一批，输出解析失败时**二分拆批**重试。

    ★ 为什么按「批」二分，而不是原地再问一次：
      解析失败的真正原因是**输出太长被 max_tokens 硬截断**（截断后的文本是合法
      JSON 的前缀，`json.loads` 必然失败），重复同一个请求只会得到同一个前缀。
      把批拆小 → 每侧输出减半 → 落到上限以内就成功了。
      二分到单分片仍失败，才是真的没救（或单分片本身超上限）。

    ★ 合并语义：左右两侧各自 `_collect` 归一，所以同实体合并 / 提及去重 / 关系
      去重都在调用方（_collect 与本函数之外）按既有规则完成 —— 这里只做列表拼接，
      不引入第二套合并规则。
    """
    if not batch:  # 二分边界兜底：空批不该发生，但也不该让调用方处理 None
        return {}

    prompt = render_extract_prompt(
        [(str(chunk.id), chunk.heading_path, chunk.content) for chunk in batch]
    )
    llm = get_llm_service()
    last_raw = ""
    for _attempt in range(2):  # 首次 + 重试一次（重试次数是代码常量，不进配置）
        last_raw = await llm.chat(
            [{"role": "user", "content": prompt}],
            temperature=0.1,
            model=model,
            timeout_seconds=timeout_seconds,
            max_tokens=max_tokens,
        )
        parsed = _extract_json(last_raw)
        if parsed is not None:
            return parsed

    if len(batch) > 1:
        mid = len(batch) // 2
        logger.info(
            "图抽取输出解析失败，二分重试：%d 分片 → %d + %d",
            len(batch),
            mid,
            len(batch) - mid,
        )
        left = await _extract_batch(
            batch[:mid],
            model=model,
            timeout_seconds=timeout_seconds,
            max_tokens=max_tokens,
        )
        right = await _extract_batch(
            batch[mid:],
            model=model,
            timeout_seconds=timeout_seconds,
            max_tokens=max_tokens,
        )
        return {
            "entities": _as_list(left, "entities") + _as_list(right, "entities"),
            "mentions": _as_list(left, "mentions") + _as_list(right, "mentions"),
            "relations": _as_list(left, "relations") + _as_list(right, "relations"),
        }

    # 单分片仍失败：区分「撞上限」与「模型乱写」，让日志直接指向修法
    chunk_id = batch[0].id
    # 中文场景下 1 token ≈ 1.5~2 字符，取 2 作为保守下界：
    # 输出字符数达到上限两倍以上，只可能是把输出预算用光了，而不是输出格式有问题。
    if len(last_raw) >= max_tokens * 2:
        raise GraphExtractTruncated(
            f"图抽取输出疑似被截断（chunk {chunk_id}，输出 {len(last_raw)} 字符，"
            f"上限 {max_tokens} tokens）：建议调大 GRAPH_EXTRACT_MAX_OUTPUT_TOKENS"
        )
    raise GraphExtractError(
        f"图抽取输出非法 JSON（chunk {chunk_id}，输出 {len(last_raw)} 字符）："
        f"{last_raw[:200]}"
    )


async def extract_document(
    session: AsyncSession, *, document_id: uuid.UUID
) -> ExtractionResult:
    """一篇文档的实体/关系抽取（离线）。

    ★ 不写库、不建事务、不改 documents.graph_status：本函数只产出产物，
      写入是 graph_job 的事务职责（抽取含网络调用，几十秒的事务会长时间持锁）。
    """
    profile = current_graph_profile()
    settings = get_settings()

    document = await session.get(Document, document_id)
    if document is None:
        raise GraphExtractError("document not found")

    chunks = await _load_chunks(session, document_id)
    if not chunks:
        raise GraphExtractSkip("文档无已索引分片（内容为空或尚未解析）")
    if len(chunks) > decisions.GRAPH_EXTRACT_MAX_CHUNKS_PER_DOC:
        # 静默截断会让「这篇只抽了一半」永远查不出来 → 明确 skipped 并告警
        raise GraphExtractSkip(
            f"分片数 {len(chunks)} 超过上限 "
            f"{decisions.GRAPH_EXTRACT_MAX_CHUNKS_PER_DOC}（graph_status=skipped）"
        )

    entities: dict[str, ExtractedEntity] = {}
    mentions: list[ExtractedMention] = []
    relations: dict[tuple[str, str, str], ExtractedRelation] = {}
    llm_calls = 0

    # ★ 事务必须在这里结束：往下全是 LLM 网络调用，而本函数**不写库**。
    #   带着事务去等网络 = 连接长期 idle in transaction（实测一次冻结把它挂了 11 分钟）：
    #   它阻塞 VACUUM、占住连接池，并发请求会被一起拖死，且 who-is-blocking 里看到的
    #   只是一条停在 SELECT chunks 上的会话，完全看不出真凶是 LLM。
    #   chunks 已是纯 dataclass（不持有 ORM 对象），document 也只用过判空 ——
    #   rollback 后不再访问其属性，不会触发懒加载（也就不会有 MissingGreenlet）。
    await session.rollback()

    batch_size = decisions.GRAPH_EXTRACT_BATCH_CHUNKS
    for offset in range(0, len(chunks), batch_size):
        batch = chunks[offset : offset + batch_size]
        parsed = await _extract_batch(
            batch,
            model=profile.extractor_model,
            timeout_seconds=settings.graph_extract_timeout_seconds,
            # 按调用覆盖输出上限：全局 4K 被 decisions 的预留校验锁死，
            # 而 4 片/请求的批量长输出会在列表中途被截断（见 settings 的说明）。
            max_tokens=settings.graph_extract_max_output_tokens,
        )
        # llm_calls 按「批」计数（不是按 HTTP 请求）：它进 ExtractionResult 做成本估算，
        # 二分产生的额外调用由日志的「二分重试」行体现，这里的口径不变。
        llm_calls += 1
        batch_entities, batch_mentions, batch_relations = _collect(
            parsed, batch_ids={chunk.id for chunk in batch}
        )
        entities.update(batch_entities)
        mentions.extend(batch_mentions)
        for src_key, dst_key, relation in batch_relations:
            # document_id 由本层统一回填（模型不知道也不需要知道它）
            key = (src_key, dst_key, relation)
            if key not in relations:
                relations[key] = ExtractedRelation(
                    src_key=src_key,
                    dst_key=dst_key,
                    relation=relation,
                    document_id=document_id,
                )

    # 跨批次的实体量护栏：EXTRACT_MAX_ENTITIES_PER_CALL 是**单批**上限，
    # 全篇仍可能被 200 片 × 12 撑到很大 —— 但那属于文档规模问题，
    # 由 GRAPH_EXTRACT_MAX_CHUNKS_PER_DOC 在前面卡住，此处不重复截断（截断会丢边）。

    return ExtractionResult(
        entities=list(entities.values()),
        mentions=mentions,
        relations=list(relations.values()),
        extractor_model=profile.extractor_model,
        prompt_version=profile.prompt_version,
        chunks_processed=len(chunks),
        llm_calls=llm_calls,
    )
