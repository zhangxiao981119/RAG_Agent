from __future__ import annotations

import uuid
from dataclasses import dataclass, field


@dataclass
class RetrievedChunk:
    """检索命中的 chunk（含引用回填所需字段）。"""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    kb_id: uuid.UUID
    filename: str
    content: str
    heading_path: str
    page_no: int | None
    level_rank: int
    # 检索过程元数据
    vector_score: float
    keyword_score: float | None
    rrf_score: float
    rerank_score: float | None
    # 最终用于排序和阈值判断的分数
    final_score: float
    # 表格决策 A1'：回捞片 = 命中表格分片的同组其余分片。
    # 仅作上下文补充，MUST NOT 参与排序 / 阈值 / 三路贡献统计 ——
    # 分数一律 0 / None，回捞片单独放在 RetrievalResult.table_recall_chunks。
    is_table_recall: bool = False
    # 长文档 RAG：章节补全片 = 命中片所在小节（同 document_id + heading_path）
    # 的其余分片。与表格回捞同款 —— 仅补上下文，MUST NOT 参与排序 / 阈值，
    # 分数一律 0 / None，单独放在 RetrievalResult.section_recall_chunks。
    is_section_recall: bool = False
    # 图召回片：由实体图扩展而来的邻居 chunk（第 4 个上下文来源）。
    # 与表格/章节补全同款 —— 仅补上下文，MUST NOT 参与排序 / 阈值 / RRF / 三路贡献统计；
    # 分数一律 0 / None，单独放在 RetrievalResult.graph_recall_chunks。
    is_graph_recall: bool = False

    @property
    def display_score(self) -> float:
        """前端展示用的分数（rerank 优先，否则 vector 余弦相似度）。"""
        return self.rerank_score if self.rerank_score is not None else self.vector_score


@dataclass
class RetrievalResult:
    """检索结果。refused=True 表示 L1 闸门拒答。

    `score_source` 记录本次打分口径（"rerank" / "vector"），
    用于审计与排查：同一个 final_score 在不同口径下含义不同，
    不记录来源则无法解释「为什么这条被拒了」。
    """

    chunks: list[RetrievedChunk] = field(default_factory=list)
    refused: bool = False
    refuse_reason: str = ""
    stage_ms: dict[str, int] = field(default_factory=dict)
    score_source: str = ""
    use_rerank: bool = True
    # 三路召回贡献：`{路径}_covered` = 最终 top-K 中该路召回到的条数（多路可重叠），
    # `{路径}_unique` = 仅该路能召回到的条数。后者直接量化"去掉这路会漏多少"，
    # 是"三路是否冗余"的唯一证据。路径键：vector / tsquery / ilike。
    path_stats: dict[str, int] = field(default_factory=dict)
    # 表格决策 A1'：大表分片回捞结果（每组上限 TABLE_RECALL_MAX_CHUNKS /
    # 预算 TABLE_RECALL_TOKEN_BUDGET）。调用方把它追加进上下文拼装序列即可 ——
    # 编号 / citations / 配额裁剪沿用既有机制，不设特殊分支。
    table_recall_chunks: list[RetrievedChunk] = field(default_factory=list)
    # 长文档 RAG 章节补全结果（每节上限 SECTION_RECALL_MAX_CHUNKS /
    # 全局预算 SECTION_RECALL_TOKEN_BUDGET）。调用方与 table_recall_chunks
    # 一样追加进上下文拼装序列，编号与 citations 沿用既有机制。
    section_recall_chunks: list[RetrievedChunk] = field(default_factory=list)
    # 图召回结果（在线 side channel）：实体图扩展得到的邻居片，上限
    # GRAPH_RECALL_MAX_CHUNKS 条 / GRAPH_RECALL_TOKEN_BUDGET token。
    # 调用方与 table/section 一样追加进上下文拼装序列，编号与 citations 沿用既有机制。
    # 关闭图能力时恒为空列表 —— 上下文拼装的第四项为空，行为与未实现前逐字节等价。
    graph_recall_chunks: list[RetrievedChunk] = field(default_factory=list)
    # 图召回的降级原因（"" = 正常）："timeout" / "error" / "no_entities" / "no_neighbors"。
    # 图路永不抛异常（增强项），失败必须可见 —— 这字段就是那条可见通道。
    graph_degraded_reason: str = ""
    # 检索侧链式推理：本次走的是哪种模式。
    # "off"（开关关闭）/ "single"（信号不满足或拆解降级）/ "multi:N"（N 个子问题）。
    # 仅作审计与口径留痕，不参与任何判定。
    chain_mode: str = ""
    # multi 模式下实际检索用的子问题列表（原始问题恒在首位）；供审计与打点。
    subquestions: list[str] = field(default_factory=list)


def assemble_context_chunks(result: RetrievalResult) -> list[RetrievedChunk]:
    """上下文拼装的**唯一真源**：四类来源按「裁剪优先级」排序（尾部先被裁）。

    顺序 MUST 是 `chunks → table → section → graph`：
      · 命中片是证据本体，必须最先、也最后才被裁；
      · 表格回捞 / 章节补全 / 图召回都只是「补上下文」，越靠后越弱，超限时先掉。
    这个顺序同时决定 L4 配额裁剪的结果（按位置裁）与 citations 的编号顺序。

    ★ 为什么必须是函数而不是各处各写一遍：
      `chat.py`（生产）与 `eval_service.py`（评测门禁）都要拼这段。两处各写一份，
      某天只改了生产那份 —— 评测测的就不是生产行为了，而且**不会报错**，
      只是分数慢慢失去意义。这类漂移只能靠「只有一处实现」来杜绝。

    ★ 只做「四类来源的收集」，不掺任何调用方特有的逻辑：
      token 估算、历史压缩、配额降级都在调用方，塞进来就会变成两个都想要的胶水。
    """
    return (
        list(result.chunks)
        + list(result.table_recall_chunks)
        + list(result.section_recall_chunks)
        + list(result.graph_recall_chunks)
    )


def recall_chunks_count(result: RetrievalResult) -> int:
    """四类里「补充片」的条数（不含命中片）—— 报告里要能分清是哪一类变多了。"""
    return (
        len(result.table_recall_chunks)
        + len(result.section_recall_chunks)
        + len(result.graph_recall_chunks)
    )
