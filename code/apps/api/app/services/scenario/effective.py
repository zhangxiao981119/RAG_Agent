"""场景策略 →（平台值）→ 检索参数有效值的**唯一折叠点**（Step 3 接线）。

为什么必须只有一个折叠点：
  四个策略键在检索链路里有 8 个读点（设计文档 §A.1 的台账）。若每处各自写
  `policy.x if policy.x is not None else decisions.X`，那么「平台值 + 场景值」的
  折叠规则就有 8 份实现；改一处漏七处，表现是「某场景的阈值对 search 生效、
  对 ask 不生效」，**且没有任何报错**。

★ 本模块**不 from-import 常量**，一律 `decisions.X` / `policy_spec.X` 模块属性访问。
  理由与 `decisions.py:6-8` 同源：from-import 把值拷进本模块命名空间，
  `monkeypatch.setattr(decisions, ...)` 锁不住口径，测试就会退化成
  「断言今天恰好等于 50」（`tests/scenario/test_effective.py::test_z3_1_*` 会红）。
  这条纪律由 `test_effective_module_does_not_from_import_constants` 强制，不靠 review。

★ 本模块纯函数、无 IO、不读 DB、不打日志：`clamped` 只**返回**，由调用方（`retrieve`）
  打点一次 —— 折叠函数保持可单测，且天然去重。

★ 检索链路只接受 `ScenarioPolicy`，**不接受 `ScenarioSnapshot`**（设计文档 §A.2）：
  快照里带着 `kb_ids`（已过场景门禁的请求集合）。把它传进检索层就多出第二条
  「取授权范围」的路径，而 `effective_kb_ids` 的三重交集是唯一授权计算点。
  把不该给的东西不给，比写下规矩更可靠。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.config import decisions
from app.services.scenario import policy_spec
from app.services.scenario.snapshot import ScenarioPolicy
from app.services.tenant.governance import EMPTY_CAPS, TenantPolicyCaps

ACTIVE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "recall_top_k",
        "rerank_enabled",
        "relevance_threshold_vector",
        "relevance_threshold_rerank",
    }
)
"""**检索链路真正读**的键（本模块是唯一声明处）。

★ 与 `policy_spec.WIRED_KEYS`（写入闭集）和 `snapshot.SCENARIO_POLICY_KEYS`
  （快照能承载）构成**三方相等**判据：三者任一不等 = 「能写不能读」或
  「能读不能用」。由 `test_three_way_keyset_equality` 锁死。
"""

WIRED_RETRIEVAL_STEP: Final[int] = 3
"""检索链路接线到第几步（S-7 用它判断「`db_binding` 是否还没到生效的步骤」）。

★ 为什么**不是** `CURRENT_STEP`（这是三个步骤号并存的原因，别合并）：
  · `CURRENT_STEP=4`   —— `policy_spec` 里「本次交付到第几步」的**交付进度值**
                          （Step 4A 由 2 改为 4，见 DQ4-2），只服务错误文案；
                          `test_effective.py::test_step_numbers_are_three_and_explicit`
                          把它钉在 4。
  · `POLICY_CLOSURE_STEP=3` —— 写入闭集覆盖到第几步（迁移 0028 的 CHECK 预置范围）。
  · `WIRED_RETRIEVAL_STEP=3` —— **检索层真正开始读取**到第几步（本常量的语义）。
  三者的来源不同（交付进度 / 闭集范围 / 接线进度），合并任意两个都会让第三个失真：
  例如把闭集步骤当成接线步骤，就会出现「键已可写但检索不读」的静默 no-op；
  而把 `CURRENT_STEP` 当成任何一个，会在「交付了写接口但没动检索」的这一步
  （Step 4A 正是这种步）给出与事实相反的结论。
"""


@dataclass(frozen=True, slots=True)
class EffectiveRetrieval:
    """一次检索请求的**有效参数**。构造后不可改（frozen）。"""

    recall_top_k: int
    rerank_enabled: bool
    threshold_vector: float
    threshold_rerank: float
    final_top_n: int
    clamped: tuple[str, ...] = ()
    """被平台上限裁掉的字段名（空 = 没裁）。**只返回不打点**，见模块说明。

    ★ 只记录「平台收窄」；租户收紧不改这里（否则 `clamped` 的告警文案
      "超出平台上限" 会对着租户 cap 说谎），租户来源见 `sources`。
    """
    sources: tuple[tuple[str, str], ...] = ()
    """每个键**最终取值的来源**：`(键, platform|scenario|tenant)`。

    顺序固定为 `recall_top_k / rerank_enabled / threshold_vector / threshold_rerank`。
    ★ 只在租户 cap **真的改变值**时记 `tenant`：记成 tenant 却不改值，
      会让管理端预览显示"租户在收紧"，而实际参数与场景配置一致。
    """

    def threshold_for(self, *, use_rerank: bool) -> float:
        """按**实际**打分来源取阈值。

        ★ `use_rerank` 必须是「降级之后」的真实值，不是「配置开启」：
          重排抖动降级时若仍用 rerank 口径的阈值，拒答行为会不可预测
          （`decisions.py:24-41` 的原始动机）。
        ★ 这是检索链路取阈值的**唯一入口**：`decisions.active_threshold` 在
          `retrieve/__init__.py` 里的直接引用必须为 0 次（Z3-4 文本守卫）。
        """
        return self.threshold_rerank if use_rerank else self.threshold_vector


def effective_retrieval(
    policy: ScenarioPolicy | None,
    *,
    platform_rerank_enabled: bool,
    caps: TenantPolicyCaps | None = None,
) -> EffectiveRetrieval:
    """把（平台值, 场景覆盖, 租户上限）折叠成有效值。

    折叠顺序固定：**L0 平台硬上限 → L2 场景值 → L1 租户 cap**。
    `policy is None` 等价于全 `None`；`caps is None` 等价于空 caps（不限制）。

    ★ 顺序的理由：平台上限是物理安全线（内存/延迟），租户 cap 是合同约束。
      先平台后租户 ⇒ 租户看到的值永远不超过平台；若反过来，一个配置过高的
      租户 cap 就能把平台的物理上限架空。

    `platform_rerank_enabled` 由调用方读一次后传入（而不是本函数自己读 settings）：
    这样本函数是**纯的**，且「平台重排开关只读一次」是调用方可以文本断言的事实
    （Z3-4：`get_settings().rerank_enabled` 在 `retrieve/__init__.py` 恰好 1 次）。
    """
    policy = policy or ScenarioPolicy()
    caps = caps or EMPTY_CAPS
    clamped: list[str] = []
    sources: list[tuple[str, str]] = []

    recall_top_k = policy.effective_recall_top_k()
    recall_source = "scenario" if policy.recall_top_k is not None else "platform"
    # ★ 上下界都收窄：上界是平台硬上限（`RECALL_TOP_K_MAX` 会随压测**下调**，
    #   届时库里已有的历史行可能越界）；下界防 0（0 会让切片为空 → 全量拒答）。
    #   收窄 MUST 打点（`clamped` 返回给调用方），否则就是静默降级 ——
    #   而「静默降级」正是 Step 1–2 用整章反对的形态。
    if recall_top_k > policy_spec.RECALL_TOP_K_MAX:
        recall_top_k = policy_spec.RECALL_TOP_K_MAX
        recall_source = "platform"
        clamped.append("recall_top_k")
    elif recall_top_k < 1:
        recall_top_k = 1
        recall_source = "platform"
        clamped.append("recall_top_k")
    if caps.recall_top_k_max is not None and recall_top_k > caps.recall_top_k_max:
        recall_top_k = caps.recall_top_k_max
        recall_source = "tenant"
    sources.append(("recall_top_k", recall_source))

    rerank_enabled = policy.effective_rerank_enabled(platform_rerank_enabled)
    # 来源如实反映「谁决定了最终值」：平台关时场景的声明无法生效，来源是 platform
    if not platform_rerank_enabled or policy.rerank_enabled is None:
        rerank_source = "platform"
    else:
        rerank_source = "scenario"
    if caps.rerank_allowed is not None and rerank_enabled and not caps.rerank_allowed:
        rerank_enabled = False
        rerank_source = "tenant"
    sources.append(("rerank_enabled", rerank_source))

    threshold_vector = policy.effective_threshold(use_rerank=False)
    if policy.relevance_threshold_vector is None:
        threshold_vector_source = "platform"
    else:
        threshold_vector_source = "scenario"
    if (
        caps.threshold_vector_min is not None
        and threshold_vector < caps.threshold_vector_min
    ):
        threshold_vector = caps.threshold_vector_min
        threshold_vector_source = "tenant"
    sources.append(("threshold_vector", threshold_vector_source))

    threshold_rerank = policy.effective_threshold(use_rerank=True)
    if policy.relevance_threshold_rerank is None:
        threshold_rerank_source = "platform"
    else:
        threshold_rerank_source = "scenario"
    if (
        caps.threshold_rerank_min is not None
        and threshold_rerank < caps.threshold_rerank_min
    ):
        threshold_rerank = caps.threshold_rerank_min
        threshold_rerank_source = "tenant"
    sources.append(("threshold_rerank", threshold_rerank_source))

    return EffectiveRetrieval(
        recall_top_k=recall_top_k,
        # 单向 AND：平台关时场景开 → 仍为 False（成本由平台承担，见
        # `ScenarioPolicy.effective_rerank_enabled`）
        rerank_enabled=rerank_enabled,
        threshold_vector=threshold_vector,
        threshold_rerank=threshold_rerank,
        # ★ Step 4 的唯一改动点：改为 `policy.final_top_n` 折叠。
        #   现在恒等平台值是**有意的**：`ScenarioPolicy` 尚无 final_top_n 字段
        #   （`snapshot.py` 只有 4 个字段），闭集里它有 `wired_in_step=4`。
        #   提前放进本对象，是为了 Step 4 接线时只改这一行，而不是回去 6 个读点里
        #   找「平台上限条数」常量（见设计文档 §A.4 的诚实说明）。
        final_top_n=decisions.TOP_K_RERANK,
        clamped=tuple(clamped),
        sources=tuple(sources),
    )
