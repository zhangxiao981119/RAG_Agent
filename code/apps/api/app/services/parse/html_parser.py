"""M7 WU-M-13：HTML 解析清洗器 —— 网页同步源的解析层。

职责（规范 §4.2 WU-M-13）：
  1. 剔除噪声区域：script / style / nav / footer / header / noscript / iframe
  2. 标题层级维护 heading_path（h1-h6，与 markdown/docx 解析器同口径）
  3. 表格识别 → 既有 is_table 契约：rowspan/colspan 网格法展开（与
     xlsx/xls/docx 的合并展开口径一致）→ cell_to_text 消毒 → TSV 序列化，
     保证 chunk 层"整表不切 / 行组切分带表头"契约原样适用

调用方：sync_service web 分支（WU-M-14）直调 parse_html；
不注册进 parse_bytes 的 _DISPATCH —— html 不在 ALLOWED_EXT 内，
网页抓取产物不走上传统一入口（显式信号，不混路径）。
"""
from __future__ import annotations

from bs4 import BeautifulSoup, Tag

from app.services.parse.base import ParsedBlock, ParseError, cell_to_text

# 噪声标签：导航/页脚/头部/脚本/样式/嵌入物 —— 整体剔除不进正文
_NOISE_TAGS = ("script", "style", "nav", "footer", "header", "noscript", "iframe", "form")

# 标题标签 → 层级（与 markdown_parser 的 # 计数、docx_parser 的 Heading N 同口径）
_HEADING_LEVELS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}

# 承担正文的块级文本标签
_TEXT_TAGS = ("p", "li", "blockquote", "pre")


def _expand_table(table: Tag) -> list[list[str]]:
    """<table> → 二维文本网格（rowspan/colspan 网格法展开，左上值填充）。

    与 xlsx/xls/docx 解析器的合并展开口径一致：被合并覆盖的格子回填
    左上格的值 —— 检索期"这一行该列是什么"不靠空位猜，生成期列对齐不错位。
    """
    grid: list[list[str]] = []
    # col -> (text, remaining_rows)：rowspan 向下延续的占位
    spans: dict[int, tuple[str, int]] = {}

    def _fill_spans(row: list[str], col: int) -> int:
        """把当前行被上方 rowspan 覆盖的列补齐，返回下一个空闲列。"""
        while col in spans:
            text, remaining = spans[col]
            row.append(text)
            if remaining == 1:
                del spans[col]
            else:
                spans[col] = (text, remaining - 1)
            col += 1
        return col

    for tr in table.find_all("tr"):
        row: list[str] = []
        col = 0
        for cell in tr.find_all(["td", "th"], recursive=False):
            col = _fill_spans(row, col)
            text = cell.get_text(strip=True)
            rowspan = int(cell.get("rowspan", 1) or 1)
            colspan = int(cell.get("colspan", 1) or 1)
            for _ in range(colspan):
                row.append(text)
                if rowspan > 1:
                    spans[col] = (text, rowspan - 1)
                col += 1
        _fill_spans(row, col)  # 行尾被上方 rowspan 覆盖的列
        if row:
            grid.append(row)
    return grid


def _table_block(table: Tag, heading_path: str) -> ParsedBlock | None:
    """<table> → is_table 块（TSV 序列化）。无有效网格返回 None（装饰性 table）。"""
    grid = _expand_table(table)
    if len(grid) < 2 or not any(grid[0]):
        # 与 pdf 表格可信度阈值同源口径：少于 2 行（只有表头）或首行全空，
        # 不构成可检索表格 —— 静默丢弃，不产出空表块
        return None
    header = [cell_to_text(c) for c in grid[0]]
    lines = ["\t".join(cell_to_text(c) for c in row) for row in grid]
    return ParsedBlock(
        text="\n".join(lines),
        heading_path=heading_path,
        is_table=True,
        table_header=header,
    )


def parse_html(data: bytes) -> list[ParsedBlock]:
    """HTML 字节 → ParsedBlock 列表（正文与表格按文档顺序交错）。

    与 docx_parser 同构：标题栈维护 heading_path，表格块随当前标题栈归属。
    全文无可检索内容 → ParseError（不产空块静默入库）。
    """
    try:
        soup = BeautifulSoup(data, "html.parser")
    except Exception as exc:
        raise ParseError(f"HTML 解析失败: {exc}") from exc

    root = soup.body or soup
    for noise in root.find_all(_NOISE_TAGS):
        noise.decompose()

    blocks: list[ParsedBlock] = []
    heading_stack: list[str] = []  # 与 docx_parser 同口径的标题栈

    def _flush_text(text: str) -> None:
        text = text.strip()
        if text:
            blocks.append(ParsedBlock(text=text, heading_path="/".join(heading_stack)))

    for el in root.find_all([*_HEADING_LEVELS.keys(), *_TEXT_TAGS, "table"]):
        # 嵌套表格的 cell 内文本标签（td 里的 p）不参与顶层遍历，
        # 否则同一段文本既进表格又进正文（重复入库）
        if el.name in _TEXT_TAGS and el.find_parent("table") is not None:
            continue
        level = _HEADING_LEVELS.get(el.name)
        if level is not None:
            # 维护标题栈：同级替换、下级追加、上级回退（docx_parser 同算法）
            while len(heading_stack) >= level:
                heading_stack.pop()
            title = el.get_text(strip=True)
            if title:
                heading_stack.append(title)
            continue
        if el.name == "table":
            block = _table_block(el, "/".join(heading_stack))
            if block is not None:
                blocks.append(block)
        else:
            _flush_text(el.get_text(" ", strip=True))

    if not blocks:
        raise ParseError("HTML 中未提取到可检索内容（正文与表格均为空）")
    return blocks
