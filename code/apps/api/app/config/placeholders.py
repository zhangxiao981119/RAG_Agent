"""站位值登记表：**必须在启动横幅里可见**的「未标定常量」。

★ 为什么放 config 层（而不放 services）：
  `capabilities.py` 属 config，它要把这些值渲染进启动横幅。若它去
  `import app.services.scenario.policy_spec`，就造出一条 **config → services**
  的反向依赖（纪律见 `decisions.py:123`）。方向必须是 `services → config`：
  登记表放这里，服务层反过来 import 本表并在 **import 期**断言「我确实是登记在册的
  站位值」。漏登记 → 进程起不来，而不是「横幅里悄悄少一行」。

★ 为什么站位值需要一张表（而不是各模块 docstring 里写一句）：
  站位值的危害不是「不准」，而是「**没人知道它不准**」。项目已把
  `GRAPH_SKIP_MIN_CHUNKS` 标成站位值，但只写在 docstring 里（`decisions.py:940`
  附近），没有任何人在启动时会看到。横幅是唯一会被每个运维看见的位置。

★ `value` 字段是**镜像**，不是真源：真源仍是各模块里的常量。镜像与真源的一致性
  由服务层 import 期的断言保证（见 `policy_spec.py` / `resolver.py` 的登记块），
  所以两边不可能悄悄漂移 —— 改了常量不改这里，import 就炸。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class Placeholder:
    """一个未标定常量。"""

    name: str
    """常量名（横幅上显示的那个名字；带组件前缀以免跨模块歧义）。"""

    value: object
    """当前值（镜像，供横幅展示；与真源的一致性由服务层 import 期断言保证）。"""

    owner: str
    """归属组件（`scenario` / `graph` / ...）。横幅按 owner 分组渲染。"""

    note: str
    """为什么是站位值 + 将来怎么标定（写给运维看，不是写给作者看）。"""


PLACEHOLDERS: Final[tuple[Placeholder, ...]] = (
    # ── 场景化配置（Step 3 接线）────────────────────────────────────
    Placeholder(
        name="RECALL_TOP_K_MAX",
        value=200,
        owner="scenario",
        note="待压测标定 · 200 条以上的召回收益从未被验证",
    ),
    Placeholder(
        name="SCENARIO_CACHE_TTL_SECONDS",
        value=60.0,
        owner="scenario",
        note="待运维发布节奏标定 · 改场景后最长 60s 才生效",
    ),
    # ── 租户治理（Step 7）──────────────────────────────────────────
    Placeholder(
        name="TENANT_DB_SCOPE_CACHE_TTL_SECONDS",
        value=60.0,
        owner="tenant",
        note="待运维治理节奏标定 · 改租户库白名单后最长 60s 才生效",
    ),
    # ── chunk 口径分布（Step 4B-1）──────────────────────────────────
    # ★ 这两个值都直接决定"会不会误报/会不会卡启动"，而两者都没有真实运维数据
    #   可标定（`框架设计-v1.md:1294` 至今把"告警阈值"列为待定项）。登记在这里，
    #   横幅每次部署都会把"它们是站位值"打在运维眼前。
    Placeholder(
        name="CALIBER_STALE_PROGRESS_HOURS",
        value=24.0,
        owner="caliber",
        note="待运维数据标定 · hard_stale>0 时「最新合规 chunk 停止推进」多少小时才升 warn",
    ),
    Placeholder(
        name="CALIBER_BANNER_TIMEOUT_SECONDS",
        value=5.0,
        owner="caliber",
        note="待十万级 chunk 实测 · 启动横幅口径统计的超时上限（超时只打提示不打数字）",
    ),
    # ── 数据层路由（Step 5）────────────────────────────────────────
    Placeholder(
        name="DB_CONN_RESERVED",
        value=20,
        owner="db_router",
        note=(
            "待实测标定 · 框架 §3A.4 明确它是「设定值不是实测」；"
            "默认 20 = 迁移 1 / pg_dump 1~2 / 监控 1~2 / psql 排障若干"
        ),
    ),
    Placeholder(
        name="DB_WORKER_CONN_TOTAL",
        value=10,
        owner="db_router",
        note="待编排核对 · 当前 compose 给 worker 的是 5+5；改编排必须同步改它，否则闸门算错",
    ),
    Placeholder(
        name="DB_REPLICAS_EXPECTED",
        value=1,
        owner="db_router",
        note="待部署标定 · 应用不知道自己有几个副本，闸门偏松是刻意的（宁可偏松也不要拒绝启动）",
    ),
    # ── 容量看板（Step 7 S7-6）──────────────────────────────────────
    # ★ 三个值全部是**声明值 / 示例假设**，没有一个来自实测。容量看板会把它们
    #   连同 calibrated=false 一起吐给页面；登记在这里是为了让启动横幅同样可见。
    Placeholder(
        name="CAPACITY_M_SHARED_DECLARED_MB",
        value=8192,
        owner="capacity",
        note=(
            "待实测标定 · prod 编排里 embedding + reranker 两个模型服务 mem_limit 声明值之和"
            "（4g+4g；dev 编排的 4g 设在 reranker/ocr，总量同）；未必等于实测驻留"
        ),
    ),
    Placeholder(
        name="CAPACITY_M_CONTROL_PG_LOWER_MB",
        value=128,
        owner="capacity",
        note=(
            "待标定 · 控制库 shared_buffers 的下界（PG 默认 128MB）；"
            "运行时以 current_setting('shared_buffers') 的实测值覆盖"
        ),
    ),
    Placeholder(
        name="CAPACITY_M_SCENE_PG_MB",
        value=300,
        owner="capacity",
        note="待实测标定 · 框架示例里假设每个热库占 300MB，当前无任何实测依据",
    ),
)


def for_owner(owner: str) -> tuple[Placeholder, ...]:
    """取某组件的全部站位值（保持登记顺序，便于横幅稳定对齐）。"""
    return tuple(item for item in PLACEHOLDERS if item.owner == owner)


def names_for(owner: str) -> frozenset[str]:
    """某组件登记在册的常量名集合（服务层 import 期断言用）。"""
    return frozenset(item.name for item in for_owner(owner))


def value_of(name: str) -> object | None:
    """取某个常量的**镜像值**（未登记 → `None`）。

    服务层用它做 import 期的一致性断言：镜像与真源不一致时启动即失败，
    否则横幅会显示一个不存在的值，而没有任何信号。
    """
    for item in PLACEHOLDERS:
        if item.name == name:
            return item.value
    return None


def render_line(owner: str) -> str:
    """把某组件的站位值渲染成横幅的一行（空登记 → 空串，调用方据此跳过）。

    形如：`含站位值参数: RECALL_TOP_K_MAX=200（待压测标定…）, ...`
    """
    items = for_owner(owner)
    if not items:
        return ""
    rendered = ", ".join(f"{item.name}={item.value}（{item.note}）" for item in items)
    return f"含站位值参数: {rendered}"
