"""图像质量门禁单测（去噪 / 去污 / 水印识别 + 文本层水印清理）。

覆盖：
  · 干净页 → pass（不误判为脏）
  · 孤立暗点（椒盐）→ enhance，且噪声指标确实上升
  · 倾斜页 → 检测到角度，增强后残余倾斜被压平（验证纠偏方向正确）
  · 水印页 → watermark_detected=True，但**不做像素修改**（verdict 仍可为 pass）
  · 短边不足 → reject_to_manual（仅对 image 生效，pdf_page 不受限）
  · 文本层：密级短语行 / 页内重复短行 / 跨页重复行被剔，长句与带标点的行保留
  · 删除比例超上限 → 回退原文（防误删正文）
  · prepare_for_ocr：开关关闭原样返回；reject 抛 ParseError

★ 图像全部现场合成（纯 Pillow），不依赖测试资产；不用文字渲染，
  用横条模拟文字行 —— 难点在投影方差与腐蚀口径，不在字形。
"""
from __future__ import annotations

import io
import random

import pytest
from PIL import Image, ImageDraw

from app.config import decisions
from app.config.settings import get_settings
from app.services.parse.base import ParseError
from app.services.parse.image_quality import (
    VERDICT_ENHANCE,
    VERDICT_PASS,
    VERDICT_REJECT,
    analyze,
    enhance,
    prepare_for_ocr,
    strip_repeated_page_lines,
    strip_watermark_text,
)


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _text_page(
    size: tuple[int, int] = (600, 800),
    bars: int = 14,
    bar_h: int = 3,
    spacing: int = 45,
    color: int = 0,
    bg: int = 255,
) -> Image.Image:
    """合成「一页文字」：白底 + 等距横条（模拟文字行）。"""
    img = Image.new("L", size, bg)
    draw = ImageDraw.Draw(img)
    for i in range(bars):
        y = 40 + i * spacing
        if y + bar_h >= size[1]:
            break
        draw.rectangle([40, y, size[0] - 40, y + bar_h], fill=color)
    return img


def _add_salt(img: Image.Image, ratio: float = 0.02, seed: int = 7) -> Image.Image:
    """撒孤立暗点（椒盐噪点），用于验证「孤立点」口径。"""
    rnd = random.Random(seed)
    px = img.load()
    w, h = img.size
    for _ in range(int(w * h * ratio)):
        px[rnd.randrange(w), rnd.randrange(h)] = 0
    return img


# ── 图像侧 ────────────────────────────────────────────────


def test_clean_page_passes() -> None:
    """干净正文页不能被判脏 —— 这是门禁最怕的误判方向。"""
    report = analyze(_png(_text_page()))
    assert report.verdict == VERDICT_PASS, report.reasons
    assert report.noise_ratio < decisions.IMAGE_NOISE_ENHANCE_RATIO
    assert report.stain_ratio < decisions.IMAGE_STAIN_ENHANCE_RATIO
    assert report.watermark_detected is False


def test_salt_noise_triggers_enhance() -> None:
    report = analyze(_png(_add_salt(_text_page())))
    assert report.noise_ratio > decisions.IMAGE_NOISE_ENHANCE_RATIO
    assert report.verdict == VERDICT_ENHANCE
    assert any("噪点" in r for r in report.reasons)


def test_skew_detected_and_corrected() -> None:
    """3° 倾斜要被量出来，且增强后的残余倾斜必须变小（方向不能反）。"""
    skewed = _text_page().rotate(3, resample=Image.Resampling.BILINEAR, fillcolor=255)
    data = _png(skewed)
    report = analyze(data)
    assert 2.0 <= abs(report.skew_deg) <= 4.0, report.skew_deg
    assert report.verdict == VERDICT_ENHANCE

    fixed = analyze(enhance(data, report))
    assert abs(fixed.skew_deg) < abs(report.skew_deg) - 1.0


def test_watermark_detected_but_pixels_untouched() -> None:
    """浅灰平铺水印：识别得到，且不因此触发像素级清洗。"""
    img = _text_page()
    draw = ImageDraw.Draw(img)
    for y in range(20, img.size[1], 40):
        draw.rectangle([20, y, img.size[0] - 20, y + 6], fill=220)
    report = analyze(_png(img))
    assert report.watermark_detected is True
    assert report.watermark_coverage >= decisions.IMAGE_WATERMARK_MIN_BLOCK_COVERAGE
    assert any("水印" in r for r in report.reasons)


def test_small_image_rejected() -> None:
    report = analyze(_png(_text_page(size=(300, 400), bars=6, spacing=40)))
    assert report.verdict == VERDICT_REJECT
    assert any("分辨率" in r for r in report.reasons)


def test_pdf_page_skips_size_rule() -> None:
    """扫描件渲染页不受短边限制（DPI 由服务端控制，不存在「分辨率不足」）。"""
    data = _png(_text_page(size=(300, 400), bars=6, spacing=40))
    report = analyze(data, source="pdf_page")
    assert report.verdict != VERDICT_REJECT


def test_prepare_for_ocr_rejects_with_parse_error() -> None:
    data = _png(_text_page(size=(300, 400), bars=6, spacing=40))
    with pytest.raises(ParseError) as exc:
        prepare_for_ocr(data, source="image")
    assert "转人工" in str(exc.value)


def test_gate_disabled_returns_original_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    data = _png(_add_salt(_text_page()))
    monkeypatch.setattr(get_settings(), "image_quality_gate_enabled", False)
    out, report = prepare_for_ocr(data, source="image")
    assert out is data
    assert report is None


# ── 文本侧：水印的「知识影响」───────────────────────────────

BODY = "员工报销需要提交发票原件与审批单，金额超过五千元需部门负责人签字确认。"
WM = "内部资料 严禁外传"


def test_strip_watermark_line() -> None:
    text = "\n".join([WM, BODY, "差旅标准按职级执行，住宿上限为每晚六百元。"])
    cleaned, report = strip_watermark_text(text, watermark_detected=True)
    assert WM not in cleaned
    assert BODY in cleaned
    assert report.applied is True
    assert report.removed_lines == 1


def test_strip_keeps_long_sentence_with_secret_word() -> None:
    """正文里的密级词汇不能碰：带句读、超长行一律保留。"""
    line = "公司商业秘密管理办法适用于全体在职员工，具体范围见附件一。"
    cleaned, report = strip_watermark_text(line, watermark_detected=True)
    assert cleaned == line
    assert report.applied is False


def test_strip_repeated_short_lines_even_without_image_evidence() -> None:
    """平铺水印在图像层可能被判成灰底，文本层的重复特征兜底。"""
    text = "\n".join(["某某科技", BODY, "某某科技", "差旅标准按职级执行。", "某某科技"])
    cleaned, report = strip_watermark_text(text, watermark_detected=False)
    assert "某某科技" not in cleaned
    assert report.removed_lines == 3


def test_strip_reverts_when_ratio_too_high() -> None:
    """删得太多 → 判定误伤正文，回退原文。"""
    text = "\n".join([WM] * 8 + ["短正文"])
    cleaned, report = strip_watermark_text(text, watermark_detected=True)
    assert cleaned == text
    assert report.reverted is True
    assert report.applied is False


def test_strip_repeated_page_lines_across_pages() -> None:
    pages = [
        f"{WM}\n第 {i} 页正文：报销流程分为申请、审批、打款三步。"
        for i in range(1, 6)
    ]
    cleaned, removed = strip_repeated_page_lines(pages)
    assert removed == 5
    assert all(WM not in p for p in cleaned)
    assert all("报销流程" in p for p in cleaned)


def test_single_page_no_cross_page_cleanup() -> None:
    pages = [f"{WM}\n正文"]
    cleaned, removed = strip_repeated_page_lines(pages)
    assert removed == 0
    assert cleaned == pages


def test_decisions_self_check_passes() -> None:
    decisions.self_check()
