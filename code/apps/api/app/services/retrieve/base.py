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

    @property
    def display_score(self) -> float:
        """前端展示用的分数（rerank 优先，否则 vector 余弦相似度）。"""
        return self.rerank_score if self.rerank_score is not None else self.vector_score


@dataclass
class RetrievalResult:
    """检索结果。refused=True 表示 L1 闸门拒答。"""

    chunks: list[RetrievedChunk] = field(default_factory=list)
    refused: bool = False
    refuse_reason: str = ""
    stage_ms: dict[str, int] = field(default_factory=dict)
