"""XLS 解析 —— xlrd 处理旧版 Excel 格式（手册 §3.3.1 表格整表不切）。

xlrd 只支持 .xls（BIFF），不支持 .xlsx（openpyxl 处理）。

★ 合并单元格（fill-down）：merged_cells 需要 formatting_info=True 才会
  被解析，返回 (rlo, rhi, clo, chi) 半开区间矩形；左上值填充到区域
  内每一格（确定性展开，无猜测）。
"""
from __future__ import annotations

from app.services.parse.base import ParsedBlock, ParseError, cell_to_text

try:
    import xlrd  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - 依赖必装
    raise


def parse_xls(data: bytes) -> list[ParsedBlock]:
    try:
        # formatting_info=True：merged_cells 只在此模式下被解析
        wb = xlrd.open_workbook(file_contents=data, formatting_info=True)
    except Exception as exc:
        raise ParseError(f"XLS 打开失败: {exc}") from exc

    blocks: list[ParsedBlock] = []
    for sheet in wb.sheets():
        if sheet.nrows == 0:
            continue

        # 读出二维矩阵后做合并展开（xlrd 无写接口，值层面填充）
        rows: list[list[object]] = [sheet.row_values(r) for r in range(sheet.nrows)]
        for rlo, rhi, clo, chi in sheet.merged_cells:
            top_left = rows[rlo][clo]
            if top_left in (None, ""):
                continue  # 空合并区没有值可填
            for r in range(rlo, min(rhi, sheet.nrows)):
                for c in range(clo, min(chi, sheet.ncols)):
                    rows[r][c] = top_left

        # 第一行当表头
        header = [cell_to_text(c) for c in rows[0]]
        text_lines = ["\t".join(header)]
        for row in rows[1:]:
            text_lines.append("\t".join(cell_to_text(v) for v in row))
        blocks.append(
            ParsedBlock(
                text="\n".join(text_lines),
                heading_path=sheet.name,
                is_table=True,
                table_header=header,
            )
        )

    return blocks
