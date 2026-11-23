"""chunk 口径分布的**唯一判定消费方**（Step 4B-1）。

本模块只有三类东西，边界刻意画得很硬：

  1. **读**：`collect()` / `scenario_kb_readability()` —— 都是一次 SQL 往返，无锁、
     无事务副作用、不写任何表。
  2. **纯判定**：`judge_publish_snapshot()` / `declared_stale_keys()` / `verdict()` ——
     零 IO、零依赖，可单测。
  3. **渲染**：`format_banner_line()` / `log_needs_reindex_banner()`。

★ 为什么判定谓词**不在本模块**：唯一实现是迁移 `0029` 里的
  `fn_chunk_reindex_candidates(...)`（I-4B-2）。本模块与
  `scripts/reindex_embeddings.py` 都调它 —— 这样「视图说 0 条、脚本说 300 条」
  这类分叉在**结构上不可能**发生。任何在本模块重写 `embedding_model <> ...`
  的写法都是第二真源（守卫：`tests/scenario/test_step4b_guards.py::T-4B-10`）。

★ 三条不可违反的口径（违反后症状都写在括号里）：

  · **分母 = `chunks.is_latest = true` 单条件**（I-4B-7）。不得再加
    `deleted_at IS NULL` / `status='indexed'` / `embedding IS NOT NULL` ——
    它们各自是一个**独立列**（`in_dead_documents` / `embedding_missing`），
    由读者决定要不要排除。分母一变，`--check` 的 CI 语义会同时变
    （一个开关改两件事）。
  · **`chunker_only` 不进任何百分比、不进 `verdict()`、不告警**（I-4B-3）。
    它只出现在横幅与端点响应里。理由：软 DNA 不匹配不拒绝检索，而它的处置是
    「下次全量重建时用新参数」，不是「现在跑个脚本」—— 把它做成闸门只会训练
    运维无视告警（R10 的病因正是「闸门永远红」，不是「数字存在」）。
  · **告警判据用 `newest_ok_at` 的推进，不用 `oldest_stale_at` 的绝对值**
    （§B.5.4）。旧 chunk 天然很老，用它的时间做阈值在稳态下**恒为真**。

★ 本模块**没有**任何口径字面量（模型名 / 文本公式版本 / 分块参数）：全部从
  `current_index_profile()` 取（I-4B-1）。守卫：T-4B-8。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import placeholders
from app.config.index_profile import current_index_profile
from app.infra import metrics
from app.services.scenario.dna_declare import ENV_BOUND_FIELDS, build_dna_declared

logger = logging.getLogger(__name__)

STALE_PROGRESS_HOURS: float = 24.0
"""`hard_stale > 0` 时，`newest_ok_at` 停止推进多少小时才从 `info` 升为 `warn`。

★ 这是**站位值**（无真实运维数据可标定，`框架设计-v1.md:1294` 至今把它列为待定项）。
  按项目纪律必须登记到 `app/config/placeholders.py`（下方 import 期断言），
  否则它会变成「只写在 docstring 里、没人知道它没标定」的常量。
"""

BANNER_TIMEOUT_SECONDS: float = 5.0
"""启动横幅里那次统计的**超时上限**（站位值，同样已登记）。

★ 为什么必须有超时（这是本步唯一需要认真论证的工程取舍）：
  设计 §B.5.2 要把统计放进启动横幅，而 §U-4B-7 用「过期扫描是离线动作、非请求路径」
  论证不必加索引 —— 这两句原本对不上：横幅一加，全表扫就进了**启动路径**。
  处置：横幅走**可超时的异步路径**，超时只打「统计超时」不打数字。
  这样 `U-4B-7` 的理由才成立：**启动期有界成本 + 超时护栏**，
  既不会阻塞启动，也不会让 api 因为「这条统计慢」而起不来。
"""


# ══════════════════════════════════════════════════════════════════════
# 站位值登记的自证（漏登记 = import 期炸，而不是横幅里悄悄少一行）
# ══════════════════════════════════════════════════════════════════════


def _assert_registered() -> None:
    """两个站位值 MUST 都已登记在 `placeholders.PLACEHOLDERS` 且值一致。

    ★ 为什么在 **import 期**断言而不是在用例里：`.env` 改不改、跑不跑测试都不影响
      它 —— 漏登记的那一刻进程就起不来。这正是 `policy_spec.py:44-48` 的既有手法。
    """
    registry = {item.name: item.value for item in placeholders.PLACEHOLDERS}
    problems: list[str] = []
    for name, value in (
        ("CALIBER_STALE_PROGRESS_HOURS", STALE_PROGRESS_HOURS),
        ("CALIBER_BANNER_TIMEOUT_SECONDS", BANNER_TIMEOUT_SECONDS),
    ):
        if name not in registry:
            problems.append(f"{name} 未登记")
        elif registry[name] != value:
            problems.append(f"{name} 登记值 {registry[name]!r} != 真源 {value!r}")
    if problems:
        raise RuntimeError(
            "chunk 口径站位值登记不一致："
            + "；".join(problems)
            + "（见 app/config/placeholders.py）"
        )


_assert_registered()


# ══════════════════════════════════════════════════════════════════════
# 值对象
# ══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class ReindexReport:
    """一次口径分布观测的**不可变**结果（四个互斥桶 + 对账恒等式）。

    ★ 为什么不是「一个百分比」：`chunker_only` 不收敛（它的处置是全链重跑，
      不是跑个脚本），把它放进分母会让百分比永远差几个点 → 又一个「永远红」的灯。
      四桶 + 恒等式把「不可收敛的分量」从百分比里拿出去，改成独立计数
      （`capabilities.py:66-72` 记录的一手教训）。
    """

    universe: int
    ok: int
    hard_stale: int
    chunker_only: int
    embedding_missing: int
    in_dead_documents: int
    newest_ok_at: datetime | None
    newest_stale_at: datetime | None
    oldest_stale_at: datetime | None
    generation_conflicts: int = 0
    """DQ4B-1 的落点：谓词 P1（同一 `(document_id, chunk_index)` 出现多行 `is_latest`）。

    **恒 0**：`parse_job` 重索引时先硬删旧 chunk 再重建，且 `is_latest` 恒为 `true`，
    所以「两代并存」在当前写路径下不可达。它存在的意义不是"检查有没有问题"，
    而是把「4B-2 要不要做」从**会议题**变成一条**查询**：
    一旦它 > 0，说明表结构已经能表示两代并存（4B-2 必须立刻开工，且那是缺陷）。
    """

    @property
    def reconciles(self) -> bool:
        """对账恒等式：`ok + hard_stale + chunker_only + embedding_missing == universe`。

        ★ 这是唯一能同时抓住**漏判**（某行四桶都不落）与**重判**（某行落两桶）的
          断言 —— 而这两种错误在单看任何一个桶时都不可见。
        """
        return (
            self.ok + self.hard_stale + self.chunker_only + self.embedding_missing
            == self.universe
        )


@dataclass(frozen=True, slots=True)
class KbReadability:
    """发布快照里「该场景 kb 集合的向量路可检程度」。

    ★ `chunks_total` / `chunks_hard_stale` 是**口径维度**的计数，**不含授权维度**：
      发布时没有具体请求者，用一个假的 `clearance` 去算 ACL 会让数字变成另一个
      口径的谎言。这句话必须同时出现在响应的 `note` 里。
    """

    kb_count: int
    chunks_total: int
    chunks_hard_stale: int
    documents_indexed: int


@dataclass(frozen=True, slots=True)
class PublishVerdict:
    """W5 的三档判定结果（`blocker` 非空 = 拒绝发布）。"""

    blocker: str | None
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Verdict:
    """告警式的结果（`level ∈ {ok, info, warn}`，**没有 error**）。"""

    level: Literal["ok", "info", "warn"]
    reason: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class DeclarationDrift:
    """版本声明的 DNA 与平台当前 DNA 的差异（只给键名 + 可比的值）。"""

    stale_keys: tuple[str, ...]
    declared: Mapping[str, object] = field(default_factory=dict)
    current: Mapping[str, object] = field(default_factory=dict)


# ══════════════════════════════════════════════════════════════════════
# 读：四个桶 + 对账（唯一函数源）
# ══════════════════════════════════════════════════════════════════════

# 四桶互斥的优先级写在 FILTER 里（hard > chunker > missing > ok）。
# ★ 这就是唯一正确的写法：若 `chunker_only` 只写 `FILTER (WHERE stale_chunker)`
#   而不加 `AND NOT stale_hard`，四桶会重叠，恒等式必失配。
#   `T-4B-3` 的破坏方式正是这个 —— 它是本模块最容易被"顺手简化"掉的一处。
_SQL_REPORT_TEMPLATE = """
WITH f AS (
    SELECT * FROM fn_chunk_reindex_candidates(:model, :text_version, :chunker_version)
)
SELECT
    count(*)                                                                     AS universe,
    count(*) FILTER (WHERE NOT stale_hard AND NOT stale_chunker
                       AND NOT embedding_missing)                                AS ok,
    count(*) FILTER (WHERE stale_hard)                                            AS hard_stale,
    count(*) FILTER (WHERE stale_chunker AND NOT stale_hard)                      AS chunker_only,
    count(*) FILTER (WHERE embedding_missing
                       AND NOT stale_hard AND NOT stale_chunker)                  AS embedding_missing,
    count(*) FILTER (WHERE document_deleted OR NOT document_is_latest)            AS in_dead_documents,
    max(chunk_created_at) FILTER (WHERE NOT stale_hard)                           AS newest_ok_at,
    max(chunk_created_at) FILTER (WHERE stale_hard)                               AS newest_stale_at,
    min(chunk_created_at) FILTER (WHERE stale_hard)                               AS oldest_stale_at
FROM f
{where}
"""

_SQL_GENERATION_CONFLICTS = """
SELECT count(*) AS n FROM (
    SELECT document_id, chunk_index
    FROM chunks WHERE is_latest = true
    GROUP BY 1, 2 HAVING count(*) > 1
) t
"""

_SQL_GENERATION_CONFLICTS_TENANT = """
SELECT count(*) AS n FROM (
    SELECT document_id, chunk_index
    FROM chunks WHERE is_latest = true AND tenant_id = :tenant_id
    GROUP BY 1, 2 HAVING count(*) > 1
) t
"""

# 该场景 kb 集合的口径可检计数。用**同一个函数**（不是另写 predicate），
# 否则"发布时的可检比例"与"端点报的过期数"会是两个数（I-4B-2 的同一理由）。
# 带 `tenant_id` 不是多余的：`kb_ids` 来自版本行，虽然版本行已按租户锁定，
# 但显式带上让「读侧 SQL 一律租户域」这条纪律在文本层也成立。
_SQL_KB_READABILITY = """
WITH f AS (
    SELECT * FROM fn_chunk_reindex_candidates(:model, :text_version, :chunker_version)
)
SELECT
    count(DISTINCT f.kb_id)              AS kb_count,
    count(*)                             AS chunks_total,
    count(*) FILTER (WHERE f.stale_hard) AS chunks_hard_stale
FROM f
WHERE f.kb_id = ANY(:kb_ids) AND f.tenant_id = :tenant_id
"""

_SQL_DOCS_INDEXED = """
SELECT count(*) AS n
FROM documents
WHERE tenant_id = :tenant_id
  AND kb_id = ANY(:kb_ids)
  AND is_latest = true AND deleted_at IS NULL AND status = 'indexed'
"""


def _profile_params() -> dict[str, object]:
    """三个口径参数（唯一来源 `current_index_profile()`，绝不写字面量）。"""
    profile = current_index_profile()
    return {
        "model": profile.embedding_model,
        "text_version": profile.embedding_text_version,
        "chunker_version": profile.chunker_version,
    }


async def collect(
    session: AsyncSession, *, tenant_id: uuid.UUID | None = None
) -> ReindexReport:
    """统计口径分布。

    `tenant_id=None` ⇒ **部署级**（只给计数，不含任何租户明细）；否则只统计该租户。
    ★ 部署级视角是必要的：`--check` 的退出码是全局的（脚本无租户条件），
      运维必须能对上那个数。而**明细**跨租户是越权 —— 故端点只给本租户。
    """
    params = _profile_params()
    if tenant_id is None:
        sql = _SQL_REPORT_TEMPLATE.format(where="")
        conflicts_sql = _SQL_GENERATION_CONFLICTS
    else:
        sql = _SQL_REPORT_TEMPLATE.format(where="WHERE f.tenant_id = :tenant_id")
        params["tenant_id"] = tenant_id
        conflicts_sql = _SQL_GENERATION_CONFLICTS_TENANT

    row = (await session.execute(sa.text(sql), params)).mappings().one()
    conflicts = (
        await session.execute(sa.text(conflicts_sql), params)
    ).scalar_one()
    return ReindexReport(
        universe=int(row["universe"]),
        ok=int(row["ok"]),
        hard_stale=int(row["hard_stale"]),
        chunker_only=int(row["chunker_only"]),
        embedding_missing=int(row["embedding_missing"]),
        in_dead_documents=int(row["in_dead_documents"]),
        newest_ok_at=row["newest_ok_at"],
        newest_stale_at=row["newest_stale_at"],
        oldest_stale_at=row["oldest_stale_at"],
        generation_conflicts=int(conflicts),
    )


async def scenario_kb_readability(
    session: AsyncSession, *, tenant_id: uuid.UUID, kb_ids: Sequence[uuid.UUID]
) -> KbReadability:
    """该场景绑定 kb 集合的口径可检程度（发布快照用）。

    ★ 空 `kb_ids` 不早退：`count(DISTINCT f.kb_id)` 在 `ANY('{}')` 下自然是 0，
      而 `chunks_total` 也自然是 0 —— 这正是「档①」需要的形状（0 chunk），
      提前 `return KbReadability(0,0,0,docs)` 反而会让「空集合」与「真没数据」
      无法区分。**唯一需要早退的是 SQL 参数**：asyncpg 对空数组仍可绑定，无需特殊处理。
    """
    params = _profile_params()
    params["tenant_id"] = tenant_id
    params["kb_ids"] = [str(kb) for kb in kb_ids]

    chunks = (await session.execute(sa.text(_SQL_KB_READABILITY), params)).mappings().one()
    docs = await session.execute(
        sa.text(_SQL_DOCS_INDEXED), {"tenant_id": tenant_id, "kb_ids": params["kb_ids"]}
    )
    return KbReadability(
        kb_count=int(chunks["kb_count"]),
        chunks_total=int(chunks["chunks_total"]),
        chunks_hard_stale=int(chunks["chunks_hard_stale"]),
        documents_indexed=int(docs.scalar_one()),
    )


# ══════════════════════════════════════════════════════════════════════
# 纯判定（零 IO）
# ══════════════════════════════════════════════════════════════════════

BLOCKER_KB_EMPTY_BUT_INDEXED = "KB_EMPTY_BUT_INDEXED"
WARN_STALE_DECLARATION = "STALE_DECLARATION"
WARN_PARTIAL_UNREADABLE = "PARTIAL_UNREADABLE"


def declared_stale_keys(dna_declared: Mapping[str, object] | None) -> tuple[str, ...]:
    """版本声明里与**平台当前** DNA 不一致的键名（只比有值的四项，不比 `env_bound`）。

    ★ 为什么只比四项、不比 `env_bound`：`env_bound` 是**字段名清单**（声明"这四项
      随环境绑定"，不落值），拿它跟当前值比是无意义的比较。这也是为什么本函数
      返回的是**键名**而不是值 —— 环境绑定项的值不该出现在响应里。
    """
    declared = dict(dna_declared or {})
    current = build_dna_declared()
    return tuple(
        sorted(
            key
            for key in current
            if key not in {"env_bound"}
            and declared.get(key) != current[key]
        )
    )


def declaration_drift(dna_declared: Mapping[str, object] | None) -> DeclarationDrift:
    """声明漂移的可读形态（preflight 端点用；只暴露可比的两项值）。"""
    declared = dict(dna_declared or {})
    current = build_dna_declared()
    comparable = ("embedding_text_version", "chunker_version")
    return DeclarationDrift(
        stale_keys=declared_stale_keys(dna_declared),
        declared={key: declared.get(key) for key in comparable},
        current={key: current[key] for key in comparable},
    )


def judge_publish_snapshot(
    readability: KbReadability, *, declared_stale_keys: Sequence[str]
) -> PublishVerdict:
    """W5 的三档判定（DQ4B-5 的落地）。

    | 档 | 数据形状 | 判定 |
    |---|---|---|
    | ① 拒 | `chunks_total == 0` **且** `documents_indexed > 0` | 409（确定性数据损坏） |
    | ② WARN | `chunks_total > 0` 且 `chunks_hard_stale > 0` | 放行 + `PARTIAL_UNREADABLE` |
    | ③ 无提示 | `chunks_total > 0` 且 `chunks_hard_stale == 0` | 放行 |

    ★ 为什么 ② 必须**放行**而不是拒：`retrieve()` 在向量路回 0 行时**不会早退**
      （只有 `authorized_kb_ids` 为空才早退），RRF 接受任意条路径。所以硬 DNA 全不
      匹配的场景**仍然可用**（关键词路），只是降级 —— 而这是
      `DNA与Policy口径对照表-v1.md:14` 写作**预期**的行为。拒绝它等于宣布
      "平台不支持自己文档里承诺的降级模式"。
    ★ 为什么 ① 可以拒：`parse_job` 把 `documents.status='indexed'` 与 chunk 写入放在
      **同一个 commit** 里 → 「有已索引文档、0 条 chunk」只可能是数据被破坏或迁移残留。
      **残余风险**：若将来有人把 chunk 写入拆成两个事务（比如为了流式进度），
      这条判据会变成误伤 —— 届时档① MUST 改成 WARN（U-4B-9）。
    ★ `declared_stale_keys` 非空**一律只 WARN**：拒绝的处置是「重建 draft 以刷新声明」，
      而旧声明恰好是「这个场景是在换模型之前设计的」这条审计事实 —— 拒绝会**销毁
      它想保护的信息**。
    """
    warnings: list[str] = []
    if declared_stale_keys:
        warnings.append(WARN_STALE_DECLARATION)
    if readability.chunks_total and readability.chunks_hard_stale:
        warnings.append(WARN_PARTIAL_UNREADABLE)
    if readability.chunks_total == 0 and readability.documents_indexed > 0:
        return PublishVerdict(
            blocker=BLOCKER_KB_EMPTY_BUT_INDEXED, warnings=tuple(warnings)
        )
    return PublishVerdict(blocker=None, warnings=tuple(warnings))


def verdict(
    report: ReindexReport,
    *,
    now: datetime,
    stale_progress_hours: float = STALE_PROGRESS_HOURS,
) -> Verdict:
    """告警判定。★ 判据用 `newest_ok_at`（最新**合规** chunk 的时间），不用 stale 的时间。

    为什么这个判据**不会变成永久红灯**：
      1. 重算进行中 → 每算完一条 `newest_ok_at` 就往前推 → `info`，不告警；
      2. 重算卡住 / 忘了 → `newest_ok_at` 停住 → 超过阈值变 `warn`，跑一次脚本就消失；
      3. 全库无合规 → `newest_ok_at IS NULL` → 立刻 `warn`（唯一一个「数据缺失」档）。

    `chunker_only` **不进本函数**（I-4B-3）：软 DNA 只出现在横幅与响应里。
    """
    if report.hard_stale == 0:
        return Verdict("ok", "all_aligned")
    if report.newest_ok_at is None:
        return Verdict(
            "warn",
            "no_compliant_chunk",
            f"有 {report.hard_stale} 条 chunk 硬口径不匹配，且全库没有一条合规 chunk",
        )
    stalled = now - report.newest_ok_at
    if stalled > timedelta(hours=stale_progress_hours):
        hours = stalled.total_seconds() / 3600.0
        return Verdict(
            "warn",
            "hard_stale_without_progress",
            f"有 {report.hard_stale} 条 chunk 硬口径不匹配，"
            f"且最新合规 chunk 已 {hours:.1f} 小时未前进",
        )
    return Verdict(
        "info",
        "reindex_in_progress",
        f"有 {report.hard_stale} 条 chunk 硬口径不匹配，重算正在进行中",
    )


# ══════════════════════════════════════════════════════════════════════
# 渲染：启动横幅（异步 + 超时降级）
# ══════════════════════════════════════════════════════════════════════

BANNER_PREFIX = " chunk 口径               : "
BANNER_TIMEOUT_HINT = "统计超时"
BANNER_FAILED_HINT = "统计失败（详见日志）"


def format_banner_line(report: ReindexReport) -> str:
    """横幅正文（**人可读、每次部署必看**）。

    ★ 用 `print` 而不是 `logger.info`：横幅要出现在 `docker logs` 第一屏，
      不被 `LOG_LEVEL` 过滤（同 `capabilities.py:443-445` 的理由）。
    ★ 文案纪律：`chunker_only` 后面必须跟「软 DNA，不拒绝检索」，否则运维看到
      这个数会去跑重算脚本 —— 而脚本**不重切**，跑完这个数也不会归零（R-6）。
    """
    line = (
        f"{report.ok}/{report.universe} 条对齐"
        f" · 硬过期 {report.hard_stale}"
        f" · 建议重切 {report.chunker_only}（软 DNA，不拒绝检索 · 下次全量重建的待办规模）"
        f" · 空向量 {report.embedding_missing}"
    )
    if report.in_dead_documents:
        line += f" · 属已删/旧版文档 {report.in_dead_documents}"
    if report.generation_conflicts:
        line += f" · ▲ 代次冲突 {report.generation_conflicts}（4B-2 必须立即开工）"
    return BANNER_PREFIX + line


async def log_needs_reindex_banner(
    *, timeout: float = BANNER_TIMEOUT_SECONDS
) -> None:
    """启动横幅（**异步 + 超时降级**，绝不阻塞启动、绝不抛）。

    ★ 三条处置，缺一不可：
      ① **超时** → 打「统计超时」，不服务数字，打一个 `rag.caliber.banner_timeout`
         计数。理由是 §U-4B-7：过期扫描是全表扫，若它进了启动路径而无护栏，
         一个十万级 chunk 的库会让 api 起得慢到被健康检查杀掉。
      ② **异常**（库抖动 / 函数缺失，例如只跑到 0028 没跑 0029）→ 打「统计失败」
         并 WARN 一行。启动 MUST 继续（同 `capabilities.py:419-422`：
         "就绪度统计失败不该阻断启动"）。
      ③ **成功** → 打数字。**只放 api 侧**：worker 不检索，跑全场景统计是纯加成本
         （同 T2 自检只在 api 侧的理由）。
    ★ 三个分支**都要**打 `caliber` 的站位值行：那两个值决定"会不会误报 / 会不会
      卡启动"，运维必须在每次部署时看到"它们是站位值"。这一行与 `PLACEHOLDERS`
      同源（`placeholders.render_line`），所以不会与登记表分叉。
    """
    from app.database import SessionLocal

    def _emit(line: str) -> None:
        print(line, flush=True)
        placeholder_line = placeholders.render_line("caliber")
        if placeholder_line:
            print(placeholder_line, flush=True)

    async def _collect() -> ReindexReport:
        async with SessionLocal() as session:
            return await collect(session)

    try:
        report = await asyncio.wait_for(_collect(), timeout=timeout)
    except TimeoutError:
        metrics.incr("rag.caliber.banner_timeout")
        logger.warning(
            "chunk 口径统计超时，横幅只打提示不打数字",
            extra={"timeout_seconds": timeout},
        )
        _emit(f"{BANNER_PREFIX}{BANNER_TIMEOUT_HINT}（>{timeout:.0f}s，本次不展示数字）")
        return
    except Exception:  # noqa: BLE001 —— 统计失败永不阻断启动
        metrics.incr("rag.caliber.banner_failed")
        logger.warning("chunk 口径统计失败（横幅仍输出）", exc_info=True)
        _emit(f"{BANNER_PREFIX}{BANNER_FAILED_HINT}")
        return

    _emit(format_banner_line(report))
    metrics.incr("rag.caliber.hard_stale_observed", report.hard_stale)
    metrics.incr("rag.caliber.chunker_mismatch_observed", report.chunker_only)
    logger.info(
        "chunk 口径快照（启动时）",
        extra={
            "event": "caliber.banner",
            "universe": report.universe,
            "ok": report.ok,
            "hard_stale": report.hard_stale,
            "chunker_only": report.chunker_only,
            "embedding_missing": report.embedding_missing,
            "in_dead_documents": report.in_dead_documents,
            "generation_conflicts": report.generation_conflicts,
            "reconciles": report.reconciles,
        },
    )


__all__ = [
    "BANNER_TIMEOUT_SECONDS",
    "BLOCKER_KB_EMPTY_BUT_INDEXED",
    "DeclarationDrift",
    "ENV_BOUND_FIELDS",
    "KbReadability",
    "PublishVerdict",
    "ReindexReport",
    "STALE_PROGRESS_HOURS",
    "Verdict",
    "collect",
    "declaration_drift",
    "declared_stale_keys",
    "format_banner_line",
    "judge_publish_snapshot",
    "log_needs_reindex_banner",
    "scenario_kb_readability",
    "verdict",
]
