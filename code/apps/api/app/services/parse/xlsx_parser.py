"""XLSX 解析 —— openpyxl 按 sheet 整表输出（手册 §3.3.1 表格整表不切）。

每个 sheet 产出 ParsedBlock：
  · is_table = True（chunk 服务整表不切，超限时按行组切并保留表头）
  · table_header = 第一行作为列头
  · text = 表格内容序列化（列头行 + 数据行，TSV 风格）
  · heading_path = sheet 名

★ 合并单元格（fill-down）：openpyxl 的 merged_cells.ranges 只能拿到
  合并信息，被覆盖格 iter_rows 出来是 None —— 直接序列化会丢语义
  （"费用总额"跨 3 行，后两行全空）。按 ranges 把左上值精确填充到
  区域内每一格（确定性展开，无猜测）。代价是放弃 read_only 流式加载
  （ReadOnlyWorksheet 无 merged_cells 属性），上传已有 max_upload_mb 上限。
"""
from __future__ import annotations

import io

from app.services.parse.base import ParsedBlock, ParseError, cell_to_text

try:
    from openpyxl import load_workbook  # type: ignore[import-untyped]
    from openpyxl.worksheet.worksheet import Worksheet  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    raise


def _build_merged_fill(ws: Worksheet, rows: list[tuple]) -> None:
    """将合并单元格的左上值原地填充进 rows（fill-down，行列即 rows 坐标）。

    rows 是 iter_rows(values_only=True) 的可变二维列表；
    MergedCellRange 的 min_row/min_col 为 1 基，转换为 0 基索引。
    """
    for rng in ws.merged_cells.ranges:
        top_left = rows[rng.min_row - 1][rng.min_col - 1]
        if top_left is None:
            continue  # 空合并区没有值可填
        for r in range(rng.min_row - 1, rng.max_row):
            row = rows[r]
            for c in range(rng.min_col - 1, rng.max_col):
                row[c] = top_left


def _row_to_tsv(row: list[object]) -> str:
    return "\t".join(cell_to_text(v) for v in row)


def parse_xlsx(data: bytes) -> list[ParsedBlock]:
    try:
        # read_only=False：ReadOnlyWorksheet 不带 merged_cells，合并展开无从谈起
        wb = load_workbook(io.BytesIO(data), data_only=True)
    except Exception as exc:
        raise ParseError(f"XLSX 打开失败: {exc}") from exc

    blocks: list[ParsedBlock] = []
    try:
        for sheet in wb.worksheets:
            rows = [list(r) for r in sheet.iter_rows(values_only=True)]
            if not rows:
                continue
            _build_merged_fill(sheet, rows)
            header = [cell_to_text(c) for c in rows[0]]
            text_lines = [_row_to_tsv(rows[0])]
            for row in rows[1:]:
                text_lines.append(_row_to_tsv(row))
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
