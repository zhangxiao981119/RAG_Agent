<!-- 本文件由 build_index.py 自动生成，请勿手动编辑 -->
# 工程教训

共 4 页。

## 方法
- [[concurrency-capacity-check]] 并发容量双校验 — 给在线链路加一条并行查询前，必须先算两笔账：会话对象能否并发、每请求连接数乘目标并发是否超过池容量。
- [[cross-role-config-fingerprint]] 跨角色配置指纹 — 多进程角色靠「启动横幅打印同一指纹」发现配置漂移时，指纹只能覆盖跨角色必须一致的字段，不能哈希全量配置。
- [[realdb-probe-vs-skip-green]] 真库探针与宿主 skip 假绿 — 验证不能靠 PGlite 最小投影或宿主缺库 skip 全绿放行，必须跑 26 迁移真实 schema 的真库探针并做对抗性实测。
- [[statement-timeout-three-phase]] statement_timeout 事务级三段式还原 — 事务内设单条 SQL 超时后，成功路径须还原为调用前原值（非硬编码 0），失败 rollback 后不再发还原 SQL。
