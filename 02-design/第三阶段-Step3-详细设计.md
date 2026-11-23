# 第三阶段 Step 3 详细设计（policy 接线与自检）

> **本文件定位**：`02-design/统一RAG平台-场景化配置-框架设计-v1.md`（框架稿）→ `02-design/第二阶段-Step1-2-详细设计.md`（Step 1–2 施工图）→ **本文件（Step 3 施工图）**。
>
> **写作前提（重要）**：Step 1–2 **已经实现并通过独立验证**。本文件**不重新推导** Step 1–2 的结论，把它的**代码**当基线；凡与我此前文档不一致处，一律以代码为准，并在**附录 A** 逐条登记（不静默改）。
>
> **标记约定（全文强制）**：
> - **【源】** = 在源码里核到的事实，附 `file:line`，可当场核对。
> - **【推导】** = 按设计意图/第一性判据推出，源码里没有对应行；若前提变则结论可变。
> - 两条绝不混写。附录 B 是这两类的索引。
>
> **不做的事**：不改 `decisions.py` / `settings.py` / MCP 契约；不写场景写接口（Step 4）；不做图/wiki 场景化（Step 5）；不碰 `db_binding`（Step 6）；不调用任何云端模型；不引入 PT/压测结论。

---

## 0. 边界与验收基数

### 0.1 输入：Step 1–2 的既成事实（作为基线，不重开）

| # | 事实 | 核对位置 |
|---|---|---|
| B-1 | `scenarios` + `scenario_versions` + `integration_applications.scenario_id`（FK RESTRICT），每租户种子行 | `alembic/versions/0028_add_scenarios.py` |
| B-2 | `POLICY_FIELDS` 16 项、`WIRED_KEYS`（= 4 键）、`validate_policy()`、`type_ok()`、`RECALL_TOP_K_MAX=200`、`CURRENT_STEP=2`、`POLICY_CLOSURE_STEP=3`、`_NEVER` 哨兵 | `policy_spec.py:24-52, 75-273, 289-360` |
| B-3 | `ScenarioPolicy` 4 字段全 Optional + 三个 `effective_*` 方法；`ScenarioSnapshot`；`build_snapshot`（六行 + 表外分支） | `snapshot.py:51-105, 224-276, 362-496` |
| B-4 | `resolve_scenario` 唯一 impure 入口；`LOOKUP_SQL` 双租户过滤；行缓存 TTL 60、只缓存行不缓存快照 | `resolver.py:53-85, 158-195` |
| B-5 | 3 个错误码，响应固定文案与 `diagnostic` 严格分离 | `errors.py:24-67` |
| B-6 | 两个 handler 各调 `_resolve_or_reject` + `_scenario_meta`（`meta` 恰 4 键） | `integrations.py:199-239, 320-334, 346-359` |
| B-7 | `tests/scenario` 96 passed / 18 skipped（skip 全因无 PG），`tests/config` 28 passed | 团队验证记录 |

#### 0.1.1 四处「实现推翻了我的文档」的既成事实 —— 全部当输入接受

Step 1–2 实现时，有四处我原来的写法被代码改掉了。**我逐条核对后全部接受**，Step 3 的设计建立在这些事实之上，不重开讨论：

| # | 我原来写的 | 代码是 | 为什么代码对 | Step 3 的影响 |
|---|---|---|---|---|
| 1 | `build_snapshot(*, row, tenant_id, requested_kb_ids)` | 多一个 `scenario_id`（`snapshot.py:362-368`） | `row is None` 无法区分「R5 未绑定 → 内置兜底」与「R1 引用不存在 → 500」，二者结局**相反** | 无（Step 3 不碰真值表）。这条正是原稿 §A.6 障碍 6 那类「纯函数表达不全」的实例，我原稿的签名确实不完整 |
| 2 | 闭集只用一个「步骤」概念 | `POLICY_CLOSURE_STEP=3` 与 `CURRENT_STEP=2` **两个常量**（`policy_spec.py:24-38`） | 原稿 §B/§E.1/§C.4 三处对闭集的说法互斥；拆两个常量后两条约束各自成立 | 直接决定了 §C.4 的 `WIRED_RETRIEVAL_STEP` 方案（**不得改 `CURRENT_STEP`**） |
| 3 | `wired_in_step: int \| None`（`None` = 锁定） | `_NEVER = 10**6` 哨兵（`policy_spec.py:47-52`） | 让 `wired_in_step <= 闭集步骤` 这个判据**逐字保持**，不引入特例分支 | 无（Step 3 不新增锁定参数） |
| 4 | 类型校验两处各写（写入 `validate_policy`、读取 `to_policy`） | 合并到公开的 `policy_spec.type_ok`（`policy_spec.py:289-305, snapshot.py:189-221`） | 两边各写一份会出现「能落库但解析 500」或「落库该拒却放行」，且两种错都不留日志 | 直接影响 §C.5 的 T4 设计：`check_policy_publishable` **复用** `validate_policy`，不重写判据 |

> 结论：这四处不是「实现偏离设计」，而是**设计不完整**。Step 3 沿用它们的形态（尤其是 #2 与 #4），并在 §C.4 / §C.5 里显式依赖。

### 0.2 Step 3 做什么 / 不做什么

| | 做 | 不做 |
|---|---|---|
| 检索参数 | 让 `retrieve()` / `retrieve_chain()` 读**场景 policy**，且**只在一个地方**折叠成有效值 | 不新增策略键；不放宽任何范围的平台上限；不改 `RRF_K` 等 L0 锁定参数 |
| 自检 | 补齐 S-2 / S-3 / S-4 / S-5 / S-6 / S-7 / S-8 / S-9（S-1 已由测试实现） | 不把「数据类」自检做成拒绝启动（见 §C.3 的理由） |
| 调用链 | `integrations` 两个 handler 传入 policy；`chat._chat_ask_impl` 增加一个可选透传参数 | 不改共用链路的会话/配额/审计逻辑；不改 `/chat/ask` 的外部契约 |
| 可观测 | 4 个新 metric + 3 类 WARN 日志 | 不改 MCP 响应契约（`meta` 的 `policy_applied` 仍归 Step 4） |
| 测试 | 新增约 40 条用例（NO-DB 为主）；**5 个既有测试文件的桩签名做最小适配** | 不放宽任何既有断言（§D.6） |

### 0.3 零影响承诺：Z3-1 ~ Z3-4（全部可执行）

| # | 断言 | 形式 | 位置 |
|---|---|---|---|
| **Z3-1** | `policy` 全 `None` 时，`EffectiveRetrieval` 的每个字段 == `decisions`/`settings` 的**当前值** | 把平台常量 monkeypatch 成不可能撞上的值（37 / 5 / 0.61 / 0.62），再断言 `eff` 跟着变 —— **反自证**，证明是现读不是碰巧相等 | §D.1 |
| **Z3-2** | 同一确定性桩下，`retrieve(policy=None)` 与 `retrieve(policy=ScenarioPolicy())` 的 `RetrievalResult` **逐字段相等**（除 `stage_ms` 计时） | `dataclasses.replace(result, stage_ms={})` 后 `==` | §D.2 |
| **Z3-3** | 未绑定场景的应用，走完 handler 后**传给 retrieve 的 policy 是 `ScenarioPolicy()`（全 None）** | 断言 `snapshot.policy.declared() == {}` 且 `eff == 平台值` | §D.2 |
| **Z3-4** | 未配 policy 时，**代码里不存在第二处读平台常量的地方** | AST/文本守卫：`retrieve/__init__.py` 内 `decisions.TOP_K_RECALL` / `TOP_K_RERANK` / `active_threshold` 出现 **0** 次，`get_settings().rerank_enabled` 出现 **恰好 1** 次 | §D.3 |

> Z3-4 是这次 Step 3 的**结构性**保证。「读两次配置」的典型后果不是立刻出错，而是半年后一个人改了 A 处、忘了 B 处，表现为「某场景的阈值只对 search 生效、对 ask 不生效」。

### 0.4 施工文件清单

| 动作 | 文件 | 说明 |
|---|---|---|
| 新增 | `app/config/placeholders.py` | 站位值登记表（S-6 需要；**放 config 层**，否则 config→services 反向依赖） |
| 新增 | `app/services/scenario/effective.py` | `EffectiveRetrieval` + `effective_retrieval()` + `ACTIVE_KEYS`（**唯一取值入口**） |
| 新增 | `app/services/scenario/self_check.py` | S-2a/S-4a（常量类）+ S-2b/S-3/S-4b/S-5/S-7/S-8/S-9（数据类）+ `check_policy_publishable()`（S-2b 的 T4 形态） |
| 修改 | `app/services/scenario/policy_spec.py` | 只**追加**：`RECALL_TOP_K_MAX` 登记站位值（import 期断言）+ `NEVER_STEP` 导出（S-7 用不到，见 §C.2） |
| 修改 | `app/services/scenario/resolver.py` | 只**追加**：`CACHE_TTL_SECONDS` 登记站位值（import 期断言） |
| 修改 | `app/services/retrieve/__init__.py` | `retrieve` / `retrieve_chain` 加 `*, policy=None`；8 个读点改读 `eff` |
| 修改 | `app/api/chat.py` | `_chat_ask_impl` 加 `policy=None` 并透传给 `retrieve_chain` |
| 修改 | `app/api/integrations.py` | 两个 handler 各加一个实参 |
| 修改 | `app/main.py` | lifespan 调 `check_scenario_startup()` / `check_scenario_data()` |
| 修改 | `app/workers/parse_job.py` | `on_startup` **只**调常量类自检（理由见 §C.1） |
| 修改 | `app/config/capabilities.py` | 横幅追加一行「含站位值」 |
| **不动** | `decisions.py` / `settings.py` / `mcp/main.py` / 任何迁移 | — |

### 0.5 术语增量

| 词 | 含义 |
|---|---|
| **有效值（effective）** | 平台值与场景覆盖按「只能收紧」折叠后的值；检索链路唯一可用的值 |
| **候选池（pool）** | 三路召回 + RRF 融合后、截断到 `recall_top_k` 的集合 |
| **取用数（final_top_n）** | 进入 L1 阈值判定与上下文的条数 |
| **接线（wiring）** | 某个策略键真正被检索链路读取。未接线的键写入是静默 no-op，故闭集必须与接线同步 |
| **数据类自检** | 需要查库才能判定的自检（与「常量自检」相对） |

---

## A. 接线点清单

### A.1 现状读点台账（**先把今天读哪儿查清，再谈接线**）

【源】`app/services/retrieve/__init__.py` 里与四个键相关的读点，逐个核过：

| # | 行 | 代码 | 属于哪个键 | 语义 |
|---|---|---|---|---|
| 1 | `258` | `"limit": decisions.TOP_K_RECALL` | `recall_top_k` | 向量路召回条数 |
| 2 | `368` | `"limit": decisions.TOP_K_RECALL` | `recall_top_k` | ILIKE 路召回条数 |
| 3 | `395` | `"limit": decisions.TOP_K_RECALL` | `recall_top_k` | tsquery 路召回条数 |
| 4 | `434` | `candidate_ids = sorted_ids[: decisions.TOP_K_RECALL]` | `recall_top_k` | RRF 候选池截断 |
| 5 | `447` | `rerank_off = not get_settings().rerank_enabled` | `rerank_enabled` | 是否发起重排 |
| 6 | `459` | `rerank_service.rerank(query, documents, decisions.TOP_K_RERANK)` | **`final_top_n`**（不是 recall！） | 重排返回条数 |
| 7 | `532` | `retrieved = retrieved[: decisions.TOP_K_RERANK]` | `final_top_n` | 降级路径的截断 |
| 8 | `537` | `threshold = decisions.active_threshold(use_rerank)` | 两个阈值键 | 按**实际**打分口径取阈值 |

另外两处**不接线**、但必须点名的读取：

| 行 | 代码 | 为什么不动 |
|---|---|---|
| `252` | `profile = current_index_profile()` | DNA 只从平台函数读（I-6）。`snapshot.index` 与它逐字段相等（Step 1–2 已测），**没有第二处需求的理由** |
| `210` | `1.0 / (decisions.RRF_K + rank + 1)` | `RRF_K` 是 L0 锁定（`policy_spec.py:132-143` 的 `_NEVER`） |

【推导】**这张表本身就是 Step 3 的风险清单**：8 个读点里 4 个是同一个常量，任何人新增第 9 个读点都不会报错，只会静默绕过场景 policy。因此 §D.3 的守卫测试比任何一条功能测试都重要。

### A.2 四键 × 读点映射

| 键 | 读点 | 取值来源 | `None` 时回落 |
|---|---|---|---|
| `recall_top_k` | #1 #2 #3 #4 | `eff.recall_top_k` | `decisions.TOP_K_RECALL`（经 `ScenarioPolicy.effective_recall_top_k()`，`snapshot.py:61-62`） |
| `rerank_enabled` | #5 | `eff.rerank_enabled` | `settings.rerank_enabled`（经 `effective_rerank_enabled(platform)` 单向 AND，`snapshot.py:64-73`） |
| `relevance_threshold_vector` | #8 | `eff.threshold_for(use_rerank=False)` | `decisions.RELEVANCE_THRESHOLD_VECTOR`（`snapshot.py:88-92`） |
| `relevance_threshold_rerank` | #8 | `eff.threshold_for(use_rerank=True)` | `decisions.RELEVANCE_THRESHOLD_RERANK`（`snapshot.py:82-87`） |
| （Step 4）`final_top_n` | #6 #7 | `eff.final_top_n` | `decisions.TOP_K_RERANK`（Step 3 恒等，见 §A.4） |

**「从 `ScenarioSnapshot` 取还是从 `ScenarioPolicy` 取」的答案（明确回答）**：
**检索链路只接受 `ScenarioPolicy`，不接受 `ScenarioSnapshot`。**
理由【推导】：`ScenarioSnapshot` 里带着 `kb_ids`（已过场景门禁的请求集合）。把它传进检索层，下游就有了第二条取得「授权范围」的路径 —— 而`effective_kb_ids` 的三重交集（`chat.py:243-255`）是唯一的授权计算点。一旦检索层能自己看快照，某天就会有人写 `retrieve(session, q, tenant, snapshot.kb_ids, ...)`，绕过 `user.authorized_kb_ids` 与 admin G2 豁免。**把不该给的东西不给，比写下规矩更可靠。**

### A.3 语义定死：`recall_top_k` / `final_top_n` / `rerank_enabled` 的三角关系（回答「顺序约束」）

这是 Step 1–2 原稿 S-2 只说 `>=` 却没定义语义的地方，此处定死。

| 参数 | 角色（定死） | 重排关闭时 | 重排在线时 | 重排降级时 |
|---|---|---|---|---|
| `recall_top_k` | **每路召回的条数上限** + **RRF 候选池上限**（两者共用同一个值，与现状一致） | 池 = `min(k, |all_chunks|)` | 同上 | 同上 |
| `final_top_n` | **进入 L1 阈值判定与上下文的条数** | 池按 `final_score` 排序后截断到 `final_top_n`（`__init__.py:530-532`） | 取 `reranked_ids`（≤ `final_top_n`），**不再二次截断**（`__init__.py:527-528`） | 同「重排关闭」 |
| `rerank_enabled` | 是否**发起**重排调用 | 不发起，`use_rerank=False` | 发起；超时/异常 → `use_rerank=False`（`__init__.py:468-486`） | — |
| 阈值 | L1 拒答门槛，口径随**实际**打分来源 | `threshold_vector` | `threshold_rerank` | `threshold_vector`（**必须切回**） |

**三条定死的结论**：

1. **`rerank_enabled` 不改变 `recall_top_k` 的语义。** `recall_top_k` 永远是「池」，无论重排开不开。所以「重排关闭时 `recall_top_k` 是只取 top_k 还是取 top_k 再截断」的答案是：**取 `recall_top_k` 进池，再截断到 `final_top_n`**，与重排无关；重排关闭改变的只是「池里的东西怎么排序」。
2. **`recall_top_k` 绝不能传给 `rerank(top_n=...)`。** 【推导】这是本步最容易写错的一处：`__init__.py:459` 的第三个参数是 **`final_top_n`**。若误传 `recall_top_k`（比如场景把它配成 50），重排会返回最多 50 条绝对分，而 `use_rerank` 分支**不做二次截断**（第 527-528 行）→ 50 条全部进 `retrieved` → 上下文与 citations 一起膨胀。§D.3 有一条专门钉这个点（断言 `rerank` 收到的 `top_k` 与 `recall_top_k` 无关）。
3. **`recall_top_k < final_top_n` 不是错误，是「池比取用数小」。** 行为上等价于取用数被压到 `recall_top_k`，进上下文的条数变少。所以 S-2b 只 WARN（§C.3），不 clamp、不 raise —— clamp 到 `final_top_n` 会让「我配了 5」变成一个看不见的 8。

### A.4 单一取值入口：`EffectiveRetrieval`

**设计**：一次请求内，四个键只在**一个函数调用**里折叠成不可变值对象，之后 8 个读点全部读这个对象。

```
policy（可 None）
   └─→ effective_retrieval(policy, platform_rerank_enabled=...)  ← 唯一折叠点
          └─→ EffectiveRetrieval（frozen）
                 ├─ recall_top_k      → 4 个读点
                 ├─ rerank_enabled    → 1 个读点
                 ├─ threshold_for()   → 1 个读点
                 ├─ final_top_n       → 2 个读点（Step 4 起才随 policy 变）
                 └─ clamped           → 1 个 metric
```

**关于 `final_top_n` 的诚实说明**：Step 3 的 `ScenarioPolicy` **没有** `final_top_n` 字段（`snapshot.py:55-58` 只有 4 个字段），闭集里也没有它（`wired_in_step=4`）。所以 `eff.final_top_n` 在 Step 3 **恒等于 `decisions.TOP_K_RERANK`**。
仍然把它放进 `EffectiveRetrieval` 的唯一理由是：**Step 4 接线时只需要改一个表达式**（`effective.py` 里那一行），不用再去 6 个读点里找 `TOP_K_RERANK`。这是「为下一步留一个唯一的改动点」，不是「提前实现」。若评审认为这是过度设计，替代方案是把第 6/7 读点保留 `decisions.TOP_K_RERANK` 并接受 Step 4 时再动两处 —— 两害相权，我选前者（改一处 vs 改三处），理由登记在附录 A #5。

---

## B. 代码级设计

### B.1 新增 `app/services/scenario/effective.py`

```python
"""场景策略 → 检索参数有效值的**唯一折叠点**（Step 3 接线）。

为什么必须只有一个折叠点：
  四个策略键在检索链路里有 8 个读点（设计文档 §A.1）。若每处各自
  `policy.x if policy.x is not None else decisions.X`，那么「平台值 + 场景值」的
  折叠规则就有 8 份实现；改一处漏七处，表现是「某场景的阈值对 search 生效、
  对 ask 不生效」，且没有任何报错。

★ 本模块**不 from-import 常量**，一律 `decisions.X` / `policy_spec.X` 模块属性访问。
  理由与 decisions.py:6-8 同源：from-import 把值拷进本模块命名空间，
  monkeypatch 锁不住口径，测试就会退化成「断言今天恰好等于 50」。
★ 本模块纯函数、无 IO、不读 DB、不打日志：`clamped` 只**返回**，由调用方打点。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.config import decisions
from app.services.scenario import policy_spec
from app.services.scenario.snapshot import ScenarioPolicy

ACTIVE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "recall_top_k",
        "rerank_enabled",
        "relevance_threshold_vector",
        "relevance_threshold_rerank",
    }
)
"""**检索链路真正读**的键。

★ 与 `policy_spec.WIRED_KEYS`（CHECK 闭集）和 `snapshot.SCENARIO_POLICY_KEYS`
  （快照能承载）构成三方相等判据：三者任一不等 = 「能写不能读」或「能读不能用」。
  由 tests/scenario/test_effective.py::test_three_way_keyset_equality 锁死。
"""


@dataclass(frozen=True, slots=True)
class EffectiveRetrieval:
    """一次检索请求的有效参数。构造后不可改（frozen）。"""

    recall_top_k: int
    rerank_enabled: bool
    threshold_vector: float
    threshold_rerank: float
    final_top_n: int
    # 被平台上限裁掉的字段名（空 = 没裁）。**只返回不打点**，见模块说明。
    clamped: tuple[str, ...] = ()

    def threshold_for(self, *, use_rerank: bool) -> float:
        """按**实际**打分来源取阈值。

        ★ `use_rerank` 必须是「降级之后」的真实值，不是「配置开启」：
          重排抖动降级时若仍用 rerank 阈值，拒答行为会不可预测
          （decisions.py:24-41 的原始动机）。
        """
        return self.threshold_rerank if use_rerank else self.threshold_vector


def effective_retrieval(
    policy: ScenarioPolicy | None,
    *,
    platform_rerank_enabled: bool,
) -> EffectiveRetrieval:
    """把（场景覆盖, 平台值）折叠成有效值。`policy is None` 等价于全 None。

    `platform_rerank_enabled` 由调用方读一次后传入（而不是本函数自己读
    settings）：这样本函数是**纯的**，且「平台值只读一次」是调用方可以
    AST 断言的事实（§D.3 Z3-4）。
    """
    policy = policy or ScenarioPolicy()
    clamped: list[str] = []

    recall_top_k = policy.effective_recall_top_k()
    # ★ 上下界都收窄：上界是平台硬上限（RECALL_TOP_K_MAX 会随压测**下调**，
    #   届时历史行可能越界）；下界防 0（0 会让切片为空 → 全量拒答）。
    #   收窄 MUST 打点（clamped 返回给调用方），否则就是静默降级。
    if recall_top_k > policy_spec.RECALL_TOP_K_MAX:
        recall_top_k = policy_spec.RECALL_TOP_K_MAX
        clamped.append("recall_top_k")
    elif recall_top_k < 1:
        recall_top_k = 1
        clamped.append("recall_top_k")

    return EffectiveRetrieval(
        recall_top_k=recall_top_k,
        rerank_enabled=policy.effective_rerank_enabled(platform_rerank_enabled),
        threshold_vector=policy.effective_threshold(use_rerank=False),
        threshold_rerank=policy.effective_threshold(use_rerank=True),
        # ★ Step 4 的唯一改动点：改为 policy.final_top_n 折叠。
        #   现在恒等平台值是**有意的**（ScenarioPolicy 尚无此字段）。
        final_top_n=decisions.TOP_K_RERANK,
        clamped=tuple(clamped),
    )
```

**为什么 `clamped` 只返回不打点**：`effective.py` 要保持纯（`§D` 的单测直接调它，不需要 monkeypatch metrics）。打点在 `retrieve()` 里做一次，天然去重。

### B.2 `retrieve()` 的改造（逐点 diff）

签名（`__init__.py:215-223`）：

```python
    async def retrieve(
        self,
        session: AsyncSession,
        query: str,
        tenant_id: uuid.UUID,
        authorized_kb_ids: list[uuid.UUID],
        clearance: int,
        user_subjects: list[str],
        *,
        policy: ScenarioPolicy | None = None,   # ← 新增，keyword-only
    ) -> RetrievalResult:
```

> **为什么 keyword-only**：`retrieve()` 今天有 4 个调用点传 6 个位置参数（`chat.py:671`、`integrations.py:352`、`eval_service.py:235`、`scripts/*`）。改成位置参数会静默改变实参含义。`*,` 之后任何老调用都不动。

构造点（插在 `__init__.py:240` 的 `pushdown` 之后、`① 向量召回` 之前）：

```python
        # ── 0. 场景策略折叠（唯一取值入口；Step 3）──────────────────────
        # ★ 位置 MUST 在 `if not authorized_kb_ids` 之后：空范围请求不产生
        #   「这台机器按什么参数跑」的读取，与改动前一致。
        platform_rerank_on = get_settings().rerank_enabled
        eff = effective_retrieval(policy, platform_rerank_enabled=platform_rerank_on)
        if eff.clamped:
            metrics.incr("rag.scenario.policy_clamped", len(eff.clamped))
            logger.warning(
                "场景策略超出平台上限，已收窄到平台值",
                extra={"fields": eff.clamped, "trace_id": trace.get_trace_id()},
            )
        declared = policy.declared() if policy is not None else {}
        if declared:
            # 只在真的配了东西时打点/记日志：默认场景的 declared 是 {}，
            # 零影响路径上一条噪音都不产生。
            metrics.incr("rag.scenario.policy_active", len(declared))
            logger.info(
                "场景策略生效",
                extra={"keys": sorted(declared), "trace_id": trace.get_trace_id()},
            )
```

8 个读点逐处替换：

| 行 | 改前 | 改后 |
|---|---|---|
| `258` | `"limit": decisions.TOP_K_RECALL,` | `"limit": eff.recall_top_k,` |
| `368` | `"limit": decisions.TOP_K_RECALL,` | `"limit": eff.recall_top_k,` |
| `395` | `{**pushdown, "tsquery": tsquery, "limit": decisions.TOP_K_RECALL}` | `{**pushdown, "tsquery": tsquery, "limit": eff.recall_top_k}` |
| `434` | `sorted_ids[: decisions.TOP_K_RECALL]` | `sorted_ids[: eff.recall_top_k]` |
| `447` | `rerank_off = not get_settings().rerank_enabled` | `rerank_off = not eff.rerank_enabled` |
| `459` | `rerank_service.rerank(query, documents, decisions.TOP_K_RERANK)` | `rerank_service.rerank(query, documents, eff.final_top_n)` |
| `532` | `retrieved[: decisions.TOP_K_RERANK]` | `retrieved[: eff.final_top_n]` |
| `537` | `decisions.active_threshold(use_rerank)` | `eff.threshold_for(use_rerank=use_rerank)` |

`447` 附近追加「重排被场景关掉」的区分打点（因为 `mode="off"` 这个既有 label 分不清「平台关」与「场景关」）：

```python
        rerank_off = not eff.rerank_enabled
        if rerank_off:
            if platform_rerank_on:
                # 平台开着、场景关掉 —— 这是**新出现**的一种 off，运维需要能区分
                metrics.incr("rag.rerank.disabled_by_scenario")
            use_rerank = False
```

> `mode` 这个既有 metric label **保持三态不变**（`off` / `on` / `degraded`，`__init__.py:489-493`）。理由：改 label 取值会让既有看板出现「全是空档」的断点，而断点比不精确更难排查。

### B.3 `retrieve_chain()` 的改造

```python
    async def retrieve_chain(
        self,
        session: AsyncSession,
        query: str,
        tenant_id: uuid.UUID,
        authorized_kb_ids: list[uuid.UUID],
        clearance: int,
        user_subjects: list[str],
        rewrite_score: int | None = None,
        *,
        policy: ScenarioPolicy | None = None,   # ← 新增
    ) -> RetrievalResult:
```

内部两处透传【源：`__init__.py:875, 909`】：

```python
            result = await self.retrieve(
                session, query, tenant_id, authorized_kb_ids, clearance, user_subjects,
                policy=policy,
            )
...
            rounds.append(await self.retrieve(
                session, sq, tenant_id, authorized_kb_ids, clearance, user_subjects,
                policy=policy,
            ))
```

> **为什么必须透传到多轮子问题**：链式检索把一个问题拆成 N 个子问题，每个子问题都走一次 `retrieve()`。漏掉任一处，场景的 `recall_top_k` 就只对单趟生效、对链式不生效 —— 而链式是 `CHAIN_RETRIEVAL_ENABLED=True`（`decisions.py:362`）的默认路径。§D.3 有一条断言「多轮每轮收到的 policy 与入口相同」。

### B.4 调用方改造

**`chat.py::_chat_ask_impl`**（签名 `268-273`）：

```python
async def _chat_ask_impl(
    payload: ChatAskRequest,
    user: CurrentUser,
    session: AsyncSession,
    integration: IntegrationApplication | None = None,
    policy: ScenarioPolicy | None = None,   # ← 新增
) -> StreamingResponse:
```

`retrieve_chain` 调用处（`671-682`）追加 `policy=policy,`。
`chat.py` 顶部加 `TYPE_CHECKING` 导入（**不运行时导入**，避免 chat → scenario 的 import 成本与潜在环）：

```python
if TYPE_CHECKING:
    from app.services.scenario.snapshot import ScenarioPolicy
```

> `/chat/ask`（`chat.py:265`）调 `_chat_ask_impl(payload, user, session)` → `policy` 取默认 `None` → **Web 端行为零变化**（Z3-1/Z3-2 覆盖）。

**`integrations.py`**：两个 handler 各加一个实参。

```python
# integration_ask（321-330）
    response = await _chat_ask_impl(
        ChatAskRequest(...),
        principal.user,
        session,
        integration=principal.application,
        policy=snapshot.policy,          # ← 新增
    )

# integration_search（352-359）
        result = await get_retrieval_service().retrieve(
            session, payload.query, user.tenant_id, effective_kb_ids,
            user.clearance, user.subjects,
            policy=snapshot.policy,      # ← 新增
        )
```

**为什么在 handler 而不是更深处解析**：`snapshot` 是 handler 已有的局部变量（`_resolve_or_reject` 的返回值），此处传入是**零新增 IO**；若放到 service 层，就得把 session/tenant/application 一起下传，等于把 resolver 的依赖链拖进检索层（而 `resolver.py:12-13` 已明确写了「resolver 不依赖 retrieve」，反向也不该依赖）。

### B.5 可观测性（只加 metric/日志，不动响应契约）

| 名称 | 类型 | 触发 | 用途 |
|---|---|---|---|
| `rag.scenario.policy_active` | counter（值 = 声明键数） | 每次检索，仅当 `policy.declared()` 非空 | 「有多少请求真的在被场景策略影响」 |
| `rag.scenario.policy_clamped` | counter（值 = 被裁字段数） | 每次检索，`eff.clamped` 非空 | 平台上限收窄后，历史配置越界的量 |
| `rag.rerank.disabled_by_scenario` | counter | 平台开、场景关 | 区分 `mode="off"` 的两种成因 |
| `rag.scenario.selfcheck_*` | counter / gauge | 启动自检（§C） | 数据类自检的命中数 |

> **为什么 Step 3 不把 `policy_applied` 加进 `meta`**：`meta` 是**对外契约**，`policy_applied` 属 Step 4（Step 1–2 文档 §D.2 已把 `required` 锁在 4 键）。在 Step 3 加它会让「契约冻结稿」与实现分叉。代价是 Step 3 的可观测只在服务端 —— 这条代价登记为 §E 的 G3-1，不隐藏。

### B.6 禁止清单（守卫测试的可执行定义）

`app/services/retrieve/__init__.py` 内：

| 模式 | 允许出现次数 | 理由 |
|---|---|---|
| `decisions.TOP_K_RECALL` | **0** | 已由 `eff.recall_top_k` 取代 |
| `decisions.TOP_K_RERANK` | **0** | 已由 `eff.final_top_n` 取代 |
| `decisions.active_threshold` | **0** | 已由 `eff.threshold_for` 取代 |
| `get_settings().rerank_enabled` | **恰好 1** | 平台值只读一次并传给折叠函数 |
| `decisions.RRF_K` / `GOVERNANCE_WEIGHT_SCALE` / `KEYWORD_TSQUERY_ENABLED` / `ALIAS_EXPANSION_ENABLED` / `TABLE_RECALL_*` / `SECTION_RECALL_*` / `GRAPH_*` | 不限 | L0 锁定或 Step 4/5 范围，**本条不覆盖**（写清边界，避免守卫测试越界误报） |

`app/services/scenario/effective.py` 内：

| 模式 | 允许出现次数 | 理由 |
|---|---|---|
| `from app.config.decisions import` | **0** | 必须模块属性访问（否则 monkeypatch 锁不住） |
| `from app.services.scenario.policy_spec import RECALL_TOP_K_MAX` | **0** | 同上，且为了 S-2a 的 monkeypatch 可用 |

---

## C. 自检 S-2 ~ S-9

### C.1 三个执行时机（先定时机，再定判据）

| 时机 | 代号 | 有无 DB | 谁调 | 装什么 |
|---|---|---|---|---|
| 启动（常量类） | **T1** | 无 | `main.lifespan` + worker `on_startup` | 代码/常量之间的自洽：S-2a、S-4a、S-6 注册 |
| 启动（数据类） | **T2** | 有 | **只 api 侧** `main.lifespan` | 库里配置的合法性/漂移：S-2b、S-3、S-4b、S-5、S-7、S-8、S-9 |
| 请求时 | **T3** | 有（已有） | `retrieve()` | 只做 clamp + 打点（§B.2），**不做检查** |
| 发布时 | **T4** | 有 | Step 4 的写接口（本步只交付函数） | 唯一能**拒绝**配置进入系统的位置：`check_policy_publishable()` |

**为什么 T2 只在 api 侧跑**：worker（`parse_job.py:631-636`）不检索，跑一遍全场景扫描是纯加启动成本与 DB 压力，且它没有任何「读策略」的路径可以受益。
**为什么 T1 两侧都跑**：常量自检成本近零（纯内存），且它能保证 api 与 worker 对「规格表是否自洽」持有同一判断 —— 两侧结论不同是最难查的一类问题。

**为什么 T2 不放在 `check_capabilities()` 里**：`self_check_ext.check_capabilities()` 在「图与 wiki 都关」时**提前 return**（`self_check_ext.py:322-325`）。本机默认 `graph_enabled=False`、`wiki_enabled=False` → 放进去等于**永远不会执行**。这是「自检写了一行、永远是绿的」的经典陷阱，必须点明。

### C.2 逐条规格（判据 / 动作 / 理由）

> 每条给：**时机**、**判据（可直接翻译成代码）**、**失败动作**、**为什么是这个动作**。
> 「动作」只有三种：`RuntimeError`（拒绝启动）、`WARN`（日志 + metric，继续跑）、`拒绝`（T4 返回 400）。

#### S-2a `recall_top_k` 上限常量自洽 — **T1 / RuntimeError**

- **判据**：`policy_spec.RECALL_TOP_K_MAX >= 1` 且 `policy_spec.RECALL_TOP_K_MAX <= 1000`。
- **为什么 `<= 1000`**：DB CHECK 的兜底上限是 1000（`0028` 的 `ck_scenario_versions_policy_recall_top_k`，即 `::int BETWEEN 1 AND 1000`）。【源】若代码上限 > DB 上限，写入会先被 DB 拒，运维看到的是「数据库约束冲突」而不是「参数超范围」—— 两处口径分裂。
- **为什么 RuntimeError**：这是**代码常量之间**的关系，不含任何数据依赖，错了就是代码 bug，启动时必现；没有理由降级为 WARN。

#### S-2b `recall_top_k` 与 `final_top_n` 的协调 — **T2 WARN + T4 拒绝**

- **判据**：对每个 `published` 版本，`min(policy.effective_recall_top_k(), RECALL_TOP_K_MAX) >= decisions.TOP_K_RERANK`。
- **T2 动作**：WARN（列出 `tenant_id / scenario_key / recall_top_k`）+ `metrics.incr("rag.scenario.recall_below_final_top_n")`。**不改行为**。
- **T4 动作**：`check_policy_publishable()` 抛 `PolicyValidationError` → 写接口返回 400。
- **为什么 T2 不是 RuntimeError（这是与 Step 1–2 原稿相反的决定，见附录 A #2）**：
  1. 这是**数据**问题，可能是有意的实验（把池压到 5 看召回质量对答准率的影响）。拒绝启动 = 一个场景的配置拖垮整个实例，**fail-closed 用错地方就是可用性事故**。
  2. 语义上它**已经生效**（进上下文的条数真的变少了），不是「配了不生效」那类静默 no-op —— 后者才是必须硬拦的。
  3. 真正能阻止它进入系统的位置是 **T4**（写入时拒绝），把硬约束放在那里，T2 只负责「让已有的越界可见」。
- **为什么不 clamp**：clamp 到 `final_top_n` 会让「我配了 5」变成看不见的 8 —— 比 WARN 更糟（WARN 至少留痕，clamp 连痕都没有… 除非打点；但打点后它和 S-2b 的 WARN 重复，不如不 clamp）。

#### S-3 单向 AND 造成的静默无效 — **T2 / WARN**

- **判据**：`settings.rerank_enabled is False` 且存在 `published` 版本满足 `policy.get("rerank_enabled") is True`。
- **动作**：WARN（列出场景数）+ `metrics.incr("rag.scenario.rerank_open_but_platform_off", n)`。有效值仍为 `False`。
- **为什么 WARN 不 raise**：平台关重排是本机 CPU 的现实决策（`__init__.py:451-453` 记录 50 条 66s），不是错误。要暴露的是「配置者以为开了」，不是「配置非法」。
- **为什么不在 T3 每次判**：`settings` 只在进程启动时从环境读，**启动扫描的时点恰好等于「平台开关可能变化」的时点**；请求期重复判只增加分支，不能给出更好的动作。

#### S-4a 阈值规格表的范围自洽 — **T1 / RuntimeError**

- **判据**：对 `THRESHOLD_KEYS` 的两个字段，`min_value is not None and min_value > 0.0` 且 `max_value is not None and max_value <= 1.0`。
- **为什么必须自检**：`validate_policy` 除了范围还额外要求「阈值必须 > 0」（`policy_spec.py:356-358`，S-4 的第二道）。**若规格表的 `min_value` 是 `0.0`，规格表与校验器就互相矛盾**：规格表说 0 合法、校验器说 0 非法。这类矛盾在 Step 1–2 的文档里确实出现过（附录 A #3），所以用常量自检把它钉死。`max_value <= 1.0` 的理由是两种分数都在 `[0,1]`（rerank 是 sigmoid 分，`__init__.py:460`；向量余弦归一后同区间）。

#### S-4b 阈值数据越界 — **T2 / WARN**

- **判据**：`published` 版本的阈值 ∉ `(0.0, 1.0]`。
- **动作**：WARN + `metrics.incr("rag.scenario.threshold_out_of_range")`。`effective_retrieval` **不 clamp**（保留原值）。
- **为什么命中即「绕过写入路径」**：写入侧有 `validate_policy` 两道（范围 + 必须 > 0），DB 侧有 CHECK（`::float8 BETWEEN 0.0 AND 1.0`）。两者都过不了 `0.0`，所以能落库的越界值只可能来自**直接写 SQL**。这正是要留观测的场景。
- **为什么不 clamp**：阈值 clamp 会**改变拒答行为**（比 `recall_top_k` 敏感得多）；把它悄悄拉到 0.01 仍然是「永不拒答」，并没有修好任何东西，只是让日志更干净。**宁可留一个显眼的错值 + WARN。**
- **为什么不升级成 `UNRESOLVABLE`**：那会让一个配置错字把整个场景变成 500。而越界的直接后果（拒答率突变）本身是可观测的。→ 是否该在 `to_policy()` 里加范围校验，属「读写同源校验」的一次重构，归 Step 4（§E G3-5）。

#### S-5 未标定的 rerank 阈值被启用 — **T2 / WARN**

- **判据**：存在 `published` 版本设了 `relevance_threshold_rerank`，且 `settings.rerank_enabled is False`。
- **动作**：WARN，文案必须包含「请先跑 `scripts/calibrate_threshold.py --mode rerank`」。
- **理由【源】**：`decisions.py:37-41` 自述 `RELEVANCE_THRESHOLD_RERANK` 是**未标定的占位值**。这个键现在是**休眠**的，一旦有人打开平台重排就立刻生效 —— 而那时没人会想起「这个值是抄来的」。WARN 是在「还没打开」的窗口里提醒。

#### S-6 站位值必须出现在启动横幅 — **T1 / 横幅（非失败动作）**

- **判据**：`app.config.placeholders.PLACEHOLDERS` 中 `owner == "scenario"` 的条目数 > 0。
- **动作**：横幅（`capabilities._render_text`）追加一行：`含站位值参数: RECALL_TOP_K_MAX=200（待压测标定）, SCENARIO_CACHE_TTL_SECONDS=60（待运维节奏标定）`。
- **为什么必须在 config 层新建 `placeholders.py`**：【源自项目纪律】`decisions.py:123` 明确「配置层不反向依赖服务层」。`RECALL_TOP_K_MAX` 在 `services/scenario/policy_spec.py`，`CACHE_TTL_SECONDS` 在 `services/scenario/resolver.py` —— 若 `capabilities.py`（config 层）去 import 它们，就造出一条 config → services 的反向依赖。做法：**登记表放 config，服务层反过来 import 它并做「我登记了没有」的 import 期断言**（方向正确 + 漏登记会立刻炸）。
- **为什么站位值这件事值得一条自检**：站位值的危害不是「不准」，而是「**没人知道它不准**」。项目已把 `GRAPH_SKIP_MIN_CHUNKS` 标了站位值，但只写在 docstring 里（`decisions.py:940`），没人会在启动时看到。横幅是唯一会被每个运维看见的位置。

```python
# app/config/placeholders.py（新增，~40 行）
"""站位值登记表：**必须在启动横幅里可见**的未标定常量。

★ 为什么放 config 层：capabilities.py 属 config，若它去 import
  app/services/... 就造出 config → services 的反向依赖（decisions.py:123 的纪律）。
  方向必须是 services → config：服务层 import 本表并断言「我的站位常量已登记」。
"""
@dataclass(frozen=True, slots=True)
class Placeholder:
    name: str        # 常量名（与人读位置）
    value: object    # 当前值，供横幅展示
    owner: str       # "scenario" / "graph" / ...
    note: str        # 为什么是站位值 + 怎么标定

PLACEHOLDERS: Final[tuple[Placeholder, ...]] = (
    Placeholder(
        name="RECALL_TOP_K_MAX", value=200, owner="scenario",
        note="待压测标定（§G-6）",
    ),
    Placeholder(
        name="SCENARIO_CACHE_TTL_SECONDS", value=60.0, owner="scenario",
        note="待运维发布节奏标定（§G-7）",
    ),
)

def for_owner(owner: str) -> tuple[Placeholder, ...]: ...
```

```python
# policy_spec.py 追加（import 期，fail-fast）
from app.config.placeholders import for_owner as _placeholders_for

if "RECALL_TOP_K_MAX" not in {p.name for p in _placeholders_for("scenario")}:
    raise RuntimeError(
        "RECALL_TOP_K_MAX 是站位值，但未登记在 app/config/placeholders.py —— "
        "未登记的站位值不会出现在启动横幅，等于没人知道它没标定"
    )
```

> 同一断言在 `resolver.py` 对 `CACHE_TTL_SECONDS` 做一次（常量名登记为 `SCENARIO_CACHE_TTL_SECONDS`，因为横幅里要带组件前缀才不歧义）。

#### S-7 `db_binding` 已配但未生效 — **T2 / WARN**

- **判据**：`SELECT count(*) FROM scenarios WHERE db_binding IS NOT NULL` > 0，且 `WIRED_RETRIEVAL_STEP < 6`（Step 3 时恒真）。
- **动作**：WARN（count）+ `metrics.incr("rag.scenario.db_binding_configured")`。
- **理由**【源自 Step 1–2 的 D-A4 对冲】：`db_binding` 列在 0028 就建了，但 Step 6 才读。若运维现在就填上，行为不变 —— 必须让它可见，否则「配了不生效」。
- **为什么用 `WIRED_RETRIEVAL_STEP` 而不是 `CURRENT_STEP`**：见 §C.4。

#### S-8 内置场景被改动 — **T2 / WARN**

- **判据**：`scenarios.is_builtin AND v.state='published' AND v.policy <> '{}'::jsonb` 行数 > 0。
- **动作**：WARN（打印 `tenant_id / scenario_key / policy 的键`）+ `metrics.incr("rag.scenario.builtin_policy_not_empty")`。
- **理由**【源自 Step 1–2 §A.6 障碍 3】：这是跨表约束（`is_builtin` 在 `scenarios`、`policy` 在 `scenario_versions`），DB 层无法表达。写入侧（Step 4）会拒，但「有人绕过程序直写 SQL」只能靠这里发现。内置场景的语义是「恒等于平台默认」，被改了就等于「默认口径被偷偷改掉」—— 这是最该报警的一件事。

#### S-9 DNA 声明漂移 — **T2 / WARN**

- **判据**：内置行的 `dna_declared` 按 `env_bound` 展开后，与 `current_dna()` 逐字段不等。展开映射表：

| `dna_declared` 键 | 取哪个 profile 的哪个字段 |
|---|---|
| `embedding_model` | `index.embedding_model` |
| `embedding_dim` | `index.embedding_dim` |
| `embedding_text_version` | `index.embedding_text_version` |
| `chunker_version` | `index.chunker_version` |
| `graph_extractor_model` | `graph.extractor_model` |
| `graph_prompt_version` | `graph.prompt_version` |
| `wiki_generator_model` | `wiki.generator_model` |
| `wiki_prompt_version` | `wiki.prompt_version` |

- **动作**：WARN + `metrics.incr("rag.scenario.dna_declared_drift")`，输出 `field: declared → actual` 三列。
- **理由**【源自 Step 1–2 §A.6 障碍 2】：CI 测试是硬门禁（`test_seed_dna_matches_platform_profiles`），运行期 WARN 是「有人直接改库」或「当前部署的 env 与迁移记录不一致」的最后一道观测。**注意**：`env_bound` 的 4 项（`embedding_model` / `embedding_dim` / 两个模型名）在迁移里没有落值，是**按当前 settings 展开**后比较 —— 所以它检验的是「声明与当前部署是否一致」，不是「声明与某个固定字面量是否一致」。这正是 Step 1–2 设计成 `env_bound` 的原因，此处不改。
- **为什么不 raise**：DNA 漂移的**真实后果**是「场景声明过期」（不可信），而不是「检索算错」—— 检索只从平台函数读 DNA（`snapshot.py:13-17`）。所以它是一个**文档/审计**问题，WARN 恰当。

### C.3 动作选择的统一判据（为什么有的 raise 有的不 raise）

| 判据 | raise（拒绝启动） | WARN + metric |
|---|---|---|
| 判断依据是**代码常量之间**的关系 | ✅ | |
| 判断依据是**库里的数据** | | ✅ |
| 违反时后果是**整实例不可用**（而非某个场景降级） | ✅ | |
| 违反时后果是**某个场景/某几个请求** | | ✅ |
| 有**别的时机**能真正拒绝它进入系统（T4 写入） | 硬约束放 T4，T2 只 WARN | ✅ |

一句话：**「代码错了就别启动，数据错了就喊出来」。** 数据是运维/用户改出来的，让一次手滑变成全员 502，是把 fail-closed 用在错的地方。

### C.4 `CURRENT_STEP` 的既成事实与 `WIRED_RETRIEVAL_STEP`

【源】`policy_spec.CURRENT_STEP == 2`，且既有测试**硬断言**它等于 2：

```python
# tests/scenario/test_resolver_policy.py:49-51
    assert CURRENT_STEP == 2
    assert POLICY_CLOSURE_STEP == 3
    assert POLICY_CLOSURE_STEP > CURRENT_STEP
```

【推导】Step 3 之后「本次交付到第几步」事实是 3，但这个常量**不能改**：
- 它现在只服务于「错误文案」语义（`policy_spec.py:25`），`validate_policy` 的实际文案读的是 `field.wired_in_step`（`policy_spec.py:328-338`），不读 `CURRENT_STEP`；
- 改它会破坏一条既有断言，而「不许放宽既有测试」是硬约束。

**解法**：新增 **`WIRED_RETRIEVAL_STEP: Final[int] = 3`**（放 `effective.py`，因为它描述的是「检索链路接线到第几步」），并加两条断言：
- `WIRED_RETRIEVAL_STEP == max(f.wired_in_step for f in POLICY_FIELDS if f.name in ACTIVE_KEYS)`；
- `WIRED_RETRIEVAL_STEP <= POLICY_CLOSURE_STEP`（接线不能超前于闭集，否则键能写但「闭集说还不能写」→ 自相矛盾）。

`CURRENT_STEP` 保留并加一行 docstring 说明它是 Step 1–2 的历史值。**这是三个步骤号并存**（`CURRENT_STEP` / `POLICY_CLOSURE_STEP` / `WIRED_RETRIEVAL_STEP`），看起来丑，但每一个都有不同的语义来源，合并任意两个都会让另一个失真 —— 这个理由必须写进 docstring，否则下一个人一定会去合并它们。

### C.5 落地骨架

```python
# app/services/scenario/self_check.py（新增）

def check_scenario_constants() -> None:
    """T1：常量/代码自洽。违反 → RuntimeError（拒绝启动）。

    只查「代码与代码」的关系，不查库。api 与 worker 两侧都调。
    """
    _s2a_recall_top_k_ceiling()
    _s4a_threshold_spec_range()
    _s6_placeholders_registered()


async def check_scenario_data(session: AsyncSession, *, max_step: int) -> dict[str, int]:
    """T2：数据类自检。**永不 raise**，返回命中计数（供 metric 与日志）。

    `max_step` 传 effective.WIRED_RETRIEVAL_STEP（S-7 用）。
    一次 SQL 拉全量 published 版本，Python 侧跑 S-2b/S-3/S-4b/S-5/S-7/S-8/S-9 ——
    场景数量级是「个位数到十几个」，N 条查询没有任何收益。
    """
    rows = await _load_all_published(session)
    ...
    return {"recall_below_final_top_n": ..., "rerank_open_but_platform_off": ..., ...}


def check_policy_publishable(
    policy: Mapping[str, object], *, platform_rerank_enabled: bool
) -> None:
    """T4：发布前校验（Step 4 的写接口 MUST 调用）。

    Step 3 只有测试调它 —— 但必须现在写，因为「硬约束」此刻就该定义好，
    而不是等 Step 4 的人在写接口时顺手发明一个。
    """
    checked = validate_policy(policy)          # 复用写入侧校验，不重写
    recall = checked.get("recall_top_k")
    if recall is not None and recall < decisions.TOP_K_RERANK:
        raise PolicyValidationError(
            f"recall_top_k={recall} 小于最终取用条数 {decisions.TOP_K_RERANK}："
            "候选池比取用数小，进上下文的条数会被压低",
            field="recall_top_k",
        )
    if checked.get("rerank_enabled") is True and not platform_rerank_enabled:
        raise PolicyValidationError(
            "平台未启用重排（settings.rerank_enabled=false），"
            "本键不会生效：请先确认算力或去掉本键",
            field="rerank_enabled",
        )
```

**`_load_all_published` 复用 `ScenarioRow`**：返回与 `resolver.LOOKUP_SQL` 同别名的一组行，再用 `snapshot.ScenarioRow.to_policy()` 折叠 —— **自检与运行时用同一份解析逻辑**。若自检自己写一套，「自检说合法、请求时 500」就成了必然。
一处必要的兜底：`to_policy()` 对类型错会抛 `ScenarioError`，自检必须 `try/except` 后计入 `selfcheck_unparsable` 并继续扫下一行 —— 因为「一行坏数据让整个实例起不来」正是 §C.3 判据要避免的。

```python
# app/main.py lifespan（插在 decisions.self_check() 之后）
    from app.services.scenario.self_check import (
        check_scenario_constants, check_scenario_data,
    )
    check_scenario_constants()                       # T1，无 DB，违反即拒绝启动
    async with SessionLocal() as _sc_check:
        _sc = await check_scenario_data(                      # T2，只 WARN
            _sc_check, max_step=WIRED_RETRIEVAL_STEP,
        )
```

```python
# app/workers/parse_job.py on_startup（只加常量类）
    from app.services.scenario.self_check import check_scenario_constants
    check_scenario_constants()
```

**为什么 T2 用独立 `SessionLocal()` 而不是借用已有的 startup session**：lifespan 里现有代码没有长期存在的 session（`SessionLocal` 是工厂）。开一次、关一次，与 `capabilities.collect_*_stats` 的做法一致【源：`capabilities.py:391-424` 里的横幅统计就是这样拿 session 的】。

### C.6 clamp 的完整决策（含「上限被下调」这个真实风险）

`RECALL_TOP_K_MAX` 是**站位值**（`policy_spec.py:41`，§G-6），这意味着它**将来会被重标定，且很可能下调**（200 条候选从未被验证过收益）。下调之后：

| 情形 | 数据来源 | 处理 |
|---|---|---|
| 历史行 `recall_top_k=150`，上限下调到 100 | 合法写入（当时 150 在范围内） | **clamp 到 100** + `metrics.incr("rag.scenario.policy_clamped")` + WARN |
| 历史行 `recall_top_k=0` | 只可能来自绕过写入路径 | **clamp 到 1** + 同上 |
| 阈值越界 | 同上 | **不 clamp**，WARN（S-4b） |

**为什么 `recall_top_k` 可以 clamp 而阈值不行**：`recall_top_k` 的语义是「资源使用量」，平台上限是**硬边界**，越界就等于违反平台约束 → 裁到边界是「按平台约束执行」，属于 I-2（只能收紧）。阈值的语义是「判据」，裁掉一个错值并不会得到一个对的判据（`0.0 → 0.01` 仍然是永不拒答），只会掩盖错误。

**为什么 clamp 必须打点**：否则「我配了 150，实际跑 100」就是静默降级 —— 这正是 Step 1–2 用整章反对的形态。打点后它变成「可观测的收窄」，性质不同。

---

## D. 测试规格

> 原则：**NO-DB 为主**（本机无 PG、无 GPU、禁止云端模型）。凡需要真库/真模型的，给「无 PG/无模型时的替代证据形式」，并明确标注它证明了什么、没证明什么（§D.7）。

### D.1 Z3-1 反自证断言（**这是 Step 3 的硬指标**）

```python
# tests/scenario/test_effective.py
@pytest.mark.parametrize("platform_rerank", [False, True])
def test_z3_1_default_policy_equals_platform_constants(monkeypatch, platform_rerank):
    """policy 全 None 时，有效值 == 平台当前值。

    ★ 不许写成 `assert eff.recall_top_k == 50`：那是「今天恰好等于 50」，
      平台常量改成 30 它照样绿（因为两边都是抓取值），证明不了「现读」。
    ★ 这里的做法是把平台常量 monkeypatch 成不可能撞上的值，再断言 eff 跟着变。
      这才是「从 decisions/settings 现读」的证据。
    """
    from app.config import decisions
    from app.services.scenario import policy_spec
    from app.services.scenario.effective import effective_retrieval
    from app.services.scenario.snapshot import ScenarioPolicy

    monkeypatch.setattr(decisions, "TOP_K_RECALL", 37)
    monkeypatch.setattr(decisions, "TOP_K_RERANK", 5)
    monkeypatch.setattr(decisions, "RELEVANCE_THRESHOLD_VECTOR", 0.61)
    monkeypatch.setattr(decisions, "RELEVANCE_THRESHOLD_RERANK", 0.62)
    monkeypatch.setattr(policy_spec, "RECALL_TOP_K_MAX", 99)

    eff = effective_retrieval(ScenarioPolicy(), platform_rerank_enabled=platform_rerank)

    assert eff.recall_top_k == 37
    assert eff.final_top_n == 5
    assert eff.threshold_vector == 0.61
    assert eff.threshold_rerank == 0.62
    assert eff.rerank_enabled is platform_rerank
    assert eff.clamped == ()
```

> 这条测试同时钉住了 §B.1 的「不 from-import 常量」纪律：若 `effective.py` 写成 `from ...policy_spec import RECALL_TOP_K_MAX`，`monkeypatch.setattr(policy_spec, ...)` 不会影响已拷贝的值，测试会红 —— **纪律由测试强制，不靠 review**。

### D.2 成对测试（四键 × 配了/没配）与 Z3-2/Z3-3

| 测试 | 构造 | 断言 |
|---|---|---|
| `test_recall_top_k_override_wins` | `policy(recall_top_k=20)` | `eff.recall_top_k == 20`（且与平台 50 不同） |
| `test_recall_top_k_platform_when_unset` | `policy()` | `eff.recall_top_k == decisions.TOP_K_RECALL`（平台常量被 monkeypatch 过） |
| `test_rerank_scenario_can_only_tighten` | 4 组合：`(平台T,None)` `(平台T,False)` `(平台F,None)` `(平台F,True)` | `T / T / F / F` —— **第 4 组是关键**：平台关时场景开 → 仍是 F |
| `test_threshold_vector_override_isolated` | `policy(relevance_threshold_vector=0.7)` | `threshold_vector==0.7` 且 `threshold_rerank==平台 rerank 值` |
| `test_threshold_rerank_override_isolated` | `policy(relevance_threshold_rerank=0.8)` | 对称断言（证明两键互不影响） |
| `test_threshold_for_follows_actual_score_source` | `policy(vector=0.7, rerank=0.3)`，`threshold_for(use_rerank=False/True)` | `0.7 / 0.3` —— 钉住「降级后切回 vector 口径」 |
| `test_effective_threshold_matches_active_threshold` | `policy()`（全 None） | 对 `use_rerank ∈ {True,False}`，`eff.threshold_for` == `decisions.active_threshold`；**再 monkeypatch 两个平台阈值后仍相等**（证明是现读） |
| `test_clamp_upper_and_lower` | `policy(recall_top_k=999)` / `policy(recall_top_k=0)` | `eff.recall_top_k` == `RECALL_TOP_K_MAX` / `1`，`eff.clamped == ("recall_top_k",)` |
| `test_z3_2_retrieval_result_identical` | 复用 `tests/retrieve/test_governance_weight.py` 的 `Harness`（FakeEmbedding/FakeRerank/FakeSession） | `retrieve(policy=None)` 与 `retrieve(policy=ScenarioPolicy())` 的 `dataclasses.replace(r, stage_ms={})` **相等** |
| `test_z3_3_unbound_app_policy_is_empty` | handler 级（复用 `test_scenario_wiring.py` 的桩） | 传给 retrieve 的 `policy.declared() == {}` |

> `test_z3_2` **必须排除 `stage_ms`**：那是计时，逐次不同。除此之外 `RetrievalResult` 是 dataclass【源：`retrieve/base.py:46`】，`==` 逐字段比较，能真正发现顺序/分数/refused 的漂移。

### D.3 接线点覆盖测试（防「读两次配置」）

**D.3.1 静态守卫（Z3-4）**

```python
def test_z3_4_no_second_read_of_platform_constants():
    src = (API_ROOT / "app/services/retrieve/__init__.py").read_text("utf-8")
    assert src.count("decisions.TOP_K_RECALL") == 0
    assert src.count("decisions.TOP_K_RERANK") == 0
    assert src.count("decisions.active_threshold") == 0
    assert src.count("get_settings().rerank_enabled") == 1   # 平台值只读一次

def test_effective_module_does_not_from_import_constants():
    src = (API_ROOT / "app/services/scenario/effective.py").read_text("utf-8")
    assert "from app.config.decisions import" not in src
    assert "import RECALL_TOP_K_MAX" not in src
```

> 用文本计数而不是 AST：这几处的形式极其固定（属性访问），文本断言更窄更稳；AST 会把注释里的同名字符串也放过或误伤。**注释里也不要写这些 token** —— 若必须解释，写成「平台召回条数常量」而不是原文，否则断言会把注释算进去。这条要写进文件头注释，否则下一个人加注释就红。

**D.3.2 `recall_top_k` 真的进了三条召回 SQL**

```python
async def test_recall_top_k_reaches_all_three_recall_paths():
    """三条召回 SQL 的 limit 绑定值都必须等于 eff.recall_top_k。"""
    # FakeSession 记录 (sql, params)；断言所有含 "limit" 的 params["limit"] == 7
```

**D.3.3 `recall_top_k` 与 `rerank(top_n)` 解耦（钉住 §A.3 结论 2）**

```python
async def test_recall_top_k_never_passed_as_rerank_top_n(monkeypatch):
    """把 recall_top_k 配成 50，rerank 收到的 top_k 仍必须是 final_top_n=8。"""
    seen = {}
    class _Rerank:
        async def rerank(self, query, documents, top_k):
            seen["top_k"] = top_k
            seen["docs"] = len(documents)
            return []
    ...
    assert seen["top_k"] == decisions.TOP_K_RERANK
    assert seen["docs"] <= 50          # 池大小受 recall_top_k 影响
```

**D.3.4 `retrieve_chain` 多轮透传**

```python
async def test_chain_passes_policy_to_every_round(monkeypatch):
    """多轮子问题每一轮收到的 policy 与入口相同（漏一处 = 场景策略只对单趟生效）。"""
```

**D.3.5 阈值真的接在 L1 闸门上**

```python
async def test_threshold_override_changes_refusal():
    """top1 向量分 0.55：policy 阈值 0.6 → refused；policy 阈值 0.5 → 不拒答。

    ★ 用与「平台默认 0.50」不同的两个值，避免断言退化成「== 平台值」。
    """
```

### D.4 自检测试（无 PG 的替代证据）

| 测试 | 层 | 手段 | 证明什么 / 没证明什么 |
|---|---|---|---|
| `test_s2a_raises_when_ceiling_below_one` | 常量 | `monkeypatch.setattr(policy_spec, "RECALL_TOP_K_MAX", 0)` → 断言 `check_scenario_constants()` 抛 `RuntimeError` | 证明判据存在且会触发；不证明真实启动流程会调（由 `test_lifespan_calls_scenario_checks` 用 AST 断言 `main.py` 里存在调用） |
| `test_s2a_raises_when_ceiling_above_db_check` | 常量 | 设成 2000 → 抛 | 同上 |
| `test_s4a_raises_when_threshold_min_not_positive` | 常量 | monkeypatch `POLICY_FIELDS` 里阈值字段的 `min_value=0.0` → 抛 | 钉住「规格表与校验器不许矛盾」 |
| `test_s6_placeholders_registered` | 常量 | 调用 `check_scenario_constants()` 不抛；再从 `placeholders.for_owner("scenario")` 读到两个条目 | 证明登记表非空且被服务层认领 |
| `test_s2b_warns_when_recall_below_final_top_n` | 数据 | `FakeSession([build_row(policy={"recall_top_k": 3})])` → `check_scenario_data` 返回 `{"recall_below_final_top_n": 1}`，**且不抛** | 证明「数据类不拒绝启动」这条判据落地 |
| `test_s3_warns_when_platform_rerank_off` | 数据 | `FakeSession([build_row(policy={"rerank_enabled": True})])` + `settings.rerank_enabled=False` | 计数 = 1，且 `effective_retrieval` 的结果仍是 `False` |
| `test_s4b_counts_out_of_range_threshold` | 数据 | `build_row(policy={"relevance_threshold_vector": 0.0})` | 计数 = 1（说明「此值只可能来自绕过写入」） |
| `test_s7_counts_db_binding` | 数据 | `build_row(db_binding="kb_hr")` + `max_step=3` | 计数 = 1 |
| `test_s8_detects_builtin_policy_not_empty` | 数据 | `build_row(is_builtin=True, policy={"recall_top_k": 9})` | 计数 = 1 |
| `test_s9_detects_dna_drift` | 数据 | `build_row(dna_declared={... "embedding_text_version": "etext-evil"})` | 计数 = 1，且输出含 `declared → actual` |
| `test_data_check_survives_unparsable_row` | 数据 | `build_row(policy={"recall_top_k": "abc"})` | 不抛，计入 `selfcheck_unparsable`（**这是 §C.5 那条兜底的证据**） |
| `test_publishable_rejects_recall_below_final_top_n` | T4 | 直接调 `check_policy_publishable({"recall_top_k": 3}, platform_rerank_enabled=False)` | 抛 `PolicyValidationError(field="recall_top_k")` |
| `test_publishable_rejects_rerank_when_platform_off` | T4 | 同上 + `{"rerank_enabled": True}` | 抛（**这是把「静默无效」提前到写入期**） |

> 数据层全部用 `tests/scenario/conftest.py` 现成的 `FakeSession` + `build_row`【源：`conftest.py:80-150`】—— 它们只实现 `execute → all → _mapping`，而 `_load_all_published` 恰好只需要这一面。**不需要 PG，也不需要改 conftest**。

### D.5 既有测试桩适配清单（**唯一允许触碰既有测试文件的地方**）

**问题**：`retrieve` / `retrieve_chain` 新增 `policy` 关键字参数后，5 个既有测试里的**假实现签名**不匹配，会 `TypeError`。

| 文件:行 | 现状 | 最小适配 |
|---|---|---|
| `tests/scenario/test_scenario_wiring.py:227` | `async def retrieve(self, session, query, tenant_id, kb_ids, clearance, subjects)` | 追加 `, *, policy=None`；并新增 `self.policies: list`，在方法里 append —— 供 §D.2 的 Z3-3 断言 |
| `tests/chat/test_sse_retrieval_observation.py:143` | `retrieve_chain(..., rewrite_score=None)` | 追加 `, *, policy=None` |
| `tests/chat/test_ask_with_image.py:137` | 同上 | 同上 |
| `tests/eval/test_eval_context_caliber.py:101` | 同上 | 同上 |
| `tests/test_integrations.py:376` | `retrieve_chain(..., rewrite_score=None)` | 同上 |

**这不算「放宽既有测试」的三条理由（评审可直接核对）**：
1. **不改任何 `assert`**。5 个文件里新增的只有形参和（第一处）一个 `self.policies` 列表。
2. 相反对既有语义是**加严**：新增的 Z3-3 断言要求「未绑定应用传进去的 policy 必须是全 None」，这是一条改动前不存在的约束。
3. 桩的签名**必须**跟生产签名一致；不跟就是「桩在替我撒谎」。若不改，Step 3 会红 5 个文件 —— 而红的原因是桩落后于生产，不是行为回归。

> **禁止的替代做法**：给假实现加 `**kwargs`。那会让「生产传了 `polcy=`（拼错）」被静默吞掉，直到线上才发现策略没生效。**显式形参 + 显式断言**。

### D.6 不许动的基线

- `tests/scenario` 现有 96 条：**断言语义一条不改**（只做 §D.5 的桩签名适配）。
- `tests/config` 28 条：不动。
- 全仓 19 个预存在失败：不修、不掩盖、不计入本步验收（已由 `git archive HEAD` A/B 证实预存在）。

### D.7 「无 PG / 无模型能否验收 Step 3」——明确判断

**结论：能验收，且验收强度足够；但有三条能力边界必须写明。**

| 验收对象 | 无 PG/无模型下的证据形式 | 强度 | 没证明什么 |
|---|---|---|---|
| 四键接线正确 | 假 Session 断言 `params["limit"]`、`rerank(top_k)`、`threshold` 取值（§D.3） | 强 —— 直接断言**出参**，不是「代码看起来对」 | 真 SQL 能否执行（与策略无关，Step 1–2 已在预发验过 schema） |
| 单一取值入口 | 文本守卫 Z3-4（0/0/0/1 次） | 强 —— 结构性事实 | 未来新增读点是否被拦住（守卫会拦，这才是它的价值） |
| 零影响 | Z3-1 反自证 + Z3-2 `RetrievalResult` 全等 | 强 | 真 embedding/rerank 的**分数分布**是否一致（模型不变则一致，属部署事实） |
| 自检 S-2a/S-4a/S-6 | 纯函数直接调 + monkeypatch 制造违反 | 强 | 真实启动顺序（由 AST 断言 `main.py` 有调用覆盖） |
| 自检 S-2b/S-3/S-4b/S-5/S-7/S-8/S-9 | `FakeSession` + `build_row` 驱动扫描函数 | 中 —— 证明**判据与计数**正确 | 真实 SQL 在真库上能跑通（**需 DBA 在预发跑一次 `check_scenario_data`**，这是 §E 的 G3-10） |
| `db_binding`（Step 6） | 不验 | — | 不在本步范围 |

**缺什么**：一条「真库上跑 `check_scenario_data` 并确认返回 0 命中」的预发冒烟。它不需要 GPU、不需要模型，只需要一个跑过 `0028` 的库 —— 若有测试 PG（`SCENARIO_TEST_DB_URL`），它就是一条 real-DB 层用例（与 `tests/scenario/test_0028_pg_constraints.py` 同款 skip 约定）；若没有，必须由人跑一次并把输出贴进验收记录。

---

## E. 遗留与未知区

| # | 项 | 影响 | 归属 |
|---|---|---|---|
| **G3-1** | `meta.policy_applied` / `score_source` / `use_rerank` 仍未进响应，场景生效只能从服务端 metric 看 | 集成方无法自查「我的场景是否生效」 | Step 4 |
| **G3-2** | `scripts/run_eval.py` 无 `--scenario` 参数，无法评估「某场景策略对答准率的影响」 | Step 3 之后能改参数但**无法量化效果**；这正是 `code/docs/按需路由设计.md` 记录的教训（三轮 217 例 A/B 后发现「不劣化」不成立） | Step 4 |
| **G3-3** | 三个步骤号并存（`CURRENT_STEP=2` / `POLICY_CLOSURE_STEP=3` / `WIRED_RETRIEVAL_STEP=3`） | 认知成本；但合并任意两个都会让另一个失真 | 长期（需一次语义整顿） |
| **G3-4** | `ScenarioPolicy.effective_threshold`（`snapshot.py:75-92`）与 `decisions.active_threshold`（`decisions.py:44-52`）是同一口径的**两份实现** | 有人改一处漏一处 → 拒答口径分裂。当前用 `test_effective_threshold_matches_active_threshold` 钉住 | Step 4（纯重构：None 分支委托 `active_threshold`） |
| **G3-5** | 读侧（`to_policy`）只校验类型，不校验范围 | 越界值（只可能来自绕过写入）会原样生效，仅 WARN（S-4b） | Step 4（扩 `type_ok` → `value_ok`，读写同源） |
| **G3-6** | `RECALL_TOP_K_MAX=200` 仍无压测依据 | 上限本身可能是错的；S-2a 只保证它与 DB 上限不矛盾 | 需压测（与 `M_scene_pg` 一起） |
| **G3-7** | `CACHE_TTL_SECONDS=60` 站位值 | 「发布新版本 → 生效」时延未经用户确认 | Step 4 开工前确认 |
| **G3-8** | 场景行缓存多副本失效仍靠 TTL（当前单 worker） | 多副本后最长 60s 生效 | Step 4 |
| **G3-9** | `rag.rerank.duration` 的 `mode="off"` 标签分不清「平台关」与「场景关」 | 已用独立 counter 缓解，但看板上仍需人工并读两个指标 | 可延后 |
| **G3-10** | `check_scenario_data` 的真 SQL 未在真库跑过 | 判据对但 SQL 可能有方言/别名错（例如 `<>` 与 JSONB 比较） | 预发冒烟（§D.7） |
| **G3-11** | **S-2b 不做 clamp 的代价**：配了 `recall_top_k=3` 的场景会真的只进 3 条，答准率下降但不报错 | 已被 WARN + metric 覆盖；若某团队长期不看告警，等价于无保护 | 依赖运维纪律，Step 4 的发布拒绝是唯一硬拦 |
| **G3-12** | 「场景策略是否真的提升答准率」整体未验证 | Step 3 只保证**接线正确**，不保证**调参有益** | 需 `--scenario` + eval（G3-2） |

**未知区（诚实声明）**：
- **U3-1**：`recall_top_k` 与答准率的关系曲线未知。缩池到 10 条时，三路召回的互补性是否仍然成立（`path_stats` 里 `*_unique` 会回答，但需要真库 + 真 embedding）。
- **U3-2**：`rerank_enabled` 的单向 AND 在多场景下会不会造成「平台一开，全场景成本突增」。平台开关是全局的，场景只能关 —— 所以平台的每次开启都会让所有没显式关掉的场景一起承担成本。这是**有意的不对称**（成本由平台承担），但它的量级从未被测量。
- **U3-3**：S-2b 的 WARN 在多租户下会不会刷屏（若所有租户的内置行都被误改）。已用「每租户一条」的计数聚合而非逐行打印来控制，但没有真实数据。

---

## 附录 A：与框架稿 / Step 1–2 文档的偏移登记（**逐条，不静默**）

| # | 原表述 | Step 3 的做法 | 理由 |
|---|---|---|---|
| **A-1** | Step 1–2 文档 §E.3 S-2：「`recall_top_k >= TOP_K_RERANK` → 启动 RuntimeError」 | 拆成 **S-2a（常量自洽 → RuntimeError）+ S-2b（数据协调 → WARN + 发布拒绝）** | 原稿把「代码常量」与「库里数据」两类判据塞进同一条、同一动作。数据类拒绝启动 = 一个场景的错配置拖垮实例（§C.3） |
| **A-2** | §E.3 S-3/S-5/S-7/S-8/S-9 未说明动作，暗示也走 RuntimeError | 全部改为 **WARN + metric** | 同上：这五条全部是数据判据 |
| **A-3** | §E.3 S-4：「两个阈值 `0.0 < v <= 1.0` → RuntimeError」 | 拆成 **S-4a（规格表自洽 → RuntimeError）+ S-4b（数据 → WARN）** | 规格表与 `validate_policy` 的 `> 0` 曾经互相矛盾（`policy_spec.py:275-278` 与 `:356-358` 两处），必须用常量自检钉住；数据越界只可能来自绕过写入 |
| **A-4** | §E.3 S-6：「`RECALL_TOP_K_MAX` 仍标注为站位值 → 横幅显示」 | 新增 **`app/config/placeholders.py` 登记表**，服务层 import 期断言已登记 | 原稿没解决「config 层怎么知道 services 层的常量」而**不造反向依赖**（`decisions.py:123`）。登记表是唯一方向正确的解法 |
| **A-5** | §A.3/§E.1 说 Step 3 只接 3 个参数 | 实际接 **4 个键**（阈值拆两键） | 与 Step 1–2 已落地的 `POLICY_FIELDS` 一致（`policy_spec.py:97-120` 就是两个键）。这是 Step 1–2 的既成事实，Step 3 沿用 |
| **A-6** | §C.3 真值表在我原稿里是 R1–R7（7 行），落地是 **R1–R6 + 表外分支** | 不改，接受代码 | 代码用「表外分支」表达内置行缺失（`snapshot.py:325-344, 444-449`），语义等价且更清晰。**Step 3 不碰真值表** |
| **A-7** | §C.4 说 `ACTIVE_KEYS` 类的三方相等 | 保留，但**新增** `WIRED_RETRIEVAL_STEP` | `CURRENT_STEP` 被既有测试硬断言为 2（`test_resolver_policy.py:49`），不能改（§C.4） |
| **A-8** | 原稿 §B.2 说 `integrations` 的改动是「两个 handler 各 3 行」 | 实际是**两个 handler 各 1 行 + `chat.py` 签名 1 处 + 5 个测试桩** | 因为 `/integrations/ask` 走的是 `chat._chat_ask_impl` → `retrieve_chain`，不是直接调 `retrieve`。原稿漏了这条路径 |
| **A-9** | 框架稿 §7 Step 3 描述为「接线 + 自检」 | 本步**额外**交付 `check_policy_publishable()`（T4 函数） | 「拒绝」是唯一能真正阻止坏配置进入系统的动作，但它属 Step 4 的写接口。函数现在写好，Step 4 只调用 —— 否则 Step 4 的人会自己发明一套判据 |
| **A-10** | 原稿未提「`recall_top_k` 不能传给 `rerank(top_n)`」 | §A.3 结论 2 单独定死 + 专门测试 | 这是最容易被误接线的一处（两个参数都是"条数"），后果是上下文膨胀 |

---

## 附录 B：结论来源索引（【源】= 源码核实 / 【推导】= 设计推导）

### B.1 源码核实（可当场核对）

| 结论 | file:line |
|---|---|
| 8 个策略读点的位置与形态 | `app/services/retrieve/__init__.py:258, 368, 395, 434, 447, 459, 532, 537` |
| `current_index_profile()` 在检索内被读一次（DNA 入口） | 同上 `:252` |
| `RRF_K` 参与融合（L0 锁定，不接线） | 同上 `:210`；`policy_spec.py:132-143` |
| 降级后 `use_rerank=False` 并打点 | 同上 `:468-486` |
| 重排在线分支**不做二次截断** | 同上 `:527-528` |
| 重排关闭的理由（CPU 66s/50 docs） | 同上 `:449-453` |
| `mode` 三态 label | 同上 `:489-493` |
| `RetrievalResult` 是 dataclass（可 `==`） | `retrieve/base.py:46-70` |
| `ScenarioPolicy` 只有 4 字段 + 三个 `effective_*` 在调用期读 `decisions` | `snapshot.py:51-105` |
| `effective_threshold` 内联了 `active_threshold` 的选择规则 | `snapshot.py:75-92` vs `decisions.py:44-52` |
| 两个阈值必须分开的理由 | `decisions.py:24-41` |
| `RELEVANCE_THRESHOLD_RERANK` 未标定 | `decisions.py:37-41` |
| `TOP_K_RECALL=50` / `TOP_K_RERANK=8` / `RRF_K=60` | `decisions.py:176-178` |
| 「判定 MUST 通过 `decisions.X` 读，不许 from-import」 | `decisions.py:6-8` |
| 配置层不反向依赖服务层 | `decisions.py:123` |
| `RECALL_TOP_K_MAX=200` 是站位值 / `CURRENT_STEP=2` / `POLICY_CLOSURE_STEP=3` / `_NEVER` | `policy_spec.py:24-52` |
| 阈值除范围外还要求 `>0` 的第二道校验 | `policy_spec.py:356-358` |
| `CURRENT_STEP == 2` 被测试硬断言 | `tests/scenario/test_resolver_policy.py:49-51` |
| `check_capabilities()` 在图/wiki 全关时**提前 return** | `app/config/self_check_ext.py:322-325` |
| lifespan 已调 `decisions.self_check()` 与 `check_capabilities()` | `app/main.py:119, 148` |
| worker `on_startup` 调同一套 | `app/workers/parse_job.py:631-636` |
| 横幅的渲染与统计入口 | `app/config/capabilities.py:179, 279, 391` |
| `_chat_ask_impl` 已有 `integration` 可选参数（先例） | `app/api/chat.py:268-273` |
| `retrieve_chain` 两处调 `self.retrieve` | `app/services/retrieve/__init__.py:875, 909` |
| handler 已有 `snapshot` 局部变量 | `app/api/integrations.py:320, 346` |
| 5 个测试桩的签名现状 | `tests/scenario/test_scenario_wiring.py:227`；`tests/chat/test_sse_retrieval_observation.py:143`；`tests/chat/test_ask_with_image.py:137`；`tests/eval/test_eval_context_caliber.py:101`；`tests/test_integrations.py:376` |
| 4 个 retrieve 测试的 `get_settings` 替身都含 `rerank_enabled` | `tests/retrieve/test_wiki_nav_off_state.py:130-132`、`test_graph_side_channel.py:135`、`test_section_recall.py:135`、`test_table_recall.py:126` 附近 |
| 可复用的无 DB 检索夹具（FakeSession/FakeEmbedding/FakeRerank/Harness） | `tests/retrieve/test_governance_weight.py:60-125` |
| 可复用的 FakeSession + `build_row`（场景层） | `tests/scenario/conftest.py:80-150` |
| DB CHECK 的 `recall_top_k` 上限是 1000 | `alembic/versions/0028_add_scenarios.py`（`ck_scenario_versions_policy_recall_top_k`） |

### B.2 设计推导（源码无对应行，前提变则结论可变）

| 结论 | 依赖的前提 |
|---|---|
| 检索链路只收 `ScenarioPolicy` 不收 `ScenarioSnapshot` | 「`effective_kb_ids` 是唯一授权计算点」这条不变量保持 |
| `recall_top_k` 是「池」、`final_top_n` 是「取用数」，与 rerank 开关无关 | 现有代码结构不变（`:530-532` 的截断与 `:527-528` 的不截断） |
| 数据类自检一律 WARN、硬约束放 T4 | 承认「一个场景的坏配置不应拖垮实例」优先于「坏配置必须立即阻止」 |
| `clamp` 只对 `recall_top_k` 不对阈值 | 阈值的错值裁不出对的判据 |
| T2 只在 api 侧跑 | worker 不读策略 |
| `final_top_n` 提前进 `EffectiveRetrieval` | 接受「为下一步留唯一改动点」这点额外结构 |
| S-2b 的阈值取 `min(effective, MAX) >= TOP_K_RERANK` | `final_top_n` 在 Step 3 恒等于 `TOP_K_RERANK` |
| `ACTIVE_KEYS` / `WIRED_RETRIEVAL_STEP` 三方相等判据 | 「能写 / 能承载 / 真读」应当是同集合 |
| 三个步骤号不合并 | 三者的语义来源不同（见 §C.4） |
| 5 个测试桩的「最小适配」不算放宽 | 「改断言」与「改桩签名」是两件事，评审可逐行核对 |
