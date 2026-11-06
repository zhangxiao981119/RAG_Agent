---
title: 图召回 v1.2 契约口径
topic: graphrag-integration
entity_type: 方法
aliases: [v1.2 契约, graph_unique, graph_covered, 候选池分母]
sources: [raw/2026-11-06-增量知识条目.md]
schema_version: v1
---
# 图召回 v1.2 契约口径

## 摘要
图召回的两枚观测口径在 v1.2 里被重新定义为「交付上下文」口径：`graph_unique = len(graph_recall_chunks)`（最终进上下文的图片数），`graph_covered = len(neighbor_ids & retrieved_ids)`（邻居中已在命中片的条数，预算裁剪前统计）。v1.1 的「三路候选池分母」被推翻——真库实测证明它让 `graph_unique` 结构性恒 0。

## 定义
- `graph_unique`：最终进入响应上下文的图召回片数，不是候选池大小。
- `graph_covered`：图邻居集合与主检索命中片集合的交集大小，在预算裁剪之前统计。
- `graph_degraded_reason`：图召回 fail-open（失败时开放降级）时贯穿响应 / 拒答 / SSE（服务端推送事件）观测 / 评测聚合的原因字段。

## 关键论点
- v1.1 用「三路候选池」做分母，分母里天然包含已被主检索命中的片，导致 `graph_unique` 永远算成 0，图召回看起来「从没补进任何东西」。
- 真库实测（dev 库）纠正了这一点：按交付上下文口径，`graph_unique` 能正确反映旁路（side channel）实际补入的片数。
- 口径是契约：设计 / 代码 / 测试 / 评测报告必须统一按 v1.2，不能各写各的分母。
- 与 [[dedup-before-trim]] 配套：`graph_covered` 正是排重后统计出来的。

## 证据 / 来源
- 出自 [[side-channel-recall]]；原文见 raw/2026-11-06-增量知识条目.md §3
- 口径裁决见 `02-design/架构设计文档.md` §9.8；代码见 `apps/api/app/services/retrieve/__init__.py` 的 `path_stats`

## 关联
- 基础：[[side-channel-recall]]
- 配套：[[dedup-before-trim]]
- 应用：[[graph-acl-pushdown]]
