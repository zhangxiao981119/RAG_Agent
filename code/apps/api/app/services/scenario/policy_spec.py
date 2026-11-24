"""场景化策略参数的规格表 —— 唯一真源（供校验 / 文档 / 前端共用）。

**为什么需要这张表**：策略参数分散在三处会被读到 —— 迁移里的 CHECK 闭集、写入校验
的范围、前端表单的上下限。三处各写一遍的典型后果是「前端允许填 500、后端默默截断
到 200」。用一张 `POLICY_FIELDS` 生成三处消费，让不一致不可能发生。

**分层总则**（设计文档 §E.1）：
  · L0 平台层（`decisions.py` / `settings.py`）是参数真源，场景**只能收窄**；
  · L2 场景层（`scenario_versions.policy`）是**稀疏覆盖**集，缺键 = 跟随平台；
  · `wired_in_step` = 该参数**开始被检索链路读取**的步骤。未到该步骤的键写进去
    就是静默 no-op（运维改了阈值、行为没变、日志没有一行），因此写入必须被拒绝。

★ 本模块只放**常量与纯校验**，不 import 检索链路、不读 DB、不写日志。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from app.config import decisions, placeholders
from app.config.settings import get_settings

CURRENT_STEP: Final[int] = 4
"""本次交付的步骤号（Step 1–4）。仅用于错误文案里告诉运维「自哪一步起可配置」。

★ 它在 Step 4A 从 2 改成 4（DQ4-2）：Step 3 交付的是**读**（检索链路开始读 policy），
  Step 4A 交付的是**写**（场景管理接口），两者都改变「运维能不能真的用上这个参数」，
  因此「交付到第几步」必须跟着走一步 —— 停在 2 会让错误文案对 Step 3 之后接线的
  参数说「自 Step 3 起可配置，现在还不能配」，与事实相反。
★ 它**不参与任何判定**（`WIRED_KEYS` 用的是 `POLICY_CLOSURE_STEP`）：改它只影响
  文案与三处语义断言。这是三个步骤号里唯一一个允许随交付推进而变化的。
"""

POLICY_CLOSURE_STEP: Final[int] = 3
"""迁移 0028 的 CHECK 闭集（C-A2）按哪一步的接线范围预置。

★ 设计文档在这里自相矛盾，施工时按可执行的那一侧落地（已在交付报告登记）：
  · §A.3.1 / §B.2 的 `_POLICY_KEYS` 明确 = Step 3 的 4 个键
    （`recall_top_k` / `rerank_enabled` / 两个阈值），C-A2 的 SQL 逐字引用它；
  · §E.1 又说「`wired_in_step > 2` 的参数在 Step 1–2 写入会被 CHECK 拒绝」，
    而这 4 个键的 `wired_in_step` 都是 3 → 按该条闭集应为空集。
  二者不可能同时成立。选「闭集 = 4 键」的理由：**Step 1–2 不存在任何写入路径**
  （场景写接口属 Step 4），闭集提前预置不会制造静默 no-op，却省掉一次 DDL。
  因此「闭集覆盖到第几步」与「当前在第几步」必须是两个常量，而不是一个。
"""

RECALL_TOP_K_MAX: Final[int] = 200
"""`recall_top_k` 的代码侧上限（**站位值**，无压测依据 —— 见设计文档 §G-6 / S-6）。

DB CHECK 的兜底上限是 1000（防天文数字），真正生效的上限是这个：
多召回一条就多一次打分，200 条以上的边际收益从未被验证过。
"""

# ── 站位值登记断言（import 期，fail-fast）──────────────────────────────
# ★ 方向：services → config（`capabilities.py` 属 config，它去 import 本模块就是
#   反向依赖，见 `decisions.py:123` 的纪律）。所以登记表在
#   `app/config/placeholders.py`，本模块反过来认领自己那一行。
# ★ 为什么要在 import 期查而不是等自检：漏登记的站位值**不会**让任何请求出错，
#   它只会让启动横幅少一行 —— 这类缺陷只能靠「起不来」来发现。
# ★ 为什么同时比对 value：登记表里的 value 是**镜像**，不是真源。只查名字的话，
#   有人把 RECALL_TOP_K_MAX 从 200 改成 100 而忘了改镜像，横幅就会**永远显示旧值**，
#   而且没有任何信号 —— 那正是「站位值没人知道它不准」的加强版。
_RECALL_TOP_K_MAX_PLACEHOLDER = "RECALL_TOP_K_MAX"

if _RECALL_TOP_K_MAX_PLACEHOLDER not in placeholders.names_for("scenario"):
    raise RuntimeError(
        f"{_RECALL_TOP_K_MAX_PLACEHOLDER} 是站位值，但未登记在 app/config/placeholders.py "
        "—— 未登记的站位值不会出现在启动横幅，等于没人知道它还没标定"
    )
if placeholders.value_of(_RECALL_TOP_K_MAX_PLACEHOLDER) != RECALL_TOP_K_MAX:
    raise RuntimeError(
        f"app/config/placeholders.py 里 {_RECALL_TOP_K_MAX_PLACEHOLDER} 的镜像值 "
        f"{placeholders.value_of(_RECALL_TOP_K_MAX_PLACEHOLDER)!r} 与本模块的 "
        f"{RECALL_TOP_K_MAX!r} 不一致 —— 启动横幅会显示一个不存在的值"
    )

_NEVER: Final[int] = 1_000_000
"""L0 锁定参数（平台能力开关，永不进策略表）的 `wired_in_step`。

用哨兵而不是 `None`：`WIRED_KEYS` 的判据是 `wired_in_step <= 闭集步骤`，
一个不可能达到的大整数让该判据**逐字保持设计文档的形态**，不必引入特例分支。
"""


@dataclass(frozen=True, slots=True)
class PolicyField:
    """一个可覆盖策略参数的完整规格。

    字段与设计文档 §C.4 一致，一个不多一个不少 —— 增加字段会让「规格表是唯一真源」
    这个性质变成「规格表 + 某个字段的特例」。
    """

    name: str
    type: Literal["int", "bool", "float"]
    # 人类可读：缺省时跟随什么（含平台出处，便于运维自查）
    default_semantics: str
    min_value: int | float | None
    max_value: int | float | None
    # 生效步骤；> 闭集步骤时禁止写入
    wired_in_step: int
    # 改了之后什么派生数据失效（"无" 是一等答案，不是占位）
    invalidates: str


POLICY_FIELDS: Final[tuple[PolicyField, ...]] = (
    PolicyField(
        name="recall_top_k",
        type="int",
        default_semantics=f"跟随平台 decisions.TOP_K_RECALL={decisions.TOP_K_RECALL}",
        min_value=1,
        max_value=RECALL_TOP_K_MAX,
        wired_in_step=3,
        invalidates="无（纯请求内计算，不落库）",
    ),
    PolicyField(
        name="rerank_enabled",
        type="bool",
        default_semantics=(
            "跟随平台 settings.rerank_enabled（默认 "
            f"{get_settings().rerank_enabled}）；单向 AND：场景只能关，不能开"
        ),
        min_value=None,
        max_value=None,
        wired_in_step=3,
        invalidates="无；但会切换 final_score 口径 → 必须同时切阈值",
    ),
    PolicyField(
        name="relevance_threshold_vector",
        type="float",
        default_semantics=(
            "跟随平台 decisions.RELEVANCE_THRESHOLD_VECTOR="
            f"{decisions.RELEVANCE_THRESHOLD_VECTOR}（30 条样本标定）"
        ),
        min_value=0.0,
        max_value=1.0,
        wired_in_step=3,
        invalidates="无（不改数据，只改判定）",
    ),
    PolicyField(
        name="relevance_threshold_rerank",
        type="float",
        default_semantics=(
            "跟随平台 decisions.RELEVANCE_THRESHOLD_RERANK="
            f"{decisions.RELEVANCE_THRESHOLD_RERANK}（**未标定的占位值**）"
        ),
        min_value=0.0,
        max_value=1.0,
        wired_in_step=3,
        invalidates="无",
    ),
    PolicyField(
        name="final_top_n",
        type="int",
        # 自检不变量「不能超过平台允许进上下文的条数上限」比「1..50」更严，
        # 合法范围取两者交集（E.2 #5 的「合法范围」列与「self_check」列在这一点上不一致）
        default_semantics=f"跟随平台 decisions.TOP_K_RERANK={decisions.TOP_K_RERANK}",
        min_value=1,
        max_value=min(50, decisions.TOP_K_RERANK),
        wired_in_step=4,
        invalidates="无",
    ),
    PolicyField(
        name="rrf_k",
        type="int",
        default_semantics=(
            f"L0 锁定 decisions.RRF_K={decisions.RRF_K}（不进策略表："
            "改它等于改三路融合的分布，跨场景结果不可比，收益未验证）"
        ),
        min_value=None,
        max_value=None,
        wired_in_step=_NEVER,
        invalidates="无（但会使所有历史 A/B 结论失效）",
    ),
    PolicyField(
        name="keyword_tsquery_enabled",
        type="bool",
        default_semantics=(
            "L0 锁定 decisions.KEYWORD_TSQUERY_ENABLED="
            f"{decisions.KEYWORD_TSQUERY_ENABLED}（关掉 = 丢掉一条召回路，属平台能力开关）"
        ),
        min_value=None,
        max_value=None,
        wired_in_step=_NEVER,
        invalidates="无",
    ),
    PolicyField(
        name="alias_expansion_enabled",
        type="bool",
        default_semantics=(
            "跟随平台 decisions.ALIAS_EXPANSION_ENABLED="
            f"{decisions.ALIAS_EXPANSION_ENABLED}；Step 4 再议"
            "（别名扩展的静默截断未修，场景化前必须先修 —— §G-8）"
        ),
        min_value=None,
        max_value=None,
        wired_in_step=4,
        invalidates="无",
    ),
    PolicyField(
        name="ilike_max_terms",
        type="int",
        default_semantics=f"跟随平台 decisions.ILIKE_MAX_TERMS={decisions.ILIKE_MAX_TERMS}",
        min_value=1,
        max_value=decisions.ILIKE_MAX_TERMS,
        wired_in_step=4,
        invalidates="无",
    ),
    PolicyField(
        name="graph_enabled",
        type="bool",
        default_semantics=(
            f"跟随平台 settings.graph_enabled（默认 {get_settings().graph_enabled}）；单向 AND"
        ),
        min_value=None,
        max_value=None,
        wired_in_step=5,
        invalidates="不改已有数据；但关→开后该库范围内未抽取的文档需回填才有效果（Step 5）",
    ),
    PolicyField(
        name="graph_recall_selective",
        type="bool",
        default_semantics=(
            "跟随平台 settings.graph_recall_selective"
            f"（默认 {get_settings().graph_recall_selective}）；单向 AND"
        ),
        min_value=None,
        max_value=None,
        wired_in_step=5,
        invalidates="无",
    ),
    PolicyField(
        name="graph_retrieve_top_k",
        type="int",
        default_semantics=(
            f"跟随平台 decisions.GRAPH_RETRIEVE_TOP_K={decisions.GRAPH_RETRIEVE_TOP_K}"
        ),
        min_value=1,
        max_value=decisions.GRAPH_RETRIEVE_TOP_K,
        wired_in_step=5,
        invalidates="无",
    ),
    PolicyField(
        name="wiki_enabled",
        type="bool",
        default_semantics=(
            f"跟随平台 settings.wiki_enabled（默认 {get_settings().wiki_enabled}）；单向 AND"
        ),
        min_value=None,
        max_value=None,
        wired_in_step=5,
        invalidates="关→开后需生成 wiki_pages（Step 4/5）",
    ),
    PolicyField(
        name="wiki_nav_selective",
        type="bool",
        default_semantics=(
            "跟随平台 settings.wiki_nav_selective"
            f"（默认 {get_settings().wiki_nav_selective}）；单向 AND"
        ),
        min_value=None,
        max_value=None,
        wired_in_step=5,
        invalidates="无",
    ),
    PolicyField(
        name="table_recall_enabled",
        type="bool",
        default_semantics=(
            "常开：decisions.TABLE_RECALL_ENABLED="
            f"{decisions.TABLE_RECALL_ENABLED}；当前写入闭集为 "
            f"{POLICY_CLOSURE_STEP}，不可按场景覆盖（wired_in_step=4）"
        ),
        min_value=None,
        max_value=None,
        wired_in_step=4,
        invalidates="无",
    ),
    PolicyField(
        name="section_recall_enabled",
        type="bool",
        default_semantics=(
            "常开：decisions.SECTION_RECALL_ENABLED="
            f"{decisions.SECTION_RECALL_ENABLED}；当前写入闭集为 "
            f"{POLICY_CLOSURE_STEP}，不可按场景覆盖（wired_in_step=4）"
        ),
        min_value=None,
        max_value=None,
        wired_in_step=4,
        invalidates="无",
    ),
)

_FIELDS_BY_NAME: Final[dict[str, PolicyField]] = {f.name: f for f in POLICY_FIELDS}

WIRED_KEYS: Final[frozenset[str]] = frozenset(
    f.name for f in POLICY_FIELDS if f.wired_in_step <= POLICY_CLOSURE_STEP
)
"""闭集内（可写入）的键 —— 与迁移 0028 的 `_POLICY_KEYS` **必须相等**（S-1）。

★ 名字沿用设计文档 §C.4。现状说明：这 4 个键自 Step 3 起**已被检索链路读取**
  （`effective.ACTIVE_KEYS`），闭集按 Step 3 预置；Step 4A 只加写接口、不动接线，
  因此「闭集 == 接线 == 可承载」三方仍相等。
  两份清单的一致性由
  `tests/scenario/test_resolver_policy.py::test_policy_keyset_matches_migration` 锁死。
"""

THRESHOLD_KEYS: Final[frozenset[str]] = frozenset(
    {"relevance_threshold_vector", "relevance_threshold_rerank"}
)
"""两个阈值键：除范围外还要求 `0.0 < v`（S-4：0 或负阈值 = 永不拒答）。"""


class PolicyValidationError(ValueError):
    """policy 校验失败。`field` 为出错的键名（未知键时为 None）。"""

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


def type_ok(value: object, expected: Literal["int", "bool", "float"]) -> bool:
    """严格类型判定 —— **写入校验与读取校验共用同一个实现**。

    ★ 三个坑，每一个都踩过：
      · `bool` 是 `int` 的子类，`isinstance(True, int)` 为真 ——
        `{"recall_top_k": true}` 会被当成合法整数。故 int 分支 MUST 排除 bool。
      · int 字段**不接受** float：`{"recall_top_k": 1.5}` 若被接受，快照里就会出现
        非整数的召回条数，下游 `range()` / 切片上会炸在很远的地方。
      · float 字段**接受** int（`0` / `1` 是常见写法），但仍不接受 bool。
    """
    if expected == "bool":
        return isinstance(value, bool)
    if isinstance(value, bool):
        return False  # True/False 不是合法的 int / float 取值
    if expected == "int":
        return isinstance(value, int)
    return isinstance(value, (int, float))


def validate_policy(policy: Mapping[str, object] | None) -> dict[str, object]:
    """校验场景策略覆盖集，返回可直接落库的普通 dict。

    **两类失败的文案刻意不同**：
      · 键不在闭集内 → 指向「自哪一步起可配置」（运维需要知道是不是写错了地方）；
      · 值不合法 → 直接说范围，不回显整份 policy（避免把别的键也带进日志/响应）。

    校验顺序 = 键闭集 → 类型 → 范围 → 阈值为正。先判键的理由：一个拼错的键名
    若先报「类型错误」，运维会去改值而不是改键名。
    """
    if policy is None:
        return {}
    if not isinstance(policy, Mapping):
        raise PolicyValidationError("policy 必须是对象（JSON object）")

    unknown = set(policy) - WIRED_KEYS
    if unknown:
        names = "、".join(sorted(unknown))
        horizons = sorted(
            {
                _FIELDS_BY_NAME[name].wired_in_step
                for name in unknown
                if name in _FIELDS_BY_NAME
            }
        )
        if horizons:
            raise PolicyValidationError(
                f"参数 {names} 尚未接线，自 Step {horizons[0]} 起可配置"
                "（未接线的键写入后不会改变任何行为）",
                field=sorted(unknown)[0],
            )
        raise PolicyValidationError(f"未知参数：{names}", field=sorted(unknown)[0])

    result: dict[str, object] = {}
    for name, value in policy.items():
        field = _FIELDS_BY_NAME[name]
        if not type_ok(value, field.type):
            raise PolicyValidationError(
                f"参数 {name} 类型应为 {field.type}", field=name
            )
        if field.min_value is not None and value < field.min_value:  # type: ignore[operator]
            raise PolicyValidationError(
                f"参数 {name} 不能小于 {field.min_value}", field=name
            )
        if field.max_value is not None and value > field.max_value:  # type: ignore[operator]
            raise PolicyValidationError(
                f"参数 {name} 不能大于 {field.max_value}", field=name
            )
        if name in THRESHOLD_KEYS and not value > 0.0:  # type: ignore[operator]
            # S-4：0 阈值 = 永不拒答
            raise PolicyValidationError(f"参数 {name} 必须大于 0", field=name)
        result[name] = value
    return result
