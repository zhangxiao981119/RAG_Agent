from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ChunkData:
    """分块结果。worker 写入 chunks 表时用这个结构。"""

    content: str
    token_count: int
    heading_path: str
    page_no: int | None
