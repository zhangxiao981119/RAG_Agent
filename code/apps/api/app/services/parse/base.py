"""解析服务 —— 按扩展名分派（手册 §3.3.1 + §6 M2 任务 3）。

输入：文件字节流 + 扩展名
输出：ParsedBlock 列表（带 heading_path / page_no / is_table）

分派表：
  · .pdf   → pypdf（仅文本层 PDF；扫描件见 §8 D-04 明确不做）
  · .md    → markdown 标题层级切分
  · .txt   → 按空行切段
  · .xlsx  → openpyxl，按 sheet 整表输出（chunk 服务整表不切）
  · .docx  → python-docx，按标题/段落/表格切分

★ PDF 标题识别：pypdf 不带 layout，这里用行首模式匹配
  （"第 X 章/X.Y 标题/数字. 序号"），识别不到则退化为"第 N 页"。
  扫描件 PDF（extract_text 返回空）→ 抛 ParseError，提示不支持 OCR。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ParsedBlock:
    """解析后的结构化块。chunk 服务基于此做 token 级分块。"""

    text: str
    heading_path: str = ""
    page_no: int | None = None
    is_table: bool = False
    # 表格块的列头（整表不切时 chunk 服务会带表头）
    table_header: list[str] = field(default_factory=list)


class ParseError(Exception):
    """解析失败。worker 捕获后写 last_error 并重试。"""


# 具体解析器在数据类定义之后再导入：
# 各 parser 反向 import ParsedBlock，放顶部会构成循环导入
from app.services.parse.markdown_parser import parse_markdown  # noqa: E402
from app.services.parse.pdf_parser import parse_pdf  # noqa: E402
from app.services.parse.text_parser import parse_text  # noqa: E402
from app.services.parse.xlsx_parser import parse_xlsx  # noqa: E402
from app.services.parse.xls_parser import parse_xls  # noqa: E402
from app.services.parse.docx_parser import parse_docx  # noqa: E402


_DISPATCH = {
    "pdf": parse_pdf,
    "md": parse_markdown,
    "txt": parse_text,
    "xlsx": parse_xlsx,
    "xls": parse_xls,
    "docx": parse_docx,
}


def parse_bytes(ext: str, data: bytes) -> list[ParsedBlock]:
    """按扩展名分派。未知扩展名 / 扫描件 → ParseError。"""
    handler = _DISPATCH.get(ext.lower())
    if handler is None:
        raise ParseError(f"不支持的文件类型: {ext}")
    return handler(data)
