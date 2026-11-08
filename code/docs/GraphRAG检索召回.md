# GraphRAG 检索召回（代码真源版）

> **文档目的**：把 GraphRAG 的**三表 Schema、抽取写入、在线召回**三条链路按「实体图 → 邻居 chunk」主线钉死，作为 `code/apps/api/app/services/graph/` 与 `code/apps/api/app/models/entities.py` 的权威参考。所有常量均标注 `文件:行号`，可回源码复算。
>
> **关联文档**：主链路全景见 [`检索链路全景.md`](./检索链路全景.md)（§9 图召回 / §10 wiki 导航）；按需路由的开关、阈值与标定计划见 [`按需路由设计.md`](./按需路由设计.md)。
>
> **口径版本**：图召回 v1.2 契约（`CONTRACT_VERSION = "v1.1"`，`app/config/decisions.py:20`），图路 `path_stats` 写 `graph_covered` / `graph_unique`。
>
> **真源与可复算性说明**：结构、SQL 片段、函数落点来自对源码的逐行核对（含 `文件:行号` 引用），可回查。行号对应当前源码，变更后请以实际为准。

---

## 0. 一图流（GraphRAG 数据流）

1. **抽取（写，worker 侧）**：解析完成后 `graph_job` 固定顺序 `delete_by_document()` → `upsert_subgraph()`，把一篇文档的实体 / 提及 / 关系落三表。
2. **在线召回（读，api 侧，side channel）**：`retrieve()` 第 5 步（L1 后、章节前）调 `recall_by_graph()`，从命中片里的实体出发扩邻居 chunk，分数恒 0，单独放 `result.graph_recall_chunks`，**不进 RRF / 排序 / 阈值**。
3. **权限**：图路看到的 chunk 集合必须等于向量路可能看到的集合 —— 取片 SQL **逐字内联** `retrieve/__init__.py` 的 `_WHERE_BASE`（四闸门 G1~G4 + is_latest），通过 `graph_entity_mentions JOIN chunks` 下推，ACL 不冗余存图侧。

---

## 1. Schema：三张表

> 模型定义见 `code/apps/api/app/models/entities.py`（`GraphEntity` / `GraphEntityMention` / `GraphRelation`）。迁移见 `alembic/versions/0024_add_graph_tables.py`。

| 表 | 角色 | NOT-NULL / 唯一约束 | ACL 落点 |
|---|---|---|---|
| `GraphEntity`（`entities.py:340-381`） | 实体，**KB 内合一**：同一实体在 A/B 文档的提及指向同一行，跨文档邻居扩展才有意义 | `name_key` NOT NULL；`uq_graph_entities_kb_key(kb_id, name_key)` 唯一；`entity_type` 受 `ck_graph_entities_type` 8 类 CHECK | **无** acl 字段（实体是 KB 级共享对象） |
| `GraphEntityMention`（`entities.py:384-421`） | 实体提及，**图 → 原文的唯一回链**，也是 ACL 的落点 | `chunk_id` / `entity_id` NOT NULL；`uq_graph_mentions_chunk_entity(chunk_id, entity_id)` 唯一 | **无** acl_tags / level_rank（设计 §8.4）：可见性一律 JOIN 回 `chunks` 用四闸门判定，冗余权限副本在文档改密级时不会跟着变 |
| `GraphRelation`（`entities.py:424-463`） | 实体关系，**边只在文档维度记账**；边永不被引用，只是遍历线索，`support_count` 作路径强度权重 | `src_entity_id` / `dst_entity_id` NOT NULL；`uq_graph_relations_triple(src, dst, relation, document_id)` 唯一；`ck_graph_relations_not_self` 自环 CHECK | **无** acl 字段 |

要点：

- **ACL 只在 `chunks` 表**（`entities.py:233-305` 有 `acl_tags` / `level_rank` / `deny_subjects` + `is_latest`）。图召回 / wiki 导航读取时一律通过 `mentions JOIN chunks`（或 wiki 页 `EXISTS` 内联 `_WHERE_BASE`）下推四闸门，绝不另存权限副本（`models/entities.py:387-390`、`884-896`）。
- **合并键 `name_key`**：`GraphEntity.name_key`（`entities.py:359`）是 (kb_id, name_key) 唯一键，由 `extract.normalize_name_key` 同一函数产出（见 §2）。
- 三表都有口径双列 `extractor_model` / `prompt_version`（`entities.py:411-412`、`449-450`），检索 MUST 按这两列过滤；旧口径 = 查不到，不混算（`postgres_store.py:8-11`）。
- `GraphEntity.name_vec` 是生成列（`to_tsvector('simple', name_fts)`，persisted，`entities.py:364-366`），应用侧 MUST NOT 写入；`name_fts` 写入侧由 `keyword_text.to_fts_text(name, "")` 生成，与查询侧 `build_fts_query` 同源。

---

## 2. 抽取写入（Extraction）

### 2.1 `upsert_subgraph`（合并语义）

`apps/api/app/services/graph/postgres_store.py:44-86`：写入实体 / 提及 / 关系，返回写入的关系行数。

要点（`postgres_store.py:12-18`、`55-61`）：

- **实体 ON CONFLICT 合并**：`_upsert_entities`（`:88-136`）按 `(kb_id, name_key)` 冲突时 DO UPDATE，更新 `name` / `name_fts` / `entity_type` / `aliases` + `updated_at`；`name_vec` 是生成列自动重算。同一实体跨文档共享，A 建、B 补提及。
- **提及 DO NOTHING**：`_insert_mentions`（`:138-177`）按 `(chunk_id, entity_id)` 冲突保持原样；指向已丢弃实体（超长名 / 类型非法）的提及跳过而非造行。
- **关系 support_count 赋值而非累加**：`_insert_relations`（`:196-251`）`on_conflict_do_update` 的 `support_count = excluded.support_count`（同文档同三元组重跑不翻倍）；同文档内多次表述先在 `_merge_relations`（`:179-194`）按 `(src, dst, relation)` 聚合成确定值（幂等前提）。
- **幂等由 ON CONFLICT 保证，删除由调用方保证**：重抽后不再出现的提及只能靠先删 —— 故 `graph_job` 固定顺序 `delete_by_document()` → `upsert_subgraph()`（`postgres_store.py:14-15`、`253-312`）。`delete_by_document` 清理因本文档失去提及的孤儿实体并**重算** `doc_freq`（count(DISTINCT document_id)，不增量）。

### 2.2 `ExtractionResult` 形状

`apps/api/app/services/graph/base.py:23-58`：

```python
@dataclass
class ExtractedEntity:
    name: str
    name_key: str        # 归一化键（KB 内唯一键），由 extract.normalize_name_key 产出
    entity_type: str
    aliases: list[str] = field(default_factory=list)

@dataclass
class ExtractedMention:
    entity_key: str      # 指向 ExtractedEntity.name_key
    chunk_id: UUID
    surface: str
    confidence: float | None = None

@dataclass
class ExtractedRelation:
    src_key: str         # ExtractedEntity.name_key
    dst_key: str
    relation: str

@dataclass
class ExtractionResult:
    entities: list[ExtractedEntity] = field(default_factory=list)
    mentions: list[ExtractedMention] = field(default_factory=list)
    relations: list[ExtractedRelation] = field(default_factory=list)
    # + extractor_model / prompt_version（写入口径双列）
```

辅助类型：`GraphNeighbor(chunk_id, hop, weight)`（`base.py:66-72`）、`GraphRecallResult(degraded_reason, ...)`（`base.py:75-81`，`degraded_reason=""`=正常，取值 `"timeout"/"error"/"no_entities"/"no_neighbors"`）。

### 2.3 `normalize_name_key`（实体名归一）

`apps/api/app/services/graph/extract.py:85-92`：

```python
def normalize_name_key(name: str) -> str:
    """NFKC + 折叠空白 + lower；这是唯一归一实现。"""
    return _WS_RE.sub(" ", unicodedata.normalize("NFKC", name)).strip().lower()
```

**★ 唯一真源**：抽取侧（建实体 `name_key`）与链接侧（查询 → 种子实体）都 MUST 调本函数。两处各写一份，一个字符处理不同 → 查询永远匹配不上库里的键，表现为「实体明明抽出来了却一条也链不上」，不报错（`extract.py:88-90`、`entities.py:345-348`）。

---

## 3. 在线召回 `recall_by_graph`（side channel）

`apps/api/app/services/graph/recall.py:150-311`。★ 第四上下文来源，但语义与三路召回完全不同（`recall.py:1-26`）：

- 产物单独放 `RetrievalResult.graph_recall_chunks`，分数恒 0，**不进 RRF / 排序 / 阈值 / 拒答判定**（只顺带多给模型几片相关原文）。
- **永不抛异常**（这是契约）：图库抖动 / 超时 / 语法问题都不该让一次问答失败 → 三路照常返回。失败 MUST 可见（`degraded_reason` + 调用方打点），不是静默吞掉。
- **失败 MUST rollback、成功 MUST 还原 statement_timeout**（见 §3.3）。

### 3.1 八步流程

调用签名（`recall.py:150-158`）：`recall_by_graph(session, *, query, pushdown, timeout_ms, profile, exclude_chunk_ids)`。8 步收进内层 `_steps()`（`recall.py:182-273`），让还原点只有一处：

| 步 | 内容 | 落点 |
|---|---|---|
| ① | DB 侧超时：读原值 → `set_config('statement_timeout', :ms, true)`（事务级） | `:286-290`（见 §3.3） |
| ② | 查询 → 种子实体：`link_entities(...)`（带 tenant/kb/口径） | `:184-191` |
| ③ | 空种子 = 正常态（问句没提到库内实体），返回 `degraded_reason="no_entities"`，不告警 | `:193-194` |
| ④ | 图扩展：`get_graph_store().neighbors(...)`（四闸门在存储层内下推） | `:197-205` |
| ⑤ | 空邻居（只有孤立实体），返回 `"no_neighbors"`，带 `seed_entity_count` | `:207-208` |
| ⑥ | 排序 + 截断：键 `(hop ASC, weight DESC, chunk_id ASC)`，截 `GRAPH_RETRIEVE_TOP_K`（与存储层逐字一致，保证同把尺子） | `:215-217` |
| ⑦ | 取 chunk 行：逐字复用 `_WHERE_BASE`（延迟 import），组装 `RetrievedChunk` | `:219-228`、`:129-147` |
| ⑧ | 预算裁剪：**先排重、后裁剪**（见 §3.2） | `:230-255` |

成功路径（含空种子 / 空邻居）最后一条 SQL 是还原（`:309`）；失败路径已 rollback，不再发还原 SQL（`:307-308`）。

### 3.2 先排重后裁剪（dedup-before-trim）

`recall.py:237-255`（契约要求，非优化项）：

```python
exclude = exclude_chunk_ids if exclude_chunk_ids is not None else frozenset()
graph_covered = sum(1 for n in neighbors if n.chunk_id in exclude)  # 裁剪前统计

chunks: list[RetrievedChunk] = []
used_tokens = 0
for neighbor in neighbors:
    row = by_id.get(neighbor.chunk_id)
    if row is None:
        continue                                  # 抽取后被重解析 / 软删：图数据在 chunk 行没了
    if neighbor.chunk_id in exclude:
        continue                                  # 已在命中片：不占 GRAPH_RECALL_MAX_CHUNKS 名额
    if len(chunks) >= decisions.GRAPH_RECALL_MAX_CHUNKS:
        break
    row_tokens = int(row.token_count or 0)
    if used_tokens + row_tokens > decisions.GRAPH_RECALL_TOKEN_BUDGET:
        break
    chunks.append(_build_chunk(row, filename=getattr(row, "filename", "") or ""))
    used_tokens += row_tokens
```

- 传入的 `exclude_chunk_ids = frozenset(c.chunk_id for c in retrieved)`（最终命中片 id 集合，`retrieve/__init__.py:164`）。
- 邻居按 `(hop, weight)` 排序，权重最高的一跳邻居**恰恰最可能已在命中片**，若不先排重会系统性聚集队首吃掉 `GRAPH_RECALL_MAX_CHUNKS=3` 名额，真正新增片被裁掉（`recall.py:166-171`）。
- `graph_covered` 在**全部**截断后邻居上统计（判据裁剪前口径），度量「重复探索量」，不该被预算截断污染（`:235-238`）。

### 3.3 三段式 `statement_timeout` 事务级还原

`recall.py` 内（`:86-127`、`:277-310`）：

- **① 保存原值**：`previous_timeout = await _read_statement_timeout(session)`（`:286`）→ 读 `current_setting('statement_timeout')`，缺省回退 `"0"`（`:93-97`）。
- **② 设超时**：`set_config('statement_timeout', :ms, true)`（`:287-290`），第三个参数 `is_local=true` = 事务级作用域（等价于 SET LOCAL）。
  - ★ MUST 走 `set_config()` 而非 `SET LOCAL`：`SET` 是工具语句不接受绑定参数，asyncpg 走扩展协议发 `$1` 会报 syntax error（`:282-285`）。
- **③ 成功还原为原值**：`_restore_statement_timeout(session, previous_timeout)`（`:309` → `:100-126`），还原点是**唯一一处**（失败路径已 rollback，事务级设置随之消失，不再发 SQL）。
  - ★ 还原成**调用前的值**而非硬编码 `"0"`：若上层配了全局 statement_timeout，写死 0 会悄悄放宽（`:113-114`）。
- **rollback 路径**（`:293-305`）：`TimeoutError`（asyncio.timeout 或 PG 取消）与通用 `Exception` 都只 `await _rollback_quietly(session)`（`:78-83`，吞掉 rollback 自身异常，连接可能已断），不掩盖原始错因。

### 3.4 fail-open（永不抛）

`recall_by_graph` 任何异常 / 超时 → 返回空 `GraphRecallOutcome`（`degraded_reason="timeout"/"error"`），三路召回照常返回，用户无感（`recall.py:9-17`、`293-305`）。图降级不阻断主链路，仅 `graph_degraded_reason` 透传（`检索链路全景.md` §8）。

---

## 4. 图召回如何接入「按需路由」

> 详见 [`按需路由设计.md`](./按需路由设计.md)。此处只列接入点。

- 两个 **opt-in 开关**（`config/settings.py`）：`graph_recall_selective: bool = False`（`:157`）、`wiki_nav_selective: bool = False`（`:183`），**默认关 = 行为与未引入路由逐字节相同**。
- 两个**纯函数**（`retrieve/__init__.py:45-68`）：`_graph_should_recall(top1_score, retrieved_n)`（`:45`）、`_wiki_should_nav(ilike_terms)`（`:58`）—— default-off 时恒 `True`。
- 接入点（`retrieve/__init__.py`）：
  - 图门（`:574-578`）：`if get_settings().graph_enabled:` 且 `not _graph_should_recall(retrieved[0].final_score, len(retrieved))` → `metrics.incr("rag.graph.skipped_by_router")`，跳过 `recall_by_graph`。
  - wiki 门（`:288-291`）：`wiki_run = wiki_on and bool(ilike_terms) and _wiki_should_nav(ilike_terms)`；若 `wiki_on and ilike_terms` 但被路由跳过 → `metrics.incr("rag.wiki.nav.skipped_by_router")`。
- **路由阈值（2026-11-08 图方向已定值）**：`GRAPH_SKIP_SCORE=0.70`（`decisions.py:930`）、`GRAPH_SKIP_MIN_CHUNKS=8`（`decisions.py:940`，仍为站位值）、`WIKI_NAV_SELECTIVE_MIN_TERMS=3`（`decisions.py:131`，仍为站位值）。定值依据：实测 217 条 `eval_cases`（原文 `question` + 单趟 `retrieve()` + 全 4 库、vector 口径）`top1.final_score` `max=0.8306` / `p90=0.7427` / `>=0.70` 为 51 条 / 原 `>=0.85` 触发 0 次。**原文口径 ≠ 生产口径**：生产链路含 LLM 改写与 `retrieve_chain()` 多轮，实际触发率须以 `/metrics` 实测为准，开启 rerank 后须重新标定（`按需路由设计.md` §2.1）。

---

## 5. `path_stats` v1.2 契约贡献

写入区 `retrieve/__init__.py:765-802`（详见 `检索链路全景.md` §11）。图路仅 `graph_enabled` 时写（`:792-795`）：

```python
neighbor_ids = {n.chunk_id for n in graph_outcome.neighbors}
path_stats["graph_covered"] = len(neighbor_ids & retrieved_ids)  # 重复探索量（= 邻居里已命中片数）
path_stats["graph_unique"]  = len(graph_chunks)                  # 真正补进上下文的片数
```

- v1.2 用「实际补进上下文的片数（`graph_unique`）」而非 v1.1 的「候选池分母」口径，更贴近真实增益（`检索链路全景.md` §11）。`graph_covered` 与召回侧 `graph_covered`（`recall.py:238`）口径一致：均在预算裁剪**前**对全部 ≤TOP_K 邻居统计。
- `RetrievalResult` 的 `graph_recall_chunks` / `path_stats` 随结果返回（`:812`），`chat.py` 的 `**result.path_stats` 自动带进 SSE / 审计。

---

## 6. 关键常量速查（可复算）

> 下表由源码逐行核对；改任一值 = 评测基线不可比，需同步回归。

| 常量 | 值 | 文件:行 |
|---|---|---|
| `GRAPH_RETRIEVE_TOP_K`（候选邻居上限） | 10 | decisions.py:912 |
| `GRAPH_RECALL_MAX_CHUNKS`（进上下文上限） | 3 | decisions.py:918 |
| `GRAPH_RECALL_TOKEN_BUDGET`（图片总 token） | 1200 | decisions.py:924 |
| `GRAPH_MAX_HOPS`（扩展跳数） | （硬上限 self_check 校验） | decisions.py:879 |
| `graph_enabled`（settings，默认关） | False | settings.py:153 |
| `graph_recall_selective`（settings，默认关） | False | settings.py:157 |
| `graph_retrieve_timeout_ms`（settings） | 800 | settings.py:167 |
| `GRAPH_SKIP_SCORE`（路由跳过阈值，2026-11-08 已定值） | 0.70 | decisions.py:930 |
| `GRAPH_SKIP_MIN_CHUNKS`（路由召回充足门槛，站位） | 8 | decisions.py:937 |
| `WIKI_NAV_SELECTIVE_MIN_TERMS`（wiki 路由门槛，站位） | 3 | decisions.py:131 |
| `ILIKE_MAX_TERMS`（顶层词元上限，wiki 路由上界） | 3 | decisions.py:73 |

> `GRAPH_SKIP_SCORE=0.70` 的定值依据（2026-11-08 实测 217 条 eval_cases）：原文 `question` + 单趟 `retrieve()` + 全 4 库、vector 口径下 `top1.final_score` `max=0.8306` / `p90=0.7427` / `>=0.70` 为 51 条 / 原 `>=0.85` 触发 0 次。**原文口径 ≠ 生产口径**：生产链路含 LLM 改写与 `retrieve_chain()` 多轮，实际触发率须以 `/metrics` 实测为准；开启 rerank 后须重新标定。详见 `按需路由设计.md` §2.1。

---

## 7. 文件落点索引

- 三表模型：`code/apps/api/app/models/entities.py`（`GraphEntity` / `GraphEntityMention` / `GraphRelation`）
- 在线召回：`code/apps/api/app/services/graph/recall.py`（`recall_by_graph` / `GraphRecallOutcome`）
- 存储实现：`code/apps/api/app/services/graph/postgres_store.py`（`upsert_subgraph` / `neighbors` / `delete_by_document`）
- 端口类型 / 抽取结果形状：`code/apps/api/app/services/graph/base.py`（`ExtractionResult` / `GraphNeighbor` / `GraphRecallResult`）
- 实体名归一：`code/apps/api/app/services/graph/extract.py`（`normalize_name_key`）
- 实体链接（查询 → 种子）：`code/apps/api/app/services/graph/linking.py`
- 按需路由接入：`code/apps/api/app/services/retrieve/__init__.py`（`_graph_should_recall` / `_wiki_should_nav` / 图门 `:574` / wiki 门 `:288`）
- 口径常量 / 启动自检：`code/apps/api/app/config/decisions.py`、`code/apps/api/app/config/settings.py`
- 主链路全景（含图/wiki/ACL 旁路）：[`检索链路全景.md`](./检索链路全景.md)
- 按需路由决策 / 阈值 / 标定计划：[`按需路由设计.md`](./按需路由设计.md)
