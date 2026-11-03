"""实体图服务 —— 端口与实现、离线抽取、派生数据清理。

本包的分工（第 1 步只落「写」的一半，读的一半属第 2 步）：

  · base.py            GraphStore 端口（4 个方法）+ 抽取/邻居产物 dataclass
  · postgres_store.py  唯一实现 PostgresGraphStore（四闸门下推 + 口径过滤）
  · extract.py         离线抽取：chunks → 实体/提及/关系（LLM）
  · prompts.py         抽取 prompt 正文（版本号在 config/graph_profile.py）
  · purge.py           重索引前的派生数据清理（MUST 与 chunks 写入同事务）

★ 第 1 步没有 linking.py / recall.py（在线召回）：`GraphStore.neighbors()` 已经
  按端口契约实现好并可测试，但**没有任何在线调用点**。这样第 2 步只需在
  retrieve 链路里接上调用，不必回头改存储层。
"""
