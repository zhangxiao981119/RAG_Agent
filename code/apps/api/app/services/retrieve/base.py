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
