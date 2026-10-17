"""解析服务 —— 按扩展名分派（手册 §3.3.1 + §6 M2 任务 3）。

输入：文件字节流 + 扩展名
输出：ParsedBlock 列表（带 heading_path / page_no / is_table）

分派表：
  · .pdf   → pypdf（仅文本层 PDF；扫描件 B' 起走本地 OCR，见 parse_pdf_async）
  · .md    → markdown 标题层级切分
  · .txt   → 按空行切段
  · .xlsx  → openpyxl，按 sheet 整表输出（chunk 服务整表不切）
  · .docx  → python-docx，按标题/段落/表格切分
  · .png/.jpg/.jpeg/.gif/.webp/.bmp → 本地 OCR 服务转录（异步，见 parse_bytes_async；
    受 settings.ocr_enabled + ocr_base_url 双门控，M7 WU-M-09 / B'）

★ PDF 标题识别：pypdf 不带 layout，这里用行首模式匹配
  （"第 X 章/X.Y 标题/数字. 序号"），识别不到则退化为"第 N 页"。
  扫描件 PDF（extract_text 返回空）→ 异步入口改道本地 OCR，同步入口抛 ParseError。
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
    # M8-P1：逐行保真信号（代码围栏块 / 列表聚合块 / html <pre>）。
    # verbatim 块在 chunk 层禁用句子/词级切分 —— 代码行与列表项是原子，
    # 只按行边界切（整块 <=800 直接成片；超限按行累积，单行超限才硬切）。
    # 普通文本走既有 段落→句子→硬切 逐级降级，语义不变。
    verbatim: bool = False


class ParseError(Exception):
    """解析失败。worker 捕获后写 last_error 并重试。"""


def cell_to_text(v: object) -> str:
    """表格单元格 → 安全文本（各表格 parser 共用，口径单点）。

    ★ MUST 在进 TSV 序列化前调用：单元格内的 \\t / \\n / \\r 会破坏
      "一行一条记录、\\t 分列"的结构约定 —— 制表符静默制造伪列、
      换行把一格内容劈成两行，下游"按行组切分带表头"会把错位行
      当成数据行 embedding 入库（错误知识，检索期才暴露）。
    """
    if v is None:
        return ""
    return str(v).replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()


# 具体解析器在数据类定义之后再导入：
# 各 parser 反向 import ParsedBlock，放顶部会构成循环导入
from app.services.parse.markdown_parser import parse_markdown  # noqa: E402
from app.services.parse.pdf_parser import parse_pdf, parse_pdf_async  # noqa: E402
from app.services.parse.text_parser import parse_text  # noqa: E402
from app.services.parse.xlsx_parser import parse_xlsx  # noqa: E402
from app.services.parse.xls_parser import parse_xls  # noqa: E402
from app.services.parse.docx_parser import parse_docx  # noqa: E402
from app.services.parse.html_parser import parse_html  # noqa: E402
from app.services.parse.image_parser import IMAGE_EXTS, parse_image  # noqa: E402


_DISPATCH = {
    "pdf": parse_pdf,
    "md": parse_markdown,
    "txt": parse_text,
    "xlsx": parse_xlsx,
    "xls": parse_xls,
    "docx": parse_docx,
    # M7 WU-M-14：web 同步源的抓取产物（ext=html）走标准 ParseJob 链路
    # （重试/状态机/审计复用）。html 只来自同步抓取 —— 上传端点的 ALLOWED_EXT
    # 仍不含 html，用户直接上传 .html 依然被拒（边界不变）
    "html": parse_html,
}


def _strip_nul(blocks: list[ParsedBlock]) -> list[ParsedBlock]:
    """NUL 字节清洗的共用实现（见 parse_bytes docstring 的口径说明）。"""
    for block in blocks:
        if "\x00" in block.text:
            block.text = block.text.replace("\x00", "")
    return blocks


def parse_bytes(ext: str, data: bytes) -> list[ParsedBlock]:
    """同步分派入口（纯文本系解析器）。未知扩展名 / 扫描件 → ParseError。

    ★ 出口统一清掉 NUL 字节（0x00）：PDF 文本层抽取会把字形噪声抽成 NUL
      （实测某 283 页 PDF 的第 282/283 页就有 `\\x001\\x002...` 这种序列），
      而 PostgreSQL 的 TEXT 明确拒收 0x00，报 invalid byte sequence for encoding "UTF8" ——
      一条 chunk 就能让整篇文档的 INSERT 整批失败。收口在这里而不是各 parser 内部，
      因为这是所有解析结果的唯一出口。
      ★ 只清 0x00：TEXT 接受其它 C0 控制字符（\\x01、\\x0b 等都能存），
        多删就是对解析内容做静默篡改。
    """
    handler = _DISPATCH.get(ext.lower())
    if handler is None:
        raise ParseError(f"不支持的文件类型: {ext}")
    return _strip_nul(handler(data))


async def parse_bytes_async(ext: str, data: bytes) -> list[ParsedBlock]:
    """异步分派入口 —— worker 的统一调用点（M7 WU-M-09 / WU-M-10）。

    图片扩展名走视觉链路（异步 LLM 调用）；pdf 走 parse_pdf_async
    （文本层优先，纯扫描件改道视觉链路）；其余走既有同步 parse_bytes；
    NUL 清洗对全部分支同样收口（同一唯一出口的口径不变）。
    ★ ocr_enabled=false 时图片 / 扫描件在此处 ParseError（fail-closed）。
    """
    ext_l = ext.lower()
    if ext_l in IMAGE_EXTS:
        return _strip_nul(await parse_image(data, ext_l))
    if ext_l == "pdf":
        return _strip_nul(await parse_pdf_async(data))
    return parse_bytes(ext_l, data)
