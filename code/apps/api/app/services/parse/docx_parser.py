"""DOCX 解析 —— python-docx 按段落/标题/表格切分。

Word 文档结构：
  · Heading 1-9 → heading_path 累积（层级路径）
  · 普通段落 → 跟随当前 heading_path
  · 表格 → is_table=True，table_header=首行，chunk 整表不切

注意：
  · 只支持 .docx（python-docx 不支持旧格式 .doc）
  · .doc 建议先让用户转 .docx 再上传（浏览器 accept 里只放 docx）
"""
from __future__ import annotations

from io import BytesIO

from docx.oxml.ns import qn  # type: ignore[import-untyped]

from app.services.parse.base import ParsedBlock, ParseError, cell_to_text

try:
    from docx import Document  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    raise


def _vmerge_is_continue(cell) -> bool:
    """判断单元格是否为纵向合并的延续格（w:vMerge 无 val 或 val="continue"）。

    Word 纵向合并：起始格 val="restart"（或省略 val 但有内容），延续格的
    text 恒为空 —— 直接序列化会整列丢语义（INV-M-05 之外的结构保真缺口）。
    """
    tcPr = cell._tc.tcPr
    if tcPr is None:
        return False
    vm = tcPr.find(qn("w:vMerge"))
    if vm is None:
        return False
    return vm.get(qn("w:val")) in (None, "continue")


def _table_to_block(table, heading_path: str) -> ParsedBlock:
    """将 python-docx Table 转为 ParsedBlock（整表）。

    ★ 横向合并无需处理：python-docx 的 row.cells 对 gridSpan 合并
      返回同一 tc 的重复引用，text 天然重复展开（探针实测 ['M','M']）。
    ★ 纵向合并：延续格 text 为空，按列跟踪上一行已序列化文本做
      fill-down（与 xlsx/xls 的左上值展开同一语义）。
    """
    rows_data: list[list[str]] = []
    for row in table.rows:
        cells = [cell_to_text(c.text) for c in row.cells]
        prev = rows_data[-1] if rows_data else None
        for idx, text in enumerate(cells):
            if (
                prev is not None
                and idx < len(prev)
                and text == ""
                and _vmerge_is_continue(row.cells[idx])
            ):
                cells[idx] = prev[idx]  # 纵向合并延续格：沿用正上方已序列化文本
        rows_data.append(cells)
    if not rows_data:
        return ParsedBlock(text="", heading_path=heading_path, is_table=True)

    header = rows_data[0]
    text_lines = ["\t".join(header)]
    for row in rows_data[1:]:
        text_lines.append("\t".join(row))

    return ParsedBlock(
        text="\n".join(text_lines),
        heading_path=heading_path,
        is_table=True,
        table_header=header,
    )


def parse_docx(data: bytes) -> list[ParsedBlock]:
    """解析 .docx 文件，产出段落/标题/表格块。"""
    try:
        doc = Document(BytesIO(data))
    except Exception as exc:
        raise ParseError(f"DOCX 打开失败: {exc}") from exc

    blocks: list[ParsedBlock] = []
    heading_stack: list[str] = []  # 当前活动的标题路径栈
    # ★ 与 markdown/pdf parser 的两处差异 —— **有意为之，不是遗漏**：
    #   ① 段落：每段独立成 block（那两个是 buffer 累积、遇标题才 flush）。
    #      解析层不做合并，交给下游分块服务：chunk._accumulate 会按 token 预算
    #      重组，而「一段一 unit」把段落边界保留到了 unit 层，比"整块再按句子切"
    #      更不容易拦腰截断段落。
    #   ② 标题：标题文本**既进 heading_path、也作为一个 block 进 content**。
    #      那两个 parser 的标题只进 heading_path。保留标题正文对向量召回有利
    #      （标题词本就是典型检索词）。代价见 chunk.to_embedding_text() 的说明。
    #   （原先这里有个 `prev_item` 变量，注释写「用于合并连续段落」，
    #     但只赋值、从未被读取，按 ① 的判断也不需要 —— 已删除。）

    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue

        style_name = (para.style.name or "").lower() if para.style else ""

        if style_name.startswith("heading"):
            try:
                level = int(style_name.replace("heading", "").strip())
            except ValueError:
                level = 1
            # 更新标题栈：截断到 level-1，再 push 新标题
            heading_stack = heading_stack[: level - 1]
            heading_stack.append(text)
            # 标题本身也作为一个 block
            blocks.append(
                ParsedBlock(
                    text=text,
                    heading_path="/".join(heading_stack),
                )
            )
        else:
            heading_path = "/".join(heading_stack)
            blocks.append(
                ParsedBlock(
                    text=text,
                    heading_path=heading_path,
                )
            )

    # 处理表格
    for table in doc.tables:
        # 表格跟随当前 heading_path
        heading_path = "/".join(heading_stack)
        block = _table_to_block(table, heading_path)
        if block.text:
            blocks.append(block)

    return blocks
