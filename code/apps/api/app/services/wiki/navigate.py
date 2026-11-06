"""wiki 导航查询扩展 —— 查页（IO）/ 选词（纯函数）两层（第 4 步）。

★ 本模块只在 `WIKI_ENABLED=true` 时被 **延迟 import**（见 `retrieve/__init__.py`
  的 ① 接入点）：关闭态连 `WikiNavPage` 这个 dataclass 都不实例化，
  「零 SQL」才不会退化成「零 SQL 但已 import」。

★ 为什么要拆两层（易被写错的顺序依赖）：`select_nav_terms` 的入参 `alias_forms`
  来自 `load_alias_map()`，而后者在 `retrieve()` 中位于导航查询之后。把选词塞进
  查页函数就得把词表查询提前（扩大改动面）或放弃「与词表别名去重」。拆层后依赖
  方向是单向的：查页 → 取别名 → 选词 → 造 pattern / tsquery，且**只有查页碰数据库**
  （一次查询两路复用，ILIKE 与 FTS 共用同一份选词结果）。

★ 红线 A：wiki 只产出**词元**进 `build_ilike_pattern` / `build_fts_query`；
  `summary` / `sections` 一个字都不进上下文（SQL 只 SELECT document_id, key_terms）。
  术语错的最坏后果是「多召回几片无关原文」，由既有 L1 阈值与 RRF 排序自然淘汰，
  不会变成「模型被告知了一个假事实」。

★ 失败语义（设计 §2.4）：在线 **fail-open** —— 超时 / 异常 / 无页都返回「不扩展」
  哨兵，绝不把异常抛回 `retrieve()`。`set_config(..., is_local=true)` 的作用域是
  **整个事务**，成功路径 MUST 还原到调用前值；失败路径 `rollback()` 后事务级设置
  随之消失，**不再发还原 SQL**（与 `services/graph/recall.py` 同款，F16）。
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass

from asyncpg.exceptions import QueryCanceledError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import decisions
from app.config.wiki_profile import WikiProfile
from app.infra import metrics
from app.services import keyword_text

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WikiNavPage:
    """**一次导航 SQL** 的产物。所有字段带默认值 → `WikiNavPage()` 即「不扩展」哨兵。

    不把选词结果放进来：选词需要 `alias_forms`（词表别名），而 `alias_map` 在
    `retrieve()` 里晚于导航查询才取得。拆成「查页（IO）/ 选词（纯函数）」两层，
    SQL 仍只有一条，两路共用同一份选词结果。

    ★ `document_id` 是稳定键，**不用 page_id**：第 3 步测试报告 P2-2 实证
      「页重建 = DELETE + INSERT 换 id」，`p.id` 不稳定。
    """

    document_id: uuid.UUID | None = None
    key_terms: tuple[str, ...] = ()       # 命中页的 LLM 原文术语，原样带回
    degraded_reason: str = ""             # "" | "timeout" | "error" | "no_match"
    elapsed_ms: float = 0.0


def _acceptable(term: str) -> bool:
    """确定性过滤（设计 §2.2.3 规则表）；不含任何模型判断、无 IO。

    长度口径与 `build_ilike_terms` 同源（中文 ≥2 / ASCII ≥4 或含 `_`），
    停用词复用 `keyword_text.ILIKE_STOPWORDS`，**不新造第二套规则表**。
    """
    if not term or len(term) > decisions.WIKI_NAV_MAX_TERM_CHARS:
        return False
    if term in keyword_text.ILIKE_STOPWORDS or not term.strip("_- 0123456789"):
        return False
    is_cjk = bool(keyword_text.CJK_RE.fullmatch(term))
    minimum = (
        decisions.WIKI_NAV_MIN_CJK_CHARS
        if is_cjk
        else decisions.WIKI_NAV_MIN_ASCII_CHARS
    )
    return len(term) >= minimum or (not is_cjk and "_" in term)


def select_nav_terms(
    page_terms: list[str],
    query_terms: list[str],
    query_norm: str,
    alias_forms: set[str],
) -> tuple[list[str], list[str]]:
    """纯函数：→ (顶层词元, FTS 索引词元)。全序、限量、**不修改任何入参**。

    ★ 原 `query_terms`（= `build_ilike_terms(query)`，未扩展）**一个字节不改**：
      wiki 词元通过 `extra_terms` / `extra_index_tokens` 注入，不追加后重排截断
      （设计 §2.2.1 的致命方案 / §6-C7）。
    ★ 全序：长度降序（长词更具体，与 `build_ilike_terms` 同源）、字典序破平局。
      页级 `key_terms` 虽已 `sorted`（字典序），但与「区分度」无关，故这里重排。
    ★ 二级 FTS 预算按**整个顶层词元**申请：中文词元经 `to_fts_text` 会 1→N
      展开成 2-gram，装不下就整词跳过（两路一起跳过），绝不截半截索引词元 ——
      半截词元在 FTS 里是静默的伪匹配。
    """
    out: list[str] = []
    fts: list[str] = []
    used = 0
    for raw in sorted(
        (keyword_text.normalize_key_term(t) for t in page_terms),
        key=lambda t: (-len(t), t),
    ):
        term = raw
        if not _acceptable(term):
            continue
        if term in out or term in alias_forms:
            continue
        # 与 query 词元去重（双向包含）：页里的「访问令牌」与 query 里的「令牌」
        # 是同一主题，扩了等于没扩；反向包含同理。
        if any(term in qt or qt in term for qt in query_terms):
            continue
        if term in query_norm:
            continue
        tokens = keyword_text.to_fts_text(term).split()
        if used + len(tokens) > decisions.WIKI_NAV_MAX_FTS_TOKENS:
            continue
        out.append(term)
        fts.extend(tokens)
        used += len(tokens)
        if len(out) >= decisions.WIKI_NAV_MAX_TERMS:
            break
    return out, list(dict.fromkeys(fts))


# ── 段 B：导航 SQL（设计 §2.1.3，可直接编码）──────────────────────────
# {where_base} 由 app.services.retrieve._WHERE_BASE **延迟 import 逐字内联**：
# 取的是同一个字符串对象，不是副本；复制一份到本文件必然漂移，而漂移的表现是
# 越权或漏召回，都不报错（与 graph/recall.py:_CHUNK_SQL_TEMPLATE 同款纪律）。
# 别名 `c` 不变、四闸门不增不减不重排；`**pushdown` 4 个绑定值原样展开。
_WIKI_NAV_SQL = """
WITH hit AS (
    SELECT p.document_id,
           p.key_terms,
           max(length(n.term))     AS best_len,
           count(DISTINCT n.term)  AS hit_terms
    FROM wiki_pages p
    CROSS JOIN LATERAL unnest(CAST(:needles AS text[])) AS n(term)
    JOIN LATERAL jsonb_array_elements_text(p.key_terms) AS kt(term) ON TRUE
    WHERE p.tenant_id = :tenant_id
      AND p.kb_id = ANY(:authorized_kb_ids)
      AND p.scope = 'doc'
      AND p.status = 'ready'
      AND p.generator_model = :gm
      AND p.prompt_version  = :pv
      AND EXISTS (
            SELECT 1 FROM chunks c
            {where_base}
              AND c.document_id = p.document_id
      )
      AND strpos(lower(kt.term), n.term) > 0
    GROUP BY p.document_id, p.key_terms
)
SELECT document_id, key_terms
FROM hit
ORDER BY best_len DESC, hit_terms DESC, document_id ASC
LIMIT :max_pages
"""
# 逐条说明（都是可写错的地方，见设计 §2.1.3）：
# · `p.tenant_id = :tenant_id` 与 `_WHERE_BASE` 的 `c.tenant_id = :tenant_id`
#   **并存不冲突**：前者让 idx_wiki_pages_kb 立刻收敛，后者保证 EXISTS 内的 chunk
#   属于本租户；`c.document_id = p.document_id` 把两者绑定。第 3 步详情 SQL 靠
#   page_id 定位所以没写，本步按 kb 集合扫描**必须**显式加。
# · **不重算 `authorized_kb_ids`**：`pushdown` 已是 chat.py 算好的 effective_kb_ids
#   （含 admin 豁免 + 前端选库交集）。再查一次库 / 再合并一次会重蹈「admin 豁免
#   失效 / 检索范围被扩大」的既有坑。
# · 口径双过滤（gm / pv 来自 `current_wiki_profile()`）：旧口径页在重建窗口期被
#   **排除**而非混算，与图召回 fail-closed 同纪律。
# · 排序全序：`best_len DESC`（命中最长查询词元 = 最具体）→ `hit_terms DESC` →
#   `document_id ASC`。**不用 `p.id`**：页重建 = DELETE+INSERT 换 id（P2-2）。
# · `LIMIT :max_pages = 1`：只取一页，避免不同主题术语拼进同一个查询。
# · **红线 A**：SQL 只 SELECT `document_id, key_terms`；`summary` / `sections`
#   连读都没被读，wiki 文本一个字都不进上下文。
# · `:needles` 传 Python `list[str]`，显式 `CAST(:needles AS text[])` 给 asyncpg
#   类型推断兜底；不拼字符串再 `string_to_array`（词元可能含逗号 / 空格）。


def _classify(exc: BaseException) -> str:
    """把在线失败归到 ``timeout`` / ``error`` 两档（只用于 metrics 的 reason 标签）。

    ★ **不新造异常分类体系**，照抄项目既有异常面（`services/graph/recall.py`）：
      · Python 侧墙钟取消 = ``TimeoutError``（``asyncio.timeout``，py3.11+ 即内建，
        ``asyncio.TimeoutError is TimeoutError`` 已实证）；
      · DB 侧 ``statement_timeout`` 取消 = ``asyncpg.exceptions.QueryCanceledError``，
        SQLAlchemy 可能包一层，原始异常挂在 ``.orig`` / ``__cause__`` / ``__context__``。
      其余一律 ``error``。沿链展开而不是只判顶层：包一层的驱动异常若被判成 error，
      超时就会静默混进「代码 bug」桶，运维据此查错方向全错。
    """
    chain: list[BaseException] = [exc]
    seen: set[int] = set()
    while chain:
        cur = chain.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, (TimeoutError, QueryCanceledError)):
            return "timeout"
        orig = getattr(cur, "orig", None)
        if isinstance(orig, BaseException):
            chain.append(orig)
        for nxt in (cur.__cause__, cur.__context__):
            if isinstance(nxt, BaseException):
                chain.append(nxt)
    return "error"


async def _rollback_quietly(session: AsyncSession) -> None:
    """吞掉 rollback 自身的异常：连接可能已断，它的失败不该盖住原始错因。"""
    try:
        await session.rollback()
    except Exception:  # noqa: BLE001 - 见 docstring
        logger.debug("wiki 导航失败后的 rollback 也失败了（忽略）", exc_info=True)


async def _read_statement_timeout(session: AsyncSession) -> str:
    """读当前事务生效的 statement_timeout，作为稍后还原的目标值。

    读失败退回 ``"0"``（PG 默认值 = 不限）：还原目标拿不到不该让导航整体失败，
    也不该抛到 `retrieve()` 破坏 fail-open（设计 §2.4 ①「读原值，不硬编码 0」
    指的是**不假设**原值为 0，读失败时的兜底与它不矛盾；与 graph/recall.py 同款）。
    """
    try:
        value = await session.scalar(text("SELECT current_setting('statement_timeout')"))
    except Exception:  # noqa: BLE001 - 见 docstring
        logger.debug("读取 statement_timeout 失败，还原目标退回 0", exc_info=True)
        return "0"
    return "0" if value is None else str(value)


async def load_wiki_nav_page(
    session: AsyncSession,
    *,
    needles: list[str],                        # = build_ilike_terms(query)，未扩展
    pushdown: dict[str, object],               # 直接 ** 展开，不重算、不改名
    profile: WikiProfile,                      # gm / pv 的唯一来源
    timeout_ms: int,
) -> WikiNavPage:
    """发 1 条 SELECT 找命中页；任何失败都返回「不扩展」，**绝不抛给主链路**。

    ★ 三段式（设计 §2.4，照抄第 2 步踩过的 F16 坑）：
      ① 读原值 → ② `set_config('statement_timeout', :ms, true)` → ③ 导航 SELECT，
      成功路径最后一条 SQL MUST 是 ④ 还原到**调用前值**；失败路径 `rollback()` 后
      事务级设置随事务消失，**不还原**、也不再发任何 SQL。
    ★ 位置特殊性：导航查询在三路关键词召回**之前**，比图召回更靠前。溢出会套到
      紧接着的 ILIKE（顺序扫描，最易超时）/ FTS 上，所以还原点唯一且不可漏。
    ★ 还原失败也 fail-open：单独 try 包住，失败就 `rollback()` 并返回「不扩展」，
      绝不把异常抛回 `retrieve()`。
    ★ ``no_match`` 是正常态：只打 ``rag.wiki.nav.no_match``，**不计 degraded**；
      保留在 `degraded_reason` 仅供单测与本地排障区分「空 needles / 无页 / 空选词」。
    """
    started = time.perf_counter()

    def _ms() -> float:
        return (time.perf_counter() - started) * 1000

    if not needles:
        return WikiNavPage(degraded_reason="no_match")

    # 延迟 import：retrieve 包正向 import 了 wiki 包（在 if wiki_on 内），模块级
    # 反向 import `_WHERE_BASE` 会成环；取同一个字符串对象，逐字内联，别名 c 不变。
    from app.services.retrieve import _WHERE_BASE

    sql = _WIKI_NAV_SQL.format(where_base=_WHERE_BASE)
    params = {
        **pushdown,
        "needles": needles,
        "gm": profile.generator_model,
        "pv": profile.prompt_version,
        "max_pages": decisions.WIKI_NAV_MAX_PAGES,
    }

    previous = await _read_statement_timeout(session)
    try:
        # ★ MUST 用 set_config() 而不是 `SET LOCAL statement_timeout = :ms`：
        #   SET 是工具语句，不接受绑定参数（asyncpg 扩展协议发 $1 → syntax error）。
        await session.execute(
            text("SELECT set_config('statement_timeout', :ms, true)"),
            {"ms": str(int(timeout_ms))},
        )
        row = (await session.execute(text(sql), params)).first()
    except Exception as exc:  # noqa: BLE001 - 契约：本函数永不抛（见模块 docstring）
        # QueryCanceled 会让事务进入 aborted 状态 → MUST rollback，否则紧接着的
        # ILIKE / FTS 两条 SQL 全部报 "current transaction is aborted"。
        await _rollback_quietly(session)
        reason = _classify(exc)
        logger.info("wiki 导航查询失败，本次不扩展词元: %s", reason, exc_info=exc)
        metrics.incr("rag.wiki.nav.degraded", reason=reason)
        return WikiNavPage(degraded_reason=reason, elapsed_ms=_ms())

    # 成功路径（含 no_match）：最后一条 SQL 是还原，且只此一处 —— 失败路径已
    # rollback，事务级设置随之消失，不需要也不应该再发 SQL。
    try:
        await session.execute(
            text("SELECT set_config('statement_timeout', :v, true)"),
            {"v": previous},
        )
    except Exception:  # noqa: BLE001 - 见 §2.4 ④
        await _rollback_quietly(session)
        logger.warning("wiki 导航还原 statement_timeout 失败", exc_info=True)
        metrics.incr("rag.wiki.nav.degraded", reason="error")
        return WikiNavPage(degraded_reason="error", elapsed_ms=_ms())

    elapsed = _ms()
    if row is None:
        metrics.incr("rag.wiki.nav.no_match")
        return WikiNavPage(degraded_reason="no_match", elapsed_ms=elapsed)
    metrics.incr("rag.wiki.nav.hit")
    return WikiNavPage(
        document_id=row.document_id,
        key_terms=tuple(row.key_terms or ()),
        elapsed_ms=elapsed,
    )
