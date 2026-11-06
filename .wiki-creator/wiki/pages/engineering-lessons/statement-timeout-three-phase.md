---
title: statement_timeout 事务级三段式还原
topic: engineering-lessons
entity_type: 方法
aliases: [statement_timeout, 三段式还原, 事务级超时, set_config]
sources: [raw/2026-11-06-增量知识条目.md]
schema_version: v1
---
# statement_timeout 事务级三段式还原

## 摘要
在事务内用 `set_config('statement_timeout', :ms, true)` 给单条 SQL 设超时时，必须在**成功路径末尾还原为调用前读到的值**（不是硬编码 `'0'`），失败路径 rollback（回滚）后**不再**发还原 SQL。因为 `set_config(..., true)` 的作用域是整个事务，会延续影响后续查询。

## 定义
三段式：①`_read_statement_timeout(session)` 读出调用前的原值；② 执行目标 SQL 前 `set_config(..., :ms, true)`；③ 成功后 `set_config(..., :previous, true)` 还原；一旦异常则靠 rollback 整事务回滚，不再单独发还原语句。

## 关键论点
- 作用域陷阱：`set_config` 的 `true`（local）是**会话级事务内有效**，不是单语句。图召回 / wiki 导航设了超时后，同一事务还要跑表格回捞、章节补全，若不还原，这些后续查询也受超时约束，轻则误超时、重则拖垮整事务。
- 硬编码 `'0'` 是错的：调用前可能本来就不是 `'0'`（比如上游设了某个值），硬写 `'0'` 会篡改上游语义。
- 还原要读「调用前」的值并原样写回，真库 P3 复核过 `previous='1234ms' → after='1234ms'` 一致。
- 失败路径不发还原 SQL：rollback 已经让 set_config 的 local 改动随事务消失，再发一条 SQL 反而可能在新事务语义下出错。
- wiki 导航 SQL 比图召回更靠前地设超时，同样吃这条纪律。

## 证据 / 来源
- 原文见 raw/2026-11-06-增量知识条目.md §5
- 代码见 `apps/api/app/services/graph/recall.py` 与 `apps/api/app/services/wiki/navigate.py` 的 `_read_statement_timeout` / `_restore_statement_timeout`

## 关联
- 关联：[[graph-acl-pushdown]]
- 对照：[[side-channel-recall]]
