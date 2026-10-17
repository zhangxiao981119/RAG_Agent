from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ChunkData:
    """分块结果。worker 写入 chunks 表时用这个结构。"""

    content: str
    token_count: int
    heading_path: str
    page_no: int | None
    # 表格决策 A1'：仅「超大表按行组切分」产出的纯表格分片共享同一组 id，
    # 检索命中任一分片时可回捞同组其余分片补全上下文。
    # 整表不切 / 混合块（分片与其他文本混装）一律为 None —— 混合块参与回捞
    # 会把无关文本卷进表格上下文。
    table_group_id: str | None = None
