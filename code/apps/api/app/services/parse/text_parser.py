"""纯文本解析 —— 按空行切段（手册 §3.3.1 切分优先级 ② 段落）。"""
from __future__ import annotations

from app.services.parse.base import ParsedBlock


def parse_text(data: bytes) -> list[ParsedBlock]:
    text = data.decode("utf-8", errors="replace")
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    return [ParsedBlock(text=p, heading_path="") for p in paragraphs]
