---
title: 真库探针与宿主 skip 假绿
topic: engineering-lessons
entity_type: 方法
aliases: [skip 假绿, PGlite 最小投影, 真库探针, 对抗性实测]
sources: [raw/2026-11-06-增量知识条目.md]
schema_version: v1
---
# 真库探针与宿主 skip 假绿

## 摘要
验证不能靠「最小投影 schema（PGlite 5 表）全过」或「宿主机缺库时用例显式 skip（跳过）全绿」放行。必须跑**真实目标库（26 迁移完整 schema）**的探针，并用对抗性实测（故意制造失败）证明测试真的会红。

## 定义
- PGlite 最小投影：WASM（网页汇编）PostgreSQL，只投影 5 张表的最小 schema，用于部分行为级验证，但**不是** 26 迁移真实 schema。
- 宿主 skip 假绿：测试在宿主机缺 DB（数据库）时 `pytest.skip`，结果全绿，掩盖了真实约束。

## 关键论点
- 真 bug（P1-1）：迁移 0011 把 `chunks.embedding_model` / `embedding_text_version` / `chunker_version` 置 `NOT NULL`（非空约束）且无 `server_default`（服务端默认值）；`_seed` 的 `INSERT`（插入）没给这三列。PGlite 的 5 表投影没有这三列，宿主缺 DB 时 9 条真库探针 skip，**全绿假象**；直到真库探针实跑才暴露 9 FAILED。
- 对抗性反证：故意删回这 3 列后，宿主「全绿、exit=0」而真库「9 FAILED、exit=1」——证明宿主 skip 是假绿，真库探针才是真验证。
- 把 PGlite 的 passed 写成「26 迁移真实 schema 全链通过」是**造假**，两者不可混为一谈。
- 结论形式：验证报告必须写明跑的是 PGlite 还是真库、skip 了几条、对抗性实测是否通过。

## 证据 / 来源
- 出自 [[closed-state-equivalence]]；原文见 raw/2026-11-06-增量知识条目.md §4
- 报告见 `04-test/测试报告-第4步wiki导航.md` §0.7 与 P1-1 章节

## 关联
- 对照：[[closed-state-equivalence]]
- 关联：[[concurrency-capacity-check]]
