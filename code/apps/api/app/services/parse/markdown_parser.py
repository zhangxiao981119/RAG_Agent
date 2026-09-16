"""Markdown 解析 —— 按 #/##/### 标题层级切分（手册 §3.3.1 切分优先级 ①）。"""
from __future__ import annotations

import re

from app.services.parse.base import ParsedBlock

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def parse_markdown(data: bytes) -> list[ParsedBlock]:
    text = data.decode("utf-8", errors="replace")
    lines = text.split("\n")

    blocks: list[ParsedBlock] = []
    # 当前标题路径栈，每个元素是 (level, title)
    stack: list[tuple[int, str]] = []
    buffer: list[str] = []

    def flush() -> None:
        if not buffer:
            return
        content = "\n".join(buffer).strip()
        if content:
            blocks.append(
                ParsedBlock(
                    text=content,
                    heading_path=" > ".join(title for _, title in stack),
                )
            )
        buffer.clear()

    for line in lines:
        match = _HEADING_RE.match(line)
        if match:
            flush()
            level = len(match.group(1))
            title = match.group(2).strip()
            # 弹掉层级 >= 当前的（同级或更深的）
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
        else:
            buffer.append(line)

    flush()
    return blocks
