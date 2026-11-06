---
title: 独立预算扩展
topic: retrieval-architecture
entity_type: 方法
aliases: [独立预算, extra_terms, extra_index_tokens, 不截断主词元, 别名预算]
sources: [raw/2026-11-06-增量知识条目.md]
schema_version: v1
---
# 独立预算扩展

## 摘要
给查询构造追加词元（lexicon（词表）别名、wiki 导航术语）时，必须走**独立于主查询窗口的预算**，用 keyword-only 默认参 `extra_terms` / `extra_index_tokens` 承载，禁止把扩展词元 append（追加）进主 `ilike_terms` 后再按 `FTS_QUERY_MAX_TERMS`（全文检索最大词元数）截断。

## 定义
主查询窗口（向量 / tsquery（全文检索查询）/ ILIKE（大小写不敏感匹配）三路共用的核心词元）冻结在 `FTS_QUERY_MAX_TERMS`；扩展词元各自走独立上限：`LEXICON_FTS_ALIAS_MAX_TERMS`（别名）、`WIKI_NAV_MAX_FTS_TOKENS`（导航），由 `build_ilike_pattern(..., extra_terms=...)` 与 `build_fts_query(..., extra_index_tokens=...)` 接收。

## 关键论点
- 真实 bug（C4/F6）：`keyword_text.py` 的 `[:FTS_QUERY_MAX_TERMS]` 在主查询窗口达上限时，会**静默吞掉** lexicon 别名——别名和主词元挤在同一个列表里，先到先得，主词元占满后别名被截断，主召回路径悄然退化。
- append 后截断的代价：别名是「锦上添花」，主词元是「保命」；把两者放进同一上限，等于让扩展词元有机会饿死主召回。
- 独立预算范式统一了两类扩展（别名 + wiki 导航），避免「同一段 4 行代码改两次、两次各跑 78 分钟全量评测」的浪费。
- 关闭态必须零成本：当 `extra_*` 为空时，`build_ilike_pattern` / `build_fts_query` 逐字节等价于不传扩展（Golden 快照 `golden_wiki_nav_baseline.json` 钉死）。

## 证据 / 来源
- 出自 [[hybrid-recall]]；原文见 raw/2026-11-06-增量知识条目.md §2
- 设计见 `02-design/C4-别名静默截断影响面量化.md`；代码见 `apps/api/app/services/keyword_text.py` 的 `build_ilike_pattern` / `build_fts_query`

## 关联
- 基础：[[hybrid-recall]]
- 对照：[[closed-state-equivalence]]
- 应用：[[graph-recall-v12-contract]]
