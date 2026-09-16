# 知识库问答 Agent · AI Coding 开发手册

> **版本** `v1.0` · **2026-09-16** · **单人开发** · **自包含**
>
> 本文档是**唯一交付物**。交给 AI Coding 后，它应当能在**不反问**的情况下开工。
> 凡是本文档没有写死的地方，全部集中列在 **§8 决策清单**，由张枭拍板。
>
> **不要引用外部文件。** 本文档之外没有需要读的东西。

---

## 0. 怎么用这份文档

### 0.1 三条元规则（先读，违反即视为交付失败）

| # | 规则 | 为什么 |
|---|---|---|
| **R1** | **§3 的口径是冻结的。** AI MUST NOT 自行"优化"权限判定、检索阈值、分块参数、错误降级策略。要改 → 先改本文档，再改代码。 | 这类参数**错了不报错**，只是结果不对。AI 一"优化"就再也没人知道它改了哪 |
| **R2** | **本文档没写的，MUST NOT 自己发明。** 遇到未覆盖的分叉 → **停下来，输出问题**，不要选一个"看起来合理"的默认值。 | 单人项目没有 Code Review 兜底，AI 的自作主张没人会发现 |
| **R3** | **每个里程碑必须先跑通验收测试，再进下一个。** 红灯不许前进。 | 单人开发最大的失败模式是"处处半成品"，最后没有一处能演示 |

**R2 是关键。** 一个会按 R2 停下来的 AI，比一个"什么都能写"的 AI 有价值得多。它多问的那几次，就是你的 Code Review。

### 0.2 阅读顺序

```
0  元规则 + 术语 + 硬约束索引     ← 每次开工前读一遍
1  产品定义与范围                 ← 决定"做什么/不做什么"
2  技术选型                       ← 已定，含理由
3  全局口径（冻结）★              ← 代码里所有常量的来源
4  数据契约                       ← 建表直接抄
5  接口契约                       ← 写 API 直接抄
6  里程碑路线 M0~M6 ★             ← 施工顺序
7  验收与测试                     ← 每个里程碑的"完成"定义
8  决策清单 ★                     ← 已定稿（16 条）
9  单人开发的现实提醒              ← 排期与取舍
```

> **配套代码**：同目录 `code/` 是 §3.5 常量表与 §7.2 十条用例的**可运行骨架**（19 条测试全绿）。
> 开 M4 之前先跑通它，再让 AI 把内存实现换成数据库实现。

### 0.3 术语表

| 术语 | 含义 |
|---|---|
| **chunk** | 文档分块后的最小检索单元，**权限判定的粒度就是 chunk** |
| **acl_tags** | 挂在 chunk 上的主体标签数组，检索时下推到向量库过滤 |
| **user_subjects** | 用户解析出来的主体集合（`dept:` / `user:` / `group:` / `role:` / `region:` / `public`）|
| **clearance** | 用户的密级数值（`level_rank`），与文档密级比大小 |
| **acl_epoch** | 权限缓存代际，任何权限相关变更 +1，用于全局失效 |
| **tenant** | 租户。私有化部署下恒为 1 个；SaaS 下每客户一个 |
| **闸门 G1~G4** | 可见性判定的四个条件，见 §3.2 |
| **三层约束** | 回答边界的 L1 检索闸门 / L2 生成引用 / L3 出口校验，见 §3.1 |

### 0.4 全文硬约束索引（代码里必须有对应断言）

| # | 约束 | 出处 |
|---|---|---|
| H1 | `DOC_TAG_ANCESTOR_EXPANSION = False` —— 文档标签 MUST NOT 展开祖先 | §3.2.3 |
| H2 | `ACL_PUBLIC_MUTEX = True` —— `public` MUST NOT 与其他标签共存 | §3.2.4 |
| H3 | `ACL_LEVEL_TAGS_ALLOWED = False` —— `acl_tags` MUST NOT 含 `level:` | §3.2.4 |
| H4 | 闸门 G1~G4 MUST 是 **AND**，MUST NOT 是 OR | §3.2.1 |
| H5 | `level_rank = (level + 1) * 10`（10/20/30/40），API 层 enum 是 0~3 | §3.2.5 |
| H6 | 部门树**结构变更** MUST 触发 `acl_epoch += 1`（全局失效） | §3.2.6 |
| H7 | 检索 MUST 带 `tenant_id` 与 `acl_tags` 下推过滤，MUST NOT 先取后过滤 | §3.3.2 |
| H8 | `public` 知识库全局最多 1 个（部分唯一索引） | §3.2.7 |
| H9 | 生成层 MUST 只依据检索到的 chunk，MUST NOT 依赖模型自身知识 | §3.1 |
| H10 | 出口层 MUST 做 grounding 校验，无法归因的句子 MUST 被剥离 | §3.1 |

---

## 1. 产品定义与范围

### 1.1 一句话定位

> **一个可私有化交付的企业知识库问答系统：上传文档 → 建索引 → 按权限问答，答案只来自知识库，每句可溯源。**

**交付形态**：可交付产品（对外给别人用）。
**默认形态**：**私有化交付**——每个客户一套部署、一个租户（§8 D-01）。
**目标客户**：**中小企业（50–500 人）**——有组织分层、有文档权限诉求，但没有专职运维（§8 D-02）。
**代码形态**：**从第一天支持多租户**（`tenant_id` 出现在所有业务表），私有化交付时恒为单租户。

> 为什么现在就加 `tenant_id`：现在加是几行 DDL + 一个中间件；以后加是全库迁移 + 所有查询重写 + 所有测试重跑。**这是全文唯一一处"提前投入"**，其余一律不做超前设计。

### 1.2 用户角色

| 角色 | 能做什么 | 界定方式 |
|---|---|---|
| **系统管理员** | 建租户、建知识库、管用户与组织、配模型、看审计 | `role:admin` |
| **知识库管理员** | 管自己负责的知识库：上传、删除、改标签、配成员 | 知识库 `owner` |
| **普通用户** | 提问、看答案与引用、看自己有权限的文档 | 默认 |
| **外部人员** | 走独立用户组，显式授权，**不给部门标签、不给密级** | `group:external_*` |

### 1.3 功能清单

**P0 · 没有它产品不成立**

| # | 功能 | 里程碑 |
|---|---|---|
| F01 | 文档上传（PDF / md / txt / xls / xlsx）· 单文件 ≤ **100 MB**、**不支持扫描件**（§8 D-04 / D-16） | M2 |
| F02 | 解析 → 分块 → 向量化 → 入库 | M2 |
| F03 | 混合检索（向量 + 关键词）→ 重排 → 阈值闸门 | M2 |
| F04 | 生成回答 + 引用溯源（可点开定位原文） | M2 |
| F05 | **回答边界三层约束**（只依据知识库作答） | M2 |
| F06 | 账号与登录（自建账号，预留 SSO） | M3 |
| F07 | 用户 / 用户组 / **部门树** / 角色管理 | M3 |
| F08 | 知识库成员管理（用户 / 部门 / 用户组 / 角色四种主体） | M4 |
| F09 | **chunk 级权限过滤（G1~G4）** | M4 |
| F10 | **公开知识库**（全员可见，全局唯一） | M4 |
| F11 | 权限变更的缓存失效 | M4 |

**P1 · 产品能卖需要它**

| # | 功能 | 里程碑 |
|---|---|---|
| F12 | 密级（4 档）与脱敏 | M5 |
| F13 | 审计日志（谁问了什么、看到了什么引用） | M5 |
| F14 | 多租户与私有化部署包 | M5 |
| F15 | 知识库数据源同步（定时/增量）· 首个源 = **本地目录 + Git**（§8 D-07） | M6 |
| F16 | 多轮对话与上下文治理 | M6 |
| F17 | 安全加固（提示注入、限流、配额） | M6 |

**P2 · 以后再说**

| # | 功能 |
|---|---|
| F18 | Agent 工具化（让模型调用外部工具） |
| F19 | 多端适配（移动端 / 嵌入 SDK） |
| F20 | 可观测面板 + 评估门禁接入 CI |
| F21 | 计费与授权 |

### 1.4 明确不做（Non-Goals）

写下来是为了让 AI **不要去做**：

| 不做 | 原因 |
|---|---|
| ❌ 通用聊天机器人（脱离知识库自由对话） | 产品定位就是"只依据知识库作答"。一旦开口子，全部约束失效 |
| ❌ **扫描件 PDF 的 OCR**（§8 D-04） | OCR 牵扯版面还原、表格识别、图片内文字、准确率调优，**是一个独立产品的量级**。接了它 M2 会从 2–3 周变成 2–3 个月。产品说明里 MUST 写明「仅支持文本层 PDF」 |
| ❌ 自研大模型 / 自训 embedding | 不是这个产品的价值所在 |
| ❌ 图数据库 / 知识图谱 | 单人无法维护，收益不确定 |
| ❌ 微服务拆分 | 单人项目拆服务 = 自己给自己加运维 |
| ❌ 自研前端组件库 | 用 Tailwind + 少量 headless 组件 |
| ❌ K8s | 私有化交付用 Docker Compose 就够 |
| ❌ 实时协同编辑文档 | 不是这个产品要做的事 |

---

## 2. 技术选型

### 2.1 总览

```
┌──────────────────────────────────────────────────────────────┐
│  Web 前端   React 18 + TS + Vite + Tailwind + TanStack Query  │
└───────────────────────────┬──────────────────────────────────┘
                            │ HTTPS / JSON + SSE
┌───────────────────────────▼──────────────────────────────────┐
│  API 层     Python 3.12 + FastAPI + Pydantic v2              │
│             ├── 认证鉴权（JWT + 主体解析）                     │
│             ├── 权限服务（user_subjects / 缓存 / acl_epoch）    │
│             └── 问答编排（检索 → 闸门 → 生成 → 出口校验）        │
└──────┬──────────────┬───────────────┬────────────┬───────────┘
       │              │               │            │
┌──────▼─────┐ ┌──────▼──────┐ ┌──────▼──────┐ ┌───▼─────────┐
│ PostgreSQL │ │  pgvector   │ │    Redis    │ │  对象存储    │
│  业务数据   │ │  向量索引    │ │ 缓存 + 队列  │ │  原始文件    │
└────────────┘ └─────────────┘ └─────────────┘ └─────────────┘
                            │
                    ┌───────▼────────┐
                    │  异步 Worker    │
                    │  解析/分块/嵌入  │
                    └───────┬────────┘
                            │ OpenAI 兼容协议
                    ┌───────▼────────────────────┐
                    │  LLM / Embedding / Rerank  │
                    │  （可指向云 API 或本地 vLLM） │
                    └────────────────────────────┘
```

### 2.2 选型表（已定）

| 层 | 选型 | 为什么是它 |
|---|---|---|
| 前端 | React 18 + TypeScript + Vite | 生态最全；Vite 冷启动快，单人开发迭代舒服 |
| 样式 | TailwindCSS + Headless UI | 不写 CSS 文件，样式跟着组件走 |
| 数据层 | TanStack Query + zustand | 服务端状态与客户端状态分开，少写一堆 loading 逻辑 |
| 后端 | Python 3.12 + FastAPI | 异步原生、Pydantic 校验强、RAG 生态在 Python |
| ORM | SQLAlchemy 2.0 + Alembic | 类型友好；Alembic 管迁移，多租户加字段时救命 |
| 主库 | PostgreSQL 16 | 一个库同时当业务库 + 向量库，**少运维一套** |
| 向量 | **pgvector** | 起步够用，和业务数据同一事务（权限过滤能下推）。规模上来了再换 Qdrant，接口抽象好即可 |
| 缓存 | Redis 7 | 权限缓存 + 会话 + 限流 |
| 队列 | Redis + **arq** | 单人不上 Celery：arq 是 asyncio 原生，代码量少一半 |
| 解析 | pypdf / python-docx / openpyxl / markdown-it | 按扩展名分派，不要一上来上 unstructured |
| 分词计数 | tiktoken | 分块长度按 token 算，不按字符算 |
| 中文全文检索 | **先降级 `ILIKE`**（已定 · §8 D-06） | PG 默认分词器不认中文（会把整句话当一个词）。`zhparser` / `pg_jieba` 效果最好但客户环境常装不了（要编译、要超级用户）；先按 `ILIKE` 跑通，**M2 用评估集对比后再决定是否升级**。关键是抽象成接口，换实现不动上层 |
| LLM | **OpenAI 兼容协议**（已定 · §8 D-08） | 一套代码可指向 DeepSeek / Qwen / 本地 vLLM / Azure。**不锁定厂商**；开发用云 API，交付可切本地 |
| Embedding | **本地 `bge-m3`，1024 维**（已定 · §8 D-05） | 中文效果好、支持长文本、可离线交付（私有化客户内网常出不去）。**维度写死在 `chunks.embedding vector(1024)`，换模型 = 重灌全库**，所以 M0 就要定死 |
| 重排 | **本地 `bge-reranker-v2-m3`**（已定 · §8 D-09） | 检索质量提升最明显的一环，必须有 |
| 部署 | Docker Compose · **两套配置：全云 / 全本地**（§8 D-14） | 私有化交付标配，一条命令起全套 |
| 观测 | 结构化日志 + OpenTelemetry（P2） | 先有日志，别一上来上全套 |

### 2.3 明确不选（及理由）

| 不选 | 理由 |
|---|---|
| LangChain / LlamaIndex 全家桶 | **这是核心判断。** RAG 的链路本身不到 500 行代码；上框架的代价是调试时要在框架源码里找 bug，且升级一次 API 变一次。**自己写编排，用库只在解析和调用层** |
| Milvus / Weaviate / Qdrant（起步阶段） | 多一个要运维的组件。pgvector 在百万级 chunk 内够用，且**权限过滤能和 SQL 事务一起做** |
| Celery | 单人项目不需要它的复杂度 |
| 自研 Agent 框架 | 不是这个产品的价值所在 |
| 前端 SSR（Next.js） | 这是一个登录后使用的工具，SSR 带来的复杂度换不到收益 |

---

---

## 3. 全局口径（冻结 · AI MUST 按此实现）

> 本节是**全文最重要的部分**。它决定了"谁能看到什么"和"什么时候必须拒答"。
> 这里的每一条都不是建议，是口径。改动流程见 §0.1 R1。

### 3.1 回答边界 · 三层约束

**产品承诺：答案只来自知识库。** 只做一层挡不住，必须三层同时成立。

```
用户提问
   │
   ├─ L1 检索闸门 ────────────────────────────────────────────
   │    检索 → 重排 → 取最高重排分
   │    若 最高分 < RELEVANCE_THRESHOLD
   │      → 直接拒答：「知识库中未找到相关内容」
   │      → MUST NOT 送进大模型
   │
   ├─ L2 生成约束 ────────────────────────────────────────────
   │    提示词 MUST 包含：
   │      · 只能使用 <context> 中的内容
   │      · 每个结论后 MUST 附 [n] 引用编号
   │      · 上下文不足时 MUST 回答「知识库中未找到相关内容」
   │      · MUST NOT 使用自身知识补充
   │    生成参数：temperature = 0.1（低随机性，减少自由发挥）
   │
   └─ L3 出口校验（grounding）─────────────────────────────────
        对生成结果做归因检查：
          · 每个 [n] 必须指向真实存在的 chunk
          · 无法归因的句子 → 剥离，并记录
          · 若剥离后有效内容为空 → 降级为拒答
        L3 是最后一道闸。MUST NOT 省略，MUST NOT 只记日志不剥离。
```

**为什么三层缺一不可**

| 缺哪层 | 后果 |
|---|---|
| 缺 L1 | 知识库里没有的问题，模型会用自己知识编一个像样的答案 |
| 缺 L2 | 模型会"补全"知识库里没说的部分 |
| 缺 L3 | 上面两层都是"提示词约束"，提示词会被绕过（长上下文、对抗输入） |

> **L3 是最容易被省掉的一层**，因为它需要额外的模型调用或规则判断。但它是唯一**不依赖模型配合**的一层。省掉它，前两层就是纸糊的。

### 3.2 权限口径（DEC-22 · 已冻结）

#### 3.2.1 判定式

**权限判定的粒度是 chunk，不是文档。** 一份文档可以同时存在不同密级、不同部门可见性的 chunk。

```
visible(chunk, user) :=
      chunk.deleted_at IS NULL
  AND chunk.is_latest = true
  AND chunk.tenant_id = user.tenant_id                  -- 租户隔离
  AND chunk.level_rank <= user.clearance                -- 闸门 G1 · 密级
  AND chunk.kb_id IN user.authorized_kb_ids             -- 闸门 G2 · 知识库（必要条件）
  AND NOT (chunk.deny_subjects ∩ user.subjects ≠ ∅)     -- 闸门 G3 · 显式拒绝（优先）
  AND (chunk.acl_tags ∩ user.subjects ≠ ∅)              -- 闸门 G4 · 标签匹配
```

**四条硬规则**

| # | 规则 | 理由 |
|---|---|---|
| 1 | **G3（deny）优先级最高** —— 命中即不可见 | 离职冻结、合规调查期间临时收回必须立即生效，不能被任何 allow 覆盖 |
| 2 | **四个闸门之间是 AND，不是 OR** | 若 G2 与 G4 是 OR，一份误标 `public` 的财务文档放进公开库就等于全公司泄漏 |
| 3 | **空 `acl_tags` ≠ "对所有人可见"** | 空标签在入库时规范化为 `{public}`，而 `public` 仍是常规主体，仍须过 G2 |
| 4 | **文档标签 MUST NOT 做祖先展开** | 做错不是"多看见一点"，而是**全公司互通**，见 3.2.3 |

> **`deny_subjects` 的运维责任人（§8 D-13 · 已定）**：交付后由**客户系统管理员**负责增删，MUST 写进交付文档与管理员手册。M5 MUST 提供 deny 的界面入口 + 操作审计（`audit_logs.action = 'acl.deny.*'`）。
> 理由：deny 是「立即收回」通道（离职冻结、合规调查），**延迟生效等于没有**。

#### 3.2.2 主体与标签

**主体（subject）有 6 种**，统一用 `<类型>:<值>` 表示：

| 类型 | 写法 | 说明 |
|---|---|---|
| 用户 | `user:<user_id>` | 具体某个人 |
| 用户组 | `group:<group_id>` | 手工维护的集合（外部人员走 `group:external_*`） |
| 部门 | `dept:<路径>` | 组织树上的路径，如 `dept:总部/技术中心/后端组` |
| 角色 | `role:<角色名>` | 职能角色，如 `role:admin`、`role:hr` |
| 区域 | `region:<区域码>` | 仅当业务需要时使用 |
| 公开 | `public` | 所有人（含新账号、含外部人员）。**单例，无值** |

**两个方向的数据**

| 方向 | 字段 | 存在哪 |
|---|---|---|
| 用户侧 | `user.subjects` | 登录时解析 + 缓存，随权限变更失效 |
| 文档侧 | `chunk.acl_tags` / `chunk.deny_subjects` | **入库时写入**，检索时下推过滤 |

> `chunk.acl_tags` **MUST** 在入库时算好落盘。**MUST NOT** 在检索时 JOIN 权限表实时计算——那样既要多一次查询，又会把权限逻辑散到各处。

#### 3.2.3 ★ 展开规则（本手册最容易写错的地方）

**两侧的展开方向是刻意不对称的。**

| | 用户主体 `user.subjects` | 文档标签 `chunk.acl_tags` |
|---|---|---|
| `dept:` | ✅ **祖先 ∪ 自己 ∪ 子孙**（双向，A1+A2） | ✅ **仅自己所属那一条路径** |
| `user:` / `group:` / `role:` / `region:` | ✅ 原样，不展开 | ✅ 原样，不展开 |
| `public` | ✅ 所有登录用户都有 | ✅ 原样 |

**用户侧为什么要双向展开（对应"上级部门能看到下级部门的文档"）**

```
用户在 dept:总部/技术中心/后端组
user.subjects 的 dept 部分 =
    { dept:总部,
      dept:总部/技术中心,
      dept:总部/技术中心/后端组,          ← 自己
      dept:总部/技术中心/后端组/*,        ← 子孙（A2）
      dept:总部/技术中心/前端组,          ← 子孙（A2）
      dept:总部/技术中心/测试组 }         ← 子孙（A2）
```

**文档侧为什么绝不能展开祖先 —— 这是全篇最危险的一条**

假设两侧都向上展开祖先：

```
财务部文档 acl_tags = { dept:总部, dept:总部/财务部 }        ← 展开祖先的后果
研发部用户 subjects = { dept:总部, dept:总部/研发部, ... }

两者交集 = { dept:总部 }  ≠ ∅  →  研发部能看到财务部文档
```

再推一步：**任意两个主体最终都会在根节点相交**，于是 `acl_tags ∩ user_subjects` 恒非空，
**G4 对所有文档、所有用户都成立** —— 权限体系等于不存在，全公司互通。

> **这不是"宽松一点"，是失效。** 所以：
> `DOC_TAG_ANCESTOR_EXPANSION = False`，必须有单元测试锁死（用例 10）。

**兄弟部门不可见**（推论，必须有测试）

```
用户 = 后端组 → subjects 含 后端组 及其子孙，但 不含 前端组
文档 = 前端组 → acl_tags = { dept:总部/技术中心/前端组 }
交集 = ∅  →  不可见   ✅ 正确
```

**敏感部门例外（§8 D-12 · 已定）**

默认**全局生效**：任何部门都能被其祖先链上的用户看到。但 `departments` 表预留
`visible_to_parent BOOLEAN NOT NULL DEFAULT true`（见 §4.2.2），M4 实现用户侧展开时
MUST 把这个字段接进去：

> **若某部门 `visible_to_parent = false`，该部门及其子树 MUST NOT 出现在祖先用户的 `user.subjects` 里。**

只约束「被祖先看见」这一个方向 —— 该部门成员自己照常可见，下级看上级（A1）也不受影响。
子孙链上只要有一级为 `false`，整棵子树对祖先不可见。

代价不到 20 行，客户真提出来时只需在界面上加个开关，**不用改判定逻辑**。
交付默认全为 `true`。财务 / 法务 / 人事类客户常提这条。

#### 3.2.4 标签书写规范（写库时的强制校验）

```
dept:<路径>          文档自身所属的部门路径          ★ MUST NOT 展开祖先
user:<主体 id>       具体用户
group:<用户组 id>    用户组
role:<角色>          角色
region:<区域>        区域
public               所有人（含新账号）              ★ 互斥
```

**两条写库断言（MUST 在入库函数里 assert，失败即拒绝入库）**

| 断言 | 内容 | 不做的后果 |
|---|---|---|
| **A1** | `'public' in acl_tags` ⇒ `len(acl_tags) == 1` | `{public, dept:财务部}` 这种混标会让 G4 对**所有用户**成立，只剩 G2 在守。等于把部门级文档降级成"全公司只要在库里就能看" |
| **A2** | 任何标签 MUST NOT 以 `level:` 开头 | 密级只通过 G1 的数值比较表达。若允许 `level:` 进 `acl_tags`，G4 就能绕过 G1，密级形同虚设 |

**空 `acl_tags` 的处理**：MUST 在入库时规范化为 `{public}`，**MUST NOT** 留空数组。
（留空数组会让 `交集 = ∅` 恒成立，文档对所有人不可见——这是"看起来安全但其实是 bug"的典型。）

#### 3.2.5 密级标尺（跨层对齐）

**API 层用枚举，检索下推用数值。两者 MUST 严格对齐。**

| API 表述（`Level` IntEnum） | 含义 | 下推字段 `level_rank` |
|---|---|---|
| `0` | 公开 | `10` |
| `1` | 内部 | `20` |
| `2` | 机密 | `30` |
| `3` | 绝密 | `40` |

```
level_rank = (level + 1) * 10
```

**为什么必须数值化**：向量库 / SQL 的过滤是数值比较（`level_rank <= clearance`），
而 API 层用 enum 更好读。**两处 MUST 用同一个转换函数**，MUST NOT 各写一份。

> 留的 10 的间隔是为了以后能插入 `15`（公开与内部之间）而不动已有数据。

#### 3.2.6 权限缓存与失效

```
缓存 key = f"acl:{tenant_id}:{user_id}:{tenant_acl_epoch}"
缓存 value = { subjects: [...], authorized_kb_ids: [...], clearance: 20 }
TTL = 300s（兜底；正常靠 epoch 主动失效）
```

| 发生什么 | 动作 |
|---|---|
| `kb_members` 增 / 删 / 改 | `tenant_acl_epoch += 1` |
| 知识库 `visibility` / `is_public` 变更 | `tenant_acl_epoch += 1` |
| 用户增删改 / 换部门 / 换角色 | `tenant_acl_epoch += 1` |
| 用户组成员的增删改 | `tenant_acl_epoch += 1` |
| **部门树结构变更**（新增 / 移动 / 删除部门）★ | `tenant_acl_epoch += 1` |

> **为什么部门树变更要全局失效**：用户侧做了**子孙展开（A2）**，所以新增或移动一个部门，
> 会改变该部门**祖先链上所有用户**的可见集合。精确失效要遍历祖先链，容易写错。
> **用一次全局失效，换掉一段容易写错的精确失效逻辑**——这对单人项目是正确的取舍。

> 缓存不可用时 MUST 降级为直连 DB 计算，MUST NOT 报错。用户会感觉"慢一点"，而不是"用不了"。

#### 3.2.7 公开知识库

| 项 | 口径 |
|---|---|
| 数量 | **全局最多 1 个**（`is_public = true`），用**部分唯一索引**强制 |
| 成员 | 所有用户**自动**视为成员 → 自动进入 `authorized_kb_ids` |
| 文档可见性 | 库内文档**仍须**过 G1/G3/G4。放进公开库 ≠ 所有人可见 |
| 新账号 | 只有 `public` 主体 → 只能看到公开库中 `acl_tags` 含 `public` 的文档 |

```sql
-- 保证全局 is_public = true 的行最多一条
CREATE UNIQUE INDEX uq_kb_single_public
  ON knowledge_bases ((is_public)) WHERE is_public;
```

#### 3.2.8 口径对实现的三处反直觉要求（照做，别"优化"）

| # | 要求 | 听起来像 bug，其实是设计 |
|---|---|---|
| 1 | 放进公开库的机密文档，非成员依然看不见 | "公开库"= 库对所有人在成员意义上开放；文档本身仍受密级与标签约束 |
| 2 | 用户能看到**子孙部门**的文档，但看不到**兄弟部门**的 | 上下级是"管理可见性"，同级是"隔离" |
| 3 | 部门树改结构会导致**所有人**的权限缓存失效 | 子孙展开的必然代价，见 3.2.6 |

### 3.3 分块与检索口径

#### 3.3.1 分块参数（冻结）

| 参数 | 值 | 说明 |
|---|---|---|
| `CHUNK_TARGET_TOKENS` | `400` | 目标长度，按 **token** 计（tiktoken），不按字符 |
| `CHUNK_MAX_TOKENS` | `800` | 硬上限，超过必须切 |
| `CHUNK_OVERLAP_TOKENS` | `60` | 相邻块重叠，防答案被切断 |
| 切分优先级 | ① 标题层级（`#`/`##`）→ ② 段落 → ③ 句子 → ④ 硬切 | 结构优先，避免把一张表切成两半 |
| 表格 | **整表不切**，超限时按行组切并保留表头 | 表头丢失会让表格 chunk 完全不可用 |

#### 3.3.2 检索链路

```
query
  │
  ├─ ① 查询改写（可选，P2）：多轮场景下先与历史对话合并
  │
  ├─ ② 混合召回（并行）
  │      · 向量召回：pgvector 余弦相似度，top 50
  │      · 关键词召回：PostgreSQL 全文检索，top 50（中文分词见 §8 D-06：先 ILIKE 降级）
  │      ★ 两者 MUST 都带下推过滤（见下）
  │
  ├─ ③ 融合：RRF（Reciprocal Rank Fusion），k = 60
  │
  ├─ ④ 重排：rerank 模型对 top 50 打分 → 取 top 8
  │
  └─ ⑤ 阈值闸门 L1：最高重排分 < RELEVANCE_THRESHOLD → 拒答
```

**★ 下推过滤（H7 · 强制性）**

检索 SQL **MUST** 把下列条件写进查询本身，**MUST NOT** 先取出再在应用层过滤：

```sql
WHERE tenant_id      = :tenant_id
  AND deleted_at     IS NULL
  AND is_latest      = true
  AND level_rank    <= :clearance              -- G1
  AND kb_id          = ANY(:authorized_kb_ids) -- G2
  AND NOT (deny_subjects && :user_subjects)    -- G3
  AND acl_tags       && :user_subjects         -- G4
```

**两个理由**：
1. **防泄漏**：应用层过滤一旦有一处漏写，就是真实泄漏；写进 SQL 只有一处
2. **性能**：先取后过滤会在权限窄的用户上做大量无用计算

> `&&` 是 PostgreSQL 数组"有交集"操作符。`acl_tags` 与 `deny_subjects` 用 `TEXT[]` 存储 + GIN 索引。

**阈值初值与标定**

| 项 | 值 |
|---|---|
| `RELEVANCE_THRESHOLD` 初值 | `0.35`（重排分归一化到 0~1 后） |
| 标定方式 | M2 结束时用 §7.3 评估集跑一遍，取值使 **拒答正确率 ≥ 0.9 且 漏答率 ≤ 0.1** |

> 阈值**必须标定，不能拍脑袋**。它是"该拒答时拒答"与"能答就答"的直接权衡点。

### 3.4 错误与降级口径

**原则：宁可拒答，不可错答；宁可降级，不可崩溃。**

| # | 故障 | 行为 | 用户看到 | 错误码 |
|---|---|---|---|---|
| 1 | 检索服务不可用 | **拒绝回答** | 「知识库暂时不可用，请稍后重试」 | `VECTOR_STORE_DOWN` |
| 2 | 重排模型超时（> 3s） | 跳过重排，用 RRF 分当阈值分 | 无感（略慢） | — |
| 3 | LLM 首 token 超时（15s） | 中止，保留已输出 | 「响应较慢，请重试」+ 重试按钮 | `LLM_TIMEOUT` |
| 4 | LLM 生成中途报错 | 中止，**保留已输出** | 部分答案 + 内联错误 | `LLM_ERROR` |
| 5 | LLM 全部实例不可用 | 维护态，禁用输入框 | 维护横幅 | `MAINTENANCE` |
| 6 | L3 出口校验失败 | **降级为拒答** | 「知识库中未找到相关内容」 | `UNGROUNDED` |
| 7 | 权限缓存不可用 | 直连 DB 计算 | 无感 | — |
| 8 | 权限服务不可用 | **拒绝回答** | 「权限校验失败，请稍后重试」 | `ACL_UNAVAILABLE` |
| 9 | 上传中途失败 | 断点续传 / 重传 | 进度条 + 重试 | — |
| 10 | 异步解析失败（重试耗尽） | 死信队列 + `status=FAILED` | 列表显示「处理失败」+ 原因 + 重试 | `PARSE_FAILED` |
| 11 | 文档正在被索引时被提问 | 只检索 `is_latest = true` 的旧版本 | 无感 | — |

**分级告警**

| 级别 | 范围 | 动作 |
|---|---|---|
| **P0** | 权限服务不可用 | 立即告警，可考虑整体降级 |
| **P1** | 向量库不可用 / LLM 全挂 | 告警，进维护态 |
| **P2** | 单次超时 / 单文档解析失败 | 记日志，不告警 |

### 3.5 常量表 · `app/config/decisions.py`

> **逐字落盘。** 业务代码 **MUST** 从本模块读取取值，**MUST NOT** 硬编码任何此处已定义的常量。

```python
"""全局口径的代码投影 —— 由开发手册 §3 生成。

修改流程: 手册 §3 → 本文件 → 启动自检
MUST NOT 手动偏离；偏离即为口径违规。
"""
from __future__ import annotations

from enum import Enum
from typing import Final

CONTRACT_VERSION: Final[str] = "v1.0"


# ── 回答边界 ──────────────────────────────────────────────
RELEVANCE_THRESHOLD: Final[float] = 0.35        # L1 闸门；M2 结束前必须标定
GENERATION_TEMPERATURE: Final[float] = 0.1      # L2
GROUNDING_CHECK_ENABLED: Final[bool] = True     # L3；MUST NOT 置 False
TOP_K_RECALL: Final[int] = 50                   # 每路召回条数
TOP_K_RERANK: Final[int] = 8                    # 重排后取用条数
RRF_K: Final[int] = 60


# ── 分块 ──────────────────────────────────────────────────
CHUNK_TARGET_TOKENS: Final[int] = 400
CHUNK_MAX_TOKENS: Final[int] = 800
CHUNK_OVERLAP_TOKENS: Final[int] = 60


# ── 密级 ──────────────────────────────────────────────────
class Level(int, Enum):
    PUBLIC = 0
    INTERNAL = 1
    CONFIDENTIAL = 2
    SECRET = 3


def level_rank(level: Level) -> int:
    """密级 → 下推数值。API 层用 enum，检索下推用数值。"""
    return (level + 1) * 10          # 10 / 20 / 30 / 40


LEVEL_RANK_PUBLIC: Final[int] = 10
LEVEL_RANK_INTERNAL: Final[int] = 20
LEVEL_RANK_CONFIDENTIAL: Final[int] = 30
LEVEL_RANK_SECRET: Final[int] = 40


# ── 可见性口径（DEC-22 · 已冻结）──────────────────────────
class GrantModel(str, Enum):
    KB = "kb"
    TAG = "tag"
    ORG = "org"


VISIBILITY_GRANTS: Final[frozenset[GrantModel]] = frozenset(
    {GrantModel.KB, GrantModel.TAG}
)
"""★ 已冻结。grant_kb 是必要条件（G2），grant_tag 做文档级细化（G4）。
grant_org MUST NOT 进入判定 —— 组织架构只作标签来源。"""

DEPT_VISIBLE_DIRECTION: Final[str] = "up_and_down"
"""上级能看下级。对应 Q3 = 能。"""

USER_SUBJECT_DESCENDANT_EXPANSION: Final[bool] = True
"""A2: 用户主体展开子孙部门。"""

DOC_TAG_ANCESTOR_EXPANSION: Final[bool] = False
"""★ 文档标签 MUST NOT 展开祖先。改成 True 会让全公司互通（§3.2.3）。"""

ACL_PUBLIC_MUTEX: Final[bool] = True
"""public MUST 单独存在，MUST NOT 与其他标签共存。"""

ACL_LEVEL_TAGS_ALLOWED: Final[bool] = False
"""acl_tags MUST NOT 含 level: 标签（会让 G4 绕过 G1）。"""

PUBLIC_KB_ENABLED: Final[bool] = True
PUBLIC_KB_MAX_COUNT: Final[int] = 1
NEW_USER_VISIBLE_SCOPE: Final[str] = "public_kb_only"
SUBJECT_EXPANSION_SOFT_LIMIT: Final[int] = 300
"""用户主体数量软上限；超过时记录告警（提示组织树过深，考虑预计算闭包表）。"""

ACL_CACHE_TTL_SECONDS: Final[int] = 300


# ── 多租户 ────────────────────────────────────────────────
TENANT_ID_REQUIRED: Final[bool] = True
"""所有业务表 MUST 带 tenant_id 并出现在每个查询条件里。"""


def self_check() -> None:
    """应用启动时调用。任何一条不成立 → 抛异常，拒绝启动（fail-closed）。"""
    if not GROUNDING_CHECK_ENABLED:
        raise RuntimeError("GROUNDING_CHECK_ENABLED MUST be True —— L3 出口校验 MUST 开启")
    if DOC_TAG_ANCESTOR_EXPANSION:
        raise RuntimeError(
            "DOC_TAG_ANCESTOR_EXPANSION MUST be False（§3.2.3）—— "
            "文档标签展开祖先将导致全公司互通。"
        )
    if GrantModel.KB not in VISIBILITY_GRANTS:
        raise RuntimeError("VISIBILITY_GRANTS MUST 含 KB（G2 是必要条件）")
    if GrantModel.ORG in VISIBILITY_GRANTS:
        raise RuntimeError("VISIBILITY_GRANTS MUST NOT 含 ORG（组织架构只作标签来源）")
    if not ACL_PUBLIC_MUTEX:
        raise RuntimeError("ACL_PUBLIC_MUTEX MUST be True（§3.2.4）")
    if ACL_LEVEL_TAGS_ALLOWED:
        raise RuntimeError("ACL_LEVEL_TAGS_ALLOWED MUST be False（§3.2.4）")
    if PUBLIC_KB_MAX_COUNT != 1:
        raise RuntimeError("PUBLIC_KB_MAX_COUNT MUST be 1")
    if level_rank(Level.SECRET) != LEVEL_RANK_SECRET:
        raise RuntimeError("level_rank 与 LEVEL_RANK_* 常量不一致")
```

---

---

## 4. 数据契约

> 建表直接抄本节。所有表 **MUST** 带 `tenant_id`，所有查询 **MUST** 带 `tenant_id` 条件。

### 4.1 ER 总览

```
tenants
  └─ users ──┬─ user_groups ── groups
             ├─ user_roles   ── roles
             └─ departments (materialized path, 自引用)
  │
  ├─ knowledge_bases ──┬─ kb_members (user/group/dept/role 四种主体)
  │                    └─ documents ──┬─ parse_jobs
  │                                   └─ chunks ★（权限判定与检索的最终载体）
  │
  ├─ conversations ── messages
  ├─ audit_logs
  └─ eval_cases
```

**★ 关键设计**：`chunks` 是**冗余存储**了 `tenant_id / kb_id / level_rank / acl_tags / deny_subjects / is_latest`
的表。这是刻意的——检索时的权限过滤必须**不 JOIN** 就能完成（§3.3.2 的 H7）。

### 4.2 表结构

#### 4.2.1 `tenants`

```sql
CREATE TABLE tenants (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    code        TEXT NOT NULL UNIQUE,          -- 私有化部署时用固定值 'default'
    status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','suspended')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

#### 4.2.2 `departments`（组织树 · 物化路径）

```sql
CREATE TABLE departments (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id),
    parent_id   UUID REFERENCES departments(id) ON DELETE RESTRICT,
    name        TEXT NOT NULL,
    path        TEXT NOT NULL,                 -- 形如 '/总部/技术中心/后端组/'
    depth       INT  NOT NULL DEFAULT 0,
    sort_order  INT  NOT NULL DEFAULT 0,
    -- ★ §8 D-12：false ⇒ 祖先用户 MUST NOT 看到本部门及其子树
    visible_to_parent BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, path)
);

CREATE INDEX idx_dept_parent ON departments(tenant_id, parent_id);
```

**`path` 的三个约定**

| 约定 | 说明 |
|---|---|
| 前后都有 `/` | 便于用 `path LIKE :prefix || '%'` 做子孙查询，不会误匹配 `后端组` 与 `后端组二` |
| 改名 / 移动 MUST 级联重写子树 | 子树内所有 `path` 前缀替换，且 **MUST 触发 `acl_epoch += 1`**（§3.2.6） |
| 删除 MUST 先校验无子部门、无用户 | `ON DELETE RESTRICT` 已挡住子部门；用户侧在应用层校验 |

#### 4.2.3 `users`

```sql
CREATE TABLE users (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID NOT NULL REFERENCES tenants(id),
    username       TEXT NOT NULL,
    email          TEXT,
    password_hash  TEXT NOT NULL,
    display_name   TEXT NOT NULL,
    dept_id        UUID REFERENCES departments(id),
    clearance      INT  NOT NULL DEFAULT 20,   -- 存 level_rank：10/20/30/40
    role_names     TEXT[] NOT NULL DEFAULT '{}'::TEXT[],   -- 冗余自 roles，便于快速解析主体
    status         TEXT NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active','disabled')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, username)
);

CREATE INDEX idx_users_dept ON users(tenant_id, dept_id);
```

> `clearance` **存 `level_rank` 数值**（10/20/30/40），MUST NOT 存 enum 序号（0~3）。
> 存 enum 序号会让 `level_rank <= clearance` 变成 `10 <= 1` 恒为假——**所有人什么都看不到**，
> 而且看起来像"权限生效了"。这是一处极易踩的坑。

#### 4.2.4 `groups` / `user_groups` / `roles` / `user_roles`

```sql
CREATE TABLE groups (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID NOT NULL REFERENCES tenants(id),
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'normal'
               CHECK (kind IN ('normal','external')),   -- 外部人员必须用 'external'
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name)
);

CREATE TABLE user_groups (
    user_id   UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    group_id  UUID NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, group_id)
);

CREATE TABLE roles (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID NOT NULL REFERENCES tenants(id),
    name       TEXT NOT NULL,                  -- 'admin' / 'hr' / 'finance' ...
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name)
);

CREATE TABLE user_roles (
    user_id  UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_id  UUID NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, role_id)
);
```

> **外部人员口径**：MUST 放进 `kind = 'external'` 的组，**MUST NOT** 给 `dept` 标签、
> MUST NOT 给高 `clearance`。对外授权一律用 `group:external_*` 显式授予。

#### 4.2.5 `knowledge_bases`

```sql
CREATE TABLE knowledge_bases (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id),
    name        TEXT NOT NULL,
    description TEXT,
    visibility  TEXT NOT NULL DEFAULT 'restricted'
                CHECK (visibility IN ('restricted','public')),
    is_public   BOOLEAN NOT NULL DEFAULT false,   -- ★ 全局最多 1 行为 true
    owner_id    UUID REFERENCES users(id),
    acl_version INT NOT NULL DEFAULT 1,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name)
);

-- ★ 保证 is_public = true 的行最多一条
CREATE UNIQUE INDEX uq_kb_single_public
  ON knowledge_bases ((is_public)) WHERE is_public;
```

> `visibility` 与 `is_public` **不是同一件事**，都要有：
> `visibility` 是人可读的展示字段，`is_public` 是**判定用的布尔**（能建部分唯一索引）。
> **MUST** 在应用层保证两者同步，且 **MUST NOT** 用 `visibility = 'public'` 直接跳过硬权限判定。

#### 4.2.6 `kb_members`

```sql
CREATE TABLE kb_members (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id),
    kb_id        UUID NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    subject_type TEXT NOT NULL
                 CHECK (subject_type IN ('user','group','dept','role')),
    subject_id   TEXT NOT NULL,      -- user/group/dept 存 UUID 字符串；role 存角色名
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (kb_id, subject_type, subject_id)
);

CREATE INDEX idx_kbmem_subject ON kb_members(tenant_id, subject_type, subject_id);
```

> 这张表是 **G2 的数据来源**：用户解析出主体后，反查"我被授权了哪些库" → `authorized_kb_ids`。
> 公开库在这个基础上**额外**无条件加入。

#### 4.2.7 `documents`

```sql
CREATE TABLE documents (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants(id),
    kb_id         UUID NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    doc_group_id  UUID NOT NULL,          -- 同一文档的多个版本共享此 id
    version       INT  NOT NULL DEFAULT 1,
    is_latest     BOOLEAN NOT NULL DEFAULT true,
    filename      TEXT NOT NULL,
    ext           TEXT NOT NULL,
    size_bytes    BIGINT NOT NULL,
    storage_key   TEXT NOT NULL,          -- 对象存储中的 key
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','parsing','indexed','failed')),
    level         INT  NOT NULL DEFAULT 1,       -- enum 0~3
    level_rank    INT  NOT NULL DEFAULT 20,      -- 10/20/30/40
    owner_dept_path TEXT,                        -- 上传者部门路径，★ 不展开祖先
    acl_tags      TEXT[] NOT NULL DEFAULT '{}'::TEXT[],
    deny_subjects TEXT[] NOT NULL DEFAULT '{}'::TEXT[],
    uploaded_by   UUID REFERENCES users(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at    TIMESTAMPTZ
);

CREATE INDEX idx_doc_kb  ON documents(tenant_id, kb_id, is_latest);
CREATE INDEX idx_doc_grp ON documents(doc_group_id, version DESC);
```

#### 4.2.8 `chunks` ★（权限与检索的最终载体）

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE chunks (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants(id),
    kb_id         UUID NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    document_id   UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index   INT  NOT NULL,
    content       TEXT NOT NULL,
    token_count   INT  NOT NULL,
    heading_path  TEXT,                  -- '第 3 章 > 3.2 权限' 便于展示定位
    page_no       INT,
    -- ↓↓ 冗余自 documents，为的是检索时不 JOIN ↓↓
    level_rank    INT  NOT NULL,
    acl_tags      TEXT[] NOT NULL,
    deny_subjects TEXT[] NOT NULL,
    is_latest     BOOLEAN NOT NULL DEFAULT true,
    -- ↑↑
    embedding     vector(1024),          -- 维度 MUST 与 embedding 模型一致（见 §8 D-05）
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_chunk_doc      ON chunks(document_id);
CREATE INDEX idx_chunk_filter   ON chunks(tenant_id, kb_id, is_latest, level_rank);
CREATE INDEX idx_chunk_acl      ON chunks USING GIN (acl_tags);
CREATE INDEX idx_chunk_deny     ON chunks USING GIN (deny_subjects);
CREATE INDEX idx_chunk_fts      ON chunks USING GIN (to_tsvector('simple', content));
CREATE INDEX idx_chunk_emb      ON chunks USING hnsw (embedding vector_cosine_ops);
```

**`chunks` 的三条硬约束**

| # | 约束 | 理由 |
|---|---|---|
| 1 | `acl_tags` MUST NOT 为空数组 | 空数组 → 交集恒空 → 文档对所有人不可见（"安全"的假象）。入库时规范化成 `{public}` |
| 2 | `acl_tags` MUST NOT 含 `dept:` 的祖先路径 | H1。`compute_doc_acl_tags()` 是**唯一**的生成入口 |
| 3 | `heading_path` / `page_no` MUST 尽量填 | 引用溯源要能点回原文，缺了这两项就退化成"只能看到片段" |

#### 4.2.9 `conversations` / `messages`

```sql
CREATE TABLE conversations (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID NOT NULL REFERENCES tenants(id),
    user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('user','assistant')),
    content         TEXT NOT NULL,
    citations       JSONB NOT NULL DEFAULT '[]'::JSONB,   -- [{n, chunk_id, doc_id, heading_path, page_no}]
    meta            JSONB NOT NULL DEFAULT '{}'::JSONB,   -- 耗时、token、是否拒答、剥离句数
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_msg_conv ON messages(conversation_id, created_at);
```

> `messages.citations` MUST 存**快照**，不能只存 `chunk_id` 引用。
> 文档被删或被重新索引后，历史回答的引用仍须可读——否则用户会看到一堆死链。

#### 4.2.10 `parse_jobs` / `audit_logs` / `eval_cases`

```sql
CREATE TABLE parse_jobs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id),
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    status      TEXT NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued','running','succeeded','failed','dead')),
    attempts    INT NOT NULL DEFAULT 0,
    max_attempts INT NOT NULL DEFAULT 3,
    last_error  TEXT,
    started_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE audit_logs (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   UUID NOT NULL,
    user_id     UUID,
    action      TEXT NOT NULL,          -- 'chat.ask' / 'doc.upload' / 'acl.member.add' ...
    object_type TEXT,
    object_id   TEXT,
    detail      JSONB NOT NULL DEFAULT '{}'::JSONB,
    ip          INET,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 保留期 1 年 + 定期归档（§8 D-15）：AUDIT_RETENTION_DAYS=365
-- 超期行 MUST 先归档到 audit_logs_archive 再删除；归档任务在 M5 实现
CREATE INDEX idx_audit_time ON audit_logs(tenant_id, created_at DESC);
CREATE INDEX idx_audit_user ON audit_logs(tenant_id, user_id, created_at DESC);

CREATE TABLE eval_cases (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          UUID NOT NULL REFERENCES tenants(id),
    kb_id              UUID REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    question           TEXT NOT NULL,
    expected_answerable BOOLEAN NOT NULL,     -- 是否应当答得出来
    expected_doc_ids   UUID[] DEFAULT '{}',   -- 期望命中的文档
    note               TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

> `eval_cases` 是 **M2 的门禁**：没有它，`RELEVANCE_THRESHOLD` 就只能拍脑袋，
> 而阈值是"该拒答时拒答"的唯一控制点。

### 4.3 迁移要点

```bash
alembic revision --autogenerate -m "init schema"
alembic upgrade head
```

| 注意事项 | 说明 |
|---|---|
| pgvector 扩展 | 迁移脚本里 MUST 先 `CREATE EXTENSION IF NOT EXISTS vector`，且需数据库超级用户权限 |
| 中文全文检索 | 需要 `zhparser` 或 `pg_jieba` 扩展。若目标环境装不了 → 关键词召回降级为 `ILIKE`（见 §8 D-06） |
| HNSW 索引 | 建索引要在数据量起来**之后**，或调大 `maintenance_work_mem`，否则慢到以为卡死 |
| 部分唯一索引 | `uq_kb_single_public` MUST 在建表时一起建，不能等出问题再补 |
| `visible_to_parent` | §8 D-12 预留字段 MUST 在 M0 建表时就有（默认 `true`）。**MUST NOT 等客户提了再补** —— 补字段要同时改判定逻辑与缓存失效路径 |
| 审计归档 | §8 D-15：`audit_logs` 保留 1 年（`AUDIT_RETENTION_DAYS=365`），归档任务在 M5 实现 |

---

## 5. 接口契约

### 5.1 API 清单

**约定**：全部以 `/api` 开头；除 `/api/auth/*` 外**全部**需要 `Authorization: Bearer <jwt>`；
所有响应都是 JSON；时间统一 ISO 8601 UTC。

| 方法 | 路径 | 用途 | 里程碑 |
|---|---|---|---|
| `POST` | `/api/auth/login` | 登录，返回 access + refresh token | M3 |
| `POST` | `/api/auth/refresh` | 刷新 token | M3 |
| `GET` | `/api/me` | 当前用户：主体、密级、可见库 | M3 |
| `GET` | `/api/kbs` | 我能访问的知识库列表 | M1/M4 |
| `POST` | `/api/kbs` | 新建知识库（管理员） | M1 |
| `GET` | `/api/kbs/{kb_id}` | 知识库详情 | M1 |
| `PUT` | `/api/kbs/{kb_id}/members` | 设置成员（四种主体） | M4 |
| `GET` | `/api/kbs/{kb_id}/documents` | 文档列表 | M1/M2 |
| `POST` | `/api/kbs/{kb_id}/documents` | 上传文档（multipart） | M2 |
| `DELETE` | `/api/documents/{doc_id}` | 删除文档（软删） | M2 |
| `GET` | `/api/documents/{doc_id}` | 文档详情 + chunk 预览 | M2 |
| `GET` | `/api/jobs/{job_id}` | 解析任务状态（前端轮询） | M2 |
| `POST` | `/api/chat/ask` | **提问（SSE 流式）** | M2 |
| `GET` | `/api/conversations` | 会话列表 | M6 |
| `GET` | `/api/conversations/{id}/messages` | 会话消息 | M6 |
| `GET` | `/api/admin/departments` | 组织树 | M5 |
| `POST` | `/api/admin/departments` | 建/改/移部门 | M5 |
| `GET` | `/api/admin/users` | 用户列表 | M5 |
| `POST` | `/api/admin/users` | 建用户 | M5 |
| `GET` | `/api/admin/audit-logs` | 审计日志 | M5 |
| `GET` | `/api/health` | 健康检查（含各依赖） | M0 |

### 5.2 关键接口

#### `POST /api/chat/ask` —— 核心接口（SSE）

**请求**

```json
{
  "kb_ids": ["uuid1", "uuid2"],
  "question": "报销流程是什么？",
  "conversation_id": "uuid-or-null"
}
```

**响应（`text/event-stream`，逐个事件推送）**

```
event: meta
data: {"conversation_id":"...","message_id":"...","stage":"retrieving"}

event: stage
data: {"stage":"retrieving","ms":312}

event: citations
data: {"citations":[{"n":1,"chunk_id":"...","doc_id":"...","filename":"报销制度.pdf","heading_path":"3.2 审批层级","page_no":7,"score":0.81}]}

event: delta
data: {"text":"根据"}

event: delta
data: {"text":"《报销制度》第 3.2 节"}

event: done
data: {"finish_reason":"stopped","grounding":{"stripped_sentences":0},"usage":{"prompt_tokens":2100,"completion_tokens":180}}

-- 或拒答：
event: refused
data: {"reason":"NO_RELEVANT_CONTENT","message":"知识库中未找到相关内容"}
```

**MUST 遵守**

| # | 约束 |
|---|---|
| 1 | `kb_ids` MUST 与用户 `authorized_kb_ids` 求交集后再使用。**MUST NOT** 直接信任前端传来的 kb 列表 |
| 2 | `citations` 事件 MUST 在 `delta` 之前推送 —— 先给引用，再给正文，用户能边看边点 |
| 3 | 拒答时 MUST 发 `refused` 事件，**MUST NOT** 用 `delta` 发一句"未找到相关内容"（前端要区分"拒答"与"正常回答"） |
| 4 | 每个 `delta` 是增量文本，前端 MUST 累加，MUST NOT 覆盖 |

#### `GET /api/me`

```json
{
  "user": { "id": "...", "display_name": "张三", "dept_path": "/总部/技术中心/后端组/", "clearance": 20 },
  "subjects": [
    "dept:/总部/", "dept:/总部/技术中心/", "dept:/总部/技术中心/后端组/",
    "user:...", "group:...", "role:user", "public"
  ],
  "authorized_kb_ids": ["uuid1", "uuid2"]
}
```

> **安全声明（MUST 写进代码注释）**：`/api/me` 的返回**只是给前端做 UI 用的**，
> **MUST NOT** 被当作安全边界。真正的过滤**只在检索 SQL 里**发生。
> 前端拿到的 subjects 是"提示"，不是"授权"。

### 5.3 错误码

| HTTP | 错误码 | 含义 | 前端动作 |
|---|---|---|---|
| 400 | `BAD_REQUEST` | 参数错误 | 提示具体字段 |
| 401 | `UNAUTHENTICATED` | 未登录 / token 过期 | 跳登录页 |
| 403 | `FORBIDDEN` | 无权限访问该资源 | 提示无权限 |
| 404 | `NOT_FOUND` | 不存在**或无权知晓其存在** | 统一提示"不存在" |
| 409 | `CONFLICT` | 重名 / 并发冲突 | 提示并刷新 |
| 413 | `FILE_TOO_LARGE` | 超过上传上限 | 提示上限值 |
| 415 | `UNSUPPORTED_FILE_TYPE` | 不支持的文件类型 | 提示支持的类型列表 |
| 429 | `RATE_LIMITED` | 触发限流 | 提示稍后重试 |
| 500 | `INTERNAL_ERROR` | 服务端异常 | 提示 + 上报按钮 |
| 503 | `ACL_UNAVAILABLE` | **权限服务不可用** | 提示稍后重试，**MUST NOT 降级放行** |
| 503 | `VECTOR_STORE_DOWN` | 向量库不可用 | 提示稍后重试 |
| 504 | `LLM_TIMEOUT` | 模型超时 | 保留已输出 + 重试按钮 |

> **404 与 403 的取舍**：对**文档 / 知识库**类资源，无权访问时返回 **404 而非 403**。
> 403 会泄露"这个资源存在"。单租户私有化场景下危害有限，但这是零成本的正确做法。

---

---

## 6. 里程碑路线（单人 · 串行）

### 6.0 为什么是 7 个里程碑，而不是 16 个步骤

原方案按 **4 人团队**排了 16 步，隐含"前端/后端/算法/测试并行"。**你一个人，并行不成立。**

改成 7 个里程碑，每个里程碑的硬标准是同一条：

> **结束时必须有一个"能打开、能演示、能给别人看"的东西。**
> 不是"完成了 30%"，是"这一块能用了"。

这条标准专门对付单人开发最大的失败模式：**处处半成品**。

```
M0 地基          3–5 天   ── 一条命令起全套，空壳能跑
M1 可演示原型     1 周    ── 假数据全链路，界面定稿  ← 第一个能演示的东西
M2 真 RAG        2–3 周   ── 核心价值验证  ★ 最重要的里程碑
M3 账号与组织     1 周    ── 能多人登录，有部门树
M4 权限与隔离    2–3 周   ── 权限全口径落地  ★ 第二重要
M5 产品化与交付  2–3 周   ── 能打包卖给客户
M6 增量与上线    3–4 周   ── 能长期运行
────────────────────────────
单人全职合计    12–18 周
单人业余合计     6–9 个月（按每周 10–15 小时）
```

**顺序的三个判断**

| 判断 | 理由 |
|---|---|
| **界面先于后端**（M1 用假数据） | 界面定稿最费来回。后端做完再改界面，等于白写接口。假数据成本几乎为零，却能一次性暴露所有交互问题 |
| **RAG 先于权限**（M2 先于 M4） | 权限是**约束**，前提是有东西可约束。先做权限会花两周去保护一个还不知道长什么样的检索链路 |
| **交付形态最后定**（M5） | 但 `tenant_id` 从 M0 就进表——**这是唯一一处提前投入**，见 §1.1 |

---

### M0 · 地基（3–5 天）

**目标**：`docker compose up` 一条命令起全套，所有依赖健康，空壳跑通。

**做什么**

| # | 任务 | 产出 |
|---|---|---|
| 1 | 仓库结构 | `apps/web`（Vite + React + TS）、`apps/api`（FastAPI）、`packages/shared`（共享类型） |
| 2 | `docker-compose.yml` | postgres 16 + pgvector、redis 7、minio（对象存储）。**一个文件，不拆** |
| 3 | 后端骨架 | FastAPI + 分层（`api/` `services/` `models/` `schemas/` `workers/`）+ `settings.py`（pydantic-settings，读 `.env`） |
| 4 | **`app/config/decisions.py`** | **逐字落盘 §3.5**，并在应用启动时调用 `self_check()` |
| 5 | `GET /api/health` | 返回各依赖状态：db / redis / 向量扩展 / 模型可达性 |
| 6 | alembic | `init` + 空迁移跑通 |
| 7 | 前端骨架 | Vite + 路由（登录 / 问答 / 知识库 / 文档 / 管理）+ Tailwind + 布局（侧边栏 + 主区） |
| 8 | **种子数据脚本** | 幂等：造 1 租户、3 层组织树（约 8 部门）、30 用户（含 1 管理员、1 外部人员）、3 个知识库 |

**验收（红灯测试）**

| # | 测试 | 期望 |
|---|---|---|
| A1 | `docker compose up -d` | 5 个容器全部 healthy |
| A2 | `curl localhost:8000/api/health` | 所有依赖 `ok` |
| A3 | 把 `decisions.py` 里 `DOC_TAG_ANCESTOR_EXPANSION` 改成 `True` 再启动 | **进程必须拒绝启动**（`self_check()` 抛异常） |
| A4 | 连续跑两次种子脚本 | 数据不重复（幂等） |
| A5 | `alembic upgrade head` 后 `alembic downgrade base` 再 `upgrade` | 无报错 |

> **A3 是这一里程碑最关键的一条。** 它验证的是"口径有牙齿"——如果改错了还能启动，
> 后面所有断言都是装饰。

**坑**

| 坑 | 说明 |
|---|---|
| pgvector 装不上 | 用官方镜像 `pgvector/pgvector:pg16`，别自己在 postgres 镜像里装扩展 |
| 中文全文检索扩展 | 目标环境可能装不了 `zhparser`。**M0 就要确认**，装不了走 §8 D-06 的降级方案 |
| 种子数据不幂等 | 单人开发会反复重置数据库，脚本必须能重复跑 |

---

### M1 · 可演示原型（1 周）

**目标**：用假数据把整条问答链路**在界面上**跑通，界面定稿。

**做什么**

| # | 任务 |
|---|---|
| 1 | mock 层：`apps/web/src/mocks/` 提供假知识库、假文档、假答案、假引用（含 SSE 模拟） |
| 2 | 登录页（假登录，不接后端） |
| 3 | **问答页**：输入框、流式答案渲染、引用卡片、引用点开 → 右侧抽屉显示原文片段与高亮 |
| 4 | 知识库列表页 + 新建弹窗 |
| 5 | 文档页：列表、上传区（进度条）、状态标签（解析中/已索引/失败） |
| 6 | 拒答态：「知识库中未找到相关内容」的专用样式（**必须和正常回答长得不一样**） |
| 7 | 空态 / 加载态 / 错误态，每个页面都要有 |

**验收**

| # | 测试 | 期望 |
|---|---|---|
| B1 | 不启动后端，纯前端跑 | 能完整走一遍：登录 → 选库 → 提问 → 看流式答案 → 点引用看原文 |
| B2 | 触发 mock 的拒答分支 | 显示专用拒答样式，不是普通答案框 |
| B3 | 断网 / mock 报错 | 显示错误态，有重试按钮 |
| B4 | 窄屏（< 1024px） | 布局不塌 |

**坑**

| 坑 | 说明 |
|---|---|
| 直接对着 mock 写死结构 | mock 的字段名 MUST 与 §5.2 的接口 schema **完全一致**，否则接真后端时要改一遍前端 |
| 引用做成弹窗 | 引用 MUST 是**抽屉/侧栏**，不是弹窗。用户一边读答案一边看原文，弹窗会挡住答案 |
| 忘了拒答态 | 拒答是这个产品的**核心特性**，不是错误分支。它 MUST 有专门的视觉表达 |

---

### M2 · 真 RAG（2–3 周）★ 核心

**目标**：用真实文档替换 mock，**核心价值可验证**。

**做什么**

| # | 任务 | 关键点 |
|---|---|---|
| 1 | 上传接口 | multipart，校验扩展名与大小，落对象存储，建 `documents` + `parse_jobs` |
| 2 | 异步 worker（arq） | 解析 → 分块 → 嵌入 → 写 `chunks` → 更新 `status`。**重试 3 次后进死信** |
| 3 | 解析器 | 按扩展名分派：`.pdf`（pypdf）/ `.md` `.txt` / `.xls` `.xlsx`（openpyxl）。**PDF 必须是文本层 PDF**，扫描件见 §8 D-04 |
| 4 | 分块器 | 按 §3.3.1 参数；表头保留；写入 `heading_path` / `page_no` |
| 5 | 嵌入 | 批量调用，失败重试；维度 MUST 等于 `vector(1024)` |
| 6 | 检索服务 | 混合召回 + RRF + 重排 + 阈值，**先写不带权限的版本**（M4 再加过滤） |
| 7 | 生成服务 | SSE 流式；prompt 见 §3.1 L2 |
| 8 | **L3 出口校验** | 归因检查 + 剥离无归因句 + 剥离后为空则降级拒答 |
| 9 | 引用回填 | 从 chunk 取 `filename` / `heading_path` / `page_no` 组成引用 |
| 10 | **评估集 30 条** | 20 条可答 + 10 条不可答，写进 `eval_cases` |
| 11 | **阈值标定** | 跑评估集，调 `RELEVANCE_THRESHOLD` 使拒答正确率 ≥ 0.9、漏答率 ≤ 0.1 |

**验收 —— 里程碑 A「无权限问答原型」**

| # | 测试 | 期望 |
|---|---|---|
| C1 | 上传 5 份真实文档（含 1 份 PDF、1 份 xlsx、1 份 md） | 全部 `status = indexed`，chunk 数合理（不是 1 个也不是 10000 个） |
| C2 | 问 10 个知识库里有的问题 | **≥ 8 个答案正确** |
| C3 | 问 3 个知识库里**没有**的问题 | **3/3 拒答**，且 MUST NOT 出现模型编造的内容 |
| C4 | 每条答案的引用点开 | 能定位到原文位置（页面 / 标题路径），片段与答案内容对得上 |
| C5 | 手动把某条答案的 `[n]` 改成 `[99]`（不存在） | L3 校验 MUST 剥离该句 |
| C6 | 上传一份 50 页 PDF | 解析在 2 分钟内完成，前端状态能从"解析中"自动变为"已索引" |
| C7 | 上传一个 `.exe` | 415 拒绝，提示支持的类型 |
| C8 | 上传中途断网 | 有失败提示 + 重试入口 |

> **C3 是这个里程碑的灵魂。** 一个"什么都能答"的 RAG 是废品——
> 用户无法信任它。**拒答能力 = 产品可信度。**

**坑**

| 坑 | 说明 |
|---|---|
| 分块不保留标题 | 丢掉 `heading_path` 后，引用只能显示一段裸文本，溯源价值大打折扣 |
| 表格被切断 | 表格 chunk 必须带表头，否则该 chunk 检索到了也没法用 |
| 阈值凭感觉设 | 不跑评估集就设阈值，会让产品要么"什么都拒答"要么"什么都敢答" |
| L3 只记日志不剥离 | 等于没做。MUST 真的把无归因的句子删掉 |
| 嵌入批量过大 | 按模型上限分批，超限会静默截断 |

---

### M3 · 账号与组织（1 周）

**目标**：真实多用户登录，组织树可用。

**做什么**

| # | 任务 |
|---|---|
| 1 | 登录 / JWT 签发 / refresh / 登出；密码 bcrypt |
| 2 | `GET /api/me`：解析 `subjects`（**含 §3.2.3 的用户侧双向展开**）、`clearance`、`authorized_kb_ids`（本步先返回空，M4 填） |
| 3 | 主体解析服务 + Redis 缓存（key 带 `tenant_acl_epoch`） |
| 4 | 用户管理界面（列表、新建、改部门、改密级、禁用） |
| 5 | 用户组管理（含 `kind = external`） |
| 6 | 角色管理 |
| 7 | **部门树管理界面**：树形展示、新增、拖拽移动、重命名 |
| 8 | 部门改名/移动时的**子树 `path` 级联重写** + `tenant_acl_epoch += 1` |

**验收**

| # | 测试 | 期望 |
|---|---|---|
| D1 | 登录后 `GET /api/me` | `subjects` 的 `dept:` 部分 **同时含祖先与子孙**（用例 8/9 的前置） |
| D2 | 把用户从 A 部门移到 B 部门 | 重新请求 `/api/me`，`subjects` 立即变化（缓存已失效） |
| D3 | 重命名一个中间层部门 | 子树所有 `path` 正确更新；`tenant_acl_epoch` +1 |
| D4 | 禁用用户后该用户请求任何接口 | 401 / 403，不能继续用 |
| D5 | 前端组织树拖拽移动部门 | 层级与 `path` 一致，刷新后不丢 |

---

### M4 · 权限与检索隔离（2–3 周）★ 第二核心

**目标**：§3.2 全口径真实生效，**10 条用例全绿**。

**做什么**

| # | 任务 | 关键点 |
|---|---|---|
| 1 | `kb_members` 管理接口 | 四种主体：user / group / dept / role |
| 2 | **`compute_doc_acl_tags()`** | 唯一生成入口。按 §3.2.3 文档侧口径：`dept:` **仅自身路径**，不展开祖先 |
| 3 | **两条写库断言** | `public` 互斥、禁止 `level:`（§3.2.4 A1/A2）。空 `acl_tags` 规范化为 `{public}` |
| 4 | `authorized_kb_ids` 解析 | 反查 `kb_members` + 无条件加入公开库 |
| 5 | **检索下推过滤** | 把 §3.3.2 的 SQL 条件接进检索；**删掉 M2 的"先取后过滤"临时逻辑** |
| 6 | 公开库 | `is_public`、部分唯一索引、成员自动加入 |
| 7 | `acl_epoch` 失效机制 | 所有变更点接上（表见 §3.2.6） |
| 8 | 前端 | 知识库成员管理界面；文档标签的展示与手动覆盖 |
| 9 | **10 条权限用例测试文件** | 见 §7.2 |

**验收 —— 里程碑 B「权限可用」**

| # | 测试 | 期望 |
|---|---|---|
| E1 | **§7.2 的 10 条用例** | **全绿** |
| E2 | 用低权限账号（`clearance = 10`）提问高密级文档内容 | 拒答或答不出该内容；引用里 MUST NOT 出现高密级文档 |
| E3 | 用 A 部门账号提问 B 部门（兄弟）文档内容 | 不可见（用例 9） |
| E4 | 用上级部门账号提问下级部门文档内容 | 可见（用例 8） |
| E5 | 在公开库放一份 `acl_tags = {dept:财务部}` 的文档，用研发部账号提问 | 不可见（用例 3b：G2 过了，G4 拦住） |
| E6 | 尝试写入 `acl_tags = ['public','dept:财务部']` | **入库被拒**（用例 3b 写入侧） |
| E7 | 尝试写入 `acl_tags = ['level:internal']` | **入库被拒**（用例 5） |
| E8 | 检索 SQL 的 `EXPLAIN` | 过滤条件出现在 SQL 里，**不是应用层循环** |

> **E8 的检查方式**：把 `EXPLAIN ANALYZE` 的输出贴进 PR 描述。
> 单人项目没有 Code Review，**这一条就是你的 Review**。

---

### M5 · 产品化与交付（2–3 周）

**目标**：能打包给一个真实客户装起来。

**做什么**

| # | 任务 |
|---|---|
| 1 | 密级 4 档完整落地：文档打标、用户密级、界面展示 |
| 2 | 脱敏：命中规则（手机号 / 身份证 / 银行卡）的片段在**输出层**打码 |
| 3 | 审计日志：记录 `chat.ask` / `doc.*` / `acl.*`，含引用到的文档 id |
| 4 | 多租户中间件：从 JWT 取 `tenant_id` 注入上下文；私有化部署恒为 `default` |
| 5 | 管理后台：组织 / 用户 / 角色 / 知识库 / 审计 五个页面 |
| 6 | **部署包**：`docker-compose.prod.yml` + 一键初始化脚本 + `.env.example` + 部署文档 |
| 7 | 首次启动引导：创建管理员 → 建公开库 → 建第一个知识库 |

**验收**

| # | 测试 | 期望 |
|---|---|---|
| F1 | 在一台干净机器上按部署文档操作 | 30 分钟内起来，能登录、能问、能看审计 |
| F2 | 连续两次运行 `is_public = true` 的插入 | 第二次被唯一索引拒绝 |
| F3 | 用户提问含手机号的文档 | 输出中手机号已打码，但引用原文**不打码**（原文是原文） |
| F4 | 查审计日志 | 能看到"谁、什么时候、问了什么、引用到哪些文档" |
| F5 | 备份恢复 | 按文档备份 pg 数据卷，恢复后数据完整 |

> **F1 是这一里程碑的定义。** 如果部署要你本人到场，那就不是"可交付产品"。

---

### M6 · 增量与上线（3–4 周）

**目标**：能长期运行，能应对真实使用的摩擦。

**做什么**（按价值排序，做不完可以砍尾部）

| # | 任务 | 价值 |
|---|---|---|
| 1 | **知识源同步**：定时/增量从指定来源拉文档（来源见 §8 D-07） | 高 —— 没有它，客户要手工传文档，用不了一周就放弃 |
| 2 | **多轮对话 + 上下文治理**：指代消解、上下文截断 | 高 —— 单轮问答体验很粗糙 |
| 3 | **安全加固**：提示注入防护、限流、配额、敏感词 | 中 —— 私有化部署下风险可控，但仍要有 |
| 4 | **可观测**：结构化日志 + 关键指标（P95 延迟、拒答率、检索命中率、token 消耗） | 中 |
| 5 | **评估门禁**：把 §7.3 评估集接进 CI，改检索就重跑 | 中 —— 防"改一处、坏一片" |
| 6 | 灰度与回滚：模型/提示词可切换版本 | 低 |

**验收**

| # | 测试 | 期望 |
|---|---|---|
| G1 | 配置一个同步源，加一份新文档 | 定时任务拉到并索引，无需人工 |
| G2 | 连续问 3 个有指代的问题 | 模型理解指代关系，不答非所问 |
| G3 | 输入一段提示注入文本（"忽略以上指令"） | 不越界，仍只依据知识库作答 |
| G4 | 单用户 1 分钟内问 20 次 | 触发限流，返回 429 |
| G5 | 改一次检索参数 | 评估门禁跑通并给出拒答率变化 |

---

## 7. 验收与测试

### 7.1 每个里程碑的"完成"定义

| 里程碑 | 完成 = 能演示这个 | 通过测试 |
|---|---|---|
| M0 | 一条命令起全套，`/api/health` 全绿 | A1–A5 |
| M1 | 假数据下完整问答体验 | B1–B4 |
| M2 | 上传真文档，问到真答案，问不到会拒答 | C1–C8 |
| M3 | 多用户登录，组织树可维护 | D1–D5 |
| M4 | 低权限用户真的看不到高密级文档 | E1–E8 + 10 条用例 |
| M5 | 在干净机器上 30 分钟部署成功 | F1–F5 |
| M6 | 客户能自己持续用，不需要你介入 | G1–G5 |

### 7.2 权限 10 条用例（M4 必须全绿）

> 前置数据：3 层组织树（`/总部/技术中心/后端组`、`/总部/技术中心/前端组`、`/总部/财务部`），
> 2 个知识库（1 个普通 + 1 个公开），若干文档。

| # | 用例 | 构造 | 期望 | 守住哪条 |
|---|---|---|---|---|
| 1 | 部门内正常命中 | 用户在后端组；文档 `acl_tags = {dept:/总部/技术中心/后端组/}` | **可见** | G4 基本路径 |
| 2 | 未授权知识库 | 文档与用户在**同一部门**，但文档所在 kb 未授权给该用户 | **不可见** | G2 是必要条件 |
| 3 | 密级不足 | 文档 `level_rank = 40`，用户 `clearance = 20` | **不可见** | G1 |
| 3b | ★ 公开库 + 混标 | 公开库中一份 `acl_tags = {dept:/总部/财务部/}` 的文档；用研发部账号问 | **不可见** | G4 拦住"进公开库就全可见"的误解 |
| 3b-写入 | ★ 混标被拒 | 尝试 `acl_tags = ['public','dept:/总部/财务部/']` | **入库被拒** | H2 · `public` 互斥 |
| 4 | deny 优先 | 文档同时命中 allow 与 deny | **不可见** | G3 优先级最高 |
| 5 | ★ `level:` 进标签 | 尝试 `acl_tags = ['level:public']` | **入库被拒** | H3 · 防 G4 绕过 G1 |
| 6 | 新账号只得公开库 | 新建用户，只给 `public` 主体 | 只能看见公开库**且** `acl_tags` 含 `public` 的文档 | Q9 口径 |
| 7 | 兼岗并集 | 用户同时挂后端组与前端组 | 两组文档**都可见** | Q4 口径 |
| 8 | ★ 上级看下级 | 用户在后端组；文档属 `dept:/总部/技术中心/`（上级） | **可见** | A1 祖先展开 |
| 9 | ★ 同级不可见 | 用户在后端组；文档属 `dept:/总部/技术中心/前端组/` | **不可见** | 兄弟隔离 |
| 10 | ★★ 防全公司互通回归 | ①构造两个兄弟部门各一份文档，断言互相不可见；②**结构断言**：`compute_doc_acl_tags()` 的输出 MUST NOT 含任何祖先路径 | 全部通过 | **H1 · 这是全篇最危险的一条** |

> **用例 3b / 5 / 9 / 10 是"口径对、实现错、测试全绿"型的泄漏面。**
> 它们不是边界情况，是**最常见的错法**。这四条必须有，且必须单独命名。

**额外：组织规模核验（M3 结束前跑一次）**

```sql
-- 看最大子树有多大：决定用户主体是"内存展开"还是"预计算闭包表"
SELECT d.path,
       (SELECT count(*) FROM departments d2
         WHERE d2.tenant_id = d.tenant_id
           AND d2.path LIKE d.path || '%') AS subtree
  FROM departments d
 ORDER BY subtree DESC
 LIMIT 5;
```

| 结果 | 动作 |
|---|---|
| 最大子树 ≤ `SUBJECT_EXPANSION_SOFT_LIMIT`（300） | 继续内存展开，不用优化 |
| 超过 300 | 记录告警；考虑预计算部门闭包表 |

> 单人项目里这棵树是你自己造的种子数据，**一定会很小**。
> 但真实客户可能有两千人的组织——这条 SQL 是"交付给客户前"的必检项。

### 7.3 RAG 评估集

**规模**：30 条起（20 可答 + 10 不可答），随迭代增长。

**结构**（`eval_cases` 表）

| 字段 | 说明 |
|---|---|
| `question` | 问题原文 |
| `expected_answerable` | 应当答得出来吗 |
| `expected_doc_ids` | 应当命中的文档（可答时为必填） |

**指标**（每次改检索/分块/提示词后重跑）

| 指标 | 定义 | 目标 |
|---|---|---|
| 拒答正确率 | 不可答问题中正确拒答的比例 | **≥ 0.9** |
| 漏答率 | 可答问题中被错误拒答的比例 | **≤ 0.1** |
| 命中率 | 可答问题中引用到期望文档的比例 | **≥ 0.8** |
| 引用准确率 | 引用片段确实支撑答案的比例 | **≥ 0.9** |

> **没有评估集，`RELEVANCE_THRESHOLD` 就无法标定，整个"只依据知识库作答"的承诺就没有度量。**
> 它是 M2 的必做项，不是可选项。

### 7.4 每步的强制检查（AI Coding 提交前自检）

```bash
# 1. 口径未被改动
python -c "from app.config.decisions import self_check; self_check()"

# 2. 权限用例全绿
pytest tests/acl/ -v

# 3. 评估集未退化
python scripts/run_eval.py --assert-refusal-rate 0.9 --assert-miss-rate 0.1

# 4. 代码围栏与规范
ruff check . && mypy app/
```

> 单人项目的 Code Review 就是这四条命令。**MUST 在每次提交前跑。**

**本手册已附可运行骨架**（同目录 `code/`）：`app/config/decisions.py` + `app/services/acl/` + `tests/acl/test_visibility.py`（十条用例已实现，含 9 条补充，共 19 条）。
开 M4 之前先跑一次：

```bash
cd code
python -m pytest tests/acl -v                        # 期望 19 passed
python -c "from app.config.decisions import self_check; self_check()"
```

> AI Coding 要做的是**把这个骨架接上数据库**（把内存版 `DeptNode` / `Chunk` / `UserRecord` 换成 SQLAlchemy 实现，把 `is_visible()` 搬进 `PUSHDOWN_WHERE_SQL`），
> **NOT 重新发明判定逻辑**。判定式与十条用例 MUST 逐字保留。

---

---

## 8. 决策清单（已定稿 · 2026-09-16）

> **16 条全部已定（2026-09-16），取值即正文口径。** 张枭批复：全部按推荐值执行。
> 表格里的「已定值」列就是最终口径，正文各节已按本表更新；AI MUST 按此实现。
>
> **状态**：✅ **已定** = 可直接开工；⏳ 到点可调 = 到该里程碑前再确认一次（调了不影响 M0）。
> 决策若需变更，走 §0.1 R1：**先改本文档，再改代码**。

### 8.1 速览表

| # | 问题 | 已定值 | 状态 |
|---|---|---|---|
| **D-01** | 交付形态：私有化 / SaaS / 都要 | **私有化优先 + 代码支持多租户** | ✅ **已定** |
| **D-02** | 首个目标客户画像 | **中小企业（50–500 人）** | ✅ **已定** |
| **D-03** | 是否收费、如何授权 | 暂不考虑，M6 前再定 | ⏳ 到 M6 前可调 |
| **D-04** | 是否支持扫描件 PDF（要 OCR） | **不支持**，只支持文本层 PDF | ✅ **已定** |
| **D-05** | Embedding 模型与向量维度 | **本地 `bge-m3`，1024 维** | ✅ **已定** |
| **D-06** | 中文全文检索扩展 | **先降级 `ILIKE`**，M2 评估后再决定 | ✅ 已定 |
| **D-07** | 知识源同步先做哪个 | **本地目录 + Git** | ⏳ 到 M6 前可调 |
| **D-08** | LLM 供应商 | **都要**（OpenAI 兼容协议，开发用云、交付可本地） | ✅ 已定 |
| **D-09** | Rerank 模型 | **本地 `bge-reranker-v2-m3`** | ✅ 已定 |
| **D-10** | 密级档位 | **4 档不变** | ✅ 已定 |
| **D-11** | 外部人员能否看公开库文档 | **能**（`public` 对所有登录用户成立） | ✅ 已定 |
| **D-12** ★ | 「上级看下级」是否全局生效，还是敏感部门例外 | **默认全局生效**；如有敏感部门，加**部门级开关** | ✅ 已定 |
| **D-13** | deny 的运维责任人（交付后） | **客户系统管理员**，写进交付文档 | ✅ 已定 |
| **D-14** | 部署环境资源预算 | 开发用云 API；交付给**两套配置**（全云 / 全本地） | ✅ 已定 |
| **D-15** | 审计日志保留期 | **1 年 + 定期归档** | ✅ 已定 |
| **D-16** | 上传大小上限与类型白名单 | **100 MB**；`pdf/md/txt/xls/xlsx` | ✅ 已定 |

### 8.2 已定决策的展开说明

> 以下 5 条是影响面最大的决策，**均已按推荐值采纳**。说明保留，供日后回溯理由。

#### D-01 交付形态 —— 唯一的架构级决策

| 选项 | 含义 | 代价 |
|---|---|---|
| A · 纯私有化 | 每个客户一套部署、一个租户 | 部署支持成本高；但数据不出客户内网，好卖 |
| B · 纯 SaaS | 一套服务多家客户，按租户隔离 | 运维压力全在你身上（你是单人）；且客户未必愿意把文档传出去 |
| C · 私有化优先 + 代码支持多租户 | 默认私有化交付；代码里 `tenant_id` 从第一天就有 | 前期多写一点，之后两条路都能走 |

**推荐 C。** 理由：`tenant_id` 早加是几行 DDL，晚加是全库迁移 + 所有查询重写 + 所有测试重跑。
而私有化交付对单人开发者是最现实的路径——**不需要你 7×24 运维**。

#### D-04 扫描件 OCR —— 决定"什么文档能进系统"

**推荐：不支持。** 明确写进产品说明："仅支持文本层 PDF，扫描件需先自行 OCR。"

理由：OCR 不是"接个库"就完事，它牵扯版面还原、表格识别、图片内文字、准确率调优——
**是一个独立产品的量级**。单人项目接了它，M2 会从 2–3 周变成 2–3 个月。
先用"不支持"把边界划清，客户真有需求再单独立项。

#### D-05 Embedding 模型与向量维度 —— 改一次要重灌全库

**推荐：本地 `bge-m3`（1024 维）。**

理由：中文效果好、支持长文本、**可离线交付**（私有化客户内网往往出不去）。
维度写死在 `chunks.embedding vector(1024)` 里，**换模型 = 重新嵌入全部文档**，
所以这是个 M0 就要定死的事。

可选替代：`text-embedding-3-large`（3072 维，效果更强但必须联网，且要付 API 费）。

#### D-06 中文全文检索 —— 最容易被环境坑掉的一件事

PostgreSQL 默认分词器不认识中文（会把整句话当一个词）。三种方案：

| 方案 | 效果 | 风险 |
|---|---|---|
| `zhparser` / `pg_jieba` 扩展 | 最好 | **客户环境可能装不了**（需要编译、需要超级用户） |
| 降级 `ILIKE '%词%'` | 够用 | 没有词干/排序，但对中文短查询影响不大 |
| 只用向量召回 | 最差 | 丢掉了关键词召回的精确匹配能力，专有名词/编号类查询会垮 |

**推荐：M0 先按 `ILIKE` 实现并跑通，M2 用评估集对比一次，再决定要不要上 `zhparser`。**
关键是**把这条路径抽象成一个接口**，换实现不动上层。

#### D-12 ★ 「上级看下级」要不要例外 —— 这条是原方案里悬着的合规风险

原方案里挂着一个待办："上级能看到整个子树的文档，在财务/法务/人事这类敏感部门可能超出预期"。

你现在是单人、自己拍板，所以这条可以定下来了。三个选项：

| 选项 | 含义 |
|---|---|
| **A · 默认全局生效**（推荐） | 简单，规则统一。客户有意见时再说 |
| B · 加部门级开关 | 部门表加 `visible_to_parent BOOLEAN`，敏感部门设 `false` |
| C · 反过来：只有标注了的部门才向上可见 | 更保守，但客户会抱怨"为什么看不到" |

**推荐 A + 预留 B**：默认全局生效；但**在 `departments` 表里预留 `visible_to_parent` 字段**
（默认 `true`），M4 实现判定时把这个字段接进去（代价不到 20 行）。
这样客户真提出来时，你只需要在界面上加个开关，不用改判定逻辑。

---

## 9. 单人开发的现实提醒

### 9.1 排期

| 场景 | 时间 | 依据 |
|---|---|---|
| 全职（每周 40h+） | **12–18 周** | 原方案 4 人 14–16 周，单人不会更慢太多——因为少了沟通成本——但会更多卡在"只有你一个人会" |
| 业余（每周 10–15h） | **6–9 个月** | 主要风险不是慢，是**中断后忘记上下文** |
| 只想先看到东西 | **M0–M2 约 4–5 周** | 到 M2 结束，你已经有一个"能真实问答、会拒答"的东西可以演示了 |

> 排期按 §8 D-02 的**中小企业（50–500 人）**客户规模估算。
> 若首个客户的部门树超过 ~300 个节点，M4 的用户主体展开需要上预计算闭包表，**+1–2 周**（核验 SQL 见 §7.2 末）。
> 上传限制（§8 D-16：100 MB / `pdf,md,txt,xls,xlsx`）与「不做 OCR」（D-04）已按此规模设定，不需要再调。

**业余开发的一条具体建议**：每次开工前先读 §0.1 三条元规则 + 上一个里程碑的验收表。
中断两周后，你记不住的口径，AI 更记不住。

### 9.2 三个会拖死你的地方

| # | 症状 | 为什么致命 | 怎么破 |
|---|---|---|---|
| 1 | 想在 M4 一次把权限全口径做完 | M4 是 **2–3 周**不是 3 天。做着做着发现组织树、缓存失效、公开库互相牵连 | 严格按 M4 的 9 个任务顺序做，每做完一个跑一遍对应用例 |
| 2 | 想支持所有文档格式 | 每加一种格式都是新的解析适配 + 新的分块策略 + 新的边界情况 | 先把 `pdf/md/txt` 做到"引用能定位原文"，`xls/xlsx` 后置 |
| 3 | 不停优化检索效果，永远到不了 M5 | 检索效果是无底洞，没有"够了"的标准 | **用评估集当刹车**：指标过了 §7.3 的目标就停，进下一个里程碑 |

### 9.3 三个必须守住的东西

| # | 东西 | 为什么 |
|---|---|---|
| 1 | **`self_check()` + 10 条权限用例** | 单人项目没有 Code Review。**这两样就是你的 Review**。它们红了就是有人（包括 AI）改坏了口径 |
| 2 | **每个里程碑的"能演示"** | 它把"进度"从主观感受变成客观事实。做不到演示 = 没完成 |
| 3 | **评估集** | 没有它，你**不知道**自己是变好了还是变坏了。改一次分块参数，感觉上更好了，指标上可能拒答率崩了 |

### 9.4 什么时候该砍

**如果 6 个月后还没到 M4**，砍掉：

| 砍掉 | 理由 |
|---|---|
| M6 的任务 4 / 5 / 6（可观测、评估门禁、灰度） | 有日志就能排查，指标和灰度可以后补 |
| M6 的任务 3（安全加固中的"敏感词"部分） | 私有化部署下优先级最低 |
| M5 的任务 2（脱敏） | 客户没提就先不做 |
| M6 的任务 1（多数据源）只留 1 个源 | 一个能用的源 > 五个半成品 |

**不能砍的**：M2 的 L3 出口校验、M2 的评估集、M4 的 10 条用例。
这三样砍了，产品就不是"只依据知识库作答"了，只是一个套了壳的聊天机器人。

---

## 附录

### 附录 A · 建议目录结构

```
knowledge-agent/
├── docker-compose.yml
├── docker-compose.prod.yml
├── .env.example
├── apps/
│   ├── api/
│   │   ├── app/
│   │   │   ├── main.py                 # FastAPI 入口，启动时调 self_check()
│   │   │   ├── config/
│   │   │   │   ├── settings.py         # 环境变量
│   │   │   │   └── decisions.py        # ★ §3.5 逐字落盘
│   │   │   ├── api/                    # 路由层（只做参数校验与转发）
│   │   │   ├── services/
│   │   │   │   ├── acl/                # ★ 权限：主体解析 / 标签生成 / 缓存
│   │   │   │   ├── parse/              # 解析器（按扩展名分派）
│   │   │   │   ├── chunk/              # 分块
│   │   │   │   ├── retrieve/           # 混合召回 + RRF + 重排
│   │   │   │   ├── generate/           # 提示词 + 流式生成
│   │   │   │   └── grounding/          # ★ L3 出口校验
│   │   │   ├── models/                 # SQLAlchemy
│   │   │   ├── schemas/                # Pydantic
│   │   │   └── workers/                # arq 任务
│   │   ├── alembic/
│   │   ├── tests/
│   │   │   └── acl/                    # ★ 10 条用例
│   │   └── scripts/
│   │       ├── seed.py                 # 幂等种子数据
│   │       └── run_eval.py             # 评估集
│   └── web/
│       └── src/
│           ├── pages/                  # 登录/问答/知识库/文档/管理
│           ├── components/
│           ├── mocks/                  # M1 的假数据层
│           └── lib/api/                # 类型 MUST 与 §5.2 一致
└── packages/shared/                    # 前后端共享的类型定义
```

### 附录 B · 环境变量清单

```bash
# ── 基础 ─────────────────────────────────
APP_ENV=dev                            # dev | prod
SECRET_KEY=change-me
TENANT_CODE=default                    # 私有化部署固定为 default

# ── 数据库 ───────────────────────────────
DATABASE_URL=postgresql+asyncpg://user:pass@postgres:5432/kagent
REDIS_URL=redis://redis:6379/0

# ── 对象存储 ─────────────────────────────
S3_ENDPOINT=http://minio:9000
S3_ACCESS_KEY=minioadmin
S3_SECRET_KEY=minioadmin
S3_BUCKET=kagent-docs

# ── 模型（全部走 OpenAI 兼容协议）─────────
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=
LLM_MODEL=deepseek-chat

EMBEDDING_BASE_URL=http://embedding:8000/v1   # 指向本地 bge-m3 服务
EMBEDDING_MODEL=bge-m3
EMBEDDING_DIM=1024                            # ★ 改了必须重灌全库

RERANK_BASE_URL=http://reranker:8000/v1
RERANK_MODEL=bge-reranker-v2-m3

# ── 上传限制 ─────────────────────────────
MAX_UPLOAD_MB=100                        # §8 D-16
ALLOWED_EXT=pdf,md,txt,xls,xlsx          # 不含扫描件 OCR（§8 D-04）
AUDIT_RETENTION_DAYS=365                 # §8 D-15 保留 1 年，超期归档
MULTI_TENANT=false                       # §8 D-01 私有化交付=单租户；SaaS 时改 true
```

### 附录 C · 给 AI Coding 的开工指令（可直接粘贴）

```
你正在按《知识库问答 Agent · AI Coding 开发手册》实现功能。

开始前 MUST：
1. 读 §0.1 三条元规则。
2. 读 §3.5 的 decisions.py，并将它逐字写入 app/config/decisions.py。
3. 确认你要实现的模块在 §6 属于哪个里程碑，并只做那个里程碑范围内的事。

工作中 MUST：
4. 任何常量从 app/config/decisions.py 读取，MUST NOT 硬编码。
5. 涉及权限判定时，严格按 §3.2 实现；MUST NOT 自行"简化"或"优化"。
6. 每写完一个可运行单元，跑 §7.4 的四条命令。

遇到下列情况 MUST 停下来提问，MUST NOT 自己决定：
7. 手册未覆盖的分叉。
8. 你认为手册的口径有问题（写出你的理由和替代方案）。
9. 需要改动 §3 的任何取值。

汇报格式：
- 改了什么文件、为什么
- 跑了哪些测试、结果如何
- 有哪些地方你做了判断（列出，我来确认）
```

### 附录 D · 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| `v1.0` | 2026-09-16 | 首版。单人开发 / 权限全保留 / React + Python / 可交付产品。 7 个里程碑替代原 16 步；新增多租户；新增 §8 决策清单 |
| `v1.1` | 2026-09-16 | §8 决策清单 16 条**全部定稿**（全部按推荐值），并落回正文：D-02 目标客户 / D-04 排除 OCR / D-05 锁定 `bge-m3` 1024 维 / D-06 中文检索先 `ILIKE` / D-07 首个数据源 / D-12 `departments.visible_to_parent` / D-13 deny 运维责任人 / D-14 两套部署配置 / D-15 审计保留 1 年 / D-16 上传限制。**新增 `code/` 可运行骨架（19 条测试全绿）** |

---

*本文档为自包含交付物，不依赖任何外部文件。*
*全文代码围栏成对，口径取值集中在 §3.5。*
*§3.5 的 `decisions.py` 与 §7.2 的十条用例已落成可运行骨架，见同目录 `code/`（19 条测试全绿）。*

