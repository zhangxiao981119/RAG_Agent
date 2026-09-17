"""XLS 解析 —— xlrd 处理旧版 Excel 格式（手册 §3.3.1 表格整表不切）。

xlrd 只支持 .xls（BIFF），不支持 .xlsx（openpyxl 处理）。
"""
from __future__ import annotations

import io

from app.services.parse.base import ParsedBlock, ParseError

try:
    import xlrd  # type: ignore[import-untyped]
except ImportError as exc:  # pragma: no cover - 依赖必装
    raise


def parse_xls(data: bytes) -> list[ParsedBlock]:
    try:
        wb = xlrd.open_workbook(file_contents=data)
    except Exception as exc:
        raise ParseError(f"XLS 打开失败: {exc}") from exc

    blocks: list[ParsedBlock] = []
    for sheet in wb.sheets():
        if sheet.nrows == 0:
            continue
        # 第一行当表头
        header = [str(c).strip() for c in sheet.row_values(0)]
        text_lines = ["\t".join(header)]
        for r in range(1, sheet.nrows):
            row_values = [str(c).strip() for c in sheet.row_values(r)]
            text_lines.append("\t".join(row_values))
        blocks.append(
            ParsedBlock(
                text="\n".join(text_lines),
                heading_path=sheet.name,
                is_table=True,
                table_header=header,
            )
        )

    return blocks
