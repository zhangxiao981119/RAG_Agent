"""文档原文预览提取 —— 把各格式文件统一转为纯文本供前端预览。

支持格式（与上传白名单一致）：
  · md / txt     原文直接返回
  · docx         python-docx 提取段落 + 表格
  · xlsx         openpyxl 提取工作表（tab 分隔）
  · xls          xlrd 提取工作表
  · pdf          pypdf 逐页提取文本层（扫描件无文本层时给提示）

不支持的格式 / 提取失败抛 PreviewError，API 层转 415。
"""
from __future__ import annotations

from io import BytesIO


class PreviewError(Exception):
    """预览提取失败（格式不支持 / 文件损坏 / 无文本层）。"""


_MAX_PREVIEW_CHARS = 200_000  # 预览文本上限，超长截断


def extract_preview_text(ext: str, data: bytes) -> str:
    """按扩展名分派提取，返回预览纯文本。"""
    handlers = {
        "md": _text_plain,
        "txt": _text_plain,
        "docx": _docx_text,
        "xlsx": _xlsx_text,
        "xls": _xls_text,
        "pdf": _pdf_text,
    }
    handler = handlers.get(ext)
    if handler is None:
        raise PreviewError(f"不支持的预览格式: {ext}")
    try:
        text = handler(data)
    except PreviewError:
        raise
    except Exception as exc:
        raise PreviewError("文件内容解析失败，无法预览") from exc

    if not text.strip():
        if ext == "pdf":
            raise PreviewError("该 PDF 无文本层（可能是扫描件），无法预览文本")
        raise PreviewError("文件内容为空，无法预览")
    if len(text) > _MAX_PREVIEW_CHARS:
        text = text[:_MAX_PREVIEW_CHARS] + "\n\n……（内容过长，已截断）"
    return text


def _text_plain(data: bytes) -> str:
    return data.decode("utf-8", "replace")


def _docx_text(data: bytes) -> str:
    from docx import Document as DocxDocument

    doc = DocxDocument(BytesIO(data))
    parts: list[str] = [p.text for p in doc.paragraphs if p.text.strip()]
    # 表格按行提取，单元格用 tab 分隔（文档分块时表格作为整体，预览保持一致）
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            parts.append("\t".join(cells))
    return "\n".join(parts)


def _xlsx_text(data: bytes) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(BytesIO(data), read_only=True, data_only=True)
    parts: list[str] = []
    for ws in wb.worksheets:
        parts.append(f"【工作表 {ws.title}】")
        for row in ws.iter_rows(values_only=True):
            cells = ["" if v is None else str(v) for v in row]
            if any(c.strip() for c in cells):
                parts.append("\t".join(cells))
        parts.append("")
    return "\n".join(parts)


def _xls_text(data: bytes) -> str:
    import xlrd

    wb = xlrd.open_workbook(file_contents=data)
    parts: list[str] = []
    for ws in wb.sheets():
        parts.append(f"【工作表 {ws.name}】")
        for r in range(ws.nrows):
            cells = ["" if v is None else str(v) for v in ws.row_values(r)]
            if any(c.strip() for c in cells):
                parts.append("\t".join(cells))
        parts.append("")
    return "\n".join(parts)


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(data))
    parts: list[str] = []
    for i, page in enumerate(reader.pages, start=1):
        parts.append(f"--- 第 {i} 页 ---")
        parts.append(page.extract_text() or "")
        parts.append("")
    return "\n".join(parts)
