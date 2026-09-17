"""PDF 解析 —— pypdf 提取文本层 + 行首标题识别（手册 §3.3.1 + §8 D-04）。

★ 扫描件 PDF（extract_text 返回空）→ ParseError，提示不支持 OCR（§1.4 Non-Goals）。
"""
from __future__ import annotations

import io
import re

from app.services.parse.base import ParsedBlock, ParseError

# 标题模式：行首匹配
#   · "第 X 章" / "第 X 节"
#   · "X.Y 标题"（如 "3.2 权限"）
#   · "X. 标题"（如 "1. 概述"）
_HEADING_PATTERNS = [
    re.compile(r"^第[一二三四五六七八九十百\d]+[章节部篇]\s*.+$"),
    re.compile(r"^\d+\.\d+(?:\.\d+)*\s+.+$"),  # 3.2 / 3.2.1
    re.compile(r"^\d+\.\s+.+$"),  # 1. 概述
]

# pypdf 延迟导入，避免无 PDF 时影响其他 parser
try:
    from pypdf import PdfReader  # type: ignore[import-untyped]
except ImportError as exc:  # pragma: no cover - 依赖必装
    raise


def _is_heading(line: str) -> bool:
    line = line.strip()
    if not line or len(line) > 80:
        return False
    return any(p.match(line) for p in _HEADING_PATTERNS)


def parse_pdf(data: bytes) -> list[ParsedBlock]:
    try:
        reader = PdfReader(io.BytesIO(data))  # type: ignore[call-arg]
    except Exception as exc:
        raise ParseError(f"PDF 打开失败: {exc}") from exc

    blocks: list[ParsedBlock] = []
    current_heading = ""
    buffer: list[str] = []

    def flush(page_no: int) -> None:
        nonlocal buffer
        content = "\n".join(buffer).strip()
        if content:
            blocks.append(
                ParsedBlock(
                    text=content,
                    heading_path=current_heading,
                    page_no=page_no,
                )
            )
        buffer = []

    total_text = 0
    for page_index, page in enumerate(reader.pages, start=1):
        try:
            page_text = page.extract_text() or ""
        except Exception:
            page_text = ""
        total_text += len(page_text)

        for line in page_text.split("\n"):
            if _is_heading(line):
                flush(page_index)
                current_heading = line.strip()
            else:
                buffer.append(line)
        flush(page_index)

    # 扫描件识别：所有页提取文本总长接近 0
    if total_text < 10:
        raise ParseError(
            "PDF 文本层为空，疑似扫描件。本系统不支持 OCR（见手册 §8 D-04），"
            "请提供文本层 PDF 或先用 OCR 工具转换。"
        )

    return blocks
