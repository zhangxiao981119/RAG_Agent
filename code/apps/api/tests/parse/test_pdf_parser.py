"""PDF 解析器测试 —— 本模块此前零覆盖。

★ 为什么现在补：实测发现 pypdf 把表格拍平成字符流，列关系全丢；
  而ParsedBlock 早有 is_table / table_header（xlsx 在用），chunk 层也已实现
  "表格整表不切、按行组切且每组带表头"，只是 PDF 从不填这两个字段 ——
  后果是 80 行表格切开后4 个 chunk 里 3 个丢了表头。

测试用 PyMuPDF 现场生成 PDF（不引入二进制测试固件），
因此 pdfplumber / PyMuPDF 任一缺失时应跳过而非失败。
"""
from __future__ import annotations

import pytest

pytest.importorskip("pymupdf", reason="PDF 测试固件生成需要 PyMuPDF")
fitz = pytest.importorskip("pymupdf")

from app.config import decisions  # noqa: E402
from app.services.chunk import chunk_blocks  # noqa: E402
from app.services.parse.base import ParsedBlock, ParseError  # noqa: E402
from app.services.parse.pdf_parser import parse_pdf, parse_pdf_async  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _warm_tiktoken():
    """预热 tiktoken 编码器。

    ★ chunk 模块在 import 时就调 tiktoken.get_encoding("cl100k_base")，
      首次使用要联网下载 BPE 编码文件。本机网络慢时这一步会挂住整个
      测试进程（实测表现为 pytest 无输出直到超时）。这里提前触发并
      容忍失败 —— 测试真正要验证的是 PDF 表格识别，不是网络。
    """
    try:
        chunk_blocks([_make_simple_block("warmup")])
    except Exception:  # pragma: no cover - 离线环境降级
        pytest.skip("tiktoken 编码器不可用（需联网下载 BPE 资源）", allow_module_level=True)


def _make_pdf(rows: list[list[str]], heading: str = "1. Section") -> bytes:
    """生成一个带边框表格的 PDF 字节流。

    ★ 只对表格画外框与横线，**不逐格画240+ 个矩形**。
      实测逐格 draw_rect + insert_text 到分钟级耗时（60×4 格即超时，
      pytest-timeout 的 thread 方法都切不断 —— 卡在 PyMuPDF 的 C 层）。
      pdfplumber 的表格识别依赖线条，规则网格的边框 + 行分隔线已经足够。
    """
    doc = fitz.open()
    page = doc.new_page()
    y = 60
    page.insert_text((60, y), heading, fontsize=14)
    y += 40

    ncol = len(rows[0])
    nrows = len(rows)
    cw, rh, x0, y0 = min(500 / ncol, 92), 20, 60, y
    total_w, total_h = cw * ncol, rh * nrows

    # 外框
    page.draw_rect(
        fitz.Rect(x0, y0, x0 + total_w, y0 + total_h),
        color=(0.3, 0.3, 0.3), width=0.7,
    )
    # 行分隔线（规则网格；pdfplumber 靠线条相交来识别表格，少画任一类都会漏识别）
    for ri in range(1, nrows):
        yy = y0 + ri * rh
        page.draw_line(
            fitz.Point(x0, yy), fitz.Point(x0 + total_w, yy),
            color=(0.3, 0.3, 0.3), width=0.7,
        )
    # 列分隔线
    for ci in range(1, ncol):
        xx = x0 + ci * cw
        page.draw_line(
            fitz.Point(xx, y0), fitz.Point(xx, y0 + total_h),
            color=(0.3, 0.3, 0.3), width=0.7,
        )

    # 文本逐格写（这步是必须的：表格内容要能被提取）
    for ri, row in enumerate(rows):
        for ci, cell in enumerate(row):
            page.insert_text(
                (x0 + ci * cw + 2, y0 + (ri + 1) * rh - 6), str(cell), fontsize=7
            )

    data = doc.tobytes()
    doc.close()
    return data


# ★ 长表格固件：4 列 × 60 行。
#   两个约束同时满足才行：
#   ① token 量要超 CHUNK_MAX_TOKENS=800 —— 否则"每个子块都带表头"是空转
#      （切分没发生时该断言永远成立）
#   ② 行数不能太多 —— 实测 120 行时 pdfplumber 的线条检测退化，
#      整页被当成普通文本（is_table 仍 True 但只截到部分内容）
#   靠"行数适中 + 单元格文本长"来同时满足，比单纯堆行数可靠。
LONG_HEADER = ["Service", "Region", "QPS", "Latency"]
LONG_ROWS = [
    [
        f"service-{i:02d}-request-handler-with-retry-policy",
        f"cn-region-{i % 5}-availability-zone-deployment",
        str(1000 + i * 7),
        f"{i}ms-p99-latency-observed-in-production",
    ]
    for i in range(60)
]


SMALL = [
    ["Resource", "Spec", "Qty", "Note"],
    ["Embedding", "bge-m3", "1", "local inference"],
    ["Reranker", "bge-reranker-v2-m3", "1", "local inference"],
    ["Database", "PostgreSQL 16", "1", "pgvector"],
]


def _make_simple_block(text: str):
    from app.services.parse.base import ParsedBlock
    return ParsedBlock(text=text)


class TestTableRecognition:
    def test_bordered_table_is_marked(self):
        """有边框的表格 MUST 被识别为表格块。"""
        blocks = parse_pdf(_make_pdf(SMALL))
        tables = [b for b in blocks if b.is_table]
        assert len(tables) == 1, f"应识别出 1 个表格块，实际 {len(tables)}"

    def test_table_header_extracted(self):
        """列头 MUST 落在 table_header 里（chunk 层靠它给每个子块带表头）。"""
        blocks = parse_pdf(_make_pdf(SMALL))
        table = next(b for b in blocks if b.is_table)
        assert table.table_header == SMALL[0]

    def test_table_text_uses_tsv_and_keeps_all_rows(self):
        """表格 text 用 TSV（保留列边界），且所有数据行都在。"""
        blocks = parse_pdf(_make_pdf(SMALL))
        table = next(b for b in blocks if b.is_table)
        lines = table.text.split("\n")
        assert lines[0].split("\t") == SMALL[0]
        assert len(lines) == len(SMALL), "数据行数必须完整保留"
        for row in SMALL[1:]:
            assert row[0] in table.text

    def test_page_no_set(self):
        blocks = parse_pdf(_make_pdf(SMALL))
        table = next(b for b in blocks if b.is_table)
        assert table.page_no == 1

    def test_wide_table_all_columns_kept(self):
        """11 列的宽表 MUST 保留全部列 —— 列丢失是最隐蔽的失败。"""
        header = ["ID", "Name", "Method", "Path", "Auth",
                  "Idem", "Timeout", "Retry", "Rate", "Owner", "Note"]
        rows = [header, ["F01", "Upload", "POST", "/documents", "yes",
                         "no", "30000", "3", "20/m", "backend", "max 100MB"]]
        table = next(b for b in parse_pdf(_make_pdf(rows)) if b.is_table)
        assert len(table.table_header) == 11
        assert len(table.text.split("\n")[0].split("\t")) == 11

    def test_no_table_content_duplication(self):
        """★ 表格内容 MUST NOT 同时出现在表格块与文本块里。

        否则同一份数据会被 embedding 两次，且两块向量不同构
        （一个 TSV 结构化、一个拍平字符流），检索时行为不可预期。
        """
        blocks = parse_pdf(_make_pdf(SMALL))
        table = next(b for b in blocks if b.is_table)
        cell = SMALL[1][0]  # "Embedding"
        in_table = cell in table.text
        in_text = any(
            not b.is_table and cell in b.text for b in blocks
        )
        assert in_table, "表格块里应含该单元格"
        assert not in_text, "表格内容不应在文本块里重复出现"


@pytest.fixture(scope="module")
def _long_table_pdf():
    """长表格 PDF 只生成一次（4 列 × 60 行，见 LONG_ROWS 处的说明）。

    ★ module 级而非 class 级：class 级 fixture 写成实例方法在新版 pytest 会告警，
      且测试顺序一变就要重算。这里用 module 级 + 提前生成，全类只付一次成本。
    """
    return _make_pdf([LONG_HEADER, *LONG_ROWS], heading="2. Capacity")


class TestChunkHeaderInjection:
    """表格超限被切开后，每个子块 MUST 都带表头（本次改造的核心目标）。"""

    def test_long_table_splits_into_multiple_chunks(self, _long_table_pdf):
        """前置断言：确认这个用例真的触发了切分，否则后面的断言是空转。"""
        blocks = parse_pdf(_long_table_pdf)
        chunks = chunk_blocks(blocks)
        assert len(chunks) > 1, "长表格应切出多个 chunk"

    def test_every_chunk_starts_with_header(self, _long_table_pdf):
        """★ 核心断言：切分后每个 chunk 的首行都必须是表头。"""
        blocks = parse_pdf(_long_table_pdf)
        chunks = chunk_blocks(blocks)
        header = "\t".join(LONG_HEADER)
        missing = [
            i for i, c in enumerate(chunks)
            if not c.content.startswith(header)
        ]
        assert not missing, f"以下 chunk 丢了表头：{missing}"

    def test_data_rows_are_not_lost(self, _long_table_pdf):
        """带表头不能以丢数据为代价 —— 所有数据行都必须还在。"""
        blocks = parse_pdf(_long_table_pdf)
        chunks = chunk_blocks(blocks)
        joined = "\n".join(c.content for c in chunks)
        missing = [
            i for i in range(len(LONG_ROWS))
            if f"service-{i:02d}" not in joined
        ]
        assert not missing, f"以下数据行丢失：{missing}"

    def test_every_chunk_stays_within_max(self, _long_table_pdf):
        """★ 切分后每个 chunk MUST <= CHUNK_MAX_TOKENS。

        这条同时守住另一件事：PDF 表格会让 unit 变成「表头 + 一行数据」，
        若那一组超过 MAX，`_accumulate` 会陷入死循环（清空 overlap 也装不下
        → index 永不推进）。所以它不只是性能约束，是可用性约束。
        """
        blocks = parse_pdf(_long_table_pdf)
        chunks = chunk_blocks(blocks)
        over = [
            (i, c.token_count) for i, c in enumerate(chunks)
            if c.token_count > decisions.CHUNK_MAX_TOKENS
        ]
        assert not over, f"以下 chunk 超过 CHUNK_MAX_TOKENS={decisions.CHUNK_MAX_TOKENS}：{over}"

    def test_short_table_not_split(self):
        """未超限的表格 MUST 保持整块（chunk 层对整表的处理路径）。"""
        blocks = parse_pdf(_make_pdf(SMALL))
        chunks = chunk_blocks(blocks)
        assert len(chunks) == 1


class TestScanRejection:
    """扫描件 MUST 显式拒绝，不能静默返回空（§8 D-04）。"""

    def test_scanned_pdf_raises(self):
        """无文本层的 PDF → ParseError，且提示里 MUST 含 OCR 字样。"""
        doc = fitz.open()
        page = doc.new_page()
        # 只画图形、不写字→ 无文本层
        page.draw_rect(fitz.Rect(50, 50, 300, 300), color=(0, 0, 0), width=2)
        data = doc.tobytes()
        doc.close()
        with pytest.raises(ParseError) as ei:
            parse_pdf(data)
        assert "OCR" in str(ei.value)

    def test_invalid_bytes_raise(self):
        with pytest.raises(ParseError):
            parse_pdf(b"not a pdf at all")

    def test_scan_check_precedes_table_detection(self):
        """★ 扫描件判定 MUST 优先于表格识别。

        顺序反了会把版面噪声（边框、分栏线）误当表格，
        让"明确拒绝扫描件"这条契约失效。
        """
        doc = fitz.open()
        page = doc.new_page()
        # 画大量格子（看起来像表格）但不写任何文字
        for r in range(6):
            for c in range(4):
                x, y = 60 + c * 80, 80 + r * 60
                page.draw_rect(fitz.Rect(x, y, x + 80, y + 60),
                               color=(0.3, 0.3, 0.3), width=0.8)
        data = doc.tobytes()
        doc.close()
        with pytest.raises(ParseError):
            parse_pdf(data)


# ── WU-M-10：扫描件改道视觉链路 ─────────────────────────────────


def _make_scanned_pdf(pages_text: list[str]) -> bytes:
    """生成真·扫描件固件：文字先渲染成图片再嵌回 PDF —— 页面无文本层。

    ★ 不能直接 insert_text：那会留下文本层，扫描判定（total_text < 10）不成立。
      正确做法是渲染成 pixmap 后 insert_image，视觉上看得到字、文本层为空。
    """
    src = fitz.open()
    out = fitz.open()
    for text in pages_text:
        page = src.new_page()
        y = 60
        for line in text.split("\n"):
            page.insert_text((60, y), line, fontsize=12)
            y += 24
        pix = page.get_pixmap(dpi=150)
        new_page = out.new_page(width=page.rect.width, height=page.rect.height)
        new_page.insert_image(new_page.rect, pixmap=pix)
    data = out.tobytes()
    src.close()
    out.close()
    return data


def _set_ocr(
    monkeypatch: pytest.MonkeyPatch, enabled: bool, base_url: str = "http://ocr:8000"
) -> None:
    """get_settings 是 lru_cache 单例，直接改实例属性，monkeypatch 负责还原。"""
    from app.config.settings import get_settings
    monkeypatch.setattr(get_settings(), "ocr_enabled", enabled)
    monkeypatch.setattr(get_settings(), "ocr_base_url", base_url)


def _patch_ocr(
    monkeypatch: pytest.MonkeyPatch,
    returns: list[str],
    exc_on: dict[int, Exception] | None = None,
) -> list:
    """打桩 OCR 客户端：按调用序返回各页文本；返回调用记录容器。

    ★ 桩打在 ocr 模块属性上 —— _parse_scanned_pdf 每次调用时才做
      from-import，读到的是打桩后的属性（与 test_image_parser 同一模式）。
    """
    from app.services import ocr as ocr_mod
    calls: list = []

    async def _fake(data: bytes, mime: str = "image/png"):
        idx = len(calls)
        calls.append((data, mime))
        if exc_on and idx in exc_on:
            raise exc_on[idx]
        return ParsedBlock(text=returns[idx])

    monkeypatch.setattr(ocr_mod, "ocr_image", _fake)
    return calls


class TestScannedPdfOcrPath:
    """WU-M-10 + B'：纯扫描件改道本地 OCR；文本层优先原则不变。"""

    @pytest.mark.asyncio
    async def test_scanned_pdf_goes_to_ocr_and_fills_page_no(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """纯扫描件 + OCR 开 → 逐页 OCR 转录，每页块 MUST 带 page_no（INV-M-05）。"""
        _set_ocr(monkeypatch, True)
        calls = _patch_ocr(monkeypatch, ["page one content", "page two content"])
        data = _make_scanned_pdf(["page one content", "page two content"])

        blocks = await parse_pdf_async(data)

        assert len(calls) == 2, "每页 MUST 各发起一次 OCR 调用"
        assert [b.page_no for b in blocks] == [1, 2]
        assert blocks[0].text == "page one content"
        assert blocks[1].text == "page two content"
        assert all(c[1] == "image/png" for c in calls), "渲染页以 PNG 字节直传"

    @pytest.mark.asyncio
    async def test_scanned_pdf_rejected_when_ocr_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """OCR 关 → 维持原 ParseError（fail-closed，M7 D6）。"""
        _set_ocr(monkeypatch, False)
        data = _make_scanned_pdf(["any content"])
        with pytest.raises(ParseError, match="疑似扫描件"):
            await parse_pdf_async(data)

    @pytest.mark.asyncio
    async def test_scanned_pdf_rejected_when_base_url_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """B'：开关已开但服务地址未配置 → ParseError（fail-closed，不降级）。"""
        _set_ocr(monkeypatch, True, base_url="")
        data = _make_scanned_pdf(["any content"])
        with pytest.raises(ParseError, match="OCR_BASE_URL"):
            await parse_pdf_async(data)

    @pytest.mark.asyncio
    async def test_scanned_pdf_rejected_over_max_pages(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """B'：页数超过 OCR_MAX_SCAN_PAGES → ParseError，且不发起任何 OCR 调用。"""
        from app.config import decisions as decisions_mod

        _set_ocr(monkeypatch, True)
        calls = _patch_ocr(monkeypatch, ["x"])
        monkeypatch.setattr(decisions_mod, "OCR_MAX_SCAN_PAGES", 1)
        data = _make_scanned_pdf(["p1", "p2"])
        with pytest.raises(ParseError, match="超过单份 OCR 上限"):
            await parse_pdf_async(data)
        assert calls == [], "超限 MUST 在渲染前拒绝，不发起任何 OCR 调用"

    @pytest.mark.asyncio
    async def test_ocr_failure_carries_page_no(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """某页 OCR 失败 → ParseError 且带页码（INV-M-04：不静默跳页）。"""
        from app.services.ocr import OcrNoContentError

        _set_ocr(monkeypatch, True)
        _patch_ocr(
            monkeypatch,
            ["page one content", ""],
            exc_on={1: OcrNoContentError("图中未识别到可读内容")},
        )
        data = _make_scanned_pdf(["page one content", "page two content"])
        with pytest.raises(ParseError, match="第 2 页"):
            await parse_pdf_async(data)

    @pytest.mark.asyncio
    async def test_text_layer_pdf_never_calls_ocr(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """文本层优先：即使 OCR 开，文本层 PDF MUST NOT 触达 OCR 链路。"""
        _set_ocr(monkeypatch, True)

        async def _forbidden(data, mime="image/png"):  # pragma: no cover - 被调用即失败
            raise AssertionError("文本层 PDF 不应进入 OCR 链路")

        from app.services import ocr as ocr_mod
        monkeypatch.setattr(ocr_mod, "ocr_image", _forbidden)

        blocks = await parse_pdf_async(_make_pdf(SMALL))
        assert any(b.is_table for b in blocks), "文本层管线的表格识别结果不变"

    @pytest.mark.asyncio
    async def test_sync_entry_still_rejects_scanned(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """同步入口 parse_pdf 无异步承载能力，扫描件在此仍 ParseError（口径见 docstring）。"""
        _set_ocr(monkeypatch, True)
        data = _make_scanned_pdf(["any content"])
        with pytest.raises(ParseError, match="疑似扫描件"):
            parse_pdf(data)