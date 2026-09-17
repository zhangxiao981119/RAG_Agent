"""XLSX 解析 —— openpyxl 按 sheet 整表输出（手册 §3.3.1 表格整表不切）。

每个 sheet 产出 ParsedBlock：
  · is_table = True（chunk 服务整表不切，超限时按行组切并保留表头）
  · table_header = 第一行作为列头
  · text = 表格内容序列化（列头行 + 数据行，TSV 风格）
  · heading_path = sheet 名
"""
from __future__ import annotations

import io

from app.services.parse.base import ParsedBlock, ParseError

try:
    from openpyxl import load_workbook  # type: ignore[import-untyped]
except ImportError as exc:  # pragma: no cover
    raise


def _row_to_tsv(row: list[object]) -> str:
    return "\t".join("" if v is None else str(v) for v in row)


def parse_xlsx(data: bytes) -> list[ParsedBlock]:
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise ParseError(f"XLSX 打开失败: {exc}") from exc

    blocks: list[ParsedBlock] = []
    try:
        for sheet in wb.worksheets:
            rows = list(sheet.iter_rows(values_only=True))
            if not rows:
                continue
            header = ["" if c is None else str(c) for c in rows[0]]
            text_lines = [_row_to_tsv(list(rows[0]))]
            for row in rows[1:]:
                text_lines.append(_row_to_tsv(list(row)))
            blocks.append(
                ParsedBlock(
                    text="\n".join(text_lines),
                    heading_path=sheet.title,
                    is_table=True,
                    table_header=header,
                )
            )
    finally:
        wb.close()

    return blocks
