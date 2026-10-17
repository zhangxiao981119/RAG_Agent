"""图片解析器单测（WU-M-09 / INV-M-01 / INV-M-02 / INV-M-04 / B'）。

覆盖：
  · ocr_enabled=false → ParseError（解析层 fail-closed，M7 D6 口径）
  · ocr_enabled=true 且 OCR_BASE_URL 已配置 → 走本地 OCR 客户端，原始字节直传
  · OCR_BASE_URL 缺失 → ParseError（B' fail-closed，无双引擎兜底）
  · OcrError → 映射为 ParseError（原因保留，不降级）
  · parse_bytes_async 分派：图片走异步分支 + NUL 清洗收口；未知扩展名仍拒
  · 同步 parse_bytes 回归：重构后纯文本系行为不变

★ OCR 调用统一在 ocr_mod 上打桩（parse_image 每次调用时延迟导入，
  桩必然生效）；不发起真实 HTTP 请求。
"""
from __future__ import annotations

import pytest

from app.config.settings import get_settings
from app.services import ocr as ocr_mod
from app.services.ocr import OcrNoContentError
from app.services.parse import parse_bytes, parse_bytes_async
from app.services.parse.base import ParsedBlock, ParseError
from app.services.parse.image_parser import parse_image


def _set_ocr(
    monkeypatch: pytest.MonkeyPatch, enabled: bool, base_url: str = "http://ocr:8000"
) -> None:
    """get_settings 是 lru_cache 单例，直接改实例属性，monkeypatch 负责还原。"""
    monkeypatch.setattr(get_settings(), "ocr_enabled", enabled)
    monkeypatch.setattr(get_settings(), "ocr_base_url", base_url)


def _patch_ocr(
    monkeypatch: pytest.MonkeyPatch,
    block: ParsedBlock | None = None,
    exc: Exception | None = None,
) -> dict:
    """打桩 ocr_image；返回记录容器供断言入参。"""
    calls: dict = {}

    async def _fake(data: bytes, mime: str = "image/png"):
        calls["data"] = data
        calls["mime"] = mime
        if exc is not None:
            raise exc
        return block

    monkeypatch.setattr(ocr_mod, "ocr_image", _fake)
    return calls


# ── 门控（fail-closed）──────────────────────────────────────────


@pytest.mark.asyncio
async def test_parse_image_rejected_when_ocr_disabled(monkeypatch: pytest.MonkeyPatch):
    _set_ocr(monkeypatch, False)
    with pytest.raises(ParseError, match="OCR_ENABLED=false"):
        await parse_image(b"raw-bytes", "png")


@pytest.mark.asyncio
async def test_parse_image_rejected_when_base_url_missing(
    monkeypatch: pytest.MonkeyPatch,
):
    """B'：开关已开但未配置服务地址 → client 层 OcrError 改型 ParseError。"""
    _set_ocr(monkeypatch, True, base_url="")
    with pytest.raises(ParseError, match="OCR_BASE_URL"):
        await parse_image(b"raw-bytes", "png")


# ── 正常路径（走本地 OCR 客户端）────────────────────────────────


@pytest.mark.asyncio
async def test_parse_image_passes_raw_bytes(
    monkeypatch: pytest.MonkeyPatch,
):
    """B'：原始字节直传 OCR 服务，MIME 按扩展名显式映射（不嗅探、不 base64 内联）。"""
    _set_ocr(monkeypatch, True)
    calls = _patch_ocr(monkeypatch, ParsedBlock(text="物料编码 M-1002"))
    blocks = await parse_image(b"raw-bytes", "png")
    assert len(blocks) == 1
    assert blocks[0].text == "物料编码 M-1002"
    assert calls["data"] == b"raw-bytes"
    assert calls["mime"] == "image/png"


@pytest.mark.asyncio
async def test_jpeg_ext_maps_mime(monkeypatch: pytest.MonkeyPatch):
    _set_ocr(monkeypatch, True)
    calls = _patch_ocr(monkeypatch, ParsedBlock(text="t"))
    await parse_image(b"raw", "jpeg")
    assert calls["mime"] == "image/jpeg"


# ── 异常映射（INV-M-04：不降级，只改型）────────────────────────


@pytest.mark.asyncio
async def test_ocr_no_content_maps_to_parse_error(monkeypatch: pytest.MonkeyPatch):
    _set_ocr(monkeypatch, True)
    _patch_ocr(monkeypatch, exc=OcrNoContentError("图中未识别到可读内容"))
    with pytest.raises(ParseError, match="OCR 转录失败"):
        await parse_image(b"raw", "png")


# ── 异步分派入口 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_parse_bytes_async_routes_image_and_strips_nul(
    monkeypatch: pytest.MonkeyPatch,
):
    _set_ocr(monkeypatch, True)
    _patch_ocr(monkeypatch, ParsedBlock(text="a\x00b"))
    blocks = await parse_bytes_async("png", b"raw")
    assert blocks[0].text == "ab"  # NUL 在统一出口被清（唯一出口口径不变）


@pytest.mark.asyncio
async def test_parse_bytes_async_unknown_ext_rejected(monkeypatch: pytest.MonkeyPatch):
    _set_ocr(monkeypatch, True)
    with pytest.raises(ParseError, match="不支持的文件类型"):
        await parse_bytes_async("exe", b"raw")


# ── 同步入口回归（重构不改变纯文本系行为）──────────────────────


def test_sync_parse_bytes_regression():
    blocks = parse_bytes("txt", "第一段\n\n第二段".encode("utf-8"))
    assert [b.text for b in blocks]
    with pytest.raises(ParseError, match="不支持的文件类型"):
        parse_bytes("exe", b"raw")
