"""PDF 解析 —— 文本层提取 + 表格结构识别 + 扫描件视觉改道（手册 §3.3.1 + M7 WU-M-10）。

三条路径：
  · 表格页：pdfplumber 识别表格 → 表格块；同页文本用 pdfplumber 的词坐标
    **排除表格区域**后重建，避免同一份数据在blocks 里出现两次
  · 无表格页：pypdf 的 extract_text() + 行首模式匹配标题（pypdf 无 layout）
  · 纯扫描件（全文文本层总长 < 10）：不在此文件内整份 OCR，而由 worker 的
    **分段流水线**（run_parse_segment / run_parse_finalize，方案 C）驱动 ——
    本文件只提供三个原语：ocr_scanned_pages（按页区间渲染 PNG → 本地 OCR
    服务 apps/ocr 转录，不做标题识别）、blocks_from_page_texts（聚合期按全局
    页序做跨页标题累积 + 每页 flush，填 page_no —— INV-M-05 可回链）、
    ensure_scanned_page_count（文档级页数护栏）。受 settings.ocr_enabled +
    ocr_base_url 双门控，任一未就绪均维持 ParseError（fail-closed，无兜底）。
    ★ 为什么分段而不整份一次 OCR：单页实测 22~180s，整份 OCR 会越过
      parse_job_timeout（1800s）被 arq 杀掉；段长 = OCR_MAX_SCAN_PAGES。

★ 文本层优先原则不变：只要全文文本层总量达标（>= 10），绝不进入视觉链路 ——
  混合文档中个别无文本的页与改造前一样被跳过，行为逐字节一致。

★ 扫描件判定优先于表格识别 —— 扫描件里既没有文本层
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
from collections.abc import Iterable

from app.services.parse.base import ParsedBlock, ParseError, cell_to_text

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
    # 输入是 _normalize_table 已清洗的字符串矩阵；仍走 cell_to_text 保持单点口径
    return "\t".join(cell_to_text(v) for v in row)


def _normalize_table(raw: list[list[object]]) -> list[list[str]] | None:
    """pdfplumber 原始单元格 → 规整字符串矩阵。None = 不可信，当普通文本处理。

    ★ 合并单元格不展开（与 xlsx/xls/docx 不同）：PDF 是渲染产物，
      pdfplumber 没有合并模型 —— 合并区的文本只落进左上格，被覆盖的
      格子是 None，而 None 同时也是"真空格"的取值。任何填充都是在
      区分不了的两种语义之间赌博（稀疏表会把空格填成错误数据），
      宁可保守留空 —— 消除猜测，不做启发式。
    """
    rows: list[list[str]] = []
    for raw_row in raw:
        cells = [cell_to_text(c) for c in raw_row]
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


_SCANNED_TEXT_MIN_CHARS: int = 10
"""文本层总量低于此值 → 判定为扫描件。口径单点：parse_pdf / parse_pdf_async
与 worker 的路径分派共用同一判定（见 is_scanned_text_layer），改一处即全局生效。"""


def is_scanned_text_layer(page_texts: list[str]) -> bool:
    """逐页文本层是否判定为扫描件（总量 < _SCANNED_TEXT_MIN_CHARS）。"""
    return sum(len(t) for t in page_texts) < _SCANNED_TEXT_MIN_CHARS


def extract_pdf_page_texts(data: bytes) -> list[str]:
    """逐页抽取文本层（pypdf）。返回每页文本列表，页序即 page_no，长度即页数。

    公开入口：worker 用它一次判定「是否扫描件 + 总页数」，避免自己重开 PDF。
    """
    try:
        reader = PdfReader(io.BytesIO(data))  # type: ignore[call-arg]
    except Exception as exc:
        raise ParseError(f"PDF 打开失败: {exc}") from exc

    page_texts: list[str] = []
    for page in reader.pages:
        try:
            page_text = page.extract_text() or ""
        except Exception:
            page_text = ""
        page_texts.append(page_text)
    return page_texts


class _HeadingAccumulator:
    """跨页标题累积 + 页内块 flush（文本层管线与扫描件聚合期共用，口径单点）。

    ★ heading 跨页延续：标题一旦出现，后续页直到下一个标题都归属它。
    ★ 每页结束 MUST flush —— 否则 page_no 会串页（INV-M-05 要求每块带正确页码）。
    ★ 扫描件被切成多段后，标题累积 MUST 在聚合期（blocks_from_page_texts）
      统一进行，段内只产原始文本 —— 否则跨段标题会断。
    """

    def __init__(self) -> None:
        self.heading = ""
        self._buffer: list[str] = []

    def feed(self, lines: Iterable[str], page_no: int, blocks: list[ParsedBlock]) -> None:
        """逐行累积：标题行先 flush 当前缓冲再更新 heading，其余行进缓冲。"""
        for line in lines:
            if _is_heading(line):
                self.flush(blocks, page_no)
                self.heading = line.strip()
            else:
                self._buffer.append(line)

    def flush(self, blocks: list[ParsedBlock], page_no: int) -> None:
        """把缓冲内容落为一个块（空则丢弃），块带当前 heading 与页码。"""
        content = "\n".join(self._buffer).strip()
        if content:
            blocks.append(
                ParsedBlock(text=content, heading_path=self.heading, page_no=page_no)
            )
        self._buffer = []


def _text_layer_pipeline(page_texts: list[str], data: bytes) -> list[ParsedBlock]:
    """文本层主管线：表格识别 + 标题切分（逻辑与 M7 前完全一致，逐字节未改）。"""
    tables_by_page, outside_by_page = _extract_page_tables(data)

    blocks: list[ParsedBlock] = []
    acc = _HeadingAccumulator()

    for page_index, page_text in enumerate(page_texts, start=1):
        rows_list = tables_by_page.get(page_index)

        if rows_list is not None:
            # 表格页：表格块用「表格之前」的 heading，随后用 pdfplumber 重建的
            # "表格之外"文本继续累积标题，避免同一数据入块两次
            outside = outside_by_page.get(page_index) or page_text
            for rows in rows_list:
                blocks.append(_table_to_block(rows, acc.heading, page_index))
            acc.feed(outside.split("\n"), page_index, blocks)
        else:
            acc.feed(page_text.split("\n"), page_index, blocks)

        acc.flush(blocks, page_index)

    return blocks


def parse_pdf(data: bytes) -> list[ParsedBlock]:
    """同步入口（纯文本层）。扫描件在此仍 ParseError —— OCR 链路是异步的，
    同步签名承载不了；扫描件请走 worker 的分段 OCR 流水线。"""
    page_texts = extract_pdf_page_texts(data)

    # ── ① 先取全文文本判定扫描件（优先级最高，先于表格识别）──
    if is_scanned_text_layer(page_texts):
        raise ParseError(
            "PDF 文本层为空，疑似扫描件。请提供文本层 PDF 或置 OCR_ENABLED=true "
            "并部署本地 OCR 服务（扫描件经 worker 分段流水线处理）。"
        )

    return _text_layer_pipeline(page_texts, data)


# 扫描件页渲染 DPI：150 是可读性与体积的常规平衡点（72 太低字糊，300 体积翻倍）
_SCAN_RENDER_DPI: int = 150


def ensure_ocr_ready() -> None:
    """OCR 服务就绪门控（fail-closed）：未启用 / 未配置地址 → ParseError。

    ★ 属**永久性**约束 —— 重试不会自愈（配置不改就永远失败），
      调用方（worker 段流水线）据此直接判死而非重试。
    ★ 与 ocr_scanned_pages 各自调用一次：前者用于失败分类，后者保证
      任何直连调用者也 fail-closed，不依赖调用方记得先检查。
    """
    from app.config.settings import get_settings

    settings = get_settings()
    if not settings.ocr_enabled:
        raise ParseError(
            "扫描件 OCR 未启用（OCR_ENABLED=false，fail-closed 无纯文本降级）。"
            "请置 OCR_ENABLED=true 并部署本地 apps/ocr 服务。"
        )
    if not settings.ocr_base_url:
        raise ParseError(
            "扫描件 OCR 已启用但未配置 OCR_BASE_URL（fail-closed）。"
            "请部署 apps/ocr 服务并注入 OCR_BASE_URL。"
        )


def ensure_scanned_page_count(page_count: int) -> None:
    """文档级页数护栏：超过 OCR_MAX_DOC_PAGES → ParseError（防 DOS）。

    ★ 与单段上限（OCR_MAX_SCAN_PAGES）职责不同：单段上限决定段数，
      本函数只管「整份文档」上限，防止超长扫描件把段链拉成千段。
    """
    from app.config import decisions

    if page_count > decisions.OCR_MAX_DOC_PAGES:
        raise ParseError(
            f"扫描件共 {page_count} 页，超过单份文档 OCR 上限 "
            f"{decisions.OCR_MAX_DOC_PAGES} 页，请拆分后上传。"
        )


async def ocr_scanned_pages(
    data: bytes, page_start: int, page_end: int
) -> list[str]:
    """渲染并逐页 OCR 扫描件 [page_start, page_end]（1-based 闭区间），
    返回**每页原始文本**（不做标题识别，标题累积见 blocks_from_page_texts）。

    ★ 只做「图片 → 文本」，区间由调用方（worker 分段）决定 —— 单段页数
      由 OCR_MAX_SCAN_PAGES 约束，保证单段耗时落在 parse_job_timeout 内。
    ★ 单引擎 fail-closed：服务未配置 / 任一页 OCR 失败 → ParseError（带页码），
      不静默跳页、不换引擎兜底。
    """
    from app.services.ocr import OcrError, ocr_image

    ensure_ocr_ready()

    try:
        import pymupdf
    except ImportError as exc:  # pragma: no cover - 依赖必装（requirements.lock）
        raise ParseError(f"扫描件渲染依赖 PyMuPDF 不可用: {exc}") from exc

    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise ParseError(f"扫描件 PDF 打开失败: {exc}") from exc

    texts: list[str] = []
    with doc:
        for page_index in range(page_start, page_end + 1):
            page = doc[page_index - 1]
            png = page.get_pixmap(dpi=_SCAN_RENDER_DPI).tobytes("png")
            # 质量门禁：检测 + 必要时去噪/去污/纠偏；reject 显式转人工（带页码）
            from app.services.parse.image_quality import prepare_for_ocr

            try:
                png, _quality = prepare_for_ocr(png, source="pdf_page")
            except ParseError as exc:
                raise ParseError(
                    f"扫描件第 {page_index} 页质量门禁拒绝：{exc}"
                ) from exc
            try:
                block = await ocr_image(png, "image/png")
            except OcrError as exc:
                raise ParseError(
                    f"扫描件第 {page_index} 页 OCR 转录失败：{exc}"
                ) from exc
            # 文本层水印清理（逐页）：页内重复短行 + 密级短语行
            from app.services.parse.image_quality import strip_watermark_text

            text, _cleanup = strip_watermark_text(
                block.text,
                watermark_detected=bool(_quality and _quality.watermark_detected),
            )
            texts.append(text)
    return texts


def blocks_from_page_texts(page_texts: list[str]) -> list[ParsedBlock]:
    """全局页序的**每页原始文本** → blocks（跨页标题累积 + 每页 flush）。

    扫描件分段流水线的聚合期唯一入口（run_parse_finalize）：段只产原始文本，
    标题识别推迟到这里，保证跨段标题延续与「整份一次解析」口径一致。
    page_texts[i] 对应 page_no = i + 1（INV-M-05 可回链）。
    """
    blocks: list[ParsedBlock] = []
    acc = _HeadingAccumulator()
    # 跨页重复短行清理（平铺水印 / 页眉页脚）—— 只有聚合期才看得到全文档页序
    from app.services.parse.image_quality import strip_repeated_page_lines

    page_texts, _removed = strip_repeated_page_lines(page_texts)
    for page_no, page_text in enumerate(page_texts, start=1):
        acc.feed(page_text.split("\n"), page_no, blocks)
        acc.flush(blocks, page_no)
    return blocks


async def parse_pdf_async(data: bytes) -> list[ParsedBlock]:
    """异步入口 —— **仅文本层**。扫描件 MUST 经 worker 的分段 OCR 流水线
    （run_parse_segment），此处 fail-closed 拒绝，避免整份一次 OCR 越过任务超时。"""
    page_texts = extract_pdf_page_texts(data)

    if is_scanned_text_layer(page_texts):
        raise ParseError(
            "PDF 文本层为空，疑似扫描件：扫描件 MUST 经 worker 分段 OCR 流水线"
            "（run_parse_segment）处理，此处不承载扫描件。"
        )

    return _text_layer_pipeline(page_texts, data)