"""图片解析器单测（WU-M-09 / INV-M-01 / INV-M-02 / INV-M-04 / B'）。

覆盖：
  · ocr_enabled=false → ParseError（解析层 fail-closed，M7 D6 口径）
  · ocr_enabled=true → 走视觉 LLM，图片以 data URL（base64 内联）送模型
  · 表格图 → is_table / table_header 原样透传（表格截图走整表切片）
  · 视觉链路无可读内容 / 不可用 → 映射为 ParseError（原因保留，不降级）
  · parse_bytes_async 分派：图片走异步分支 + NUL 清洗收口；未知扩展名仍拒
  · 同步 parse_bytes 回归：重构后纯文本系行为不变

★ 视觉调用统一在 vision 模块上打桩（parse_image 每次调用时延迟导入，
  桩必然生效）；不发起真实模型请求。
★ 送进解析器的字节是无效图片：质量门禁的 analyze 会失败并按其既有口径
  回退原图（本文件验证的是视觉链路，不含质量门禁分支）。
"""
from __future__ import annotations

import base64

import pytest

from app.config.settings import get_settings
from app.services.multimodal import vision as vision_mod
from app.services.parse import parse_bytes, parse_bytes_async
from app.services.parse.base import ParsedBlock, ParseError
from app.services.parse.image_parser import parse_image


def _set_ocr(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    """get_settings 是 lru_cache 单例，直接改实例属性，monkeypatch 负责还原。"""
    monkeypatch.setattr(get_settings(), "ocr_enabled", enabled)


def _patch_vision(
    monkeypatch: pytest.MonkeyPatch,
    block: ParsedBlock | None = None,
    exc: Exception | None = None,
) -> dict:
    """打桩视觉转录；返回记录容器供断言入参（data URL）。"""
    calls: dict = {}

    async def _fake(image_ref):
        calls["data_url"] = image_ref.data_url
        if exc is not None:
            raise exc
        return block

    monkeypatch.setattr(vision_mod, "extract_text_from_image", _fake)
    return calls


# ── 门控（fail-closed）──────────────────────────────────────────


@pytest.mark.asyncio
async def test_parse_image_rejected_when_ocr_disabled(monkeypatch: pytest.MonkeyPatch):
    _set_ocr(monkeypatch, False)
    with pytest.raises(ParseError, match="OCR_ENABLED=false"):
        await parse_image(b"raw-bytes", "png")


# ── 正常路径（走视觉 LLM）───────────────────────────────────────


@pytest.mark.asyncio
async def test_parse_image_inlines_base64_data_url(
    monkeypatch: pytest.MonkeyPatch,
):
    """图片以 data URL 内联送模型，MIME 按扩展名显式映射（不做字节嗅探）。"""
    _set_ocr(monkeypatch, True)
    calls = _patch_vision(monkeypatch, ParsedBlock(text="物料编码 M-1002"))
    blocks = await parse_image(b"raw-bytes", "png")
    assert len(blocks) == 1
    assert blocks[0].text == "物料编码 M-1002"
    prefix = "data:image/png;base64,"
    assert calls["data_url"].startswith(prefix)
    assert base64.b64decode(calls["data_url"][len(prefix):]) == b"raw-bytes"


@pytest.mark.asyncio
async def test_jpeg_ext_maps_mime(monkeypatch: pytest.MonkeyPatch):
    _set_ocr(monkeypatch, True)
    calls = _patch_vision(monkeypatch, ParsedBlock(text="t"))
    await parse_image(b"raw", "jpeg")
    assert calls["data_url"].startswith("data:image/jpeg;base64,")


@pytest.mark.asyncio
async def test_table_image_keeps_table_contract(monkeypatch: pytest.MonkeyPatch):
    """表格截图 → is_table / table_header 原样透传（与文本表格同一套切片路径）。"""
    _set_ocr(monkeypatch, True)
    _patch_vision(
        monkeypatch,
        ParsedBlock(
            text="型号\t外观\nAX3000\t白色",
            is_table=True,
            table_header=["型号", "外观"],
        ),
    )
    blocks = await parse_image(b"raw", "png")
    assert blocks[0].is_table is True
    assert blocks[0].table_header == ["型号", "外观"]


# ── 异常映射（INV-M-04：不降级，只改型）────────────────────────


@pytest.mark.asyncio
async def test_vision_no_content_maps_to_parse_error(monkeypatch: pytest.MonkeyPatch):
    _set_ocr(monkeypatch, True)
    _patch_vision(
        monkeypatch, exc=vision_mod.VisionNoContentError("图中未识别到可读内容")
    )
    with pytest.raises(ParseError, match="未识别到可读内容"):
        await parse_image(b"raw", "png")


@pytest.mark.asyncio
async def test_vision_error_maps_to_parse_error(monkeypatch: pytest.MonkeyPatch):
    _set_ocr(monkeypatch, True)
    _patch_vision(monkeypatch, exc=vision_mod.VisionError("模型不可用"))
    with pytest.raises(ParseError, match="视觉转录失败"):
        await parse_image(b"raw", "png")


# ── 异步分派入口 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_parse_bytes_async_routes_image_and_strips_nul(
    monkeypatch: pytest.MonkeyPatch,
):
    _set_ocr(monkeypatch, True)
    _patch_vision(monkeypatch, ParsedBlock(text="a\x00b"))
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
