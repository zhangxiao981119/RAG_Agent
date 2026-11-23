"""场景配置自检 S-2 ~ S-9 —— 三个执行时机，两种失败动作。

**动作选择的统一判据**（设计文档 §C.3，一句话）：

    代码错了就别启动，数据错了就喊出来。

| 判据来源 | 时机 | 动作 | 为什么 |
|---|---|---|---|
| **代码常量之间**的关系（S-2a / S-4a / S-6） | T1 启动（无 DB） | `RuntimeError`（拒绝启动） | 不含数据依赖，错了就是代码 bug，启动时必现；没有理由降级 |
| **库里的数据**（S-2b / S-3 / S-4b / S-5 / S-7 / S-8 / S-9） | T2 启动（有 DB，仅 api 侧） | WARN + metric，**永不 raise** | 数据是运维/用户改出来的。让它拒绝启动 = 一个场景的错配置拖垮整个实例，且形成死锁：坏数据在库里 → 实例起不来 → 接口不可用 → 只能手写 SQL 改库。**fail-closed 用错地方就是可用性事故** |
| 能否在**写入时**拒绝（S-2b / S-3 的硬约束） | T4 发布（Step 4 的写接口调用） | `PolicyValidationError` → 400 | 这是唯一能真正阻止坏配置进入系统的位置。函数本步就交付，避免 Step 4 的人自己发明一套判据 |

★ T2 **不放进 `check_capabilities()`**：`self_check_ext.check_capabilities()` 在
  「图与 wiki 都关」时**提前 return**（`self_check_ext.py:322-325`）。本机默认
  `graph_enabled=False`、`wiki_enabled=False` → 放进去等于**永远不会执行**。
  「自检写了一行、永远是绿的」是这类代码的经典陷阱，故独立成函数、由调用点显式调用。

★ T2 只在 **api** 侧跑：worker（`parse_job.py`）不检索，跑全场景扫描是纯加启动成本
  与 DB 压力，且它没有任何「读策略」的路径可以受益。T1 两侧都跑（成本近零，且能保证
  两侧对「规格表是否自洽」持有同一判断 —— 两侧结论不同是最难查的一类问题）。
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import decisions
from app.config.placeholders import for_owner
from app.config.settings import get_settings
from app.infra import metrics
from app.services.scenario import policy_spec, resolver
from app.services.scenario.errors import ScenarioCode, ScenarioError
from app.services.scenario.snapshot import ScenarioPolicy, ScenarioRow, current_dna

logger = logging.getLogger(__name__)

RECALL_TOP_K_DB_CEILING: Final[int] = 1000
"""DB CHECK 给 `recall_top_k` 的兜底上限（`0028` 的
`ck_scenario_versions_policy_recall_top_k` = `::int BETWEEN 1 AND 1000`）。

S-2a 用它判「代码上限与 DB 上限是否自洽」：代码上限若大于它，写入会先被 DB 拒，
运维看到的是「数据库约束冲突」而不是「参数超范围」—— 两处口径分裂。
"""

DB_BINDING_WIRED_STEP: Final[int] = 6
"""`db_binding` 开始被读取的步骤（S-7 用）。Step 6 之前配了也不生效。"""

_THRESHOLD_UPPER_BOUND: Final[float] = 1.0
"""两种分数都在 [0,1]：rerank 是 sigmoid 绝对相关性分，向量余弦归一后同区间。"""


# ══════════════════════════════════════════════════════════════════════
# T1：常量 / 代码自洽 —— 违反即拒绝启动
# ══════════════════════════════════════════════════════════════════════


def check_scenario_constants() -> None:
    """T1：代码常量之间的自洽。违反 → `RuntimeError`（拒绝启动）。

    只查「代码与代码」的关系，**不查库、不读数据**。api 与 worker 两侧都调。
    纯内存、无 IO —— 因此两侧都跑的成本可忽略。
    """
    _s2a_recall_top_k_ceiling()
    _s4a_threshold_spec_range()
    _s6_placeholders_registered()


def _s2a_recall_top_k_ceiling() -> None:
    """S-2a：`RECALL_TOP_K_MAX` 必须落在 `[1, DB 上限]` 内。

    ★ 为什么是 RuntimeError 而不是 WARN：这是**代码常量之间**的关系，与库里的数据
      无关。错了就是代码 bug，每一台机器启动时都必现，没有理由降级成「跑起来再说」。
    ★ 为什么必须有上界：见 `RECALL_TOP_K_DB_CEILING` 的说明。
    """
    ceiling = policy_spec.RECALL_TOP_K_MAX
    if ceiling < 1:
        raise RuntimeError(
            f"RECALL_TOP_K_MAX={ceiling} 小于 1：召回条数为 0 会让候选池恒空 → 全量拒答。"
            "（S-2a，代码常量自检）"
        )
    if ceiling > RECALL_TOP_K_DB_CEILING:
        raise RuntimeError(
            f"RECALL_TOP_K_MAX={ceiling} 超过数据库 CHECK 的上限 "
            f"{RECALL_TOP_K_DB_CEILING}（0028 的 "
            "ck_scenario_versions_policy_recall_top_k）：合法范围内的配置会先被数据库"
            "拒绝，运维看到的是「约束冲突」而不是「参数超范围」——两处口径必须同时改。"
            "（S-2a，代码常量自检）"
        )


def _s4a_threshold_spec_range() -> None:
    """S-4a：阈值规格表 MUST 与写入校验器（`validate_policy`）自洽。

    ★ 这一条与设计文档的字面判据**不同**，理由必须记下来（见交付报告「设计不符」）：
      设计原文写「`min_value is not None and min_value > 0.0`」。
      但 Step 1–2 已落地的规格表里两个阈值字段是 `min_value=0.0`
      （**闭区间**下界），而「必须 > 0」这条严格性由 `validate_policy` 的
      `THRESHOLD_KEYS` 第二道校验承担（`policy_spec.py` 里 `not value > 0.0` → 拒）。
      按原文实现会在**已验收的基线上**立刻抛 `RuntimeError`（启动即失败），
      而把规格表 `min_value` 改成 0.5 之类的正数会让既有断言
      `"必须大于 0" in str(exc)` 变成「不能小于 0.5」，破坏已验收测试。
      所以真正的自洽判据是下面三条 —— 它们检查的是**同一件事**
      （两处不许分叉），但表达成今天可满足、且一旦漂移就会炸的形式：

        ① 上界 MUST 落在 `(0, 1]`（分数域）
        ② 下界 MUST 恰为 `0.0`（闭区间表达不了「>0」；改成别的数就是规格表
           与第二道校验分叉，前端按规格表生成表单就会允许一个必然被拒的值）
        ③ 行为交叉验证：`validate_policy({name: 0.0})` MUST 抛错
           （「第二道闸还在」的可执行证据）
    """
    by_name = {field.name: field for field in policy_spec.POLICY_FIELDS}
    for name in sorted(policy_spec.THRESHOLD_KEYS):
        field = by_name.get(name)
        if field is None:
            raise RuntimeError(
                f"阈值键 {name} 不在 POLICY_FIELDS 里 —— 规格表缺项，"
                "写入校验会在取值时 KeyError。（S-4a，代码常量自检）"
            )

        # ① 上界
        if field.max_value is None or not (0.0 < field.max_value <= _THRESHOLD_UPPER_BOUND):
            raise RuntimeError(
                f"参数 {name} 的 max_value={field.max_value!r} 不在 (0, "
                f"{_THRESHOLD_UPPER_BOUND}] 内：两种分数都在 [0,1]，"
                "上界超出这个区间等于允许一个不可能被满足的阈值。（S-4a，代码常量自检）"
            )

        # ② 下界
        if field.min_value != 0.0:
            raise RuntimeError(
                f"参数 {name} 的 min_value={field.min_value!r} 不是 0.0："
                "阈值的下界是**闭区间**写法，表达不了「必须 > 0」；"
                "那条严格性由 validate_policy 的 THRESHOLD_KEYS 第二道校验承担。"
                "两者必须成对看：改了这一侧就必须同步改另一侧的错误文案与前端表单，"
                "否则规格表说合法、后端说非法。（S-4a，代码常量自检）"
            )

        # ③ 行为交叉验证：第二道闸必须仍然生效
        try:
            policy_spec.validate_policy({name: 0.0})
        except policy_spec.PolicyValidationError:
            pass
        else:
            raise RuntimeError(
                f"参数 {name} 的 0 值被 validate_policy 放行了："
                "0 阈值 = 永不拒答（S-4 的第二道校验丢失）。"
                "规格表下界是 0.0，若第二道校验也消失，前端按规格表填 0 就会静默生效。"
                "（S-4a，代码常量自检）"
            )


def _s6_placeholders_registered() -> None:
    """S-6：场景侧的站位值必须登记在 `app.config.placeholders`（横幅才看得见）。

    ★ 为什么是 RuntimeError：登记表是**代码常量**，漏登记在每台机器上都一样，
      而且漏了之后「横幅少一行」这种症状永远没人发现。宁可起不来。
      `policy_spec` / `resolver` 各自还有一条 import 期断言做同一件事 —— 这里再查一次
      是为了覆盖「有人删掉了那两条 import 期断言」的情况（自检不能只依赖被检查对象
      自己声明「我登记了」）。
    """
    registered = {item.name for item in for_owner("scenario")}
    if not registered:
        raise RuntimeError(
            "app.config.placeholders.PLACEHOLDERS 里没有任何 owner='scenario' 的条目："
            "场景侧的站位值（RECALL_TOP_K_MAX / 场景行缓存 TTL）将不会出现在启动横幅，"
            "等于没人知道它们还没标定。（S-6，代码常量自检）"
        )
    expected = {"RECALL_TOP_K_MAX", "SCENARIO_CACHE_TTL_SECONDS"}
    missing = sorted(expected - registered)
    if missing:
        raise RuntimeError(
            f"站位值 {missing} 未登记在 app/config/placeholders.py。"
            "未登记的站位值不会出现在启动横幅，等于没人知道它没标定。"
            "（S-6，代码常量自检）"
        )


# ══════════════════════════════════════════════════════════════════════
# T2：数据类自检 —— 永不 raise，返回命中计数
# ══════════════════════════════════════════════════════════════════════

SCAN_SQL: Final[str] = """
SELECT
    s.id            AS scenario_id,
    s.tenant_id     AS tenant_id,
    s.scenario_key  AS scenario_key,
    s.status        AS status,
    s.is_builtin    AS is_builtin,
    s.db_binding    AS db_binding,
    v.version       AS version,
    v.policy        AS policy,
    v.kb_ids        AS kb_ids,
    v.binding_mode  AS binding_mode,
    v.dna_declared  AS dna_declared,
    v.checksum      AS checksum
FROM scenarios s
JOIN scenario_versions v
  ON v.scenario_id = s.id
 AND v.state = 'published'
ORDER BY s.tenant_id, s.scenario_key
"""
"""启动扫描的唯一 SQL。列别名与 `resolver.LOOKUP_SQL` **逐字相同**，因此结果行可以
直接喂给同一个 `ScenarioRow` 解析器（自检与运行时用同一份解析逻辑 —— 若自检自己写
一套，「自检说合法、请求时 500」就成了必然）。

★ 这里是全仓**唯一**不带 `tenant_id` 条件的场景读取，三条约束让它安全：
  ① 只在启动期执行一次（不在任何请求路径上）；
  ② 结果只用于**计数与日志**，绝不返回给任何请求；
  ③ 日志 `extra` 里允许出现的内容**仅限**：
     `tenant_id / scenario_key / version`、
     `policy` 四个标量键的**字段名与数值**（`recall_top_k`、`rerank_enabled`、
     两个阈值 —— 都是非敏感的数值型配置）、
     `dna_declared` 的**字段名与口径字符串**（模型名 / 版本号，与启动横幅同源，
     本来就会打印）、`db_binding` 字面量。
     MUST NOT 出现 `kb_ids` / 文档内容 / 用户标识 / 整个 policy 原文。
     ★ 为什么数值可以留：WARN 只报「阈值越界」而不报**哪个键、什么值**，运维只能
       去翻库对比 —— 一条无法定位的告警等于没有告警。数值型配置的敏感度低于
       排查成本，这是有意的取舍（`test_warn_payload_policy` 锁住这条口径）。
  场景的量级是「个位数到十几个」，逐租户查询没有任何收益。
"""

_DNA_FIELD_MAP: Final[tuple[tuple[str, str], ...]] = (
    ("embedding_model", "index.embedding_model"),
    ("embedding_dim", "index.embedding_dim"),
    ("embedding_text_version", "index.embedding_text_version"),
    ("chunker_version", "index.chunker_version"),
    ("graph_extractor_model", "graph.extractor_model"),
    ("graph_prompt_version", "graph.prompt_version"),
    ("wiki_generator_model", "wiki.generator_model"),
    ("wiki_prompt_version", "wiki.prompt_version"),
)
"""`dna_declared` 的每个键该与哪个口径字段比对（S-9 的展开表）。

前 4 项是**代码常量**（迁移里落了字面量），后 4 项是**环境绑定项**：
迁移只声明它们的**字段名**（`env_bound`），不落值 —— 因此它们的「声明」就是
「等于当前部署」，漂移只可能发生在「有人往 dna_declared 里写了一个字面量」。
这正是 Step 1–2 把 4 项设计成 `env_bound` 的原因，此处不改。
"""

_DRIFT_METRIC: Final[str] = "rag.scenario.dna_declared_drift"

# 各判据 → (metric 名, 命中时的 WARN 文案)。集中成表，避免七段几乎相同的代码。
_CHECK_METRICS: Final[dict[str, str]] = {
    "recall_below_final_top_n": "rag.scenario.recall_below_final_top_n",
    "rerank_open_but_platform_off": "rag.scenario.rerank_open_but_platform_off",
    "threshold_out_of_range": "rag.scenario.threshold_out_of_range",
    "rerank_threshold_uncalibrated": "rag.scenario.rerank_threshold_uncalibrated",
    "db_binding_configured": "rag.scenario.db_binding_configured",
    "builtin_policy_not_empty": "rag.scenario.builtin_policy_not_empty",
    "dna_declared_drift": _DRIFT_METRIC,
    "selfcheck_unparsable": "rag.scenario.selfcheck_unparsable",
    "selfcheck_query_failed": "rag.scenario.selfcheck_query_failed",
}
"""判据名 → metric 名（判据名同时是返回 dict 的键，便于测试与日志共用一份清单）。"""

_EMPTY_COUNTS: Final[dict[str, int]] = {
    **{key: 0 for key in _CHECK_METRICS},
    "published_versions_scanned": 0,
}
"""全 0 的基线计数。

★ `published_versions_scanned` **不计入** `_CHECK_METRICS`：它不是判据，而是
  「这次到底扫了多少行」的事实。有它才能区分「全绿」与「一个 published 版本都没扫到」——
  后者看起来同样是 8 个 0，但含义是「自检没生效」而不是「配置没问题」。
"""


async def check_scenario_data(
    session: AsyncSession, *, max_step: int
) -> dict[str, int]:
    """T2：库里的场景配置自检。**永不 raise**，返回各判据的命中计数。

    `max_step` 传 `effective.WIRED_RETRIEVAL_STEP`（S-7 用它判断 `db_binding`
    是否还没到生效的步骤）。用参数而不是 import 常量，是为了让「Step 3 之后
    Step 4/5/6 各改一个调用点」成为可能，而不必回头改判据。

    一次 SQL 拉全量 published 版本，Python 侧跑 S-2b/S-3/S-4b/S-5/S-7/S-8/S-9 ——
    场景数量级是「个位数到十几个」，N 条查询没有任何收益。
    """
    counts = dict(_EMPTY_COUNTS)
    platform = get_settings()
    platform_rerank_on = bool(platform.rerank_enabled)

    try:
        rows = await _load_all_published(session)
    except Exception:  # noqa: BLE001 —— 见 docstring：永不 raise
        # ★ 查询失败（迁移没跑 / 连接断）**不能**当成「全 0 = 全绿」。
        #   单独计一个键并打 ERROR：这是运维必须当场处理的事，不是配置漂移。
        counts["selfcheck_query_failed"] = 1
        metrics.incr(_CHECK_METRICS["selfcheck_query_failed"])
        logger.error(
            "场景自检查询失败（不阻断启动，但结论无效）：请确认 0028 迁移已应用",
            exc_info=True,
            extra={"check": "S-2b/S-3/S-4b/S-5/S-7/S-8/S-9"},
        )
        return counts

    counts["published_versions_scanned"] = len(rows)

    for row in rows:
        try:
            _scan_one(row, counts, platform_rerank_on=platform_rerank_on, max_step=max_step)
        except Exception:  # noqa: BLE001 —— 「永不 raise」是契约，不是尽力而为
            # ★ 最后一道网：本函数的契约是**永不 raise**（违反 = 一行坏数据让整个实例
            #   起不来 = 设计文档说的死锁）。字段级兜底（`to_policy` / `_dna_mapping`）
            #   覆盖已知的两种坏值，这里是结构保证 —— 将来新增判据读了别的列、
            #   而那一列出现了意料之外的类型，也只会让**这一行**计入 unparsable。
            counts["selfcheck_unparsable"] += 1
            logger.warning(
                "场景自检：本行判据执行异常，已跳过（不阻断启动）",
                exc_info=True,
                extra={
                    "tenant_id": str(getattr(row, "tenant_id", "?")),
                    "scenario_key": getattr(row, "scenario_key", "?"),
                    "version": getattr(row, "version", "?"),
                },
            )

    _emit(counts)
    return counts


async def _load_all_published(session: AsyncSession) -> list[ScenarioRow]:
    """读出全部已发布版本行（跨租户，启动期一次性扫描；见 `SCAN_SQL` 的三条约束）。"""
    result = await session.execute(text(SCAN_SQL))
    # 复用 resolver 的行折叠：别名一致 ⇒ 解析逻辑必须只有一份
    return [resolver._row_from(row._mapping) for row in result.all()]  # noqa: SLF001


def _scan_one(
    row: ScenarioRow,
    counts: dict[str, int],
    *,
    platform_rerank_on: bool,
    max_step: int,
) -> None:
    """对一行已发布版本跑全部数据类判据（纯函数，除 `counts` 外无副作用）。"""
    try:
        policy = row.to_policy()
    except ScenarioError:
        # ★ 一行坏数据**不许**让整个实例起不来（§C.3 的判据），也不许让它悄悄跳过：
        #   计入 selfcheck_unparsable 并继续扫下一行 —— 坏行本身由请求期的
        #   fail-closed（`to_policy` → 500）暴露。
        counts["selfcheck_unparsable"] += 1
        logger.warning(
            "场景自检：版本行无法解析为策略（请求期会按 500 处理）",
            extra={
                "tenant_id": str(row.tenant_id),
                "scenario_key": row.scenario_key,
                "version": row.version,
            },
        )
        return

    # `dna_declared` 与 `policy` **对称**兜底：两者都是 jsonb，都可能被绕过写入路径
    # 塞进非对象值（`jsonb_typeof` 的 CHECK 只挡正常写入）。policy 侧由 `to_policy`
    # 兜成 `ScenarioError`；dna 侧原先完全没有 → `dict(['not-a-mapping'])` 直接抛
    # ValueError 冒到 lifespan，**api 起不来**。
    # ★ 与 policy 的唯一差别：dna 只喂 S-9 一条判据，故解析失败**只跳过 S-9**，
    #   其余判据照跑（口径相同、爆炸半径更小）。
    declared_dna: Mapping[str, object] | None
    try:
        declared_dna = _dna_mapping(row)
    except ScenarioError:
        counts["selfcheck_unparsable"] += 1
        declared_dna = None
        logger.warning(
            "场景自检：dna_declared 不是 JSON 对象（S-9 跳过本行，其余判据照跑）",
            extra={
                "tenant_id": str(row.tenant_id),
                "scenario_key": row.scenario_key,
                "version": row.version,
                "type": type(row.dna_declared).__name__,
            },
        )

    _check_recall_vs_final_top_n(row, policy, counts)
    _check_rerank_open_but_platform_off(row, policy, counts, platform_rerank_on=platform_rerank_on)
    _check_threshold_range(row, policy, counts)
    _check_rerank_threshold_uncalibrated(row, policy, counts, platform_rerank_on=platform_rerank_on)
    _check_db_binding(row, counts, max_step=max_step)
    _check_builtin_policy(row, policy, counts)
    _check_dna_drift(row, counts, declared=declared_dna)


def _dna_mapping(row: ScenarioRow) -> Mapping[str, object]:
    """把 jsonb 的 `dna_declared` 解析成 Mapping；非对象 → `ScenarioError`。

    jsonb 可能是 `'abc'`（字符串）/ `[1,2]`（数组）/ `123`（数字）—— `dict()` 对
    前两者分别抛 ValueError / TypeError。统一归到 `ScenarioError`，让调用点和
    `to_policy()` 用**同一种**失败类型处理（结构对称，不为第二个 jsonb 列发明新异常）。
    """
    raw = row.dna_declared
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ScenarioError(
            ScenarioCode.UNRESOLVABLE,
            # 诊断只放类型名：不把脏值本身写进日志（可能是任意长度/含用户数据）
            diagnostic={"reason": "dna_declared_not_object", "type": type(raw).__name__},
        )
    return raw


def _check_recall_vs_final_top_n(
    row: ScenarioRow, policy: ScenarioPolicy, counts: dict[str, int]
) -> None:
    """S-2b：`min(effective_recall_top_k, RECALL_TOP_K_MAX) >= TOP_K_RERANK`。

    ★ 为什么是 WARN 而不是拒绝启动：见模块 docstring 的判据表 —— 这是数据问题，
      而且它**已经生效**（进上下文的条数真的变少了），不属于「配了不生效」的静默
      no-op。硬约束放在 T4（发布期拒绝），T2 只负责让已有的越界可见。
    ★ 为什么**不 clamp 到 final_top_n**：clamp 会让「我配了 5」变成看不见的 8 ——
      比 WARN 更糟（WARN 至少留痕）。`clamp` 只用于平台**硬上限**
      （`effective.py` 的 `RECALL_TOP_K_MAX`），不用于这条语义协调。
    """
    effective = policy.effective_recall_top_k()
    # 平台上限会在取值层被裁（effective.py），这里按**裁后**的值判，避免
    # 「上限下调后所有历史行都报 WARN」这种噪音
    effective = min(effective, policy_spec.RECALL_TOP_K_MAX)
    if effective < decisions.TOP_K_RERANK:
        counts["recall_below_final_top_n"] += 1
        logger.warning(
            "场景自检：recall_top_k 小于最终取用条数，进上下文的条数会被压低",
            extra={
                "tenant_id": str(row.tenant_id),
                "scenario_key": row.scenario_key,
                "recall_top_k": effective,
                "final_top_n": decisions.TOP_K_RERANK,
            },
        )


def _check_rerank_open_but_platform_off(
    row: ScenarioRow,
    policy: ScenarioPolicy,
    counts: dict[str, int],
    *,
    platform_rerank_on: bool,
) -> None:
    """S-3：单向 AND 造成的静默无效（场景开、平台关 → 有效值仍是关）。

    ★ 为什么 WARN 不 raise：平台关重排是本机 CPU 的现实决策（`retrieve/__init__.py`
      记录 50 条 66s），不是错误。要暴露的是「配置者以为开了」。
    """
    if platform_rerank_on or policy.rerank_enabled is not True:
        return
    counts["rerank_open_but_platform_off"] += 1
    logger.warning(
        "场景自检：场景声明 rerank_enabled=true，但平台 RERANK_ENABLED=false —— "
        "单向 AND 使该键静默无效（有效值仍为 false）",
        extra={
            "tenant_id": str(row.tenant_id),
            "scenario_key": row.scenario_key,
        },
    )


def _check_threshold_range(
    row: ScenarioRow, policy: ScenarioPolicy, counts: dict[str, int]
) -> None:
    """S-4b：已发布版本的阈值 ∉ `(0, 1]`。

    ★ 命中即意味着**绕过写入路径**：写入侧有两道（范围 + 必须 > 0），DB 侧还有
      CHECK（`::float8 BETWEEN 0.0 AND 1.0`），`0.0` 三者都过不了。所以能落库的
      越界值只可能来自直接写 SQL —— 这正是要留观测的场景。
    ★ 为什么不 clamp：阈值 clamp 会**改变拒答行为**（比 recall_top_k 敏感得多），
      把 0.0 悄悄拉到 0.01 仍然是「永不拒答」，并没有修好任何东西，只让日志更干净。
    """
    lowered = policy.declared()
    bad = [
        name
        for name in sorted(policy_spec.THRESHOLD_KEYS)
        if name in lowered and not (0.0 < float(lowered[name]) <= _THRESHOLD_UPPER_BOUND)  # type: ignore[arg-type]
    ]
    if not bad:
        return
    counts["threshold_out_of_range"] += 1
    logger.warning(
        "场景自检：阈值越界（只可能来自绕过写入路径，有效值**原样生效**）",
        extra={
            "tenant_id": str(row.tenant_id),
            "scenario_key": row.scenario_key,
            "fields": {name: lowered[name] for name in bad},
        },
    )


def _check_rerank_threshold_uncalibrated(
    row: ScenarioRow,
    policy: ScenarioPolicy,
    counts: dict[str, int],
    *,
    platform_rerank_on: bool,
) -> None:
    """S-5：设了 rerank 阈值但平台重排关着 —— 这个键现在**休眠**，一开就生效。

    ★ 理由：平台的那个阈值本身是**未标定的占位值**（`decisions.py` 自述）。
      一旦有人打开平台重排，场景里抄来的这个数就立刻生效 —— 而那时没人会想起
      「这个值是抄来的」。WARN 是在「还没打开」的窗口里提醒。
    """
    if platform_rerank_on or policy.relevance_threshold_rerank is None:
        return
    counts["rerank_threshold_uncalibrated"] += 1
    logger.warning(
        "场景自检：场景设置了 relevance_threshold_rerank，但平台 RERANK_ENABLED=false。"
        "该值当前休眠，一旦打开平台重排就会生效 —— 请先跑 "
        "scripts/calibrate_threshold.py --mode rerank 重新标定",
        extra={
            "tenant_id": str(row.tenant_id),
            "scenario_key": row.scenario_key,
            "relevance_threshold_rerank": policy.relevance_threshold_rerank,
        },
    )


def _check_db_binding(row: ScenarioRow, counts: dict[str, int], *, max_step: int) -> None:
    """S-7：`db_binding` 已配但还没到生效的步骤（Step 6 才读）。

    ★ 为什么要让它可见：`db_binding` 列在 0028 就建好了，若运维现在就填上，
      行为**完全不变** —— 这是典型的「配了不生效」，不喊出来就永远不会被发现。
    """
    if row.db_binding is None or max_step >= DB_BINDING_WIRED_STEP:
        return
    counts["db_binding_configured"] += 1
    logger.warning(
        "场景自检：db_binding 已配置，但该键自 Step %d 起才被读取（当前接线到 Step %d）—— "
        "配了不生效",
        DB_BINDING_WIRED_STEP,
        max_step,
        extra={
            "tenant_id": str(row.tenant_id),
            "scenario_key": row.scenario_key,
            "db_binding": row.db_binding,
        },
    )


def _check_builtin_policy(
    row: ScenarioRow, policy: ScenarioPolicy, counts: dict[str, int]
) -> None:
    """S-8：内置场景的 policy 必须为空（内置 ≡ 平台默认）。

    ★ 这是跨表约束（`is_builtin` 在 `scenarios`、`policy` 在 `scenario_versions`），
      DB 层无法表达。写入侧（Step 4）会拒，但「有人绕过程序直写 SQL」只能靠这里发现。
      内置场景被改了 = 「默认口径被偷偷改掉」，是最该报警的一件事。
    """
    if not row.is_builtin:
        return
    keys = sorted(policy.declared())
    if not keys:
        return
    counts["builtin_policy_not_empty"] += 1
    logger.warning(
        "场景自检：内置场景的 policy 不为空 —— 内置场景的语义是「恒等于平台默认」，"
        "被改动等于默认口径被偷偷改掉（只可能来自直写 SQL）",
        extra={
            "tenant_id": str(row.tenant_id),
            "scenario_key": row.scenario_key,
            "keys": keys,
        },
    )


def _check_dna_drift(
    row: ScenarioRow, counts: dict[str, int], *, declared: Mapping[str, object] | None
) -> None:
    """S-9：内置行的 `dna_declared` 与当前部署的口径不一致（声明过期）。

    ★ 为什么不 raise：DNA 漂移的真实后果是「场景声明**不可信**」，而不是「检索算错」
      —— 检索只从平台函数读 DNA（`snapshot.py` 的 `current_dna()`），从不读
      `dna_declared`。所以它是文档/审计问题，WARN 恰当。
    ★ `declared is None` = 该行的 dna_declared 不是 JSON 对象，已由 `_scan_one`
      计入 `selfcheck_unparsable` 并 WARN 过；此处直接跳过，不重复计数、不再解析。
    """
    if not row.is_builtin or declared is None:
        return
    index, graph, wiki = current_dna()
    actuals = {
        "index.embedding_model": index.embedding_model,
        "index.embedding_dim": index.embedding_dim,
        "index.embedding_text_version": index.embedding_text_version,
        "index.chunker_version": index.chunker_version,
        "graph.extractor_model": graph.extractor_model,
        "graph.prompt_version": graph.prompt_version,
        "wiki.generator_model": wiki.generator_model,
        "wiki.prompt_version": wiki.prompt_version,
    }
    drift: dict[str, str] = {}
    for declared_key, actual_path in _DNA_FIELD_MAP:
        if declared_key not in declared:
            continue  # 未声明（环境绑定项的正常形态）→ 不参与漂移判定
        actual = actuals[actual_path]
        if str(declared[declared_key]) != str(actual):
            drift[declared_key] = f"{declared[declared_key]} → {actual}"
    if not drift:
        return
    counts["dna_declared_drift"] += 1
    logger.warning(
        "场景自检：dna_declared 与当前部署口径不一致（声明过期；检索行为不受影响，"
        "因为 DNA 只从平台函数读）",
        extra={
            "tenant_id": str(row.tenant_id),
            "scenario_key": row.scenario_key,
            "drift": drift,
        },
    )


def _emit(counts: dict[str, int]) -> None:
    """把命中数打成 metric + 一行结构化 INFO（全 0 也要有痕）。

    ★ 全 0 也必须打这一行：否则「自检跑了、什么都没发现」与「自检根本没跑」
      在日志里长得一样。
    """
    for key, metric_name in _CHECK_METRICS.items():
        if counts.get(key):
            metrics.incr(metric_name, counts[key])
    logger.info(
        "场景自检完成（数据类，仅 WARN；判据与计数一并留痕）",
        extra={"scenario_selfcheck": dict(counts)},
    )


# ══════════════════════════════════════════════════════════════════════
# T4：发布期校验 —— 唯一能真正阻止坏配置进入系统的位置
# ══════════════════════════════════════════════════════════════════════


def check_policy_publishable(
    policy: Mapping[str, object] | None, *, platform_rerank_enabled: bool
) -> dict[str, object]:
    """T4：发布前校验。合规返回可落库的 dict，不合规抛 `PolicyValidationError`。

    ★ Step 3 **只有测试调它** —— 但必须现在写：硬约束此刻就该定义好，
      否则 Step 4 的人在写接口时会顺手发明一套判据（两套判据必然分叉）。
    ★ 复用 `validate_policy`（键闭集 / 类型 / 范围 / 阈值 > 0），**不重写判据**：
      S-1 与 S-4 已经把它钉住了，这里只补两条**只能在「即将发布」这个时点**判的事。
    """
    checked = policy_spec.validate_policy(policy)

    recall = checked.get("recall_top_k")
    if recall is not None and int(recall) < decisions.TOP_K_RERANK:  # type: ignore[arg-type]
        raise policy_spec.PolicyValidationError(
            f"recall_top_k={recall} 小于最终取用条数 {decisions.TOP_K_RERANK}："
            "候选池比取用数小，进上下文的条数会被压低",
            field="recall_top_k",
        )

    if checked.get("rerank_enabled") is True and not platform_rerank_enabled:
        raise policy_spec.PolicyValidationError(
            "平台未启用重排（RERANK_ENABLED=false），本键不会生效："
            "请先确认算力或去掉本键",
            field="rerank_enabled",
        )
    return checked
