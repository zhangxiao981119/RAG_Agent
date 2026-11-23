"""场景口径快照：**纯数据 + 纯函数**，本模块无 IO、不读 DB、不打日志。

三件事在这里：

  1. `ScenarioPolicy` —— 稀疏覆盖集的**值对象**。全部字段 `None` = 跟随平台。
  2. `ScenarioSnapshot` —— 一次请求解析出的不可变口径快照（frozen，构造后不可改）。
  3. `build_snapshot()` —— §C.3.1 六行解析真值表的**唯一实现**，100% 可单测。

★ 为什么每个 policy 字段是 `Optional` 而不是给「等于平台默认」的字面量默认值：
  字面量默认值会把平台当前值**复制**一份进代码，平台一改就出现两份不同的「默认」。
  `None` 表示「不覆盖」，取值时现读 `decisions` / `settings` —— 单一真源。

★ 为什么 `index` / `graph` / `wiki` 只从三个 `current_*_profile()` 读，
  **绝不从 `scenario_versions.dna_declared` 读**：
  DNA（口径基因）锁在 L0 平台层。若从行读，一次
  `UPDATE scenario_versions SET dna_declared=...` 就能让某场景偷偷换口径 ——
  正是「静默劣化」的入口。`dna_declared` 只是声明镜像，价值在审计与漂移检测。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from typing import Final, Literal

from app.config import decisions
from app.config.graph_profile import GraphProfile, current_graph_profile
from app.config.index_profile import IndexProfile, current_index_profile
from app.config.wiki_profile import WikiProfile, current_wiki_profile
from app.services.scenario.errors import ScenarioCode, ScenarioError
from app.services.scenario.policy_spec import POLICY_FIELDS, type_ok

BUILTIN_SCENARIO_KEY = "default"
"""内置场景保留 key（迁移 0028 创建）。应用层 MUST 拒绝其它场景改用这个 key。"""

BUILTIN_FALLBACK_VERSION = 0
"""内置行缺失时的版本号。

★ 用 0 而不是 1：1 留给真实的第一个版本行，否则 `fingerprint` 会与真实 v1 撞车
  （审计时无法区分「有 v1 行」与「漏补种」）。0 = 无版本行的显式表达。
"""


# ══════════════════════════════════════════════════════════════════════
# 一、值对象
# ══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class ScenarioPolicy:
    """场景级策略覆盖。全部字段 `None` = 跟随平台（稀疏覆盖语义）。"""

    recall_top_k: int | None = None
    rerank_enabled: bool | None = None
    relevance_threshold_vector: float | None = None
    relevance_threshold_rerank: float | None = None

    # ── 有效值解析（None 折叠为平台值） ────────────────────────────
    def effective_recall_top_k(self) -> int:
        return decisions.TOP_K_RECALL if self.recall_top_k is None else self.recall_top_k

    def effective_rerank_enabled(self, platform_enabled: bool) -> bool:
        """I-2「覆盖只能收紧」在取值层的落实：场景能把重排关掉，不能把关掉的重排打开。

        ★ 单向 AND 的理由：rerank 的开启成本由**平台**承担（模型常驻内存 / CPU 上
          50 条耗时 66s）。若场景能自行打开，一个场景的配置就能把整台机器的检索
          P95 拖垮，而平台侧开关形同虚设。
        """
        if self.rerank_enabled is None:
            return platform_enabled
        return platform_enabled and self.rerank_enabled

    def effective_threshold(self, *, use_rerank: bool) -> float:
        """按当前打分口径取阈值。

        两个键分开的理由：两种分数的分布不同（rerank 在线比绝对相关性分、
        重排降级比余弦分），重排服务抖动时两种口径会**交替出现**，
        单一阈值不可预测（decisions.py:24-41）。
        """
        if use_rerank:
            return (
                decisions.RELEVANCE_THRESHOLD_RERANK
                if self.relevance_threshold_rerank is None
                else self.relevance_threshold_rerank
            )
        return (
            decisions.RELEVANCE_THRESHOLD_VECTOR
            if self.relevance_threshold_vector is None
            else self.relevance_threshold_vector
        )

    def declared(self) -> dict[str, object]:
        """只保留**显式设置过**的键（稀疏覆盖集的原样形态，落库/指纹用）。"""
        return {
            name: value
            for name, value in (
                ("recall_top_k", self.recall_top_k),
                ("rerank_enabled", self.rerank_enabled),
                ("relevance_threshold_vector", self.relevance_threshold_vector),
                ("relevance_threshold_rerank", self.relevance_threshold_rerank),
            )
            if value is not None
        }


SCENARIO_POLICY_KEYS: Final[tuple[str, ...]] = tuple(
    field.name for field in fields(ScenarioPolicy)
)
"""`ScenarioPolicy` 能承载的键（**从 dataclass 字段推导**，不手抄）。

场景层只认识这 4 个键；闭集里将来新增的键要先加到这里才算真接线。
`test_resolver_policy.py::test_scenario_policy_keys_are_wired` 断言它与
`WIRED_KEYS` 相等 —— 两份清单不一致 = 键「能写不能读」或「能读不能写」。
"""


def _build_type_map() -> dict[str, Literal["int", "bool", "float"]]:
    """import 期把「场景层能读的键」映射到规格表里的类型；缺一个就立刻炸。

    ★ 为什么不等请求期再 KeyError：那时错误会被 `_resolve_or_reject` 包成 500，
      运维看到的是「场景配置不可解析」而不是「代码里少了一条规格」。
      配置类错误必须在启动/导入时暴露。
    """
    by_name = {field.name: field.type for field in POLICY_FIELDS}
    missing = [key for key in SCENARIO_POLICY_KEYS if key not in by_name]
    if missing:
        raise RuntimeError(
            f"ScenarioPolicy 的键 {missing} 在 policy_spec.POLICY_FIELDS 里没有规格 —— "
            "新增策略键时必须同时补齐规格表与类型"
        )
    return {key: by_name[key] for key in SCENARIO_POLICY_KEYS}


_POLICY_TYPE_BY_NAME: Final[dict[str, Literal["int", "bool", "float"]]] = _build_type_map()


@dataclass(frozen=True, slots=True)
class ScenarioRow:
    """`_load_published` 的读出结果 —— 已发布版本行 + 场景身份行的**原始值**。

    ★ 这是缓存里存的类型（不是 `ScenarioSnapshot`）：行与请求无关，快照含请求态
      `kb_ids`。缓存快照 = 把 A 用户的请求集合发给 B 用户，是最严重的一类越权。
    """

    tenant_id: uuid.UUID
    scenario_id: uuid.UUID
    scenario_key: str
    status: str
    is_builtin: bool
    db_binding: str | None
    version: int
    policy: Mapping[str, object]
    kb_ids: tuple[str, ...]
    binding_mode: str
    # 声明镜像：**不参与解析**，只为审计与「代码 bump 了但迁移没跟」的漂移检测留存
    dna_declared: Mapping[str, object]
    checksum: str

    @property
    def declared_kb_ids(self) -> tuple[uuid.UUID, ...]:
        """场景声明的知识集合（审计用）。

        非法 UUID 一律视为数据损坏并抛 `SCENARIO_UNRESOLVABLE`（fail-closed）：
        静默跳过会让「场景声明里少了一个库」这件事永远查不出来。
        """
        result: list[uuid.UUID] = []
        for raw in self.kb_ids:
            try:
                result.append(uuid.UUID(raw))
            except (ValueError, AttributeError, TypeError) as exc:
                raise ScenarioError(
                    ScenarioCode.UNRESOLVABLE,
                    diagnostic={
                        "reason": "invalid_kb_id_in_scenario_version",
                        "scenario_id": str(self.scenario_id),
                    },
                ) from exc
        return tuple(result)

    def to_policy(self) -> ScenarioPolicy:
        """把稀疏覆盖集折叠成值对象。

        未知键或类型不符在这里**不该出现**（写入时已由 policy_spec 校验 + DB CHECK
        兜底），但既然它来自库，仍按「只取认识的键、类型不符即损坏」处理：
        合法的覆盖集缺失某个键是正常，出现类型错的值是不可解析。

        ★ 类型判定与写入校验**共用** `policy_spec.type_ok`：
          两边各写一份的话，写入严、读取松（或反之）就会出现「能落库但解析 500」
          或「落库就该拒但被放行」—— 而这两种错都不会在任何日志里留下痕迹。
          读取侧比写入侧更严没有意义，更松则是漏。
        """
        raw = self.policy or {}
        if not isinstance(raw, Mapping):
            raise ScenarioError(
                ScenarioCode.UNRESOLVABLE,
                diagnostic={
                    "reason": "policy_not_object",
                    "scenario_id": str(self.scenario_id),
                },
            )
        values: dict[str, object] = {}
        for name in SCENARIO_POLICY_KEYS:
            if name not in raw:
                continue
            value = raw[name]
            expected = _POLICY_TYPE_BY_NAME[name]
            if not type_ok(value, expected):
                raise ScenarioError(
                    ScenarioCode.UNRESOLVABLE,
                    diagnostic={
                        "reason": f"policy_type_mismatch:{name}",
                        "scenario_id": str(self.scenario_id),
                        # 记类型而不是值：值可能很长，且类型已足够定位
                        "actual_type": type(value).__name__,
                        "expected_type": expected,
                    },
                )
            values[name] = value
        return ScenarioPolicy(**values)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ScenarioSnapshot:
    """一次请求解析出的不可变口径快照。构造后不得修改（frozen）。"""

    # ── 身份 ────────────────────────────────────────────────────────
    # scenario_id / checksum 为 None 只发生在 source='builtin_fallback'
    # （本租户还没补种内置行）。这不影响任何**检索行为**：行为字段与有行时逐字段相等，
    # 但 `version` 与 `fingerprint` **必须不同**（兜底取 0 防与真实 v1 撞车）—— 见 test_scenario_wiring:529。
    scenario_id: uuid.UUID | None
    scenario_key: str
    version: int
    checksum: str | None
    is_builtin: bool

    # ── 口径基因（只从平台函数读，绝不从 dna_declared 读）────────────
    index: IndexProfile
    graph: GraphProfile
    wiki: WikiProfile

    # ── 策略 ────────────────────────────────────────────────────────
    policy: ScenarioPolicy

    # ── 绑定 ────────────────────────────────────────────────────────
    binding_mode: Literal["explicit", "unrestricted"]
    # 场景声明的集合（审计用）
    binding_kb_ids: tuple[uuid.UUID, ...]
    # **已过场景门禁的请求集合**（下游直接用这个）
    kb_ids: tuple[uuid.UUID, ...]
    # Step 6 前恒为 None 且无人读
    db_binding: str | None

    # ── 来源（审计/排障用；决定 R5 与 R6 在日志里可区分）───────────
    source: Literal["builtin_row", "builtin_fallback", "application"]

    @property
    def fingerprint(self) -> str:
        """场景口径指纹 = sha1(key|version|index.fingerprint|canonical_policy)[:12]。

        用途：审计日志一行就能回答「这次回答是按哪个场景哪个版本哪个口径给的」。
        只用**已声明的**覆盖键参与指纹：稀疏覆盖集里「没设」与「设成平台值」
        是两种配置，但只在前者需要区分时才有意义 —— 这里按声明原样算，
        两者会得到不同指纹，符合「配置不同即版本不同」的预期。
        """
        raw = "|".join(
            (
                self.scenario_key,
                str(self.version),
                self.index.fingerprint,
                _canonical_policy(self.policy),
            )
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


# ══════════════════════════════════════════════════════════════════════
# 二、纯函数
# ══════════════════════════════════════════════════════════════════════


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _canonical_policy(policy: ScenarioPolicy) -> str:
    return _canonical_json(policy.declared())


def compute_checksum(
    *, policy: Mapping[str, object], kb_ids: Sequence[str], binding_mode: str,
    dna_declared: Mapping[str, object],
) -> str:
    """版本行内容的规范校验和 = sha256(规范序列化)[:16]。

    与迁移 `0028_add_scenarios.py::compute_checksum` 必须**逐字节一致**：
    DB 无法重算（`jsonb::text` 的键序与空格不受控），所以只能靠
    `test_scenario_checksum_matches_runtime` 把「迁移与运行时同源」锁死。
    这也意味着两边都**不能**在不改另一边的同时改这里的规范形式。
    """
    raw = _canonical_json(
        {
            "binding_mode": binding_mode,
            "dna_declared": dict(dna_declared),
            "kb_ids": list(kb_ids),
            "policy": dict(policy),
        }
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def current_dna() -> tuple[IndexProfile, GraphProfile, WikiProfile]:
    """平台口径三元组的组合器（方案甲：三个 `current_*_profile()` 一行不改）。

    ★ 为什么不给三个函数加 `snapshot` 可选参数：场景**根本改不了** DNA（I-6），
      所以「参数化」这个能力本身没有存在理由；一个必须被忽略的参数是 API 谎言，
      而一旦有人真传了不同值就会静默生效。此组合器让调用方一次拿到三个口径，
      同时保证「口径只有平台一个真源」不会因未来重构而悄悄漂移
      （`test_current_dna_equals_three_profiles` 守这条）。
    """
    return current_index_profile(), current_graph_profile(), current_wiki_profile()


def _builtin_identity() -> dict[str, object]:
    """内置行缺失时的同值身份字段（source='builtin_fallback'）。

    ★ 为什么允许「缺行」而不抛 500：内置场景的快照**定义上等于平台默认**，
      缺失不产生任何行为差异（由 `test_builtin_row_equals_builtin_fallback` 锁死）；
      而把它做成 500 会让「新租户创建后忘记补种」这条冷路径上的疏漏
      直接变成该租户全部集成请求失败。两害相权取「同值 + 打点」。
    """
    return {
        "scenario_id": None,
        "scenario_key": BUILTIN_SCENARIO_KEY,
        "version": BUILTIN_FALLBACK_VERSION,
        "checksum": None,
        "is_builtin": True,
        "binding_mode": "unrestricted",
        "binding_kb_ids": (),
        "db_binding": None,
        "policy": ScenarioPolicy(),
        "source": "builtin_fallback",
    }


def _identity_from_row(row: ScenarioRow, *, source: str) -> dict[str, object]:
    return {
        "scenario_id": row.scenario_id,
        "scenario_key": row.scenario_key,
        "version": row.version,
        "checksum": row.checksum,
        "is_builtin": row.is_builtin,
        "binding_mode": row.binding_mode,
        "binding_kb_ids": row.declared_kb_ids,
        "db_binding": row.db_binding,
        "policy": row.to_policy(),
        "source": source,
    }


def build_snapshot(
    *,
    row: ScenarioRow | None,
    tenant_id: uuid.UUID,
    scenario_id: uuid.UUID | None,
    requested_kb_ids: Sequence[uuid.UUID],
) -> ScenarioSnapshot:
    """六行解析真值表（§C.3.1）的唯一实现，**顺序即语义**。

    失败一律 raise `ScenarioError`，**绝不返回默认快照兜底**（fail-closed）。

    参数说明（与设计文档签名的差异，已在交付报告登记）：
      `scenario_id` 是**应用绑定的场景 ID**（`application.scenario_id`）。
      设计文档 §C.3.3 的骨架只给了 `row` / `tenant_id` / `requested_kb_ids`，
      但 R1（引用的场景解析不出 → 500）与 R5（未绑定 → 内置）在 `row is None` 时
      的结局**相反**，而 `row is None` 无法区分二者。因此必须额外传入
      `scenario_id`，否则真值表无法完整表达在纯函数里。
    """
    requested = tuple(requested_kb_ids)

    # ── R1：绑定了场景却解析不出（不存在 / 无 published 版本 / 不属于本租户）
    #          —— 数据一致性破坏，**绝不退回默认场景** ──────────────────
    if scenario_id is not None and (row is None or row.tenant_id != tenant_id):
        raise ScenarioError(
            ScenarioCode.UNRESOLVABLE,
            diagnostic={
                "reason": "scenario_not_found",
                "tenant_id": str(tenant_id),
                "scenario_id": str(scenario_id),
            },
        )
    # 内置分支若查出「不是本租户的内置行」也是数据损坏（SQL 已过滤，纯函数再兜一次）
    if scenario_id is None and row is not None and (
        row.tenant_id != tenant_id or not row.is_builtin
    ):
        raise ScenarioError(
            ScenarioCode.UNRESOLVABLE,
            diagnostic={
                "reason": "builtin_row_inconsistent",
                "tenant_id": str(tenant_id),
                "scenario_id": str(row.scenario_id),
            },
        )

    # ── 行本身可信吗（与 R1 同族，**必须先于 R2/R3/R4**）───────────────────
    # ★ 为什么放在 R2 之前而不是 R2/R3 之后：
    #   这两条与「请求」无关，只与「库里的行是不是合法配置」有关，和 R1 是同一族
    #   （所以也复用同一个 500 码）。放在 R2 之后会留下一条绕过路径 ——
    #   空请求会在 R2 处直接返回快照，把一个非法 binding_mode 原样塞进
    #   `ScenarioSnapshot.binding_mode`（该字段声明为
    #   `Literal["explicit","unrestricted"]`），而快照的这条不变量本该**全路径**成立。
    #   这不会削弱 R3 先于 R4 的防枚举顺序（两者都是 400，与这里无关）。
    if row is not None:
        if row.binding_mode not in ("explicit", "unrestricted"):
            raise ScenarioError(
                ScenarioCode.UNRESOLVABLE,
                diagnostic={
                    "reason": "invalid_binding_mode",
                    "tenant_id": str(tenant_id),
                    "scenario_id": str(row.scenario_id),
                    # 记的是「有个非法值」而不是值本身：值是脏数据，不该进日志正文
                    "actual_type": type(row.binding_mode).__name__,
                },
            )
        if row.binding_mode == "unrestricted" and not row.is_builtin:
            # 设计文档 §A.3：「unrestricted 只允许内置场景使用」。
            # 非内置场景被设成 unrestricted，R4 的范围检查会被**整条跳过** ——
            # 那正是本抽象要防的 fail-open（「忘了绑定知识库」= 静默放开全部范围）。
            # DB 无法跨表约束（is_builtin 在 scenarios、binding_mode 在
            # scenario_versions），所以这道闸只能在读侧守。
            # Step 1–2 没有写入路径，但 Step 4 一旦开写接口，这里就是唯一防线。
            raise ScenarioError(
                ScenarioCode.UNRESOLVABLE,
                diagnostic={
                    "reason": "non_builtin_unrestricted",
                    "tenant_id": str(tenant_id),
                    "scenario_id": str(row.scenario_id),
                },
            )

    index, graph, wiki = current_dna()

    if scenario_id is not None:
        identity = _identity_from_row(row, source="application")  # type: ignore[arg-type]
    elif row is not None:
        identity = _identity_from_row(row, source="builtin_row")
    else:
        identity = _builtin_identity()

    def _snapshot(kb_ids: tuple[uuid.UUID, ...]) -> ScenarioSnapshot:
        return ScenarioSnapshot(index=index, graph=graph, wiki=wiki, kb_ids=kb_ids, **identity)  # type: ignore[arg-type]

    # ── R2：请求集合为空 → 不额外判空、不做场景门禁 ────────────────────
    #   理由（安全）：若把 status 判定放在前面，攻击者可用「空 kb_ids」探测
    #   某个停用场景的边界（400 vs 403 的差异泄露信息）。空请求对
    #   `explicit` 与 `unrestricted` 两个分支的结局本来就相同（都 403），
    #   合并成一条可避免「内置场景空请求 403、非内置场景空请求 200」的实现分叉。
    if not requested:
        return _snapshot(())

    # ── R3：status 先于范围判断（安全）────────────────────────────────
    #   若先判 K_r ⊄ binding 再判 status，攻击者可用「必然 mismatch 的 K_r」
    #   探测停用场景的 kb_ids 边界。先判 status，停用场景对所有 K_r 返回同一错误。
    if scenario_id is not None and row.status != "active":  # type: ignore[union-attr]
        raise ScenarioError(
            ScenarioCode.NOT_ALLOWED,
            diagnostic={
                "reason": "scenario_not_active",
                "tenant_id": str(tenant_id),
                "scenario_id": str(scenario_id),
                # status 只进日志，不进响应体（不区分停用与草稿）
                "status": row.status,  # type: ignore[union-attr]
            },
        )

    # ── R4：请求集合超出场景声明（explicit 模式才约束）─────────────────
    if scenario_id is not None and row.binding_mode == "explicit":  # type: ignore[union-attr]
        declared = set(row.declared_kb_ids)  # type: ignore[union-attr]
        if not set(requested) <= declared:
            raise ScenarioError(
                ScenarioCode.MISMATCH,
                diagnostic={
                    "reason": "requested_outside_scenario_scope",
                    "tenant_id": str(tenant_id),
                    "scenario_id": str(scenario_id),
                    # 只记命中/未命中的条数，不记具体 id（日志降噪 + 少一份越权面）
                    "requested": str(len(requested)),
                    "outside": str(len(set(requested) - declared)),
                },
            )

    # ── R5 / R6：正常解析。场景**不额外限制**请求集合（kb_ids = K_r）────
    #   三重交集仍由既有 `_integration_kb_ids` 施加（chat.py:243-255）：
    #   R5/R6 与改动前逐字节相同，这是零影响承诺 Z-3 的来源。
    return _snapshot(requested)
