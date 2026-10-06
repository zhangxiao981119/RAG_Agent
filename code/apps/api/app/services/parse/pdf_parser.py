"""PDF 解析 —— 文本层提取 + 表格结构识别（手册 §3.3.1 + §8 D-04）。

两条路径，按页独立决策：
  · 表格页：pdfplumber 识别表格 → 表格块；同页文本用 pdfplumber 的词坐标
    **排除表格区域**后重建，避免同一份数据在blocks 里出现两次
  · 无表格页：pypdf 的 extract_text() + 行首模式匹配标题（pypdf 无 layout）

★ 扫描件 PDF（全文提取文本总长接近 0）→ ParseError，提示不支持 OCR
  （§1.4 Non-Goals）。**扫描件判定优先于表格识别** —— 扫描件里既没有文本层
  也没有可识别的表格线，先判扫描件才不会把版面噪声误当表格。

★ 为什么要引 pdfplumber：pypdf 只吐字符流，表格会被拍平成
  "资源 规格 数量 备注\\n嵌入模型 bge-m3 1 本地推理" —— 列关系全丢。
  而 ParsedBlock 早有 is_table / table_header 两个字段（xlsx 解析器在用），
  chunk 层也已实现"表格整表不切、按行组切且每组带表头"，只是 PDF 从不填它们
  —— 实测导致 80 行表格切开后，4 个 chunk 里有 3 个丢了表头，
  单独 embedding 时模型不知道 "123" 是 QPS 还是延迟。
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

# 表格识别的最小可信度阈值 —— pdfplumber 的 find_tables 对任意有线条的
# 区域都会返回"表格"，阈值太低会把正文里的分隔线、分栏框误判成表格。
_MIN_TABLE_ROWS: int = 2      # 至少 2 行（表头 + 1 行数据）
_MIN_TABLE_COLS: int = 2      # 至少 2 列
_MIN_TABLE_CELLS: int = 4     # 至少 4 个非空单元格，排除"线框装饰"

# 同一行内词的纵向归并容差（PDF 里同一行的字形 top 会有细微浮动）
_LINE_TOLERANCE: float = 3.0

# pypdf 延迟导入，避免无 PDF 时影响其他 parser
try:
    from pypdf import PdfReader  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - 依赖必装
    raise

# pdfplumber 用于表格识别与"排除表格区域"的文本重建。
# 它是 pdfminer.six 的封装，纯 Python、无二进制依赖。
try:
    import pdfplumber  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - 表格识别降级为纯文本
    pdfplumber = None  # type: ignore[assignment]


def _is_heading(line: str) -> bool:
    line = line.strip()
    if not line or len(line) > 80:
        return False
    return any(p.match(line) for p in _HEADING_PATTERNS)


def _row_to_tsv(row: list[object]) -> str:
    return "\t".join("" if v is None else str(v) for v in row)


def _normalize_table(raw: list[list[object]]) -> list[list[str]] | None:
    """pdfplumber 原始单元格 → 规整字符串矩阵。None = 不可信，当普通文本处理。"""
    rows: list[list[str]] = []
    for raw_row in raw:
        cells = ["" if c is None else str(c).replace("\n", " ").strip() for c in raw_row]
        if any(cells):  # 跳过全空行
            rows.append(cells)
    if len(rows) < _MIN_TABLE_ROWS:
        return None
    width = max(len(r) for r in rows)
    if width < _MIN_TABLE_COLS:
        return None
    if sum(1 for r in rows for c in r if c) < _MIN_TABLE_CELLS:
        return None
    return [r + [""] * (width - len(r)) for r in rows]  # 补齐宽度


def _table_to_block(
    rows: list[list[str]], heading_path: str, page_no: int
) -> ParsedBlock:
    """表格矩阵 → ParsedBlock（输出契约与 xlsx_parser 一致）。

    text 用 TSV 风格（列头行 + 数据行）。用 \\t 而非空格分隔列：
    空格分隔的列在切分后无法还原行列边界，\\t 至少保留了列这一层信息，
    让向量侧的文本仍带着"这是第几列"的弱信号。
    """
    lines = [_row_to_tsv(rows[0])]
    lines.extend(_row_to_tsv(r) for r in rows[1:])
    return ParsedBlock(
        text="\n".join(lines),
        heading_path=heading_path,
        page_no=page_no,
        is_table=True,
        table_header=list(rows[0]),
    )


def _text_outside_tables(
    page: object, bboxes: list[tuple[float, float, float, float]]
) -> str:
    """重建该页中**不在表格区域内**的文本行。

    ★ 这是避免重复的关键：表格区域的内容已经进了表格块，若文本流里再留一份，
      同一份数据会被 embedding 两次，检索时重复命中、且两块的向量不一致
      （一个是 TSV 结构化，一个是拍平的字符流）。

    pdfplumber 的词/表坐标都是"自页面顶部向下"的同一套坐标系（top/bottom），
    所以可以直接做矩形包含判断。
    """
    try:
        words = page.extract_words(x_tolerance=1.5, y_tolerance=3)  # type: ignore[attr-defined]
    except Exception:
        return ""

    kept = []
    for w in words:
        cx = (w["x0"] + w["x1"]) / 2
        cy = (w["top"] + w["bottom"]) / 2
        inside = any(
            x0 <= cx <= x1 and top <= cy <= bottom for (x0, top, x1, bottom) in bboxes
        )
        if not inside:
            kept.append(w)
    if not kept:
        return ""

    # 按纵向位置聚成行，行内按 x 排序
    kept.sort(key=lambda w: (w["top"], w["x0"]))
    lines: list[str] = []
    cur: list[dict] = []
    cur_top: float | None = None
    for w in kept:
        if cur_top is not None and abs(w["top"] - cur_top) > _LINE_TOLERANCE:
            lines.append(" ".join(x["text"] for x in cur))
            cur, cur_top = [], None
        if cur_top is None:
            cur_top = w["top"]
        cur.append(w)
    if cur:
        lines.append(" ".join(x["text"] for x in cur))
    return "\n".join(lines)


def _extract_page_tables(
    data: bytes,
) -> tuple[
    dict[int, list[list[list[str]]]],
    dict[int, str],
]:
    """按页抽取可信表格，并重建各页"表格区域之外"的文本。

    返回 (tables_by_page, outside_text_by_page)：
      · tables_by_page   {page_no: [rows, ...]}
      · outside_text_by_page {page_no: "该页表格之外的文本"}

    ★ 只 open一次 pdfplumber document。早期实现里每页都重新
      pdfplumber.open() 整份文档 —— 80 行表格实测直接超时（>120s），
      文档解析是 worker 的主路径，不能这么写。

    pdfplumber 不可用或整体失败时返回空 dict —— 表格识别是增强项，
    不能因为它让整份 PDF 解析失败。
    """
    tables: dict[int, list[list[list[str]]]] = {}
    outside: dict[int, str] = {}
    if pdfplumber is None:  # pragma: no cover
        return tables, outside
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page_index, page in enumerate(pdf.pages, start=1):
                try:
                    found = page.find_tables()
                except Exception:
                    continue
                rows_list: list[list[list[str]]] = []
                bboxes: list[tuple[float, float, float, float]] = []
                for tbl in found:
                    rows = _normalize_table(tbl.extract())
                    if rows is None:
                        continue
                    rows_list.append(rows)
                    bboxes.append(tuple(tbl.bbox))  # type: ignore[arg-type]
                if not rows_list:
                    continue
                tables[page_index] = rows_list
                try:
                    outside[page_index] = _text_outside_tables(page, bboxes)
                except Exception:
                    outside[page_index] = ""
    except Exception:
        return tables, outside
    return tables, outside


def parse_pdf(data: bytes) -> list[ParsedBlock]:
    try:
        reader = PdfReader(io.BytesIO(data))  # type: ignore[call-arg]
    except Exception as exc:
        raise ParseError(f"PDF 打开失败: {exc}") from exc

    # ── ① 先取全文文本判定扫描件（优先级最高，先于表格识别）──
    page_texts: list[str] = []
    total_text = 0
    for page in reader.pages:
        try:
            page_text = page.extract_text() or ""
        except Exception:
            page_text = ""
        page_texts.append(page_text)
        total_text += len(page_text)

    if total_text < 10:
        raise ParseError(
            "PDF 文本层为空，疑似扫描件。本系统不支持 OCR（见手册 §8 D-04），"
            "请提供文本层 PDF 或先用 OCR 工具转换。"
        )

    tables_by_page, outside_by_page = _extract_page_tables(data)

    blocks: list[ParsedBlock] = []
    current_heading = ""
    buffer: list[str] = []

    def flush(page_no: int) -> None:
        nonlocal buffer
        content = "\n".join(buffer).strip()
        if content:
            blocks.append(
                ParsedBlock(text=content, heading_path=current_heading, page_no=page_no)
            )
        buffer = []

    for page_index, page_text in enumerate(page_texts, start=1):
        rows_list = tables_by_page.get(page_index)

        if rows_list is not None:
            # 表格页：用 pdfplumber 重建的"表格之外"文本，避免同一数据入块两次
            outside = outside_by_page.get(page_index) or page_text
            for rows in rows_list:
                blocks.append(_table_to_block(rows, current_heading, page_index))
            for line in outside.split("\n"):
                if _is_heading(line):
                    flush(page_index)
                    current_heading = line.strip()
                else:
                    buffer.append(line)
            flush(page_index)
            continue

        # 无表格页：走原来的 pypdf 路径（行为与改造前一致）
        for line in page_text.split("\n"):
            if _is_heading(line):
                flush(page_index)
                current_heading = line.strip()
            else:
                buffer.append(line)
        flush(page_index)

    return blocks