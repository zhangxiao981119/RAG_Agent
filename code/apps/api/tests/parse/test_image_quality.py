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
    """稀疏平铺水印：识别得到，且不因此触发像素级清洗。

    ★ 条纹间距必须显著大于分块高度（块高 = 页高/6）：水印是**条带**，块与块
      之间深浅必须有差异。等距密排（间距≈块高）会被块间离散度判成「均匀网纹」，
      那是摩尔纹的特征，不是水印 —— 见 test_uniform_texture_is_not_watermark。
    """
    img = _text_page()
    draw = ImageDraw.Draw(img)
    for y in range(15, img.size[1], 260):
        draw.rectangle([20, y, img.size[0] - 20, y + 16], fill=220)
    report = analyze(_png(img))
    assert report.watermark_detected is True, report.reasons
    assert report.watermark_coverage >= decisions.IMAGE_WATERMARK_MIN_BLOCK_COVERAGE
    assert any("水印" in r for r in report.reasons)


def test_uniform_texture_is_not_watermark() -> None:
    """整页均匀浅灰纹理（摩尔纹 / 网纹）：不许报成水印，只提示重扫。"""
    img = _text_page()
    draw = ImageDraw.Draw(img)
    rnd = random.Random(5)
    w, h = img.size
    for _ in range(int(w * h * 0.2)):  # 20% 均匀散点，无条带
        draw.point((rnd.randrange(w), rnd.randrange(h)), fill=220)
    report = analyze(_png(img))
    assert report.watermark_detected is False, report.reasons
    assert any("网纹" in r or "摩尔纹" in r for r in report.reasons), report.reasons


def test_gray_background_triggers_enhance() -> None:
    """灰底复印件：既不判水印也不能放过 —— 必须走增强（拉伸 + 二值化）。"""
    page = _text_page()
    gray_bg = Image.new("L", page.size, 212)
    ink = page.point(lambda p: 45 if p < 128 else 212)
    report = analyze(_png(Image.composite(ink, gray_bg, page.point(lambda p: 255 if p < 128 else 0))))
    assert report.verdict == VERDICT_ENHANCE, report.reasons
    assert report.watermark_detected is False
    assert any("底色偏灰" in r for r in report.reasons)


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


def test_strip_long_watermark_line_without_image_evidence() -> None:
    """真实水印行带公司名与编号（归一化后 40+ 字），图像侧漏检时也必须删掉。

    ★ 这是实测暴露的核心缺陷：行长上限 30 会把「西安博信云创科技有限公司
      内部资料 严禁外传 编号 BXYC-2026-0917」当正文保留，同时短语规则要求
      图像侧双证据 —— 两条一叠加，水印行 100% 进入 chunk。
    """
    line = "西安博信云创科技有限公司 内部资料 严禁外传 编号 BXYC-2026-0917"
    text = "\n".join([line, BODY, "差旅标准按职级执行，住宿上限为每晚六百元。"])
    cleaned, report = strip_watermark_text(text, watermark_detected=False)
    assert "严禁外传" not in cleaned, cleaned
    assert BODY in cleaned
    assert report.removed_lines == 1


def test_strip_keeps_short_secret_heading() -> None:
    """「机密」这类 2 字小节标题不是水印 —— 行长下限必须挡住它。"""
    text = "\n".join(["机密", BODY])
    cleaned, report = strip_watermark_text(text, watermark_detected=False)
    assert cleaned == text
    assert report.applied is False


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


# ── 强特征水印（2026-10-18 端到端实测：12_combo 的 3 处编号进了 chunk）──


def test_strip_watermark_with_trailing_period() -> None:
    """OCR 把水印末尾的噪点读成句号 → 仍须删除。

    实际入库原文就是这样（12_combo，平铺水印 + 噪点 + 2° 倾斜）：
      `西安博信云创新科技有限公司内部资料：严禁外传编号 BXYC-2026-0917。`
    旧逻辑被「不含句末标点」挡住，整行进 chunk，编号进了 embedding。
    """
    wm = "西安博信云创新科技有限公司内部资料：严禁外传编号 BXYC-2026-0917。"
    # 必须是多行文本：单行会被「删除比例 > 90%」这条兜底判成「把正文删光了」而回退
    text = "\n".join([wm, BODY, "差旅标准按职级执行。"])
    cleaned, report = strip_watermark_text(text, watermark_detected=False)
    assert wm not in cleaned
    assert "BXYC" not in cleaned
    assert report.removed_lines == 1
    assert report.reverted is False


def test_strip_heavily_corrupted_watermark_line() -> None:
    """被倾斜 + 噪点毁掉的水印行，靠「公司 + 公文编号」双线索仍能识别。"""
    wm = "市容评估三合的其他非信入公司，供首次料，由次料、证等编号 BXYC-2026-0917"
    text = "\n".join([wm, BODY, "差旅标准按职级执行。"])
    cleaned, _ = strip_watermark_text(text, watermark_detected=False)
    assert "BXYC" not in cleaned


def test_keep_body_sentence_citing_doc_number() -> None:
    """正文引用文号 MUST NOT 被当成水印删掉 —— 这是强特征规则的主要误伤面。"""
    line = "依据 BXYC-2026-0917 号文，本办法自发布之日起施行。"
    text = "\n".join([BODY, line])
    cleaned, report = strip_watermark_text(text, watermark_detected=False)
    assert line in cleaned
    assert report.applied is False


def test_keep_overlong_line_with_org_and_doc_number() -> None:
    """超过强特征行长上限（80）一律保留 —— 长正文里同时出现「公司」「编号」很常见。"""
    line = (
        "本公司与各子公司之间的往来编号 BXYC-2026-0917 所对应的全部归档材料，"
        "须由财务部统一收取并按年度装订成册后移交档案室保存，不得私自留存。"
    )
    text = "\n".join([BODY, line])
    cleaned, _ = strip_watermark_text(text, watermark_detected=False)
    assert line in cleaned


def test_strip_reverts_when_mass_deleting_non_phrase_lines() -> None:
    """表格页的大量重复短单元格：无密级短语却要删掉 39% → 判误伤，回退原文。

    ★ 回退判据不是「比例超限」而是「比例超限 **且被删行不含密级短语**」。
      平铺水印能占全文 40%，按纯比例判会把该删的水印退回去 —— 见下一个用例。
    """
    text = "\n".join(["是"] * 20 + ["否"] * 5 + [BODY])
    cleaned, report = strip_watermark_text(text, watermark_detected=False)
    assert cleaned == text
    assert report.reverted is True
    assert report.applied is False


def test_tiled_watermark_high_ratio_still_cleaned() -> None:
    """平铺水印：6 行全部命中密级短语，占全文 40% 也必须删，不许回退。

    这是实测暴露的缺陷：合成画像真水印页 OCR 出 6 行水印（含多种错字变体），
    占 39.8%，被旧的纯比例规则整页回退，水印 100% 留在 chunk 里。
    """
    lines = [
        f"西安博信云创科技有限公司 内部资料 严禁外传 编号 BXYC-2026-{i:04d}"
        for i in range(6)
    ] + [BODY, "差旅标准按职级执行，住宿上限为每晚六百元。"]
    cleaned, report = strip_watermark_text("\n".join(lines), watermark_detected=True)
    assert "严禁外传" not in cleaned
    assert BODY in cleaned
    assert report.reverted is False
    assert report.removed_lines == 6


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
