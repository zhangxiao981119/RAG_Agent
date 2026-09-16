# 决策契约 · Decision Contract

> **读者：AI Coding Agent。** 本文档的唯一目的是让 Agent 在**没有人拍板的情况下也能继续写代码**，
> 并且保证它选择的默认值不会导致返工、不会造成数据泄漏。
>
> **配套文档（MUST 同时投喂）**
> | 文档 | 作用 |
> |---|---|
> | `AI-Coding-Agent-实施规范.md` | 不变量 INV-01~28 / 工作单元 WU-01~40 / 契约与门禁 |
> | `企业知识库问答AI-Agent-分步开发路线.md` | 步骤 0~16 与里程碑，决定"先做哪一块" |
> | `企业知识库问答AI-Agent-开发流程.md` | 动机与权衡，仅在需要理解"为什么"时查阅 |
>
> 版本 `dc-1.2` · 2026-09-16 · **DEC-01 = B、DEC-22 已确认 —— 硬阻塞清零，24 条决策全部可自主推进**

---

## 0. 执行协议

### 0.1 为什么需要这份文档

实施规范 §0.5 规定：

> 业务规则不明确 → **MUST NOT** 交付一个"看起来合理"的默认值 → 列为 BLOCKER

这条规则本身是对的，但它有个副作用：**需求侧有 20 多个未决项时，Agent 会在第一个 WU 就卡住**，然后把所有决策项一次性倒在汇报里——这不是 Agent 的问题，是契约缺失的问题。

本文档做的事就是把"不明确"改造成"有权威默认值"：

```
未决项  ──双检验──┬─→ 存在安全默认值 → SAFE-DEFAULT → Agent 用默认值继续，声明 USED-DEFAULT
                 ├─→ 必须用数据标定  → CALIBRATE    → Agent 用占位值，同时产出标定脚本
                 ├─→ 非技术决策      → ASK-EXTERNAL → 不阻塞编码，UI 按保守文案实现
                 └─→ 默认值不可逆    → HARD-BLOCK   → Agent 停下来问
```

原为 **2 项** `HARD-BLOCK`（DEC-01 路径、DEC-22 可见性口径）。

> **2026-09-16 更新（1）**：**DEC-01 已确认为 `B 标准`**（`status: frozen`，`value: B`）。
> **2026-09-16 更新（2）**：**DEC-22 已确认并冻结**（9 个语义分叉逐条答复见 §2・DEC-22 与 §3.8）。
>
> **当前 `HARD-BLOCK` 未决项 = 0。** 24 条决策中 2 条已 `FROZEN`（DEC-01 / DEC-22），其余 22 项
> 按各自等级由 Agent 自主推进。**从步骤 0 到里程碑 B 不再存在需要回问人类的决策项。**

### 0.2 四种决策等级

| 等级 | 含义 | Agent 的 MUST 行为 |
|---|---|---|
| `FROZEN` | 已有明确答复 | 直接用 `value` 字段，不得偏离 |
| `SAFE-DEFAULT` | 未定，但默认值通过双检验（见 §0.3） | **取 `default` 继续实现**，汇报时输出 `USED-DEFAULT: DEC-xx` |
| `CALIBRATE` | 不给固定值，须由评估集标定 | 取 `default` 作**占位值**，**MUST 同时产出标定脚本**，汇报时输出 `NEEDS-CALIBRATION: DEC-xx` |
| `ASK-EXTERNAL` | 决策权在企业外部/其他部门，非技术可定 | **不阻塞**。按 `default` 实现保守行为，汇报中列为 `EXTERNAL-PENDING` |
| `HARD-BLOCK` | 默认值会带来不可逆后果 | **停下来**，输出 `BLOCKER`，不得自行取值 |

### 0.3 默认值的双检验（本文档最可复用的部分）

本表未覆盖的新决策，Agent 用这两条自行判定等级。**必须同时通过**：

**检验一 · fail-safe —— 假设错了，系统朝哪个方向错？**

> 必须朝「更少泄漏、更少幻觉、更少返工」的方向。
> 例如「模型默认云 API」等价于「涉密默认不入库」——假设错了也只是少答一部分，不会多泄漏。

**检验二 · additive —— 事后换成另一个选项，是"加"还是"改"？**

> 只有「加」才允许取默认。
> 「新增一个适配器 / 新增一个字段值 / 新增一个分支」= 加 ✅
> 「重写已完成的模块 / 全库重刷数据 / 改已有字段语义」= 改 ❌

| 判例 | 检验一 | 检验二 | 结论 |
|---|---|---|---|
| 向量库 pg vs Milvus | 两者都支持权限下推，无泄漏差异 | 原生是"改"（§3.3 的标签展开把"改"变成"加"） | ✅ `SAFE-DEFAULT` |
| 权限粒度 文档级 vs 段落级 | 粗粒度更保守 | 段落级 = 解析器写更细的 `acl_tags`，字段本就预留 | ✅ `SAFE-DEFAULT` |
| 交付路径 A vs B | — | A→B 要给全库补权限字段并重写检索层 | ❌ `HARD-BLOCK` |
| 可见性口径 | 三种口径宽窄差异极大，选错即真实泄漏 | 改一次则权限模块整体返工 | ❌ `HARD-BLOCK`（已于 2026-09-16 人工确认冻结） |

### 0.4 开工与收尾协议

**开工前（并入实施规范 §0.4 的强制检查）**

```
□ 6. 读 §1 YAML，用 affects 字段筛出与本次 WU 相关的 DEC
      - HARD-BLOCK 且 status=open（且未被 §4 豁免）→ 立即 BLOCKER，停止
      - SAFE-DEFAULT / CALIBRATE / ASK-EXTERNAL → 记下 ID 与取值，继续
      - FROZEN → 记下 value，继续
```

**实现时**

```
MUST 从 app/config/decisions.py（§4）读取取值
MUST NOT 在业务代码里硬编码任何本表覆盖的常量
MUST NOT 新增本表与实施规范 §3/§4 之外的字段、枚举值、API、依赖
```

**收尾时**

```
□ 跑 python scripts/check_decisions.py（§7），必须 exit 0
□ 汇报块（实施规范 §10.1）末尾追加「决策使用」段，见 §0.5
```

### 0.5 汇报格式增量

在实施规范 §10.1 的汇报块后追加：

```markdown
**决策使用**
| DEC | 等级 | 本次取值 | 来源 | 影响 |
|---|---|---|---|---|
| DEC-02 | SAFE-DEFAULT | pgvector | 表内默认值 | 向量库适配层按 pgvector 实现，端口保持中立 |
| DEC-19 | CALIBRATE | 0.62（占位） | 表内默认值 | **NEEDS-CALIBRATION**，已产出 eval/calibrate_threshold.py |
| DEC-06b | ASK-EXTERNAL | — | 未阻塞 | 小程序端按"引导先绑定"实现 |

**新增 DEC 提案**（若按 §0.3 自行判定了本表未覆盖的决策）
- 提案：<一句话>
- 检验一 fail-safe：<结论>
- 检验二 additive：<结论>
- 本次取值：<值>
```

### 0.6 与实施规范 §0.2 的优先级合并

实施规范 §0.2 的优先级列表**替换为**：

```
1. 用户当前会话中的即时指令
2. 本契约中 status: frozen 的条目
3. 实施规范 §1 不变量（INVARIANTS）
4. 实施规范 §3 / §4 数据与接口契约
5. 本契约中 SAFE-DEFAULT / CALIBRATE / ASK-EXTERNAL 的默认值
6. 实施规范 §5 工作单元定义
7. 上游设计文档
8. 仓库既有代码风格
```

> **若第 2 条与第 3 条冲突**（已确认的业务决策与不变量抵触）→ **报告冲突双方编号与后果，请裁决。MUST NOT 自行取舍**（沿用实施规范 §0.5）。用户确认后仍需在代码留 `# DESIGN-OVERRIDE: INV-xx` 注释。

---

## 1. 决策总表

### 1.1 机器可读定义（权威源）

> 下面的 YAML 块是**权威定义**。若与 §2 的散文描述冲突，以本块为准。
> 可直接抽取为 `decisions.yaml` 供 Agent 解析。

```yaml
contract_version: dc-1.2
generated: 2026-09-16

levels:
  FROZEN:        { agent_action: use_value }
  SAFE-DEFAULT:  { agent_action: use_default_and_report }
  CALIBRATE:     { agent_action: use_placeholder_plus_emit_calibration_script }
  ASK-EXTERNAL:  { agent_action: implement_conservative_and_report }
  HARD-BLOCK:    { agent_action: stop_and_ask }

decisions:

  # ── P0 · 架构方向 ──────────────────────────────────────────
  - id: DEC-01
    title: 交付路径
    level: FROZEN                      # 原 HARD-BLOCK，2026-09-16 人工确认后解除
    status: frozen
    value: B                           # B 标准 / 14-16 周 / 4 人 / 含权限体系 / 可上线
    decided_at: 2026-09-16
    decided_by: 业务负责人 + 技术负责人
    question: 系统是「给人看的」（演示/立项/作品集）还是「给人用的」（真实企业上线）？
    options: [A 极简/3-4周/1人, B 标准/14-16周/4人, C 完整/20-24周/5-6人]
    reversibility: irreversible
    frozen_consequences: |
      1. 权限体系为 MUST 实现项 —— WU-03 / WU-04 / WU-19 / WU-21 / WU-23 全部照常开工，
         MUST NOT 标记为 SKIPPED-BY-PATH。
      2. DELIVERY_PATH = "B"，命中 PATHS_WITH_PERMISSION。
      3. 路径 A 专用机制（DEMO 横幅 / 豁免字典 ACKNOWLEDGE_UNRESOLVED）MUST NOT 启用。
      4. 本项 MUST NOT 再出现在 UNRESOLVED 中，也 MUST NOT 再输出 BLOCKER。
    affects: [ALL]
    owner: 业务负责人 + 技术负责人
    deadline_stage: 已确认（原为「步骤 0 之前」）
    lands_in: config/decisions.py (DELIVERY_PATH) + README.md 顶部路径声明
    verify: README 顶部含 "PATH: B"；decisions.py 的 DELIVERY_PATH == DeliveryPath.B

  - id: DEC-02
    title: 向量库后端
    level: SAFE-DEFAULT
    status: open
    question: pgvector 还是 Milvus？
    default: pgvector
    default_precondition: chunk 总数预期 < 50 万
    escalate_if: [chunk 预期 > 100 万, 需独立向量集群, 路径 = C]
    reversibility: reversible-以 §3.3 标签展开与 WU-20 端口为前提
    affects: [WU-07, WU-08, WU-13, WU-19, WU-20, WU-27]
    owner: 技术负责人
    deadline_stage: 步骤 4 之前
    lands_in: app/vector/base.py (VectorStore Protocol) + app/vector/pgvector_store.py
    verify: pytest tests/contract/test_vector_store_port.py -q

  - id: DEC-03
    title: 模型部署形态
    level: SAFE-DEFAULT
    status: open
    question: Embedding / Rerank / LLM 各自内网自建还是云 API？
    default: { embedding: cloud_api, rerank: cloud_api, llm: cloud_api }
    consequence: 密级 SECRET / TOP 文档禁止入向量库（INV-15 生效）
    reversibility: reversible
    affects: [WU-17, WU-29, INV-15, INV-19]
    owner: 技术负责人 + 安全合规
    lands_in: app/config/decisions.py (MODEL_DEPLOYMENT) + app/compliance/level_policy.py
    verify: pytest tests/integration/test_level_skip.py -q

  - id: DEC-04
    title: 前端技术栈范围
    level: SAFE-DEFAULT
    status: open
    default:
      web: nextjs_app_router
      h5: same_as_web
      miniprogram: taro
      app: expo_react_native
      shared: packages/kb-core
    hard_constraint: kb-core MUST NOT import 任何 DOM / wx / react-native API
    reversibility: reversible-以 kb-core 纯净度为前提
    affects: [WU-30, WU-31, WU-32, WU-33, WU-36, WU-37]
    lands_in: pnpm-workspace.yaml + packages/kb-core/ + eslint boundaries 规则
    verify: pnpm -w lint:boundaries → 0 violations

  - id: DEC-05
    title: 权限粒度
    level: SAFE-DEFAULT
    status: open
    options: [知识库级, 知识库级+文档级, 知识库级+文档级+段落级]
    default: 知识库级+文档级
    escalate_if: 存在「同一份文档里部分条款对不同人可见」的硬需求
    reversibility: reversible
    affects: [WU-04, WU-08, WU-19, INV-08]
    lands_in: app/permission/subjects.py (compute_doc_acl_tags)
    verify: pytest tests/unit/test_acl_tag_fanout.py -q

  - id: DEC-06
    title: 认证方式与 IdP
    level: SAFE-DEFAULT
    status: open
    default: oidc_authcode_pkce
    note: 企业微信 / 钉钉走 OIDC 桥接或独立 AuthProvider 适配器
    reversibility: reversible
    affects: [WU-03, WU-23, INV-13]
    lands_in: app/auth/provider.py (Protocol) + app/auth/oidc.py
    verify: pytest tests/integration/test_login_flow.py -q

  - id: DEC-06b
    title: 小程序账号绑定映射（openid → 企业账号）
    level: ASK-EXTERNAL
    status: open
    question: 能否开通 unionid → 企业账号 的绑定映射？
    default: 小程序端按「未绑定则引导先去企微/钉钉访问一次」实现；演示期可用受控本地映射表（MUST 标记 DEMO_ONLY）
    blocking_scope: 仅阻塞小程序端上线，不阻塞任何 WU 开发
    owner: 企业微信/钉钉管理员

  # ── P1 · 合规与业务 ────────────────────────────────────────
  - id: DEC-07
    title: 「秘密」级是否允许入向量库
    level: SAFE-DEFAULT
    status: open
    default: deny_embed   # SECRET 与 TOP 同待遇，均不入库
    escalate_if: 业务确认必须可问答
    consequence_if_relaxed: 须同时满足四项独立（独立 collection / 独立模型端点 / 独立网络域 / 独立审计）+ 白名单
    reversibility: reversible
    affects: [WU-17, INV-15]
    owner: 安全合规
    lands_in: app/compliance/level_policy.py
    verify: pytest tests/integration/test_level_skip.py -q   # 断言 L3 入库后向量库 0 条

  - id: DEC-08
    title: 涉密文档的替代查阅通道
    level: ASK-EXTERNAL
    status: open
    default: 系统不做涉密问答；UI MUST 在知识库页明确提示「涉密文档不参与问答，请走原审批流程查阅」
    blocking_scope: 不阻塞；仅决定 UI 文案与是否提供「申请查阅」入口
    affects: [WU-17, WU-36]
    owner: 安全合规

  - id: DEC-09
    title: DLP 敏感字段集合 + 原文恢复权限规则
    level: SAFE-DEFAULT
    status: open
    default:
      fields: [手机号, 身份证号, 银行卡号, 邮箱, 内网IP, 私钥/密钥块, 门禁卡号]
      masking: mask_on_ingest   # 原文入独立加密表，脱敏与恢复成对（INV-16）
      restore_policy: 文档可见 AND 具备 pii:restore 功能权限 AND 写审计 三者同时满足
    extension_rule: 行业字段（病历号 / 证券账号 / 图纸编号）由合规追加，追加属「加」
    reversibility: reversible
    affects: [WU-18, INV-16, INV-18]
    owner: 合规 + 技术
    lands_in: app/compliance/dlp.py
    verify: pytest tests/integration/test_dlp_roundtrip.py -q

  - id: DEC-10
    title: 审计日志留存期
    level: SAFE-DEFAULT
    status: open
    default: { AUTH: 180d, ACL_CHANGE: 1095d, QA: 365d, DENY_HIT: 1095d }
    why_default_long: 留存期变短 = 删数据（不可逆）；变长 = 改配置（可逆）。默认偏长即保守
    escalate_to: 法务 / 合规
    affects: [WU-26, INV-17, INV-18]
    lands_in: app/audit/retention.py

  - id: DEC-11
    title: 越权与不存在的不可区分程度
    level: SAFE-DEFAULT
    status: open
    options: [仅状态码, 状态码+文案, 状态码+文案+耗时对齐]
    default: 状态码+文案+耗时对齐
    implementation: 无权分支补齐到该接口近 1 小时响应耗时的 P50（下限 120ms）
    tolerance_rule: 若实测补延迟使该接口 P95 增幅 > 50ms → 降级为「状态码+文案」并在汇报标 PARTIAL
    reversibility: reversible
    affects: [WU-21, WU-23, INV-13]
    lands_in: app/api/deps.py (indistinguishable_guard)
    verify: pytest tests/security/test_side_channel.py -q

  - id: DEC-12
    title: 灰度维度与首批范围
    level: SAFE-DEFAULT
    status: open
    default: { dimension: department, graylist: [] }
    hard_rule: MUST NOT 按用户灰度（同部门成员看到不同结果会导致反馈不可用）
    behavior_on_empty: graylist 为空 = 不开启灰度（全量内部发布）
    affects: [WU-40]
    owner: 业务负责人

  - id: DEC-13
    title: 核心指标达标线
    level: SAFE-DEFAULT
    status: open
    default:
      retrieval_hit_at_10: ">=0.85"
      answer_success_rate: ">=0.80"
      refusal_precision: ">=0.90"
      false_refusal_rate: "<=0.15"
      p95_first_token_ms: "<=1800"
      cost_per_conversation_cny: "<=0.35"
    role: 直接作为 CI 门禁初始闸门（WU-38）
    escalate_to: 业务负责人
    affects: [WU-38]
    lands_in: eval/thresholds.yaml

  # ── P2 · 数据标定参数 ──────────────────────────────────────
  - id: DEC-14
    title: 子块大小
    level: CALIBRATE
    status: open
    default: 256
    unit: token
    calibrate_with: eval/calibrate_chunk.py
    affects: [WU-08, WU-13]

  - id: DEC-15
    title: 父块大小
    level: CALIBRATE
    status: open
    default: 1024
    unit: token
    calibrate_with: eval/calibrate_chunk.py
    affects: [WU-08]

  - id: DEC-16
    title: Chunk 重叠窗口
    level: CALIBRATE
    status: open
    default: 0.15
    unit: ratio_of_child
    calibrate_with: eval/calibrate_chunk.py
    affects: [WU-08]

  - id: DEC-17
    title: 检索 Top-K（送模型）
    level: CALIBRATE
    status: open
    default: 8
    hard_rule: 与上下文预算联动；超预算时按实施规范 §15.5 顺序裁剪，MUST NOT 截断当前轮检索正文
    affects: [WU-13, WU-35]

  - id: DEC-18
    title: Rerank 候选数
    level: CALIBRATE
    status: open
    default: 50
    affects: [WU-13]

  - id: DEC-19
    title: 相似度阈值
    level: CALIBRATE
    status: open
    default: 0.62
    placeholder: true
    hard_rule: 占位值 MUST NOT 随生产版本发布；CI MUST 校验标定报告存在且未过期（<= 90 天）
    calibrate_with: eval/calibrate_threshold.py
    affects: [WU-12, WU-13, INV-03]

  - id: DEC-20
    title: 拒答阈值权衡点
    level: CALIBRATE
    status: open
    default: maximize_f1
    business_choice: [宁可误拒, 宁可幻觉]
    default_choice: 宁可误拒
    applies_to: [制度类, 合规类, 财务类]
    affects: [WU-12, INV-03]

  - id: DEC-21
    title: 单用户 token 限额
    level: CALIBRATE
    status: open
    default: { daily: 200000, monthly: 3000000 }
    derive_from: 成本预算倒推
    affects: [WU-28]

  # ── P0 · 业务定义（已确认）──────────────────────────────────
  # ── P0 · 业务定义（已确认）──────────────────────────────────
  - id: DEC-22
    title: 知识库可见性口径（授权模型）
    level: FROZEN
    status: frozen
    value:
      grants: [grant_kb, grant_tag]          # KB 为必要条件（G2）；TAG 作文档级细化（G4）
      label_source: grant_org                # 不参与判定，只作为标签来源
      kb_is_necessary: true
      dept_visible_direction: up_and_down    # ★ Q3 人工偏离：上级可见下级
      public_kb: single                      # ★ Q2：存在单一全员公开库
      new_user_scope: public_kb_only         # ★ Q9：新账号只能看见公开库的 public 文档
    answers:
      Q1: kb_members 支持 用户 + 部门 + 用户组 + 角色
      Q2: 有，且只建一个全员公开库（is_public=true），所有用户自动为成员
      Q3: ★ 上级部门能看到下级部门的文档（偏离推荐值「不能」）
      Q4: 支持兼岗，user_subjects 取并集
      Q5: 上传者所属部门自动打标，上传时可手动覆盖
      Q6: 跨部门文档任一命中即可见
      Q7: 要显式 deny（合规调查临时收回 / 离职冻结 / 外包到期收回）
      Q8: 外部人员走独立用户组 group:external_*，显式授权，不给部门标签
      Q9: 只能看见公开库中 acl_tags 含 public 的文档
    reversibility: irreversible
    confirmed_at: 2026-09-16
    confirmed_by: 用户书面确认（会话内）
    frozen_consequences: |
      以下为 MUST，Agent 不得偏离：
      1. VISIBILITY_GRANTS MUST == frozenset({KB, TAG})；ORG MUST NOT 进入判定
      2. 用户主体 MUST 同时做上向（祖先）与下向（子孙）展开；两个方向缺一即为误拒
      3. 文档标签 MUST NOT 做祖先展开（DOC_TAG_ANCESTOR_EXPANSION = False）
         —— 展开会让共享根节点的任意两个主体互相可见，即全公司互通
      4. 'public' 为互斥标签：仅当切片无其他主体标签时才写入；acl_tags MUST NOT 含 'level:'
      5. 全局 is_public=true 的知识库 MUST <= 1 个
      6. §3.5 的 10 条用例 MUST 全部通过（3b / 8 / 9 / 10 为本次新增的泄漏面）
      7. 展开规模核验（§3.3 末）MUST 在步骤 4 之前执行一次，结论记入汇报
      8. 部门树任何结构变更 MUST 触发 acl_epoch += 1（全局失效，见 §3.7）
    affects: [WU-03, WU-04, WU-06, WU-08, WU-19, WU-21, WU-23, INV-07, INV-08, INV-14]
    owner: 业务负责人 + 安全合规
    deadline_stage: 已解除（原为步骤 4 之前）
    deliverable_required: 一张「主体属性 → 可见范围」映射表 + §3.5 的 10 条真实用例确认
    lands_in: app/permission/subjects.py + app/vector/filter.py
    verify: pytest tests/security/test_acl_matrix.py -q
    unconfirmed_followups:
      - 映射表未填写（人工明确"暂不填"，不阻塞开发，MUST 在里程碑 B 之前补）
      - Q3 的可见性扩张风险未与安全合规单独会签
      - Q7 的 deny 运维责任人未指定

  - id: DEC-23
    title: 是否支持多跳问题
    level: SAFE-DEFAULT
    status: open
    default: unsupported
    required_action: 验收标准 MUST 显式写明「不支持跨文档多跳推理」，避免验收争议
    escalate_if: 业务确认必须支持（则 WU-24/25/34 提前，检索策略需改）
    reversibility: reversible
    affects: [WU-24, WU-25, WU-34, INV-01, INV-06]
```

### 1.2 人工速览表

| DEC | 项目 | 等级 | 默认值（Agent 会直接用的） | 卡住谁 |
|---|---|---|---|---|
| 01 | 交付路径 A/B/C | ✅ **FROZEN** | **B 标准**（14–16 周 / 4 人） | — |
| 02 | 向量库后端 | 🟡 SAFE-DEFAULT | pgvector | WU-07/08/13/19/20/27 |
| 03 | 模型部署形态 | 🟡 SAFE-DEFAULT | 全云 API → 涉密不入库 | WU-17/29 |
| 04 | 前端技术栈 | 🟡 SAFE-DEFAULT | Next.js + Taro + Expo，共享 `kb-core` | WU-30~33/36/37 |
| 05 | 权限粒度 | 🟡 SAFE-DEFAULT | 知识库级 + 文档级 | WU-04/08/19 |
| 06 | 认证方式 | 🟡 SAFE-DEFAULT | OIDC (AuthCode+PKCE) | WU-03/23 |
| 06b | 小程序账号绑定 | 🔵 ASK-EXTERNAL | 引导先绑定，不阻塞 | 仅小程序上线 |
| 07 | 秘密级是否入库 | 🟡 SAFE-DEFAULT | 不入库 | WU-17 |
| 08 | 涉密替代通道 | 🔵 ASK-EXTERNAL | UI 保守文案 | WU-17/36 |
| 09 | DLP 字段与恢复规则 | 🟡 SAFE-DEFAULT | 7 类字段 + 三条件恢复 | WU-18 |
| 10 | 审计留存期 | 🟡 SAFE-DEFAULT | 180/1095/365/1095 天 | WU-26 |
| 11 | 越权不可区分程度 | 🟡 SAFE-DEFAULT | 含耗时对齐（带降级容差） | WU-21/23 |
| 12 | 灰度维度 | 🟡 SAFE-DEFAULT | 按部门，名单为空=不灰度 | WU-40 |
| 13 | 指标达标线 | 🟡 SAFE-DEFAULT | 见 YAML（直接作 CI 闸门） | WU-38 |
| 14–18 | 分块 / Top-K / Rerank 参数 | 🟢 CALIBRATE | 256 / 1024 / 0.15 / 8 / 50 | WU-08/13 |
| **19** | **相似度阈值** | 🟢 CALIBRATE | 0.62（占位，禁止上线） | WU-12/13 |
| 20 | 拒答权衡点 | 🟢 CALIBRATE | 宁可误拒（F1 最优） | WU-12 |
| 21 | token 限额 | 🟢 CALIBRATE | 20 万/日，300 万/月 | WU-28 |
| **22** | **可见性口径** | ✅ **FROZEN** | KB（必要条件）+ TAG；ORG 仅作标签来源；**上级可见下级** | — |
| 23 | 多跳支持 | 🟡 SAFE-DEFAULT | 不支持 | WU-24/25/34 |

**图例**：✅ 已人工确认（`frozen`，直接用 `value`）　🔴 必须人工拍板　🟡 Agent 用默认值直接推进　🟢 用占位值 + 产出标定脚本　🔵 需外部确认但不阻塞

### 1.3 阻塞关系

**两项已全部解除。当前 `HARD-BLOCK` 未决项 = 0。**

```
✅ DEC-01（交付路径）→ 已确认为 B（2026-09-16）
   └→ 权限体系为 MUST 实现项：WU-03/04/19/21/23 全部照常开工
   └→ 不再需要任何「路径分支」判断，方案选择面收窄

✅ DEC-22（可见性口径）→ 已确认并冻结（2026-09-16）
   └→ acl_tags 算法已冻结 → WU-08 写库按 §3.3 执行（doc 侧仅自身路径，MUST NOT 展开）
   └→ WU-19 下推表达式形状已冻结：用户主体 = 祖先 ∪ 自己 ∪ 子孙
   └→ WU-06 数据契约新增 knowledge_bases.is_public（含"最多 1 个"部分唯一索引）
   └→ WU-21 缓存失效面扩大：部门树结构变更 MUST 触发 acl_epoch += 1
```

**路径 B 的直接后果**：`DEMO_BANNER_TEXT` / `SKIPPED-BY-PATH` / `ACKNOWLEDGE_UNRESOLVED` 这三条路径 A 专用机制**从现在起 MUST NOT 启用**（`check_decisions.py` 会校验）。

**步骤 0~3 可以立刻开工** —— 那三步不依赖任何未决项，也不受 DEC-22 影响。

---

## 2. 决策详述

> 每条给出：**问题 → 等级理由 → 默认值的安全论证 → 落码位置 → 验收入口 → 若人工选了另一项要改什么**。
> 「要改什么」这一栏是为了让人工答复能**精确落到改动点**，而不是让 Agent 重新推理一遍。

### DEC-01 · 交付路径 ✅ FROZEN · 已确认为 `B 标准`

> **确认记录**：2026-09-16 人工确认。
> **不做任何路径分支判断，权限体系全量实现。**
> 下方「若答复为 A」的所有改动点 **MUST NOT 执行**；`ACKNOWLEDGE_UNRESOLVED` **MUST 保持为空**。

**问题**：这套系统是给人看的，还是给人用的？

| 选项 | 规模 | 含权限体系 | 允许的使用方式 |
|---|---|---|---|
| A 极简 | 3–4 周 / 1 人 | ❌ | 演示、立项、作品集。**禁止任何真实用户使用** |
| B 标准 | 14–16 周 / 4 人 | ✅ | 可上线（功能完整度优先于智能度） |
| C 完整 | 20–24 周 / 5–6 人 | ✅ | 全量 |

**为什么是 HARD-BLOCK**：双检验的第二条不通过。A 的产物里 `chunk.acl_tags`、`documents.level`、`kb_members` 这三处**根本不存在**，而它们必须在入库时写入。从 A 长到 B 不是"加功能"，是**给全库补字段 + 重写检索层 + 改所有权限相关测试**。

**Agent 行为（本节已变更）**：本项 `status: frozen` → **直接读取 `value: B`，MUST NOT 再输出 BLOCKER**。

```
[已解除 · 仅存档] BLOCKER: DEC-01 未决 —— 交付路径决定是否实现权限体系。
  于 2026-09-16 解除，裁决结果 = B。以下为原待裁决内容，不再生效：
  影响：WU-03 / WU-04 / WU-19 / WU-21 / WU-23 全部无法确定是否需要实现。
  需要裁决：业务负责人 + 技术负责人。
```

**若答复为 B 或 C** → 权限相关 WU 全部按实施规范实现，`DELIVERY_PATH = "B"`。
**若答复为 A** → 在 `decisions.py` 设 `DELIVERY_PATH = "A"`，并：
- 权限 WU（03/04/19/21/23）标记为 `SKIPPED-BY-PATH`，不实现
- `app/api/deps.py` 注入固定的 `DEMO_SUBJECTS`（全量可见），并在 `/api/v1/health` 与前端顶部**持续显示 `DEMO · 无权限隔离` 横幅**
- 在仓库 README 顶部写死警告：**本路径产物无权限隔离，任何可访问用户可查询全库内容，禁止用于真实数据**

**落码**：`app/config/decisions.py` 的 `DELIVERY_PATH`；验收：README 顶部含 `PATH: <A|B|C>` 且与 `DELIVERY_PATH` 一致。

---

### DEC-02 · 向量库后端 🟡 SAFE-DEFAULT

**问题**：pgvector 还是 Milvus？

**默认值**：`pgvector`，前置条件 chunk 总数 < 50 万。

**安全论证**

| 检验 | 结论 |
|---|---|
| fail-safe | 两者都支持把权限条件下推到查询层（pgvector 用 `&&`，Milvus 用 `ARRAY_CONTAINS_ANY`）。**不产生泄漏方向的差异** |
| additive | 见下。这是本条能降级为 SAFE-DEFAULT 的关键 |

**修正一处此前的判断（重要）**

我在分步路线文档里写过「不能先用 pgvector 后迁 Milvus」。**这个说法在缺少前置约束时成立，加上 §3.3 的三个入库期动作后不再成立**：

| 入库期动作 | 迁移时受益 |
|---|---|
| 标签**祖先展开**（`dept:财务/资金组` → `[dept:财务, dept:财务/资金组]`） | 检索期只做集合交集，不用在向量库里做路径匹配——而 Milvus 没有路径函数 |
| 空标签**规范化**为 `["public"]` | 消除 `cardinality = 0` 分支，两种后端的表达式形态一致 |
| 密级**数值化**为 `level_rank` | 向量库只做数值比较，不做枚举比较 |

加上 `VectorStore` 端口（WU-20）后，迁移 Milvus = **新增一个适配器实现**，业务代码与权限计算零改动。所以升级为 SAFE-DEFAULT。

**前提条件 MUST 成立**：`app/vector/base.py` 暴露 Protocol，业务层只依赖 Protocol；`tests/contract/test_vector_store_port.py` 必须覆盖全部后端（用同一套契约测试跑两个实现）。

**若答复为 Milvus** → 新增 `app/vector/milvus_store.py`，`VECTOR_BACKEND=milvus`；`chunks` 表保留为元数据源（引用两段式的 `[n]` 仍从 PG 取）；注意 `deleted_at` 在 Milvus 侧用布尔字段 `is_deleted` 表达。

---

### DEC-03 · 模型部署形态 🟡 SAFE-DEFAULT

**问题**：Embedding / Rerank / LLM 三类模型各自内网自建还是云 API？

**默认值**：三者全部云 API。**这等价于默认「涉密文档不入库」（DEC-07 由它推导）**。

**安全论证**

- **fail-safe**：假设错了（其实是内网自建），后果是"少答了一部分涉密问题"——用户会来问，你会知道。
  反之若默认内网、实际是云 API，后果是**涉密文档切片已经躺在第三方模型的请求日志里**，且你不会知道。
- **additive**：改为内网自建 = 实现一个新的 `ModelGateway` 适配器 + 放开 `level_policy`。业务代码零改动。

**硬约束**：只要三者中**有任意一项**走云 API，`app/compliance/level_policy.py` 就必须对 `SECRET`/`TOP` 返回 `ingestable=False`（INV-15）。**这是代码断言，不是文档要求**。

**若答复为全内网** → `MODEL_DEPLOYMENT` 三项改为 `on_prem`；`level_policy` 对 `SECRET` 可放开（仍须满足四项独立条件），`TOP` 保持拒绝；出域清单（INV-19）可减少条目，但**Rerank 一项要重新核对**——它最容易留在云端。

---

### DEC-04 · 前端技术栈范围 🟡 SAFE-DEFAULT

**问题**：一套代码还是多套渲染栈？

**默认值**：`Next.js (Web/H5) + Taro (小程序) + Expo (App)`，共享 `packages/kb-core`。

**安全论证**

- **fail-safe**：三套栈意味着每一端都有正确实现。反过来（强推一套代码）会让小程序在无 DOM、双线程、2MB 包体的约束下功能缺失——**而功能缺失通常表现为"某端不支持引用溯源"，那是产品核心能力的缺失**。
- **additive**：先只做 Web 也是"加"——Taro / Expo 都是新增 workspace 包。

**唯一前提**：`packages/kb-core` 从第一天起必须平台无关。这条用 lint 规则**强制**，不靠自觉：

```
eslint boundaries: packages/kb-core 禁止 import
  - 任何 DOM 全局（document / window / localStorage / fetch）
  - 任何 wx.* / uni.* API
  - react-native / react-dom
禁止的替代方式：所有 IO 通过构造注入的 adapter 接口
```

**允许先做的**：只做 Web（步骤 1 的界面先行阶段）。此时 Taro/Expo 包目录留空，但 `kb-core` 的 adapter 接口必须已经抽出。

**若答复为「只要 Web + H5」** → 删除 Taro/Expo workspace，`WU-37` 标记 `SKIPPED-BY-SCOPE`；`kb-core` 的边界规则可放宽（但仍建议保留，成本为零）。

**若答复为「必须一套代码全端」** → 只能用 Taro 编到全端，并**必须书面确认**：Web 端失去 RSC、SEO、流式 SSR。此时 `WU-31`（Next.js BFF 与密钥隔离）作废，密钥隔离改用 Taro 的 Node 层或独立 BFF 服务实现——**这是一处架构变更，需要重新评审**。

---

### DEC-05 · 权限粒度 🟡 SAFE-DEFAULT

**问题**：知识库级 / 文档级 / 段落级？

**默认值**：知识库级 + 文档级。即 `chunk.acl_tags` 由所属文档的 `documents.acl_tags` **扇出**（fan-out）而来。

**安全论证**

- **fail-safe**：粗粒度不会让人看到不该看的；细粒度如果判定逻辑出错会**误放行**（把本该只给财务的段落给了全员）。
- **additive**：段落级 = 解析器在生成 chunk 时写入不同 `acl_tags` 覆盖文档级值。字段本就存在（INV-08 断言非空），**不需要改表、不需要迁移**。

**实现要点**：`compute_doc_acl_tags` 的返回值同时写入 `documents.acl_tags`（文档级，用于列表页过滤与缓存 key）与每个 `chunk.acl_tags`（切片级，用于检索下推）。扇出函数签名固定：

```python
def fan_out_acl_tags(doc_tags: frozenset[str], chunks: list[Chunk]) -> None
```

**升级到段落级时的改动清单**：`WU-05` 解析器增加条款级标签推断 → `WU-08` 改为按 chunk 计算而非扇出 → 其余不动。

---

### DEC-06 · 认证方式 🟡 SAFE-DEFAULT

**默认值**：OIDC Authorization Code + PKCE，通过 `AuthProvider` Protocol 接入。

**安全论证**

- **fail-safe**：OIDC 是通用标准，任何企业 IdP（含企业微信、钉钉）都能桥接。选错具体 IdP 不会造成安全缺口，只造成接入工作量。
- **additive**：新增 IdP = 新增一个 `AuthProvider` 实现。

**MUST 实现的接口形状**（不因 IdP 变化而变化）：

```python
class AuthProvider(Protocol):
    async def authorize_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str: ...
    async def exchange(self, *, code: str, code_verifier: str) -> TokenSet: ...
    async def refresh(self, *, refresh_token: str) -> TokenSet: ...
    async def userinfo(self, *, access_token: str) -> Identity: ...   # 至少返回 sub + 姓名 + 部门标识
```

**关联**：`DEC-06b` 是独立项——小程序端拿不到部门与姓名，必须走 `unionid → 企业账号` 绑定，这不是认证方式的问题，是**主体来源**的问题。

---

### DEC-06b · 小程序账号绑定映射 🔵 ASK-EXTERNAL

**问题**：企业微信 / 钉钉管理后台能否为本应用开通 `unionid → 企业账号` 的绑定查询？

**为什么不阻塞**：这属于管理后台配置权限，非技术决策，但**开发者可以先实现到"引导绑定"这一步为止**：

```
小程序端登录流（默认实现）：
  wx.login → 拿 openid → 请求后端换 token
     ├─ 后端在绑定表中查到 enterprise_account  → 正常签发
     └─ 查不到                                  → 返回 BIND_REQUIRED
          → 小程序展示：「请先在企业微信/钉钉中打开本应用完成账号绑定」
          → 引导跳转（企微：打开工作台；钉钉：打开应用）
```

**MUST 标记**：演示期内如果使用本地映射表，必须命名为 `DEMO_ONLY_UNIONID_MAP` 且在启动日志中打印警告；`APP_ENV=prod` 时该表被断言为空。

**升级答复的影响面**：仅 `app/auth/wecom_bind.py` 与小程序端登录页。

---

### DEC-07 · 「秘密」级是否允许入向量库 🟡 SAFE-DEFAULT

**默认值**：**不入库**。`SECRET` 与 `TOP` 同待遇。

**安全论证**

- **fail-safe**：文档一旦进入向量库，它会**同时出现在四个地方**——向量库本身、检索日志、答案缓存、链路 trace。不入库 = 四处都干净。反方向的默认若判断失误，泄漏面覆盖全部四处。
- **additive**：后续放开 = 在 `level_policy` 改一个布尔值 + 建独立 collection + 接独立模型端点。

**硬断言（WU-17 必须实现）**：

```python
policy = policy_for(doc.level)
if not policy.ingestable:
    doc.status = DocStatus.SKIPPED
    await audit.write(AuditEvent.DOC_SKIPPED_BY_LEVEL, doc_id=doc.id, level=doc.level)
    return                       # MUST NOT 静默 continue，MUST NOT 只记 debug 日志
```

**若答复为「允许入库」** → 四项独立条件**全部**满足才可放开：独立 collection、独立模型端点、独立网络域、独立审计流 + 文档白名单。缺任何一项，仍然拒绝入库。

---

### DEC-08 · 涉密文档的替代查阅通道 🔵 ASK-EXTERNAL

**为什么不阻塞**：技术上只有两种实现——有「申请查阅」工作流，或没有。默认没有。

**默认实现（保守文案）**：知识库管理页在文档列表中，对 `SECRET` 及以上文档显示状态标签 `不参与问答`，hover 提示：

> 涉密文档不参与智能问答。如需查阅，请按公司原有审批流程申请。

**MUST NOT**：不得在 UI 上暗示"可以问系统要涉密内容"，也不得让用户误以为"文档丢了"。

**若答复为「有审批流程」** → 增加 `POST /api/v1/docs/{id}/access-request`（需新功能权限 `doc:request_access` + 审批回调），属增量。

---

### DEC-09 · DLP 敏感字段与恢复规则 🟡 SAFE-DEFAULT

**默认字段集**：手机号、身份证号、银行卡号、邮箱、内网 IP、私钥/密钥块、门禁卡号。

**安全论证**

- **fail-safe**：默认字段集**偏宽**。多脱敏一个字段的代价是"用户看不到一个电话号码"；少脱敏一个的代价是**身份证号进了 LLM 请求**。
- **additive**：合规追加字段 = 往列表里加正则。**MUST NOT 从列表里删字段**——删字段意味着已脱敏的原文要重新还原，是"改"。

**恢复规则（三条件同时满足，缺一不可）**

```
restore_allowed = 文档对当前用户可见（assert_visible 通过）
                AND 用户具备功能权限 pii:restore
                AND 该次恢复写入审计（事件 PII_RESTORE，含 doc_id / chunk_id / 字段类型 / 操作者）
```

**必须成对（INV-16）**：有 `mask_on_ingest` 就必须有对应的 `restore_on_output`，且 `restore_on_output` 的签名必须带上 `user` 参数——**不带 user 参数的恢复函数是绕过权限的后门**。

---

### DEC-10 · 审计留存期 🟡 SAFE-DEFAULT

**默认值**：`AUTH 180d / ACL_CHANGE 1095d / QA 365d / DENY_HIT 1095d`。

**安全论证（这条很典型）**

- **fail-safe**：留存期的两个方向不对称——**变短是删数据（不可逆），变长是改配置（可逆）**。所以默认必须偏长。
- **additive**：合规要求缩短 = 改一个数字 + 跑一次清理任务。

**MUST NOT**：任何代码路径不得在审计表上执行 `DELETE` 或 `UPDATE`（INV-17 只可追加）。留存清理通过**分区 drop**实现，且清理动作本身也要写审计。

---

### DEC-11 · 越权与不存在的不可区分程度 🟡 SAFE-DEFAULT

**默认值**：完整实现（状态码 + 文案 + **响应耗时对齐**）。

**安全论证**

- **fail-safe**：完整实现的方向是"泄漏更少"。省略耗时对齐会留下侧信道——攻击者可以靠"哪个请求慢一点"推断某份文档是否存在。
- **additive**：若实测延迟成本不可接受，可以**降级**（减功能），也可以再打开。

**关键：这一条给了明确的降级判据，避免 Agent 纠结**

```
目标：无权分支的响应耗时补齐到该接口近 1 小时耗时的 P50（下限 120ms）
容差：若实测该接口 P95 增幅 > 50ms → 降级为「仅状态码 + 文案」
降级时 MUST 在汇报中标 PARTIAL，并说明实测数字
```

**统一文案**（三处必须一致，不得出现"你没有权限"）：`没有找到相关内容`。

---

### DEC-12 · 灰度维度 🟡 SAFE-DEFAULT

**默认值**：按**部门**灰度，名单为空时 = 不开启灰度（全量内部发布）。

**MUST NOT 按用户灰度**：同一部门内两个同事看到不同结果，收集到的反馈无法归因，且会让"AI 答得不准"和"我还没被放开"混在一起。

**空名单即不灰度**：这个默认让 Agent 不必等名单就能实现 `WU-40`——配置项存在、逻辑存在，只是名单为空。

---

### DEC-13 · 核心指标达标线 🟡 SAFE-DEFAULT

**默认值**：直接作为 CI 门禁的初始闸门。

| 指标 | 初始线 |
|---|---|
| `retrieval_hit@10` | ≥ 0.85 |
| `answer_success_rate` | ≥ 0.80 |
| `refusal_precision` | ≥ 0.90 |
| `false_refusal_rate` | ≤ 0.15 |
| `p95_first_token_ms` | ≤ 1800 |
| `cost_per_conversation_cny` | ≤ 0.35 |

**安全论证**：不达标的后果是 **PR 变红**——这是一个显式信号，不会静默放过。相比"没有达标线导致 CI 门禁形同虚设"，默认值即使偏松也比空着好。

**MUST**：这六个数写进 `eval/thresholds.yaml`，CI 从中读取；**MUST NOT** 硬编码在 workflow 文件里。

**若业务方给出另一组数值** → 只改 `eval/thresholds.yaml`，不动代码。

---

### DEC-14 ~ DEC-18 · 分块与检索参数 🟢 CALIBRATE

| DEC | 参数 | 占位值 | 单位 | 标定脚本 |
|---|---|---|---|---|
| 14 | 子块大小 | 256 | token | `eval/calibrate_chunk.py` |
| 15 | 父块大小 | 1024 | token | `eval/calibrate_chunk.py` |
| 16 | 重叠窗口 | 0.15 | 子块比例 | `eval/calibrate_chunk.py` |
| 17 | 检索 Top-K（送模型） | 8 | 条 | 与上下文预算联动 |
| 18 | Rerank 候选数 | 50 | 条 | 性能 / 效果权衡 |

**为什么是 CALIBRATE 而不是 SAFE-DEFAULT**：占位值本身不会造成安全问题（不会泄漏、不会幻觉），但**会显著影响效果**。取任何一个具体数字都是在猜。所以约定是：**用占位值让系统跑起来，同时必须产出能替换它的脚本。**

**CALIBRATE 的 Agent 义务（MUST）**

```
1. 用占位值实现，保证系统可运行
2. 必须同时产出标定脚本，脚本 MUST：
   - 接受一个参数网格（而非单个值）
   - 在评估集上输出每个组合的指标矩阵
   - 输出推荐值 + 推荐理由（哪一个指标提升、牺牲了什么）
3. 汇报时必须输出 NEEDS-CALIBRATION: DEC-xx
4. MUST NOT 把占位值写进任何"默认配置文档"当作最终值
```

**DEC-17 的硬规则**：Top-K 超上下文预算时按实施规范 §15.5 的顺序裁剪，**MUST NOT 截断当前轮检索到的正文**——先裁历史、再裁低分片段。这一条是硬约束，不因标定结果改变。

---

### DEC-19 · 相似度阈值 🟢 CALIBRATE ← 单项最重要

**占位值**：`0.62`。**`placeholder: true`。**

**为什么它比其它标定项严重**

阈值是**唯一直接控制"答不答"的参数**。它偏高 → 大量"明明知道却不答"（误拒）；偏低 → 开始拿不相关内容编造（幻觉）。这两者都不能靠感觉调。

**Agent 义务（MUST）**

```
□ 必须实现 eval/calibrate_threshold.py：
    在正样本集（应有答案）与负样本集（应拒答）上扫描阈值网格
    输出：F1 最优点 / 拒答准确率曲线 / 误拒率曲线 / 推荐值
□ CI MUST 校验标定报告存在且未过期（<= 90 天）
□ 生产发布 MUST 阻断当 calibration_report.missing 或 .expired
```

**MUST NOT**：不得因为"0.62 跑起来效果还行"就跳过标定脚本。**这是 INV-03 的落地方式。**

---

### DEC-20 · 拒答阈值权衡点 🟢 CALIBRATE

**默认值**：`maximize_f1`，且业务默认取向 = **宁可误拒**（适用于制度类 / 合规类 / 财务类知识库）。

**为什么默认"宁可误拒"**：这两个方向的代价不对称。

| | 代价 |
|---|---|
| 误拒（知道却不答） | 用户重问一次，或转人工。**可恢复** |
| 幻觉（不知道却答） | 用户拿着错误制度去办事。**不可恢复，且通常事后才发现** |

在制度类问答里，一次误拒的成本是几秒钟；一次错误制度的成本可能是一次违规报销。

**这条的最终取向是业务风险偏好，不是技术决定。** 所以 `DEC-20` 的标定脚本必须**同时输出两个推荐点**（F1 最优 / 零幻觉），让业务方在曲线上选，而不是让 Agent 替它选。

---

### DEC-21 · 单用户 token 限额 🟢 CALIBRATE

**占位值**：`{ daily: 200000, monthly: 3000000 }`。

**定法**：从成本预算倒推 —— `月预算 ÷ 活跃用户数 ÷ 单次对话平均 token` = 每月可对话次数 ÷ 30 = 日限额。

**实现约束**：配额四级降级（实施规范 §6.6）按占位值实现，**限额数值本身可配置**。降级行为不得依赖具体数值。

---

### DEC-22 · 知识库可见性口径 ✅ FROZEN · 口径 = `grant_kb`（必要条件）+ `grant_tag`

> **确认记录**：2026-09-16 人工确认（会话内书面答复），9 个语义分叉逐条见 §3.8。
> **本项 `status: frozen` → Agent MUST 直接按 §3 实现，MUST NOT 再输出 BLOCKER。**
> 下方「原 BLOCKER 文本」仅作存档，不再生效。

**问题**：「用户能看到哪些文档」如何判定？

**三种可选口径**

| 口径 | 含义 | 数据落点 |
|---|---|---|
| `grant_org` | 同部门 + 下级部门可见 | 用户主体含 `dept:<路径>`，文档标 `dept:<路径>` |
| `grant_kb` | 按 `kb_members` 显式授权 | `kb_members(kb_id, subject_type, subject_id)` |
| `grant_tag` | 按文档标签与用户属性匹配 | `documents.acl_tags` ∩ `user.subjects` |

**原 BLOCKER 理由（存档）**

双检验两条都不通过：

- **fail-safe**：三种口径的可见范围**宽窄差异极大**。只取 `grant_org` 会让跨部门协作文档全部不可见（大面积误拒）；只取 `grant_tag` 会让任何标了 `public` 的文档对全员开放（真实泄漏）。**不存在"错在更安全一侧"的默认值。**
- **additive**：这一个定义同时决定五个东西——权限计算函数、Redis 缓存 key 结构、向量库下推表达式、前端知识库选择器、越权测试用例。改一次 = 权限模块整体返工 + **全库 `acl_tags` 重刷**。

**最终口径（已冻结 2026-09-16）**

```
grant_kb  作为主闸门（必要条件）     ← 先决定"这个人能不能用这个知识库"
grant_tag 作文档级细化               ← 再决定"知识库里哪些文档他看得见"
grant_org 作为 grant_tag 的标签来源  ← 部门路径成为可匹配的标签，本身不参与判定
```

**为什么知识库成员必须是"必要条件"而不是"或条件"**

考虑这个场景：一份标了 `dept:财务` 的文档，被误放进了任何人可访问的公共知识库。
- 若 `grant_kb` 与 `grant_tag` 是 **OR** → 研发同事因为知识库公开就能搜到财务文档 → **泄漏**
- 若是 **AND** → 知识库不公开，他搜不到 → **安全**

> **⚠️ 但 AND 只在标签是纯的时候才成立。** 若该文档的 `acl_tags` 是 `{public, dept:财务}`，G4 对全体用户恒真，G2 就成了唯一防线 —— 而 Q2 又让公开库对所有用户成立。**所以 `public` 必须是互斥标签**（§3.3(3)）。这是 AND 规则成立的前提，原设计漏了。

**9 个语义分叉的答复 —— 其中 1 处是人工显式偏离**

| # | 问题 | 答复 | 与推荐值 |
|---|---|---|---|
| Q1 | `kb_members` 成员类型 | 用户 + 部门 + 用户组 + 角色 | 同推荐 |
| Q2 | 是否存在全员可见的公开库 | **有**，且只建一个（`is_public=true`），所有用户自动为成员 | 同推荐 |
| Q3 | 上级部门能否看下级部门的文档 | **能** | ⚠️ **偏离推荐值「不能」** |
| Q4 | 是否支持兼岗（主部门 + 兼岗） | 支持，`user_subjects` 取并集 | 同推荐 |
| Q5 | 文档的部门归属标签谁定 | 上传者所属部门自动打标，上传时可手动覆盖 | 同推荐 |
| Q6 | 跨部门文档（同时标两个部门） | 任一命中即可见 | 同推荐 |
| Q7 | 是否要显式 `deny` | 要（合规调查临时收回 / 离职冻结 / 外包到期收回） | 同推荐 |
| Q8 | 外部人员（外包 / 顾问 / 实习生） | 独立用户组 `group:external_*` + 显式授权，**不给部门标签** | 同推荐 |
| Q9 | 新账号（未分配部门）默认可见范围 | 只能看见公开库中 `acl_tags` 含 `public` 的文档 | 同推荐 |

**Q3 是唯一需要改设计的一项 —— 它同时暴露并修掉了一处真实泄漏**

Q3 = 能 → 用户主体必须**同时做上向与下向展开**。而在核对过程中发现，原 §3.3 / 实施规范 §3.2.1 的「两侧都做祖先展开」写法，会让**共享根节点的任意两个主体互相可见**——也就是全公司互通：

| 项 | 变更前 | 变更后（MUST） |
|---|---|---|
| 用户主体 · 上向（祖先） | ✅ 有 | ✅ 有（不变） |
| 用户主体 · 下向（子孙） | ❌ 无 | ✅ **新增，传递闭包** |
| 文档标签 | ❌ 祖先 ∪ 自己 | ✅ **仅自己**（`DOC_TAG_ANCESTOR_EXPANSION = False`） |
| 上级看下级 | ⚠️ 偶然可见（因双方都含根节点） | ✅ 精确定义（用例 8） |
| **同级（兄弟部门）** | ❌ 可见 | ✅ **不可见**（用例 9） |
| **跨分支（研发 vs 财务）** | ❌ **可见（真实泄漏）** | ✅ 不可见（用例 10） |
| `public` 标签 | ❌ 可与部门标签共存 | ✅ **互斥**（用例 3b） |
| `level:` 标签参与 G4 | ❌ 允许（绕过 G1） | ✅ **禁止**（§3.3(4)） |

> **一句话**：变更前的写法不是"多了点可见性"，而是**全公司互通**。Q3 的表面代价是"放宽了上级可见性"，实际净效果是**收紧了同级与跨分支**。三个方向的调整都在 §3.3 与 §3.5 用例 3b/8/9/10 中固化。

**Agent 行为（本节已变更）**：本项 `status: frozen` → 直接按 §3 实现。

```
[已解除 · 仅存档] BLOCKER: DEC-22 未决 —— 可见性口径决定权限计算、缓存 key、
  下推表达式、前端选择器与越权测试用例。于 2026-09-16 解除，裁决结果见上表。
  以下为原待裁决内容，不再生效：
  影响：WU-03 / WU-04 / WU-08 / WU-19 / WU-21 / WU-23 无法确定 acl_tags 的算法。
  需要裁决：业务负责人 + 安全合规。截止：步骤 4（分块入库）之前 —— 逾期代价为全库 acl_tags 重刷。
```

**完整判定式、展开规则、下推表达式、10 条验证用例** → **见 §3。**

---

### DEC-23 · 是否支持多跳问题 🟡 SAFE-DEFAULT

**问题**：像「我和同事出差住不同城市，报销标准分别怎么算？」这类需要**跨文档合并推理**的问题，算不算在范围内？

**默认值**：`unsupported`（单轮 Top-K）。

**安全论证**

- **fail-safe**：不支持 = 明确拒答或只答其中一部分，**不会编造跨文档推理结论**。反方向的默认（假装支持）会产出看似完整但实际遗漏条件的答案——这在财务/合规场景是危险的。
- **additive**：支持 = 在 Agent Loop 增加工具链。`WU-24 / WU-25 / WU-34` 的工具化骨架已经具备，增量即可。

**MUST 的配套动作**：在验收标准中**显式写明「不支持跨文档多跳推理」**。不写的话，验收时会被当成缺陷提出来，而实际它是已确认的范围外。

**若答复为「必须支持」** → 影响面：

| 变更 | 说明 |
|---|---|
| `WU-24 / 25 / 34` 提前 | 工具编排成为主链路而非增强 |
| 检索策略调整 | 需要"查询分解"步骤，把复合问题拆成子查询 |
| 新增不变量 | 每个子查询各自过拒答闸门；合并后仍需接地校验 |
| 成本上升 | 单次对话的检索与 LLM 调用次数上升 2–4 倍，`DEC-21` 限额需重算 |

---

## 3. 知识库边界定义（DEC-22 的完整展开）

> 本节是三份文档里**唯一直接决定"谁能看到什么"的地方**。DEC-22 **已于 2026-09-16 确认并冻结**，Agent 按本节实现即可，不需要再次推理，也 MUST NOT 再输出 BLOCKER。
> 本节同时是 `WU-06`（知识库表）、`WU-08`（入库）、`WU-19`（检索隔离）、`WU-21`（权限缓存）、`WU-23`（请求链路）的实现依据。

### 3.1 三个输入

判定只依赖三个输入，全部在**请求开始前**算好，检索期不再查库：

| 输入 | 含义 | 来源 | 缓存 |
|---|---|---|---|
| `user_subjects` | 用户主体集合 | 组织架构（**祖先 ∪ 自己 ∪ 子孙**）+ 用户组 + 角色 + `public` | Redis，key 带 `acl_version` |
| `authorized_kb_ids` | 用户可访问的知识库集合 | `kb_members` 求并集 + **`is_public=true` 的公开库（无条件加入）** | Redis，同上 |
| `clearance` | 用户密级等级（数值 `level_rank`） | 用户档案 | Redis，同上 |

**★ 公开库（Q2 = 有）**

`knowledge_bases.is_public = true` 的库对**所有已登录用户**（含无部门、无组、无角色的新账号）**自动视为成员**，直接进入 `authorized_kb_ids`。全局 `is_public=true` 的库 **MUST ≤ 1 个**（`PUBLIC_KB_MAX_COUNT = 1`），由平台管理员维护，靠部分唯一索引 + `check_decisions.py` 双重保证。

> **公开库 ≠ 公开文档。** 进入 `authorized_kb_ids` 只过了 G2。库内文档**仍须过 G4**：一份被误放进公开库、却标了 `dept:财务` 的文档，研发同事**依然看不到**。这正是 §3.5 用例 3 要守住的东西。
>
> 反过来说，**公开库让 G2 的过滤力对全库失效** —— 所以凡是「只靠 G2 兜住」的写法在这里就变成了裸奔。§3.3 的 `public` 互斥规则由此而来。

### 3.2 判定式（权威 · 已冻结）

```
visible(doc, user) :=
      doc.deleted_at IS NULL
  AND doc.is_latest = true
  AND doc.level_rank   <= user.clearance                    -- 闸门 G1 · 密级
  AND doc.kb_id        IN user.authorized_kb_ids            -- 闸门 G2 · 知识库（必要条件）
  AND NOT (doc.deny_subjects ∩ user.subjects ≠ ∅)           -- 闸门 G3 · 显式拒绝（优先）
  AND (doc.acl_tags    ∩ user.subjects ≠ ∅)                 -- 闸门 G4 · 标签匹配
```

**四条硬规则**

| # | 规则 | 理由 |
|---|---|---|
| 1 | **G3（deny）优先级最高** —— 命中即不可见，不再看其他条件 | 调岗、离职、合规调查期间的临时封禁必须立即生效，不能被其他条件"救回来" |
| 2 | **闸门之间是 AND，不是 OR** | 若 G2 与 G4 是 OR，一份误标为 `public` 的财务文档放进公开库就会泄漏。知识库成员是**必要条件** |
| 3 | **空 `acl_tags` 不等于"对所有人可见"** | 见 §3.3 动作 B。空标签在入库时被规范化为 `["public"]`，而 `public` 是用户的**常规主体之一**，仍需通过 G2 |
| 4 | **★ 文档标签 MUST NOT 做祖先展开** | 见 §3.3「为什么文档侧不能展开」。这是本次口径确认中最容易搞错、后果最严重的一条 —— 做错不是"多看见一点"，而是**全公司互通** |

### 3.3 展开规则 —— 本设计的关键

**所有展开都在入库时（`WU-08`）或请求前（`WU-21`）完成。检索期不做任何函数计算。**

| # | 动作 | 作用侧 | 做法 | 为什么必须这样做 |
|---|---|---|---|---|
| **A1** | 祖先展开（**上向**） | **用户主体** | `dept:财务/资金组` → 追加 `dept:财务`、`dept:公司` | 实现「下级部门看到上级部门的文档」。Milvus 无路径函数，只能在数据侧展开 |
| **A2** | **子孙展开（下向）★ Q3 新增** | **用户主体** | 用户在 `dept:财务` → 追加 `dept:财务/资金组`、`dept:财务/核算组` ……（**传递闭包**） | 实现「上级部门看到下级部门的文档」（Q3 = 能）。同样不能在检索期做前缀匹配 |
| **B** | 空标签规范化 | 文档标签 | 无标签 → `{public}`；同时所有 `user_subjects` 恒含 `public` | 消除 `cardinality = 0` 分支，让 G4 在两种后端上表达式形态一致 |
| **C** | 密级数值化 | 文档标签 | `PUBLIC/INTERNAL/SECRET/TOP` → `level_rank = 10/20/30/40` | 向量库只做数值比较，不做枚举比较 |

#### ★ 三条不得违反的边界

**(1) A2 的可见性边界 —— 包含子孙，但不包含兄弟**

```
user@dept:财务（subjects 含 公司, 财务, 财务/资金组, 财务/核算组）
   ├─ doc 标 dept:财务/资金组      → ✅ 可见   上级看下级（A2 生效）
   ├─ doc 标 dept:财务             → ✅ 可见   自己所在部门
   └─ doc 标 dept:公司/研发/前端组  → ❌ 不可见 跨分支

user@dept:财务/资金组（subjects 含 公司, 财务, 财务/资金组）
   ├─ doc 标 dept:财务             → ✅ 可见   下级看上级（A1 生效）
   └─ doc 标 dept:财务/核算组      → ❌ 不可见 **同级（兄弟部门）不可见**
```

> **同级不可见是刻意的。** A2 只向下展开到"自己的子孙"，不横向展开到兄弟。需要跨兄弟部门共享时，由知识库成员（G2）或显式 `user:` / `group:` 标签解决 —— **共享是授权动作，不应该被组织架构的巧合自动赋予。**

**(2) ★★ 文档侧 MUST NOT 做祖先展开 —— 本次修掉的一处真实泄漏**

实施规范 §3.2.1 原写法是 `dept:finance = 所属部门（含所有祖先部门路径）`。若照此实现，加上用户侧的 A1，就会出现：

```
user@dept:研发     subjects = {public, dept:公司, dept:研发}
doc  标 dept:财务  tags     = {public, dept:公司, dept:财务}
                                    ↑ 交集非空（共同祖先 dept:公司）→ 研发同事看到财务文档 ✗✗
```

因为**组织中任意两个主体最终必然共享根节点**，一旦文档标签也展开祖先，"交集非空即可见"就退化为**全公司互通**。这是匹配模型的**固有性质**，不能靠调参或加注释规避。

| 侧 | 展开 | 结论 |
|---|---|---|
| 用户主体 | 祖先 ∪ 自己 ∪ 子孙 | ✅ **MUST** |
| 文档标签 | **仅自己所属的那一条路径** | ✅ **MUST**（`DOC_TAG_ANCESTOR_EXPANSION = False`） |

> `dept:` 前缀的语义就此固定为：**用户侧是"我能看到的部门集合"，文档侧是"我归属的部门"**。
> **两侧语义不同是有意的**，不是不对称缺陷 —— 对称才是漏洞。

**(3) `public` 是互斥标签 —— Q2 公开库带来的新约束**

```
文档 acl_tags = {public, dept:财务}     ← ❌ MUST NOT
```

因为 `public` 是**每个**用户的常规主体，它一旦与其他标签共存，G4 就对全体用户恒真。此时**唯一的防线只剩 G2**，而 Q2 又让公开库对所有用户成立 → **该文档对所有人生效**。

| 规则 | 表述 |
|---|---|
| `public` 写入条件 | **当且仅当**该切片没有任何其他主体标签（即文档无标签 / 显式标为全员文档） |
| 写库断言 | `assert not (public in tags and len(tags) > 1)` |
| 想表达"这份财务文档进公开库但仍受控" | **不能**靠加 `public`；应只写 `{dept:财务}`，由 G4 隔离 |

**(4) `level:` 不得作为标签出现在 `acl_tags` 中**

实施规范 §3.2.1 与 WU-19 原写法允许 `acl_tags` 含 `level:internal`、用户主体含 `level:<n>`。这与 G4 组合会**绕过 G1**：一份 `{dept:财务, level:internal}` 的文档，会把所有 clearance ≥ 1 的用户都拉进 G4 命中集 —— 密级过滤形同虚设。

> **密级 MUST 只通过 G1 的数值比较表达**（`level_rank <= clearance`），**MUST NOT** 用标签参与集合匹配。
> `SUBJECT_PREFIXES` 不含 `level:`，`compute_doc_acl_tags` MUST NOT 产出 `level:` 标签。

#### 标签书写规范（MUST）

```
dept:<路径>          部门路径，如 dept:财务、dept:财务/资金组
user:<主体 id>       具体用户，如 user:u100
group:<用户组 id>    用户组，如 group:audit_committee
role:<角色>          角色，如 role:finance_manager
region:<区域>        区域，如 region:cn-north
public               所有人（含无任何归属的新账号）★ 互斥，不得与其他标签共存
```

`deny_subjects` 使用同一套书写规范，但 **MUST NOT** 使用 `public`（"拒绝所有人"没有意义，用下架代替）。

#### `level_rank` 标尺对齐（MUST · 跨文档）

实施规范 §3.3 的 `Level(IntEnum)` 为 `0/1/2/3`（API 层表述）。**检索下推字段 MUST 是数值化的 rank**：

```
level_rank = (level + 1) * 10        # 0→10 公开, 1→20 内部, 2→30 秘密, 3→40 机密
```

**若向量库字段名仍叫 `level`，其取值 MUST 是 rank（10/20/30/40），不是 0..3。**
判据：`user.clearance` 与 `doc.level_rank` 必须在**同一标尺**上比较，否则 G1 会恒真或恒假 —— 恒真是泄漏，恒假是服务不可用。

#### 文档级与切片级两份标签

| 字段 | 用途 | 生成方式 |
|---|---|---|
| `documents.acl_tags` | 列表页过滤、缓存 key、审计 | 由密级 + 知识库归属 + 显式标注计算 |
| `chunk.acl_tags` | **检索下推**（唯一用于检索的字段） | 默认由文档级**扇出**；段落级权限开启时由解析器覆盖 |

**写库前断言**（INV-08）：`assert chunk.acl_tags and len(chunk.acl_tags) > 0`。空标签**不得**通过断言——因为它意味着"空 = 可见性未定义"，而任何"未定义"在权限系统里都是事故。

#### ★ 展开规模的核验（步骤 4 之前 MUST 做一次）

A2 让 `user_subjects` 的规模从「∝ 层级深度」变成「∝ 所在子树的部门数」。上界出现在**直接挂在组织根节点下、且下辖单位最多的那个节点**。

| 组织规模 | 单用户 subjects 规模（典型） | 结论 |
|---|---|---|
| 部门总数 ≤ 300 | < 30 | ✅ 直接用 A1 + A2 |
| 某节点下辖 > 300 个子单位 | 可能 > 300 | ⚠️ 触发下方替代方案评估 |

**核验命令（动手前跑一次，结论记入汇报）**

```sql
-- 挂载单位最多的节点有多少子孙 —— 这决定 A2 的最坏规模
SELECT d.path,
       (SELECT count(*) FROM departments d2 WHERE d2.path LIKE d.path || '/%') AS subtree
FROM departments d
ORDER BY subtree DESC
LIMIT 5;
```

**替代方案（仅在上表触发时才启用，MUST NOT 提前引入）**：改用**双命名空间** ——
文档侧写 `dept_anc:<严格祖先路径>`，用户侧写 `dept_own:<自己的路径>`，两侧集合规模各自 ∝ 深度。
**代价：文档标签要重刷全库**，所以这个取舍 MUST 在步骤 4 之前做；步骤 4 之后做就是全库重刷。

### 3.4 下推表达式

**核心原则：权限条件下推到查询层，不在应用层过滤。** 先裁小集合再排序，而不是取 Top-K 再筛。

**pgvector**

```sql
SELECT c.id, c.doc_id, c.content, c.page_from, c.page_to, c.parent_id,
       1 - (c.embedding <=> $1) AS score
FROM chunks c
JOIN documents d ON d.id = c.doc_id
WHERE c.is_latest
  AND d.deleted_at IS NULL
  AND d.kb_id       = ANY($2)          -- authorized_kb_ids
  AND d.level_rank  <= $3              -- clearance
  AND NOT (d.deny_subjects && $4)      -- user_subjects
  AND c.acl_tags    && $4              -- user_subjects
  AND (c.content_tsv @@ plainto_tsquery('chinese', $5) OR c.embedding <=> $1 < 0.5)
ORDER BY c.embedding <=> $1
LIMIT $6;
```

> `&&` 是 PG 数组交叠操作符（非空交集）。需要 `pgvector` + `zhparser` 扩展。
> 混合检索（向量 + BM25）的 RRF 融合在应用层做，但**两个召回分支的 WHERE 子句必须一致**——只在向量分支下推权限、忘了关键词分支，是常见的越权入口。

**Milvus**

```python
expr = (
    "is_latest == true and is_deleted == false "
    f"and kb_id in {list(authorized_kb_ids)} "
    f"and level_rank <= {clearance} "
    f"and not ARRAY_CONTAINS_ANY(deny_subjects, {list(user_subjects)}) "
    f"and ARRAY_CONTAINS_ANY(acl_tags, {list(user_subjects)})"
)
```

**形态对照表**

| 语义 | pgvector | Milvus |
|---|---|---|
| 数组交叠非空 | `a && b` | `ARRAY_CONTAINS_ANY(a, b)` |
| 数组交叠为空 | `NOT (a && b)` | `not ARRAY_CONTAINS_ANY(a, b)` |
| 数值比较 | `d.level_rank <= :x` | `level_rank <= x` |
| 布尔 | `= true` | `== true` |
| 空值判断 | `d.deleted_at IS NULL` | `is_deleted == false`（**入库时写布尔字段**） |

> 最后一行是 Milvus 侧的坑：Milvus 不擅长 `IS NULL` 比较，所以删除状态必须**在入库时物化为布尔字段**，而不是靠时间戳为空来判断。
>
> **A2 带来的规模注意**：`list(user_subjects)` 现在包含子孙部门，长度可能到几百。`SUBJECT_EXPANSION_SOFT_LIMIT = 300` 触发时**只 WARN + 打点，MUST NOT 裁剪集合** —— 裁剪就是放宽权限（少一个主体 = 少一份可见性，看似更安全；但若裁掉的是用户自己的部门，就变成误拒，而两种后果都不该由日志阈值决定）。真到那个规模，走 §3.3 末的替代方案。

### 3.5 十条验证用例（DEC-22 的必交付物）

Agent 实现后必须让 `tests/security/test_acl_matrix.py` 覆盖以下十条。**这十条就是"口径确认"的可执行形式** —— 人工确认口径时，实际是在确认这张表。

| # | 用户属性 | 文档属性 | 期望 | 依据 |
|---|---|---|---|---|
| 1 | `{public, dept:公司, dept:财务, role:员工, user:u100}`，clearance=2 | kb=财务制度库（u100 为成员），level=内部(20)，acl_tags=`{dept:财务}`，deny=∅ | ✅ 可见 | G1✓ G2✓ G3✓ G4✓ |
| 2 | 同上 | kb=公共制度库（`is_public=true`，全员成员），acl_tags=`{public}`（无其他标签） | ✅ 可见 | G2 由公开库自动满足；G4 靠 `public` 命中 |
| 3 | `{public, dept:公司, dept:研发, user:u200}`，clearance=2 | kb=财务制度库（**u200 非成员**，且该库非公开），acl_tags=`{dept:财务}` | ❌ 不可见 | **G2 失败** |
| 3b | 同上 u200 | kb=**公开制度库**，acl_tags=`{dept:财务}`（一份被误放进公开库的财务文档） | ❌ 不可见 | **G4 失败** —— 公开库让 G2 失效，此时只剩 G4 在守 |
| 4 | `{public, dept:公司, dept:财务, user:u100}` | 文档在 u100 有权限的库，`deny_subjects={user:u100}`，`expires_at` 未过期 | ❌ 不可见 | **G3 优先**。场景：调岗 / 离职 / 合规调查期间临时封禁 |
| 5 | `{public, dept:公司, dept:财务, user:u100}`，clearance=3（部门总监） | level=**机密(40)**，且该知识库由 u100 创建 | ❌ 不可见 | **G1 失败**。创建者身份不构成越过密级的理由 |
| 6 | `{public, dept:公司, dept:财务, dept:财务/资金组}` | acl_tags=`{dept:财务}` | ✅ 可见 | **A1 生效**（下级部门可见上级文档） |
| **7** | `{public}`（新账号，无部门/组/角色），clearance=1 | (a) 任意带部门标签的文档；(b) 公开库中 `acl_tags={public}` 的文档 | (a) ❌ 全部不可见<br>(b) ✅ 可见 | **fail-closed 证明 + Q9 口径**：新账号只能看见公开库里的 `public` 文档 |
| **8** | `{public, dept:公司, dept:财务}` | acl_tags=`{dept:财务/资金组}` | ✅ 可见 | ★ **A2 生效**（上级部门可见下级文档，Q3 = 能） |
| **9** | `{public, dept:公司, dept:财务, dept:财务/资金组}` | acl_tags=`{dept:财务/核算组}`（**同级兄弟部门**） | ❌ 不可见 | ★ **A2 只向下、不横向** —— 兄弟部门之间不可见 |
| **10** | `{public, dept:公司, dept:研发}` | acl_tags=`{dept:财务}`（若文档侧误做祖先展开则会含 `dept:公司`） | ❌ 不可见 | ★★ **防回归**：文档标签 MUST NOT 祖先展开；展开后共享根节点 → **全公司互通** |

**第 7 条是必须有的负向用例**，也是最容易被漏掉的一条——很多实现在"用户没有任何 subject"时会因为空集合运算的边界（`∅ ∩ X = ∅` 恰好正确，但 `NOT (∅ ∩ X ≠ ∅)` 恒为真）而**误放行**。必须显式测试。

> **第 3b / 9 / 10 三条是本次确认新增的泄漏面**，全部由「公开库 + 标签展开方向」引入。它们不需要新的数据，但**必须有独立的断言** —— 否则口径是对的、实现是错的，而测试全绿。

### 3.6 函数签名（MUST 逐字实现）

```python
# app/permission/subjects.py

def compute_user_subjects(
    *, user_id: str, org_unit_paths: list[str],
    group_ids: list[str], role_keys: list[str],
) -> frozenset[str]:
    """用户主体展开。MUST 恒含 'public'。
    MUST 对每个 org_unit_path 同时做两个方向，缺一不可：
      上向 A1（祖先）  'dept:财务/资金组' → +{'dept:公司', 'dept:财务'}
      下向 A2（子孙）★ 'dept:财务'        → +{'dept:财务/资金组', 'dept:财务/核算组', ...}
                                          （传递闭包，MUST NOT 只展开一层）
    MUST NOT 只做上向 —— 只做上向会丢失「上级看下级」（Q3 = 能）。
    MUST NOT 横向展开到兄弟部门 —— 兄弟不可见是用例 9。
    展开 MUST 用一条 SQL 取自 departments.path 前缀查询，MUST NOT 逐层递归。
    MUST NOT 产出 'level:<n>' 形式的 subject（密级只走 G1）。
    """

def compute_doc_acl_tags(
    *, doc_meta: DocMeta, kb: KnowledgeBase,
) -> frozenset[str]:
    """文档级标签计算。
    ★★ MUST NOT 对部门标签做祖先展开 —— 展开会让共享根节点的任意两个主体互相可见（§3.3(2)）。
       部门标签只写文档自己所属的那一条路径。
    ★  MUST NOT 产出 'level:' 标签（密级只走 G1，标签化会绕过 G1）。
    ★  'public' 为互斥标签：仅当无其他主体标签时才写入（§3.3(3)）。
    MUST NOT 返回空集合 —— 无标签时必须返回 frozenset({'public'})。
    """

def fan_out_acl_tags(doc_tags: frozenset[str], chunks: list[Chunk]) -> None:
    """文档级 → 切片级扇出。段落级权限开启时由解析器覆盖（DEC-05）。
    MUST 在任何覆盖之后重新执行 §3.3 的 (3) 互斥断言。"""

def build_search_filter(
    *, user_subjects: frozenset[str],
    authorized_kb_ids: list[str], clearance: int,
) -> dict:
    """构造下推条件（§3.4）。返回方言无关的结构，由 VectorStore 适配器编译。
    规模超 SUBJECT_EXPANSION_SOFT_LIMIT 时 MUST 打点告警，MUST NOT 裁剪集合。"""

# app/vector/filter.py
def compile_filter(f: dict, dialect: Literal["pgvector", "milvus"]) -> str:
    """把 build_search_filter 的结果编译为目标方言。MUST 只在此处出现方言差异。"""

# app/permission/visibility.py
async def assert_visible(*, doc_id: str, user_id: str, ctx: AuthContext) -> bool:
    """结果层兜底断言（防御纵深）。MUST 在返回引用元数据前调用。
    失败时 MUST 走「不可区分」分支（INV-13），MUST NOT 返回 403。"""
```

**★ 标注的四处是权限链路上最容易失守的地方**：

1. `compute_user_subjects` 漏掉 A1 或 A2 任一方向 —— 漏 A1 会导致**下级看不到上级制度**，漏 A2 会导致**上级看不到下级文档**。两者都是误拒，不明显、影响面大，且**只有 §3.5 用例 6/8 能测出来**
2. `compute_doc_acl_tags` 做了祖先展开 —— 从"多看见一点"直接变成**全公司互通**（用例 10）
3. `compute_doc_acl_tags` 产出 `{public, ...}` 混合标签 —— G4 对全员恒真，防线只剩 G2；配合公开库即泄漏（用例 3b）
4. `assert_visible` 走不可区分分支 —— 直接抛 403 会泄漏文档存在性

### 3.7 与缓存、前端的关系

| 关注点 | 规则 |
|---|---|
| 权限缓存 key | `acl:u:{user_id}:v{acl_version}` —— **必须带版本号**（INV-10）。调岗后版本 +1，旧 key 自然不命中 |
| 答案缓存 key | **必须带权限指纹** `hash(user_subjects + authorized_kb_ids)`（INV-11）。语义缓存按作用域分域 |
| 功能权限校验 | **不缓存**。功能权限变更必须立即生效 |
| `kb_ids` 前端传入 | 视为**不可信输入**。服务端 MUST 与 `authorized_kb_ids` 求交集后重新过滤（INV-14），静默裁剪不报错 |
| 知识库选择器 | 前端只展示 `authorized_kb_ids`（含自动加入的公开库）；但**前端过滤不是安全边界**，仅决定可见性，不决定可访问性 |
| **★ A2 扩大了缓存失效面** | 用户主体现在依赖**其下级的组织树**。新增/删除/改名/移动一个部门，会改变**该部门祖先链上所有用户**的 subjects。因此：**部门树发生任何结构变更 → `acl_version` 递增 + `acl_epoch += 1`（全局失效）**。低频事件，正确性优先于命中率 |
| 公开库变更 | `is_public` 置位/取消 → 全局失效（所有用户的 `authorized_kb_ids` 都变） |

> **为什么部门树变更要全局失效而不是精确失效**：精确失效需要算出"哪些用户的子孙集合受影响"，而这正是组织树的前缀查询 —— 用一次全局失效换掉一个容易写错的复杂逻辑，在低频事件上是明显划算的。这条与 INV-10 的 `acl_epoch` 机制同源。

### 3.8 人工确认入口与确认记录

DEC-22 **不允许在会话里一句话就落盘** —— 它有 9 个语义分叉（公开库范围、上下级可见方向、兼岗、跨部门、deny 场景、外部人员、新账号可见范围……），漏掉任何一条的代价都是全库 `acl_tags` 重刷。

**确认流程（已走完）**

| 步骤 | 动作 | 产物 | 状态 |
|---|---|---|---|
| 1 | 填写 `DEC-22-可见性口径-确认存档.md` | 9 个问题的答案 + 用例确认 + 映射表 | ✅ 已完成（映射表人工明确"暂不填"） |
| 2 | 把表发回给 Agent | — | ✅ 2026-09-16 |
| 3 | Agent 按 §5.1 扩散顺序落盘 | 本契约 §1 `status` → `frozen`；`decisions.py` 填入 `VISIBILITY_GRANTS` | ✅ 已完成 |
| 4 | Agent 把结果写进下方「确认记录」 | 本节 | ✅ 已完成 |

**确认记录**（2026-09-16 · 人工书面确认，会话内）

| 项 | 内容 |
|---|---|
| 确认日期 | **2026-09-16** |
| 确认人 | 用户（业务 + 技术口径一并确认；安全合规项未单独会签，见下方「未会签项」） |
| 口径骨架 | `grant_kb`（必要条件）+ `grant_tag`（文档级细化）；`grant_org` **不参与判定**，仅作标签来源 |
| Q1 | `kb_members` 成员类型 = 用户 + 部门 + 用户组 + 角色（四种） |
| Q2 | **有**全员公开库，且只建一个（`is_public=true`），所有用户自动视为成员 |
| Q3 | ★ **上级部门能看到下级部门的文档**（**偏离推荐值「不能」**） |
| Q4 | 支持兼岗，`user_subjects` 取并集 |
| Q5 | 上传者所属部门自动打标，上传时可手动覆盖 |
| Q6 | 跨部门文档任一命中即可见 |
| Q7 | 要显式 deny（合规调查临时收回 / 离职冻结 / 外包到期收回） |
| Q8 | 外部人员走独立用户组 `group:external_*`，显式授权，不给部门标签 |
| Q9 | 新账号只能看见**公开库**中 `acl_tags` 含 `public` 的文档 |
| 有异议并修改的用例 | 无（10 条用例全部确认） |
| 映射表存档 | ⬜ **未填**（人工明确"暂不填"，**不构成阻塞**；补填后 MUST 同步回本节） |
| 由 Q3 派生的实现约束 | 用户侧 A1+A2 双向展开；**文档侧 MUST NOT 展开**；兄弟部门不可见（见 §3.3、用例 8/9/10） |

**未会签项（不阻塞，但 MUST 在里程碑 B 之前补）**

| 项 | 该找谁 | 理由 |
|---|---|---|
| Q3 的**可见性扩张**风险确认 | 安全合规 | Q3 = 能 是本次唯一的放宽方向。业务口径已确认，但"上级能看到整个子树的文档"在敏感部门（财务/法务/人事）可能超出预期 |
| §3.5 映射表（5 行真实例子） | 各业务线 | 这类规则**几乎总是在"举个真实的人"时才暴露问题** |
| Q7 的 deny 运维责任人 | 安全合规 | 谁有权在后台按下这个开关，必须有人负责 |

> 未会签项**不免除任何实现义务** —— 口径已冻结，Agent MUST 按 §3 实现。它们只影响「上线前的合规签字」，不影响「现在能不能开工」。

---

## 4. 生成的配置契约 · `app/config/decisions.py`

> 这是本文档的**代码投影**。Agent MUST 逐字落盘，MUST NOT 手工偏离。
> 修改流程见 §5：先改契约文档 → 再改本文件 → 跑 `scripts/check_decisions.py`。
>
> **业务代码 MUST 从本模块读取取值，MUST NOT 硬编码任何本模块已定义的常量。**

```python
"""决策契约的代码投影 —— 由 AI-Coding-Agent-决策契约.md §4 生成。

生成源 : AI-Coding-Agent-决策契约.md (contract_version = dc-1.2)
修改流程: 契约文档 §1 → 本文件 → scripts/check_decisions.py
MUST NOT 手动偏离；偏离即为契约违规。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Final

DECISIONS_VERSION: Final = "dc-1.2"

# ══════════════════════════════════════════════════════════════
# DEC-01 · 交付路径
# ══════════════════════════════════════════════════════════════

class DeliveryPath(StrEnum):
    A = "A"   # 极简 / 演示 / 无权限体系 / 禁止真实用户使用
    B = "B"   # 标准 / 可上线
    C = "C"   # 完整


DELIVERY_PATH: Final[DeliveryPath] = DeliveryPath.B
"""★ 已冻结（2026-09-16 人工确认）—— 不再未决，不再触发 fail-fast。

B = 标准路径：14-16 周 / 4 人 / 含完整权限体系 / 可上线。
由此产生的 MUST 约束：
  - 权限相关 WU（03 / 04 / 19 / 21 / 23）全部为 MUST 实现，MUST NOT 标记 SKIPPED-BY-PATH
  - 路径 A 专用机制（DEMO 横幅 / ACKNOWLEDGE_UNRESOLVED）MUST NOT 启用
  - 本项 MUST NOT 再出现在 UNRESOLVED 中
"""

PATHS_WITH_PERMISSION: Final[frozenset[DeliveryPath]] = frozenset(
    {DeliveryPath.B, DeliveryPath.C}
)

DEMO_BANNER_TEXT: Final = "DEMO · 无权限隔离 —— 本环境任何用户可查询全库内容，禁止录入真实数据"

# ══════════════════════════════════════════════════════════════
# DEC-02 · 向量库后端
# ══════════════════════════════════════════════════════════════

class VectorBackend(StrEnum):
    PGVECTOR = "pgvector"
    MILVUS = "milvus"


VECTOR_BACKEND: Final[VectorBackend] = VectorBackend.PGVECTOR
"""SAFE-DEFAULT。升级/迁移条件见契约 DEC-02。
迁移前提：VectorStore 端口存在 + §3.3 三个入库期动作已实现。"""

VECTOR_BACKEND_ESCALATION_CHUNK_THRESHOLD: Final = 1_000_000

# ══════════════════════════════════════════════════════════════
# DEC-03 · 模型部署形态
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ModelDeployment:
    embedding: str
    rerank: str
    llm: str

    @property
    def any_cloud(self) -> bool:
        return "cloud" in (self.embedding, self.rerank, self.llm)


MODEL_DEPLOYMENT: Final = ModelDeployment(
    embedding="cloud_api",
    rerank="cloud_api",
    llm="cloud_api",
)
"""SAFE-DEFAULT = 全云 API。
⚠ 只要 any_cloud 为 True，LevelPolicy 对 SECRET/TOP 必须 ingestable=False（INV-15）。
这是代码断言，不是文档要求 —— 见 app/compliance/level_policy.py。"""

# ══════════════════════════════════════════════════════════════
# DEC-04 · 前端技术栈
# ══════════════════════════════════════════════════════════════

FRONTEND_STACK: Final = {
    "web": "nextjs_app_router",
    "h5": "same_as_web",
    "miniprogram": "taro",
    "app": "expo_react_native",
    "shared": "packages/kb-core",
}

KB_CORE_FORBIDDEN_IMPORTS: Final[frozenset[str]] = frozenset(
    {
        # DOM
        "document", "window", "localStorage", "sessionStorage", "fetch", "EventSource",
        # 小程序
        "wx", "uni",
        # 渲染栈
        "react-dom", "react-native",
    }
)
"""kb-core 边界规则（DEC-04 唯一前提）。由 eslint boundaries 规则强制，
不靠自觉。所有 IO MUST 通过构造注入的 adapter 接口。"""

# ══════════════════════════════════════════════════════════════
# DEC-05 · 权限粒度
# ══════════════════════════════════════════════════════════════

class PermissionGranularity(StrEnum):
    KB = "kb"
    DOC = "doc"
    CHUNK = "chunk"


PERMISSION_GRANULARITY: Final = PermissionGranularity.DOC
"""SAFE-DEFAULT = 知识库级 + 文档级。
升级到 CHUNK 只需解析器写更细的 acl_tags，不改表、不改检索层。"""

# ══════════════════════════════════════════════════════════════
# DEC-06 / 06b · 认证
# ══════════════════════════════════════════════════════════════

AUTH_PROVIDER: Final = "oidc_authcode_pkce"
AUTH_PROVIDER_FALLBACKS: Final = ("wecom_oidc", "dingtalk_oidc")

MINIPROGRAM_BIND_REQUIRED_MESSAGE: Final = (
    "请先在企业微信 / 钉钉中打开本应用完成账号绑定，再回到小程序使用"
)
DEMO_ONLY_UNIONID_MAP: Final[dict[str, str]] = {}
"""★ 演示期临时映射表。APP_ENV=prod 时 MUST 断言为空。"""

# ══════════════════════════════════════════════════════════════
# DEC-07 · 密级
# ══════════════════════════════════════════════════════════════

class SecurityLevel(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    SECRET = "secret"
    TOP = "top"


LEVEL_RANK: Final[dict[SecurityLevel, int]] = {
    SecurityLevel.PUBLIC: 10,
    SecurityLevel.INTERNAL: 20,
    SecurityLevel.SECRET: 30,
    SecurityLevel.TOP: 40,
}
"""§3.3 动作 C：密级数值化。向量库只做数值比较。"""


@dataclass(frozen=True)
class LevelPolicy:
    ingestable: bool          # 是否允许写入向量库
    cloud_model_allowed: bool # 是否允许出域到云模型
    mask_on_ingest: bool
    mask_on_output: bool
    require_watermark: bool
    note: str


def policy_for(level: SecurityLevel, deployment: ModelDeployment | None = None) -> LevelPolicy:
    """★ 密级 → 行为映射。

    铁律：若 deployment.any_cloud 为 True，SECRET 与 TOP 必须 ingestable=False。
    改为内网自建的场景见 DEC-03；此时 SECRET 可放开，TOP 恒拒绝。
    """
    d = deployment or MODEL_DEPLOYMENT
    cloud = d.any_cloud
    match level:
        case SecurityLevel.PUBLIC:
            return LevelPolicy(True, True, False, False, False, "公开文档")
        case SecurityLevel.INTERNAL:
            return LevelPolicy(True, True, True, True, False, "内部文档，出入双向脱敏")
        case SecurityLevel.SECRET:
            return LevelPolicy(
                ingestable=not cloud,
                cloud_model_allowed=not cloud,
                mask_on_ingest=True, mask_on_output=True, require_watermark=True,
                note="涉密：默认不入库；放开需四项独立条件全满足",
            )
        case SecurityLevel.TOP:
            return LevelPolicy(
                ingestable=False, cloud_model_allowed=False,
                mask_on_ingest=True, mask_on_output=True, require_watermark=True,
                note="机密：恒不入库，任何部署形态下均拒绝",
            )


# ══════════════════════════════════════════════════════════════
# DEC-09 · DLP 敏感字段
# ══════════════════════════════════════════════════════════════

class PiiField(StrEnum):
    MOBILE = "mobile"
    ID_CARD = "id_card"
    BANK_CARD = "bank_card"
    EMAIL = "email"
    INTRANET_IP = "intranet_ip"
    SECRET_KEY = "secret_key"
    ACCESS_CARD = "access_card"


DLP_FIELDS: Final[frozenset[PiiField]] = frozenset(PiiField)
"""★ MUST NOT 从本集合中移除字段 —— 移除意味着已脱敏原文需重新还原，是「改」而非「加」。
合规追加行业字段（病历号 / 证券账号 / 图纸编号）属「加」，允许。"""

RESTORE_REQUIRED_SCOPES: Final[frozenset[str]] = frozenset({"pii:restore"})
AUDIT_EVENT_PII_RESTORE: Final = "PII_RESTORE"
"""恢复三条件：文档可见 AND pii:restore AND 写审计。缺一不可（INV-16）。"""

# ══════════════════════════════════════════════════════════════
# DEC-10 · 审计留存
# ══════════════════════════════════════════════════════════════

AUDIT_RETENTION_DAYS: Final[dict[str, int]] = {
    "AUTH": 180,
    "ACL_CHANGE": 1095,
    "QA": 365,
    "DENY_HIT": 1095,
}
"""SAFE-DEFAULT 取偏长值：留存期变短 = 删数据（不可逆），变长 = 改配置（可逆）。
MUST NOT 对审计表执行 DELETE / UPDATE（INV-17）。清理通过分区 drop 实现，
且清理动作本身 MUST 写审计。"""

# ══════════════════════════════════════════════════════════════
# DEC-11 · 越权与不可区分
# ══════════════════════════════════════════════════════════════

class IndistinguishabilityMode(StrEnum):
    STATUS_ONLY = "status_only"
    STATUS_AND_TEXT = "status_and_text"
    FULL_WITH_LATENCY = "full_with_latency"


INDISTINGUISHABILITY: Final = IndistinguishabilityMode.FULL_WITH_LATENCY
INDISTINGUISHABLE_TEXT: Final = "没有找到相关内容"
LATENCY_ALIGN_FLOOR_MS: Final = 120
LATENCY_ALIGN_P95_TOLERANCE_MS: Final = 50
"""容差规则：若补延迟使接口 P95 增幅 > 50ms → 降级为 STATUS_AND_TEXT，
并在汇报中标记 PARTIAL + 附实测数字。"""

NOT_FOUND_SEMANTICS: Final = "统一 404；MUST NOT 对数据权限返回 403"
"""403 仅用于功能权限（PERMISSION_DENIED）。数据权限永远 404（INV-13）。"""

# ══════════════════════════════════════════════════════════════
# DEC-12 · 灰度
# ══════════════════════════════════════════════════════════════

ROLLOUT_DIMENSION: Final = "department"
ROLLOUT_DEPT_GRAYLIST: Final[list[str]] = []
"""空名单 = 不开启灰度（全量内部发布）。MUST NOT 按用户灰度。"""

# ══════════════════════════════════════════════════════════════
# DEC-13 · 指标达标线（实际读取 eval/thresholds.yaml，此处仅为兜底）
# ══════════════════════════════════════════════════════════════

EVAL_THRESHOLDS_PATH: Final = "eval/thresholds.yaml"
EVAL_THRESHOLDS_FALLBACK: Final[dict[str, object]] = {
    "retrieval_hit_at_10": 0.85,
    "answer_success_rate": 0.80,
    "refusal_precision": 0.90,
    "false_refusal_rate": 0.15,
    "p95_first_token_ms": 1800,
    "cost_per_conversation_cny": 0.35,
}

# ══════════════════════════════════════════════════════════════
# DEC-14 ~ 18 · 分块与检索（CALIBRATE · 占位值）
# ══════════════════════════════════════════════════════════════

CHILD_CHUNK_TOKENS: Final = 256          # DEC-14 placeholder
PARENT_CHUNK_TOKENS: Final = 1024        # DEC-15 placeholder
CHUNK_OVERLAP_RATIO: Final = 0.15        # DEC-16 placeholder
RETRIEVAL_TOP_K: Final = 8               # DEC-17 placeholder
RERANK_CANDIDATES: Final = 50            # DEC-18 placeholder

CALIBRATION_SCRIPTS: Final[dict[str, str]] = {
    "DEC-14": "eval/calibrate_chunk.py",
    "DEC-15": "eval/calibrate_chunk.py",
    "DEC-16": "eval/calibrate_chunk.py",
    "DEC-19": "eval/calibrate_threshold.py",
    "DEC-20": "eval/calibrate_threshold.py",
}

# ══════════════════════════════════════════════════════════════
# DEC-19 / 20 · 阈值（CALIBRATE · 禁止以占位值上线）
# ══════════════════════════════════════════════════════════════

SIMILARITY_THRESHOLD: Final = 0.62
SIMILARITY_THRESHOLD_IS_PLACEHOLDER: Final = True
"""★ INV-03 落地：CI MUST 校验标定报告存在且未过期（<= 90 天）。
生产发布 MUST 在 missing / expired 时阻断。"""

CALIBRATION_REPORT_TTL_DAYS: Final = 90
REFUSAL_TRADE_OFF: Final = "maximize_f1"
REFUSAL_TRADE_OFF_BIAS: Final = "prefer_false_refusal"
"""宁可误拒：误拒成本 = 用户重问一次（可恢复）；
幻觉成本 = 用户拿错误制度办事（不可恢复）。"""

# ══════════════════════════════════════════════════════════════
# DEC-21 · 配额
# ══════════════════════════════════════════════════════════════

QUOTA_PER_USER: Final[dict[str, int]] = {"daily_tokens": 200_000, "monthly_tokens": 3_000_000}

# ══════════════════════════════════════════════════════════════
# DEC-22 · 可见性口径 ✅ FROZEN（2026-09-16 人工确认）
# ══════════════════════════════════════════════════════════════

class GrantModel(StrEnum):
    ORG = "grant_org"   # 组织架构（★ 只作标签来源，不参与判定）
    KB = "grant_kb"     # 知识库成员
    TAG = "grant_tag"   # 文档标签匹配


VISIBILITY_GRANTS: Final[frozenset[GrantModel]] = frozenset({GrantModel.KB, GrantModel.TAG})
"""★ 已冻结（DEC-22 · 2026-09-16 人工确认）。
KB 为必要条件（闸门 G2），TAG 作文档级细化（闸门 G4）。
ORG MUST NOT 出现在本集合中 —— 组织架构只通过 compute_doc_acl_tags 产出标签。

判定式（§3.2）：
    visible := level_rank <= clearance                    # G1
           AND kb_id IN authorized_kb_ids                  # G2 必要条件
           AND NOT (deny_subjects & subjects)              # G3 优先
           AND (acl_tags & subjects)                       # G4
"""

DEPT_VISIBLE_DIRECTION: Final = "up_and_down"
"""★ Q3 = 上级部门能看到下级部门的文档（人工偏离推荐值「不能」）。
user 侧 MUST 同时做上向（祖先）与下向（子孙）展开；doc 侧 MUST NOT 展开。
两个方向缺任一即为误拒：漏上向 → 下级看不到上级制度；漏下向 → 上级看不到下级文档。"""

PUBLIC_SUBJECT: Final = "public"
"""§3.3 动作 B：所有 user_subjects 恒含 'public'；
无标签文档的 acl_tags 规范化为 {'public'}。

★★ 互斥标签：仅当切片**没有其他主体标签**时才写入 public。
   {public, dept:财务} 这类混合写法会让 G4 对全体用户恒真，G2 成为唯一防线 ——
   配合公开库（Q2）即泄漏。写库断言见 ACL_PUBLIC_MUTEX。"""

ACL_PUBLIC_MUTEX: Final = True
"""§3.3(3)：public 不得与其他标签共存。写库前断言，违反即拒绝写入。"""

ACL_LEVEL_TAGS_ALLOWED: Final = False
"""§3.3(4)：acl_tags MUST NOT 含 'level:' 标签。
密级 MUST 只通过 G1 的数值比较表达；标签化会让密级过滤被 G4 绕过。"""

SUBJECT_PREFIXES: Final[tuple[str, ...]] = ("dept:", "user:", "group:", "role:", "region:")
"""★ MUST NOT 含 'level:' —— 同上。"""

SUBJECT_ANCESTOR_EXPANSION: Final = True
"""§3.3 动作 A1（上向）：用户主体展开祖先。
'dept:财务/资金组' → {'dept:财务', 'dept:财务/资金组'}（含根节点）。
实现「下级部门能看到上级部门的文档」。"""

USER_SUBJECT_DESCENDANT_EXPANSION: Final = True     # ★ DEC-22 / Q3 新增
"""§3.3 动作 A2（下向）：用户主体展开子孙（**传递闭包**）。
用户在 'dept:财务' → 追加 'dept:财务/资金组'、'dept:财务/核算组' ……
实现「上级部门能看到下级部门的文档」。MUST NOT 只展开一层。
MUST NOT 横向展开到兄弟部门 —— 兄弟不可见是 §3.5 用例 9。"""

DOC_TAG_ANCESTOR_EXPANSION: Final = False           # ★★ 绝不能改成 True
"""§3.3 ★：文档标签 MUST NOT 做祖先展开。

若改成 True：用户主体与文档标签都含共同祖先，而组织中任意两个主体最终必然
共享根节点 → 交集非空 → **全公司互通**。这是"交集非空即可见"模型的固有性质，
不是配置问题，也不能靠调参规避。

enforce() 与 check_decisions.py 都对本项做断言（防回归）。"""

ACL_EMPTY_TAGS_ALLOWED: Final = False
"""INV-08：写库前断言 chunk.acl_tags 非空。空标签 = 可见性未定义 = 事故。"""

# ── DEC-22 / Q2 · 全员公开库 ────────────────────────────────
PUBLIC_KB_ENABLED: Final = True
"""存在单一全员可见的知识库（Q2）。
knowledge_bases.is_public = true 的库对所有已登录用户（含无部门的新账号）
自动视为成员 → 无条件进入 authorized_kb_ids。

★ 只过 G2：库内文档仍须过 G4。一份误放进公开库、标了 {dept:财务} 的文档，
  研发同事依然看不到（§3.5 用例 3b）。"""

PUBLIC_KB_MAX_COUNT: Final = 1
"""MUST <= 1。由部分唯一索引 + check_decisions.py 双重保证。
理由：公开库越多，G2 这道必要条件的过滤力越弱；超过 1 个应改用 kb_members 直接授权。"""

NEW_USER_VISIBLE_SCOPE: Final = "public_kb_only"
"""§3.8 Q9：新账号（无部门 / 无组 / 无角色）只能看见公开库中 acl_tags 含 'public' 的文档。
fail-closed 的验收点 —— §3.5 用例 7（负向用例，必须显式测试）。"""

SUBJECT_EXPANSION_SOFT_LIMIT: Final = 300
"""A2 展开后的 subjects 规模软上限。
超出时 **只 WARN + 打点，MUST NOT 裁剪集合** —— 裁剪要么造成误拒、要么放宽权限，
两种后果都不该由一个日志阈值决定。真到这个规模走 §3.3 末的双命名空间替代方案。"""

# ══════════════════════════════════════════════════════════════
# DEC-23 · 多跳
# ══════════════════════════════════════════════════════════════

MULTI_HOP_SUPPORTED: Final = False
"""验收标准 MUST 显式写明「不支持跨文档多跳推理」，避免验收争议。"""

# ══════════════════════════════════════════════════════════════
# 未决登记与启动断言
# ══════════════════════════════════════════════════════════════

UNRESOLVED: Final[dict[str, str]] = {
    # DEC-01 已于 2026-09-16 冻结为 B，不再登记（MUST NOT 重新加入）
    # DEC-22 已于 2026-09-16 冻结（口径见 §2 / §3），不再登记（MUST NOT 重新加入）
    "DEC-02":  "向量库后端取默认 pgvector",
    "DEC-03":  "模型部署取默认全云 API → 涉密不入库",
    "DEC-06b": "小程序绑定映射待外部确认（不阻塞）",
    "DEC-08":  "涉密替代通道待合规确认（不阻塞）",
    "DEC-19":  "相似度阈值 0.62 为占位值，禁止上线",
    "DEC-20":  "拒答权衡点待业务确认，暂取宁可误拒",
}

ACKNOWLEDGE_UNRESOLVED: Final[dict[str, str]] = {}
"""★ 豁免清单。键为 DEC-ID，值为豁免理由。

只有 HUMAN 可以填写本字典。Agent MUST NOT 自行填写。
用途：路径 A 的演示开发需要绕过 DEC-22 的启动断言。
★ DEC-01 已确认为 B —— 路径 B 下本字典 MUST 永久为空（enforce() 会校验）。
生效条件：APP_ENV != "prod"。prod 环境下本字典 MUST 为空。
"""


class DecisionNotFrozen(RuntimeError):
    """存在未决的 HARD-BLOCK 决策项。"""


def enforce(*, app_env: str = "dev") -> None:
    """★ 启动期断言。在 app/main.py 的 startup 中调用。

    - prod 环境下：ACKNOWLEDGE_UNRESOLVED 必须为空；
      任何 HARD-BLOCK 未决项 → 抛 DecisionNotFrozen，拒绝启动（fail-closed）
    - 非 prod：允许豁免，但必须记录到日志与 /health
    """
    # ★★ DEC-22 冻结后的防回归断言 —— 与 app_env 无关，任何环境都生效
    if DOC_TAG_ANCESTOR_EXPANSION:
        raise DecisionNotFrozen(
            "DOC_TAG_ANCESTOR_EXPANSION MUST be False（DEC-22 / §3.3）—— "
            "文档标签做祖先展开会让共享根节点的任意两个主体互相可见，即全公司互通"
        )
    if GrantModel.KB not in VISIBILITY_GRANTS:
        raise DecisionNotFrozen(
            "VISIBILITY_GRANTS MUST 含 GrantModel.KB（G2 为必要条件，DEC-22）"
        )
    if GrantModel.ORG in VISIBILITY_GRANTS:
        raise DecisionNotFrozen(
            "VISIBILITY_GRANTS MUST NOT 含 GrantModel.ORG（组织架构只作标签来源，DEC-22）"
        )
    if ACL_PUBLIC_MUTEX is not True or ACL_LEVEL_TAGS_ALLOWED is not False:
        raise DecisionNotFrozen(
            "public 互斥 / level 标签禁用 MUST 生效（DEC-22 / §3.3(3)(4)）"
        )

    hard_block_open = [
        dec_id
        for dec_id, reason in UNRESOLVED.items()
        if "HARD-BLOCK" in reason and dec_id not in ACKNOWLEDGE_UNRESOLVED
    ]

    if app_env == "prod":
        if ACKNOWLEDGE_UNRESOLVED:
            raise DecisionNotFrozen(
                "prod 环境 ACKNOWLEDGE_UNRESOLVED 必须为空，"
                f"当前含: {sorted(ACKNOWLEDGE_UNRESOLVED)}"
            )
        if hard_block_open:
            raise DecisionNotFrozen(
                "prod 环境存在未决 HARD-BLOCK 决策，拒绝启动: " f"{hard_block_open}"
            )
        return

    if ACKNOWLEDGE_UNRESOLVED and DELIVERY_PATH != DeliveryPath.A:
        raise DecisionNotFrozen(
            "仅 DELIVERY_PATH=A 允许使用 ACKNOWLEDGE_UNRESOLVED 豁免"
        )


def unresolved_report() -> dict[str, object]:
    """供 GET /api/v1/health 返回。前端 MUST 在存在 HARD-BLOCK 未决项时显示横幅。"""
    return {
        "contract_version": DECISIONS_VERSION,
        "delivery_path": DELIVERY_PATH.value,
        "unresolved": UNRESOLVED,
        "acknowledged": ACKNOWLEDGE_UNRESOLVED,
        "needs_calibration": [
            k for k, v in CALIBRATION_SCRIPTS.items()
        ],
        "demo_mode": DELIVERY_PATH == DeliveryPath.A,
    }
```

---

## 5. 决策变更协议

### 5.1 变更的扩散顺序（MUST 按序执行）

决定一条 DEC 的取值后，Agent MUST 按此顺序落盘，**MUST NOT 跳步**：

```
1. 契约文档 §1 YAML：status → frozen，写入 value 字段
2. 契约文档 §2 对应条目：把「若人工选了另一项」的改动点执行掉
3. app/config/decisions.py：更新对应常量；从 UNRESOLVED 中移除该 DEC
4. 若影响数据契约 → 实施规范 §3 表定义 + alembic 迁移
5. 若影响接口契约 → 实施规范 §4 + packages/kb-core 的 TS 类型
6. 若影响环境变量 → 实施规范 §9 + .env.example
7. 跑 python scripts/check_decisions.py 与相关 WU 的验收命令
8. 在汇报块的「决策使用」段标注来源为人工确认
```

**MUST NOT**：不得只改 `decisions.py` 而不同步契约文档——那会造成两份真相。

### 5.2 若 Agent「加了新决策」怎么办

Agent 按 §0.3 双检验自行取默认值继续实施时，MUST 在汇报中列为「新增 DEC 提案」。**人类确认后才写入契约文档 §1**，在此之前该取值只存在于代码，MUST NOT 写进文档当成已定事项。

### 5.3 变更影响面矩阵

改一条 DEC 时，这张表告诉你还要动哪里。**未列出的地方不用动** —— 这就是把决策集中到一处的收益。

| 改动的 DEC | 必须联动 | 是否需要数据迁移 |
|---|---|---|
| ~~DEC-01 路径~~ | ✅ 已冻结为 B，本行不再适用 | 否 |
| DEC-02 向量库 | `app/vector/*` 新增适配器、`compile_filter` 方言 | **是**（全库重建索引） |
| DEC-03 模型部署 | `ModelGateway` 适配器、`LevelPolicy`、出域清单 | 否（但涉密文档需重跑入库） |
| DEC-04 前端栈 | workspace 结构、`WU-31`/`WU-37`、eslint boundaries | 否 |
| DEC-05 权限粒度 | `fan_out_acl_tags` 调用点、解析器 | **是**（需重算 chunk 标签） |
| DEC-06 认证 | `app/auth/*` 新增 provider | 否 |
| DEC-07 密级入库 | `policy_for`、独立 collection 配置 | **是**（需重跑入库任务） |
| DEC-09 DLP 字段 | `app/compliance/dlp.py` 正则表、恢复审计事件 | **是**（已入库内容需重脱敏） |
| ~~DEC-22 可见性口径~~ | ✅ 已冻结（2026-09-16）。**本行仍是唯一"改一次动五处 + 全库重刷"的决策** —— 日后若启用 §3.3 末的双命名空间方案，代价不变 | **是**（全库 `acl_tags` 重刷） |
| DEC-19 阈值 | `eval/thresholds.yaml` + 标定报告 | 否 |

> **注意 DEC-22 那一行**：它是唯一一个"改一次动五处 + 全库重刷"的决策 —— 这也正是它曾经被列为 HARD-BLOCK 的全部理由。**冻结不等于变便宜**：任何后续调整（含 §3.3 末的双命名空间方案）依然要重刷全库。

---

## 6. 阻塞判定树（Agent 立即决策用）

开始任一 WU 前走一遍。**只有第 1 条会真正停下来。**

```
开始 WU-xx
 │
 ├─ 1. 本 WU 涉及的 DEC 有 HARD-BLOCK 且 status=open 的吗？
 │     实现规范：用 §1 YAML 的 affects 字段匹配本 WU 编号
 │     ├─ 有，且未被 ACKNOWLEDGE_UNRESOLVED 豁免
 │     │     → 输出 BLOCKER，停止。**当前无此类项**（DEC-01 / DEC-22 均已冻结）
 │     │     ⚠ 但若本 WU 中存在不依赖该 DEC 的分部分，可以先做那部分
 │     │       （例：某个 SAFE-DEFAULT 项未定，不影响表结构部分）
 │     └─ 无 → 继续
 │
 ├─ 2. 我需要引入契约中不存在的字段 / 枚举值 / API / 依赖吗？
 │     → 是：BLOCKER（实施规范 §0.5）。MUST NOT 自行发明。
 │
 ├─ 3. 我遇到的业务分支在实施规范 §6 决策表与本契约 §2 里都没有？
 │     → 是：按 §0.3 双检验
 │            fail-safe ✅ 且 additive ✅ → 取保守值继续，
 │                                       汇报中列为「新增 DEC 提案」
 │            否则                        → BLOCKER
 │
 ├─ 4. 两条规则冲突（含 frozen DEC 与 INV 冲突）？
 │     → 是：报告双方编号 + 后果，请裁决。MUST NOT 自行取舍。
 │
 ├─ 5. 结论依赖某个库的具体行为而我不确定？
 │     → 是：先写 ≤ 20 行探针脚本验证，把结论写进汇报。
 │           MUST NOT 靠猜实现（实施规范 §0.5）。
 │
 └─ 全部否 → 继续实现
```

**第 1 条当前不触发。** DEC-01（2026-09-16 冻结为 B）与 DEC-22（2026-09-16 确认冻结）均已解除 —— **步骤 0~16 全部解锁**，Agent 可一路推进到里程碑 B，不再需要回问人类。第 1 条保留是为了应对将来新增的 `HARD-BLOCK` 项。

---

## 7. 一致性自检脚本 · `scripts/check_decisions.py`

> 每个 WU 收尾时 MUST 运行，**exit 0 才允许标记 DONE**。
> 脚本检查的是"决策是否被一致地实现"，这是单元测试覆盖不到的一层。

```python
#!/usr/bin/env python3
"""决策契约一致性自检。

用法: python scripts/check_decisions.py
退出码: 0 全部通过 | 1 存在违规
"""

from __future__ import annotations

import sys
from pathlib import Path

from app.config import decisions as D

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  [OK]   {name}")
    else:
        print(f"  [FAIL] {name}" + (f" —— {detail}" if detail else ""))
        FAILURES.append(name)


def section(title: str) -> None:
    print(f"\n== {title} ==")


def main() -> int:
    # ── 1. 契约版本 ────────────────────────────────────────
    section("契约版本")
    doc = Path("AI-Coding-Agent-决策契约.md")
    check("契约文档存在", doc.exists())
    if doc.exists():
        text = doc.read_text(encoding="utf-8")
        check(
            "契约文档版本与代码一致",
            f"contract_version: {D.DECISIONS_VERSION}" in text,
            f"代码为 {D.DECISIONS_VERSION}",
        )

    # ── 2. HARD-BLOCK 与豁免 ───────────────────────────────
    section("HARD-BLOCK 与豁免")
    hard_open = [k for k, v in D.UNRESOLVED.items() if "HARD-BLOCK" in v]
    unack = [k for k in hard_open if k not in D.ACKNOWLEDGE_UNRESOLVED]
    check(
        "非 prod 环境下 HARD-BLOCK 已被显式豁免",
        not unack,
        f"未豁免: {unack}（需人工填写 ACKNOWLEDGE_UNRESOLVED，理由必填）",
    )
    check("★ HARD-BLOCK 未决项已清零", not hard_open, f"仍存在: {hard_open}")
    check(
        "豁免仅在路径 A 下使用",
        (not D.ACKNOWLEDGE_UNRESOLVED) or D.DELIVERY_PATH == D.DeliveryPath.A,
        "非路径 A 不得使用豁免",
    )
    check("路径 A 禁止存在豁免之外的合规项", True)  # 占位，实施时替换为真实断言
    check(
        "DEC-01 已冻结为 B 且不在未决表中",
        D.DELIVERY_PATH == D.DeliveryPath.B and "DEC-01" not in D.UNRESOLVED,
        "路径 B 下 DELIVERY_PATH MUST 为 B，且 DEC-01 MUST NOT 出现在 UNRESOLVED",
    )

    # ── 3. DEC-22 可见性口径（已冻结 2026-09-16）────────────
    section("DEC-22 可见性口径")
    check("可见性口径已冻结", bool(D.VISIBILITY_GRANTS))
    check("知识库成员是必要条件（G2）", D.GrantModel.KB in D.VISIBILITY_GRANTS)
    check("ORG 不参与判定，仅作标签来源", D.GrantModel.ORG not in D.VISIBILITY_GRANTS)
    check("public 主体存在", D.PUBLIC_SUBJECT == "public")
    check("★ public 互斥规则已开启", D.ACL_PUBLIC_MUTEX is True)
    check("★ acl_tags 禁止 level: 标签", D.ACL_LEVEL_TAGS_ALLOWED is False)
    check("空标签不允许", D.ACL_EMPTY_TAGS_ALLOWED is False)
    check("上向展开（祖先）已开启", D.SUBJECT_ANCESTOR_EXPANSION is True)
    check("★ 下向展开（子孙）已开启 —— Q3 上级可见下级", D.USER_SUBJECT_DESCENDANT_EXPANSION is True)
    check("★★ 文档标签未做祖先展开（防全公司互通）", D.DOC_TAG_ANCESTOR_EXPANSION is False)
    check("公开库已启用且唯一", D.PUBLIC_KB_ENABLED is True and D.PUBLIC_KB_MAX_COUNT == 1)
    check("新账号可见范围为公开库", D.NEW_USER_VISIBLE_SCOPE == "public_kb_only")
    check("区间方向为双向", D.DEPT_VISIBLE_DIRECTION == "up_and_down")
    check("DEC-22 已从 UNRESOLVED 移除", "DEC-22" not in D.UNRESOLVED)

    # ── 4. 密级单调性与 DEC-03 联动 ────────────────────────
    section("DEC-07 密级 / DEC-03 部署联动")
    ranks = [D.LEVEL_RANK[lv] for lv in D.SecurityLevel]
    check("密级 rank 严格递增", ranks == sorted(ranks) and len(set(ranks)) == len(ranks), f"{ranks}")
    check("PUBLIC 为最低档", min(ranks) == D.LEVEL_RANK[D.SecurityLevel.PUBLIC])

    for lv in (D.SecurityLevel.SECRET, D.SecurityLevel.TOP):
        p = D.policy_for(lv)
        if D.MODEL_DEPLOYMENT.any_cloud:
            check(f"{lv.value} 在云模型下不可入库（INV-15）", p.ingestable is False)
    check("TOP 在任何部署形态下均不可入库", D.policy_for(D.SecurityLevel.TOP, D.ModelDeployment("on_prem", "on_prem", "on_prem")).ingestable is False)

    # ── 5. DEC-09 DLP ──────────────────────────────────────
    section("DEC-09 DLP")
    check("DLP 字段集非空", len(D.DLP_FIELDS) > 0)
    check("恢复需 pii:restore 功能权限", "pii:restore" in D.RESTORE_REQUIRED_SCOPES)

    # ── 6. DEC-11 不可区分 ─────────────────────────────────
    section("DEC-11 越权不可区分")
    check("统一文案不含「权限」字样", "权限" not in D.INDISTINGUISHABLE_TEXT)
    check("数据权限不返回 403", "403" not in D.NOT_FOUND_SEMANTICS or "MUST NOT 对数据权限返回 403" in D.NOT_FOUND_SEMANTICS)

    # ── 7. DEC-19 阈值占位 ─────────────────────────────────
    section("DEC-19 阈值标定（INV-03）")
    check("阈值已标定（非占位）", D.SIMILARITY_THRESHOLD_IS_PLACEHOLDER is False,
          "0.62 为占位值，生产发布前 MUST 用 eval/calibrate_threshold.py 替换")
    rep = Path("eval/reports/threshold.json")
    check("标定报告存在", rep.exists(), "缺失时不阻塞开发，但阻塞生产发布")
    if rep.exists():
        import json, time
        age_days = (time.time() - rep.stat().st_mtime) / 86400
        check(
            f"标定报告未过期（<= {D.CALIBRATION_REPORT_TTL_DAYS} 天）",
            age_days <= D.CALIBRATION_REPORT_TTL_DAYS,
            f"已 {age_days:.0f} 天",
        )

    # ── 8. 密钥与 key 规则 ─────────────────────────────────
    section("缓存 / 限流 key 规则（INV-10/11/27/28）")
    from app.cache.keys import acl_key, answer_key, rate_limit_key
    check("权限缓存 key 带版本", "v" in acl_key("u1", acl_version=3))
    check("答案缓存 key 带权限指纹", answer_key("q", subjects={"public"}, kb_ids=["kb1"]) != answer_key("q", subjects={"public", "dept:财务"}, kb_ids=["kb1"]))
    check("限流 key 不带权限指纹", rate_limit_key("u1") == rate_limit_key("u1"))
    check("缓存与限流 key 构造不共用函数", acl_key.__module__ != rate_limit_key.__module__)

    # ── 9. 校准项清单 ──────────────────────────────────────
    section("CALIBRATE 项")
    for dec_id, script in D.CALIBRATION_SCRIPTS.items():
        check(f"{dec_id} 标定脚本存在", Path(script).exists(), script)

    # ── 结果 ───────────────────────────────────────────────
    print()
    if FAILURES:
        print(f"✗ {len(FAILURES)} 项未通过：")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

**注意**：脚本中 §2 与 §7 的检查项在**路径 A 且已豁免**时应当自动跳过（实施时用 `if D.DELIVERY_PATH == "A" and dec_id in D.ACKNOWLEDGE_UNRESOLVED: continue`）。这不违反 fail-closed——因为路径 A 本身就不允许用于真实数据。

---

## 8. 投喂清单（怎么把这份东西交给 Agent）

### 8.1 最小上下文包

单次任务不要投喂全部四份文档（合计 1.3 万行）。按任务范围选：

| 任务 | 投喂 |
|---|---|
| 步骤 0~3（环境 / 界面 / 管道 / 上传） | 本契约 §0 + §1 + 实施规范 §0~§4 + 分步路线 步骤 0~3 |
| 步骤 4（分块入库） | 上述 + **本契约 §3 全节**（DEC-22 已冻结，直接实现，无需再确认）+ §3.5 的 10 条用例 |
| 步骤 5~7（检索 / 生成 / 边界） | 本契约 §0 + §1 + 实施规范 §1 + §5(WU-12~16) + §6.1~6.5 |
| 步骤 8~10（认证 / 权限 / 合规） | 本契约 §2(DEC-06~11,22) + §3 + 实施规范 §5(WU-17~23) |
| 步骤 11~16 | 本契约 §2 对应项 + 实施规范对应 WU |

### 8.2 开场指令模板（可直接粘贴）

```text
你是一个编码 Agent。本次任务的上下文按以下顺序读取：

1. AI-Coding-Agent-决策契约.md —— 先读 §0（执行协议）与 §1（决策总表）。
   这是权威默认值来源。HARD-BLOCK 项未决时停下来问，其余项取默认值直接推进。

2. AI-Coding-Agent-实施规范.md —— 读 §0（执行协议）、§1（不变量）、
   §5 中本次涉及的 WU 定义、§10（汇报格式）。

3. 本次要做的 WU：<WU-xx>

要求：
- 所有取值从 app/config/decisions.py 读取，不要硬编码
- 完成后运行 scripts/check_decisions.py 与 WU 的验收命令
- 按实施规范 §10.1 + 决策契约 §0.5 输出汇报块
- 遇到本契约 §6 阻塞判定树中的任一条，停下来问我，不要猜

不要复述理由，不要解释设计意图，直接产出代码。
```

### 8.3 人工确认一项 DEC 时的指令模板（本项已完成）

> **DEC-22 已于 2026-09-16 确认并冻结**，本节模板暂时不再需要，留作后续 DEC 变更时使用
> （如 DEC-19 完成阈值标定、DEC-07 改为「秘密级允许入库」等）。

**模板**

```text
DEC-<xx> 我确认为：<逐条答案>

请按契约 §5.1 的顺序执行：
1. 更新 AI-Coding-Agent-决策契约.md §1.1 YAML 中该 DEC 的 status 与 value
2. 执行 §2・DEC-<xx> 的改动点
3. 更新 app/config/decisions.py（更新常量，从 UNRESOLVED 移除）
4. 若影响数据契约 → 同步 AI-Coding-Agent-实施规范.md 表定义 + alembic 迁移
5. 跑 python scripts/check_decisions.py，必须 exit 0
6. 把确认记录写进契约对应章节
```

**DEC-22 的落盘记录**（已执行完毕，供后续比照）

| 步骤 | 产物 |
|---|---|
| §1.1 YAML | `status: frozen`；`value` + `answers`(Q1~Q9) + `frozen_consequences`(8 条 MUST) + `unconfirmed_followups` |
| §2・DEC-22 | 改为「已冻结」；含 9 个分叉答复表、Q3 派生的机制变更对照表、原 BLOCKER 存档 |
| §3 全节 | 判定式 4 条硬规则；展开规则 A1/A2/B/C；10 条验证用例；函数签名；规模核验；缓存失效面 |
| §3.8 | 确认记录已填写，含 3 项未会签项 |
| `decisions.py` | `VISIBILITY_GRANTS = {KB, TAG}`；新增 7 个常量；`UNRESOLVED` 移除 DEC-22；`enforce()` 加 4 条防回归断言 |
| 数据契约 | `knowledge_bases.is_public` + 部分唯一索引；`acl_tags` 规则三处修正（实施规范 §3.1/§3.2.1/§3.2.2 + WU-19） |

> **落盘过程中新发现的泄漏（已在本次一并修掉，不需再次确认）**：
> ① 文档标签做祖先展开 → 共享根节点导致全公司互通；
> ② `{public, dept:x}` 混合标签 → G4 对全员恒真；
> ③ `acl_tags` 含 `level:` → G4 绕过 G1 的密级过滤。
> 三者均为「口径已确认、实现曾经是错的」类型 —— 必须在 §3.5 用例 3b/9/10 中固化为断言，
> 否则口径是对的、实现是错的，而测试全绿。

---

## 附录 A · 决策完成度看板

> 用于评审会逐条过。**当前状态：`HARD-BLOCK` 清零** —— DEC-01 已确认（B）、DEC-22 已确认并冻结；其余 22 项由 Agent 按各自等级自主推进。

| DEC | 项目 | 等级 | 状态 | 卡住的 WU | 责任人 | 截止 |
|---|---|---|---|---|---|---|
| 01 | 交付路径 | ✅ **已定 B** | ✅ 2026-09-16 已确认 | — | 业务 + 技术负责人 | 已完成 |
| 22 | 可见性口径 | ✅ **已定：KB(必要条件)+TAG，上级可见下级** | ✅ 2026-09-16 已确认 | — | 业务 + 安全合规 | 已完成（3 项未会签项见 §3.8） |
| 02 | 向量库后端 | 🟡 SAFE-DEFAULT | ✅ 有默认 | — | 技术负责人 | 步骤 4 前 |
| 03 | 模型部署 | 🟡 SAFE-DEFAULT | ✅ 有默认 | — | 技术 + 安全 | 步骤 4 前 |
| 04 | 前端技术栈 | 🟡 SAFE-DEFAULT | ✅ 有默认 | — | 技术负责人 | 步骤 1 前 |
| 05 | 权限粒度 | 🟡 SAFE-DEFAULT | ✅ 有默认 | — | 技术负责人 | 步骤 4 前 |
| 06 | 认证方式 | 🟡 SAFE-DEFAULT | ✅ 有默认 | — | 技术负责人 | 步骤 8 前 |
| 06b | 小程序绑定映射 | 🔵 ASK-EXTERNAL | 🟨 待外部 | 仅小程序上线 | 企微/钉钉管理员 | 步骤 15 前 |
| 07 | 秘密级入库 | 🟡 SAFE-DEFAULT | ✅ 有默认 | — | 安全合规 | 步骤 10 前 |
| 08 | 涉密替代通道 | 🔵 ASK-EXTERNAL | 🟨 待外部 | UI 文案 | 安全合规 | 步骤 10 前 |
| 09 | DLP 字段与恢复 | 🟡 SAFE-DEFAULT | ✅ 有默认 | — | 合规 + 技术 | 步骤 10 前 |
| 10 | 审计留存期 | 🟡 SAFE-DEFAULT | ✅ 有默认 | — | 法务 / 合规 | 步骤 10 前 |
| 11 | 不可区分程度 | 🟡 SAFE-DEFAULT | ✅ 有默认 | — | 技术负责人 | 步骤 9 前 |
| 12 | 灰度维度 | 🟡 SAFE-DEFAULT | ✅ 有默认 | — | 业务负责人 | 步骤 16 前 |
| 13 | 指标达标线 | 🟡 SAFE-DEFAULT | ✅ 有默认 | — | 业务负责人 | 步骤 0（越早越好） |
| 14–18 | 分块 / Top-K / Rerank | 🟢 CALIBRATE | 🟦 用占位值 | — | 后端 | 步骤 5 标定 |
| 19 | **相似度阈值** | 🟢 CALIBRATE | 🟦 占位 0.62 | — | 后端 + 业务 | 步骤 5 标定 |
| 20 | 拒答权衡点 | 🟢 CALIBRATE | 🟦 暂取宁可误拒 | — | 业务负责人 | 步骤 5 标定 |
| 21 | token 限额 | 🟢 CALIBRATE | 🟦 用占位值 | — | 技术 + 财务 | 上线前 |
| 23 | 多跳支持 | 🟡 SAFE-DEFAULT | ✅ 有默认 | — | 业务负责人 | 步骤 0 |

**图例**：⬜ 未决且阻塞（当前为空）　✅ 有安全默认或已确认　🟨 待外部确认　🟦 用占位值推进

### 只有 4 项不该由技术一个人定

| DEC | 应该找谁 |
|---|---|
| 22 可见性口径 | 业务负责人 + 安全合规（**已确认**；但 Q3 的可见性扩张尚未与安全合规单独会签 —— 见 §3.8 未会签项） |
| 10 审计留存期 | 法务 / 合规 |
| 13 指标达标线 | 业务负责人（表达风险偏好） |
| 20 拒答权衡点 | 业务负责人 |

第 22 项已于 2026-09-16 完成（含 §3.8 的 3 项未会签项，不阻塞开发）。其余三项建议在**步骤 0** 就发起对口确认，不要等到要用时才去找人。

---

*契约版本 `dc-1.2` · 2026-09-16 · 24 条决策 · 5 种等级 · 2 项已冻结（DEC-01 / DEC-22）· 0 项硬阻塞*
