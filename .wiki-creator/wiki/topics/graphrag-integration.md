<!-- 本文件由 build_index.py 自动生成，请勿手动编辑 -->
# 图召回接入

共 3 页。

## 概念
- [[entity-graph-store]] 实体图存储 — 实体、实体提及、关系三张表落在 PostgreSQL，用递归 CTE 走 1~2 跳；不引入图数据库，但留 GraphStore 端口以便替换实现。

## 方法
- [[graph-acl-pushdown]] 图扩展的权限下推 — 图召回从实体走到邻居 chunk 时必须复用检索链路的四道权限闸门，否则邻居分片会越权进入上下文。
- [[graph-recall-v12-contract]] 图召回 v1.2 契约口径 — `graph_unique` / `graph_covered` 按交付上下文口径定义，推翻 v1.1 候选池分母（真库实测会让 graph_unique 恒 0）。
