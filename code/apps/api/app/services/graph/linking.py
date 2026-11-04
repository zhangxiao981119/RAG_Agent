"""查询 → 种子实体（在线图召回的第 2 步，**不调 LLM**）。

★ 为什么用词法匹配而不是让 LLM 判「问句里提到了哪些实体」：
  这一步在每次提问的在线主链路上，预算只有几十毫秒（GRAPH_RETRIEVE_TIMEOUT_MS 默认
  800ms 是**兜底上限**，实测目标 p95 ≤ 80ms）。LLM 一次往返就是几百毫秒，且会引入
  不确定性 —— 同一句话两次链接出不同种子，图召回结果不可复现。
  词法匹配是确定性的：同一 query + 同一库，种子集合永远相同，这是可排查的前提。

★ 为什么必须复用 keyword_text 与 normalize_name_key，而不是就地写一套切词：
  · 切词：查询侧与索引侧各写一份 bigram 逻辑，只要一个字符处理不同（全角/半角、
    断点位置），索引里的 token 与查询生成的 token 就对不上 —— 表现为静默 0 召回。
  · 归一：name_key 是**写入时**由 extract.normalize_name_key 产出的合并键，
    链接侧另写一份（哪怕只是多 strip 一个字符）就会出现「实体明明在库里却一条也
    链不上」，而且不报错。

★ 三层匹配（先精确、后索引、最后兜底），得分只用于**排先后**：
  1.0 精确命中 name_key；0.6 命中 name_vec（GIN 索引，3 字窗口 OR）；
  0.3 子串兜底（POSIX 正则，无索引，只在前面两层凑不满 limit 时才发一次）。
  分数**不参与**任何阈值判定 —— 图召回整体不进 RRF/阈值（见设计文档 §9.3）。

★ 口径（extractor_model / prompt_version）在**提及侧**过滤，不在实体侧：
  `graph_entities` 按 `uq_graph_entities_kb_key (kb_id, name_key)` 在 KB 内合一，
  表上**没有**口径列（实体是跨文档合并的共享节点；口径记在 mentions / relations 上）。
  所以这里用 EXISTS 子查询要求「该实体在当前口径下至少有一条提及」——
  若不要求，旧口径的实体（换模型/改 prompt 之后的残留）会占满
  GRAPH_LINK_MAX_ENTITIES=8 个名额，把真正有效的种子挤掉，而 neighbors() 已经按
  口径把它们的边过滤干净 → 结果恒为 0 邻居，且看不出是「名额被旧数据吃掉了」。
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import decisions
from app.services.graph.extract import normalize_name_key
from app.services.keyword_text import build_fts_query, build_ilike_pattern, build_ilike_terms

logger = logging.getLogger(__name__)

# 租户 + kb + 口径三件套：三层查询共用同一段 WHERE，避免某一层漏掉其中一条。
# （口径漏了不报错，只会让种子池混进旧口径实体 —— 见模块 docstring 末段。）
_SCOPE_WHERE = """WHERE e.tenant_id = :tenant_id
  AND e.kb_id = ANY(:kb_ids)
  AND EXISTS (
      SELECT 1 FROM graph_entity_mentions m
      WHERE m.entity_id = e.id
        AND m.extractor_model = :extractor_model
        AND m.prompt_version = :prompt_version
  )"""

_EXACT_SQL = text(
    f"""
SELECT e.id, e.name, e.name_key
FROM graph_entities e
{_SCOPE_WHERE}
  AND e.name_key = ANY(:keys)
ORDER BY e.name_key, e.id
LIMIT :limit
"""
)

_FTS_SQL = text(
    f"""
SELECT e.id, e.name, e.name_key
FROM graph_entities e
{_SCOPE_WHERE}
  AND e.name_vec @@ to_tsquery('simple', :fts_query)
ORDER BY e.name_key, e.id
LIMIT :limit
"""
)

# 子串兜底：复用 build_ilike_pattern 的正则分支（型号词元展开成 `xyz[-_ ]?1000`，
# 同时命中 XYZ-1000 / xyz1000 / XYZ 1000）。不用 LIKE '%key%'：
# name_key 里的 `_` 在 LIKE 里是单字符通配符，`get_retrieval` 会命中 `getXretrieval`。
_LIKE_SQL = text(
    f"""
SELECT e.id, e.name, e.name_key
FROM graph_entities e
{_SCOPE_WHERE}
  AND e.name_key ~* :pattern
ORDER BY e.name_key, e.id
LIMIT :limit
"""
)


@dataclass(frozen=True)
class LinkedEntity:
    """一个被链接到的种子实体。score 只表示匹配层次，不参与阈值判定。"""

    entity_id: uuid.UUID
    name: str
    score: float


def query_keys(query: str) -> list[str]:
    """query → 归一化后的候选实体键（≤ GRAPH_LINK_MAX_TERMS，去重保序）。

    词元取自 `build_ilike_terms`（与 ILIKE 召回同一真源）：它给出的是**整词**
    （中文按疑问/功能词切段、ASCII 整词、型号），正好是实体名的自然粒度。
    刻意不用 `to_fts_text` 的 bigram 当键：2-gram（「能优」）做子串匹配会命中大量
    无关实体名（「优化」「性能」互相串），把种子池冲淡 —— bigram 交给第二层的
    name_vec 索引去用。
    """
    keys: list[str] = []
    for term in build_ilike_terms(query):
        key = normalize_name_key(term)
        if key and key not in keys:
            keys.append(key)
    return keys[: decisions.GRAPH_LINK_MAX_TERMS]


def _match_score(name_key: str, keys: list[str]) -> float:
    """按匹配层次给分（确定性、可解释）：精确 > 前缀 > 子串。"""
    best = 0.0
    for key in keys:
        if name_key == key:
            return 1.0
        if name_key.startswith(key):
            best = max(best, 0.6)
        elif key in name_key:
            best = max(best, 0.3)
    return best


async def link_entities(
    session: AsyncSession,
    *,
    query: str,
    tenant_id: uuid.UUID,
    kb_ids: list[uuid.UUID],
    extractor_model: str,
    prompt_version: str,
    limit: int = decisions.GRAPH_LINK_MAX_ENTITIES,
) -> list[LinkedEntity]:
    """查询 → 种子实体。返回按 (score 降序, name_key 升序) 的**全序**结果。

    全序（而不是只按 score）是为了让「同样 8 个名额给了谁」可复现：score 相同是常态
    （同层命中都是 0.6），此时若顺序不定，同一句话两次召回会给出不同邻居。

    ★ 口径由调用方传入，而不是在本模块内取 `current_graph_profile()`：
      调用方（recall_by_graph）已经持有一次请求的口径快照，若这里再取一次，两次
      取值之间配置被改动就会「种子按新口径、邻居按旧口径」——同一次请求内口径不一致，
      表现为静默 0 召回。一个请求只允许取一次口径。
    """
    keys = query_keys(query)
    if not keys or not kb_ids or limit < 1:
        # 问句里没有可用词元（如纯符号）→ 正常态，不是故障
        return []

    scope = {
        "tenant_id": tenant_id,
        "kb_ids": list(kb_ids),
        "extractor_model": extractor_model,
        "prompt_version": prompt_version,
    }
    # id → (score, name_key, name)：name_key 单独留着做次级排序键（LinkedEntity 不带它）
    picked: dict[uuid.UUID, tuple[float, str, str]] = {}

    def _take(rows, base: float) -> None:  # noqa: ANN001 - Row 序列
        for row in rows:
            if row.id in picked:
                # 已被更高层命中：保留高档分数（精确命中不会被 FTS 覆盖成 0.6）
                continue
            picked[row.id] = (max(base, _match_score(row.name_key, keys)), row.name_key, row.name)

    _take(
        (await session.execute(_EXACT_SQL, {**scope, "keys": keys, "limit": limit})).all(),
        1.0,
    )

    if len(picked) < limit:
        fts_query = build_fts_query(query)
        if fts_query:
            _take(
                (
                    await session.execute(
                        _FTS_SQL, {**scope, "fts_query": fts_query, "limit": limit * 4}
                    )
                ).all(),
                0.6,
            )

    if len(picked) < limit:
        _take(
            (
                await session.execute(
                    _LIKE_SQL,
                    {**scope, "pattern": build_ilike_pattern(keys), "limit": limit * 4},
                )
            ).all(),
            0.3,
        )

    ordered = sorted(picked.items(), key=lambda item: (-item[1][0], item[1][1]))
    return [
        LinkedEntity(entity_id=entity_id, name=name, score=score)
        for entity_id, (score, _name_key, name) in ordered[:limit]
    ]
