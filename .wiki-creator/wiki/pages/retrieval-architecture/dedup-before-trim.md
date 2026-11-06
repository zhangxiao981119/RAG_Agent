---
title: 先排重后裁剪
topic: retrieval-architecture
entity_type: 方法
aliases: [dedup before trim, 排重后裁剪, 先排重, exclude_chunk_ids]
sources: [raw/2026-11-06-增量知识条目.md]
schema_version: v1
---
# 先排重后裁剪

## 摘要
图召回把「与最终命中片重复的邻居」先跳过、不占预算名额，再按 `GRAPH_RECALL_MAX_CHUNKS`（图召回最大片数）裁剪。否则权重最高的一跳邻居往往已在命中片里，会白白挤掉真正新增的图片。

## 定义
`recall_by_graph(... exclude_chunk_ids: frozenset)` 在扩展阶段接收主检索已命中的 chunk（文档分块）集合；邻居遍历时，命中片内的邻居只用于统计 `graph_covered`，不参与预算计数。

## 关键论点
- 直觉陷阱：一跳邻居里权重最高的那批，多半就是主检索已经召回的同一批文档片；若先裁剪后去重，这些「重复片」会占满 `GRAPH_RECALL_MAX_CHUNKS` 名额，导致真正来自图结构、主检索没召回的新片被截断。
- 顺序决定结果：必须是「先排重、后裁剪」——`if neighbor.chunk_id in exclude: continue` 在预算累加之前。
- `graph_covered = sum(1 for n in neighbors if n.chunk_id in exclude)` 与预算裁剪前统计，是 [[graph-recall-v12-contract]] 里「图到底补了多少新东西」的对照基准。
- 这是 [[side-channel-recall]] 旁路（side channel）模式在图召回里的具体实现纪律，不遵守会让旁路名存实亡。

## 证据 / 来源
- 出自 [[side-channel-recall]]；原文见 raw/2026-11-06-增量知识条目.md §1
- 代码见 `apps/api/app/services/graph/recall.py` 的 `recall_by_graph` 与第 ⑧ 步

## 关联
- 基础：[[side-channel-recall]]
- 对照：[[graph-recall-v12-contract]]
- 约束：[[concurrency-capacity-check]]
