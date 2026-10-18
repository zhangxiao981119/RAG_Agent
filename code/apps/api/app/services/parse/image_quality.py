"""文档图像质量门禁 —— 去噪 / 去污 + 质量识别（水印只标记、不擦除）。

职责：OCR 之前对「图片文件」与「扫描件渲染页」做一次可读性评估，产出
`ImageQualityReport`，按评分给出三档处置 `pass` / `enhance` / `reject_to_manual`。
增强只做**无损**预处理：灰度 → 中值去噪 → 纠偏 → 局部自适应二值化。

★ 为什么不擦水印（本模块的边界，MUST NOT 被后续「优化」掉）：
  ① 水印是版权与密级标识，擦除等于销毁溯源证据；
  ② RAG 的承诺是「答案可回溯原文」—— 擦除后入库文本与用户点开的原图
     不一致，审计直接对不上；
  ③ 生成式 inpaint 会连压在字上的笔画一起重建，OCR 拿到的是模型猜的字，
     且这种错误不可检测。
  因此水印只输出 `watermark_detected` + 覆盖率，由上游决定转人工或降权，
  本模块**不做任何像素级擦除**，也不引入 inpaint 模型。

★ 为什么只用 Pillow（零新增依赖、零新增容器）：
  判定全部是可解释的启发式，靠 Pillow 的 C 实现完成，单页约 200ms
  （150dpi A4 实测 190–215ms，含 17 次小图旋转）：
  · 噪点密度  = 中值滤波残差 → `ImageChops.difference` 的直方图占比
  · 污渍占比  = 暗区二值化后做 5×5 腐蚀，剩下的大块（细笔画被腐蚀掉）
  · 倾斜角    = 缩略图上穷举旋转角，取**行投影方差**最大者（文字行对齐时最尖锐）
  · 水印      = 浅灰窄峰（200–245）占比 + 分块覆盖率（局部、非整体）
  · 红章      = R 显著高于 G/B 的像素占比（`ImageChops` 链，全 C 路径）
  · 高频能量  = FIND_EDGES 均值，仅作「是否重扫」提示，不参与判定
  上模型（LaMa / 版面分析）会把离线整包撑大数 GB，收益为负。

★ 失败策略（与解析层 fail-closed 的区别，别混）：
  质量门禁是**观测与优化**，不是正确性前提 —— 检测本身异常时回退原图并打
  warning，MUST NOT 因 CV 代码出错而让所有文档解析失败。但
  `reject_to_manual` 是**结论**，必须显式抛 ParseError（带原因），
  否则「转人工」在链路里无从体现，等于静默降级。
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field

from PIL import Image, ImageChops, ImageFilter, ImageOps

from app.config import decisions
from app.services.parse.base import ParseError

logger = logging.getLogger(__name__)

# 倾斜角搜索：缩略图宽度（越小越快，300 宽对 1° 精度足够）
_SKEW_THUMB_WIDTH: int = 320
_SKEW_COARSE_STEP: float = 1.0
_SKEW_FINE_STEP: float = 0.25
_SKEW_MAX_ABS: float = 4.0
# 水印分块：6×6 网格，块内浅灰占比 >= IMAGE_WATERMARK_BLOCK_LIGHT_RATIO 记命中块
_WM_GRID: int = 6
# 块间离散度的分母下限：避免 min=0 时除零，同时保留「空块 = 强条带证据」的语义
_SHARE_EPS: float = 1e-4
# 暗区（污渍候选）二值化阈值：低于此灰度算暗
# ★ 90 太低：污渍/墨块经高斯模糊后边缘灰度升到 100–150，整块算出 0%，
#   三个明显黑斑的合成页 stain_ratio = 0.0000（实测）。128 是「白底 255 与
#   正文 0」的中点，正文笔画 3px 在 5×5 腐蚀下仍会被抹掉，不会误判。
_DARK_THRESHOLD: int = 128

VERDICT_PASS = "pass"
VERDICT_ENHANCE = "enhance"
VERDICT_REJECT = "reject_to_manual"


@dataclass(frozen=True)
class ImageQualityReport:
    """单张图像的质量评估结果。全部字段可复算，不含任何模型推断。"""

    verdict: str
    width: int
    height: int
    dpi: int | None
    skew_deg: float
    noise_ratio: float
    stain_ratio: float
    watermark_detected: bool
    watermark_coverage: float
    light_gray_ratio: float
    red_stamp_ratio: float
    high_freq_score: float
    bg_level: int = 0
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def summary(self) -> str:
        return (
            f"{self.width}x{self.height}@{self.dpi or '-'}dpi "
            f"skew={self.skew_deg:.2f} noise={self.noise_ratio:.3f} "
            f"stain={self.stain_ratio:.3f} wm={self.watermark_detected}"
            f"({self.watermark_coverage:.2f}) stamp={self.red_stamp_ratio:.4f}"
        )


# ── 单项检测（全部 Pillow C 路径）─────────────────────────────


def _noise_ratio(gray: Image.Image, total: int) -> float:
    """噪点密度：**孤立暗点**占全页的比例（椒盐 / 扫描尘点）。

    ★ 不用「中值残差」直接计数：文字笔画边缘的灰度跳变同样会被中值改写，
      实测一页干净的正文会算出 5% 的「噪点」，把所有正常页都推向增强。
      改用「暗且 3×3 邻域内暗点 ≤1 个」—— 笔画边缘至少 2 个暗邻居，天然被排除。
    """
    dark = gray.point(lambda p: 255 if p < 100 else 0)  # 暗区 = 255
    # BoxBlur(1) 即 3×3 均值：每个像素得到「自身 + 8 邻域」的暗点密度（0–255）
    density = dark.filter(ImageFilter.BoxBlur(1))
    # 密度 <= 30 表示 3×3 内最多 1 个暗点（自身）—— 孤立点
    lonely = ImageChops.multiply(dark, density.point(lambda p: 255 if p <= 30 else 0))
    return lonely.histogram()[255] / total if total else 0.0


def _stain_ratio(gray: Image.Image, total: int) -> float:
    """厚暗区（污渍 / 墨块）占比：5×5 腐蚀后仍存活的暗区。

    ★ 会连带算进粗体标题、实心 logo、黑色表格填充 —— 所以阈值定得高（2%），
      只让成片污损触发增强，不让一个标题把整页判脏。
    """
    dark = gray.point(lambda p: 0 if p < _DARK_THRESHOLD else 255)
    eroded = dark.filter(ImageFilter.MaxFilter(5))
    return eroded.histogram()[0] / total if total else 0.0


def _row_projection_variance(img: Image.Image) -> float:
    """行投影方差：文字行对齐时行间白、行内黑，投影曲线最尖锐（方差最大）。"""
    w, h = img.size
    data = img.tobytes()
    sums = [sum(data[y * w:(y + 1) * w]) for y in range(h)]
    mean = sum(sums) / h if h else 0.0
    return sum((s - mean) ** 2 for s in sums) / h if h else 0.0


def _estimate_skew(gray: Image.Image, dark_ratio: float) -> float:
    """穷举旋转角求最优对齐角；返回**应施加的纠正角**（度）。

    先 1° 粗扫，再在最优值 ±1° 内以 0.25° 细扫。页面几乎没有文字时
    投影方差无意义，直接返回 0（避免给空白页编一个倾斜角）。
    """
    if dark_ratio < 0.005:
        return 0.0
    thumb = gray.copy()
    thumb.thumbnail((_SKEW_THUMB_WIDTH, _SKEW_THUMB_WIDTH))

    def best_in(angles: list[float]) -> tuple[float, float]:
        best_a, best_v = 0.0, -1.0
        for a in angles:
            rotated = thumb.rotate(
                a, resample=Image.Resampling.BILINEAR, fillcolor=255
            )
            v = _row_projection_variance(rotated)
            if v > best_v:
                best_a, best_v = a, v
        return best_a, best_v

    coarse = [i * _SKEW_COARSE_STEP for i in range(
        int(-_SKEW_MAX_ABS / _SKEW_COARSE_STEP),
        int(_SKEW_MAX_ABS / _SKEW_COARSE_STEP) + 1,
    )]
    a0, _ = best_in(coarse)
    fine = [a0 + i * _SKEW_FINE_STEP for i in (-3, -2, -1, 1, 2, 3)]
    a1, _ = best_in(fine)
    return round(a1 if abs(a1) > abs(a0) else a0, 2)


def _watermark(
    gray: Image.Image, total: int
) -> tuple[bool, float, float, float]:
    """水印识别：浅灰窄峰占比 + 分块覆盖率 + 块间离散度。

    三个条件同时成立才算水印：
      ① 全局浅灰占比落在 IMAGE_WATERMARK_MIN/MAX_RATIO 之间（局部而非整页）
      ② 命中块覆盖率达到 IMAGE_WATERMARK_MIN_BLOCK_COVERAGE
      ③ 块间离散度（max/min）达到 IMAGE_WATERMARK_MIN_BLOCK_SPREAD

    ★ 只认「局部」浅灰：整页均匀偏灰是底色 / 对比度问题（走 enhance），
      不是水印 —— 所以给 upper 上限，超过就判成灰底而不是 watermark。
    ★ ③ 是为了排除「整页均匀网纹」：摩尔纹 / 灰底的块间几乎无差异（实测 1.08 /
      1.15），而水印是条带（实测 7.0 / 5.5）。没有这条会输出「水印覆盖 100%」。
    """
    lo, hi = decisions.IMAGE_WATERMARK_LIGHT_RANGE
    light = sum(gray.histogram()[lo:hi]) / total if total else 0.0

    w, h = gray.size
    bw, bh = max(1, w // _WM_GRID), max(1, h // _WM_GRID)
    blocks = 0
    shares: list[float] = []
    for by in range(_WM_GRID):
        for bx in range(_WM_GRID):
            box = (bx * bw, by * bh, min(w, (bx + 1) * bw), min(h, (by + 1) * bh))
            if box[2] <= box[0] or box[3] <= box[1]:
                continue
            area = (box[2] - box[0]) * (box[3] - box[1])
            if area <= 0:
                continue
            share = sum(gray.crop(box).histogram()[lo:hi]) / area
            shares.append(share)
            if share >= decisions.IMAGE_WATERMARK_BLOCK_LIGHT_RATIO:
                blocks += 1
    coverage = blocks / (_WM_GRID * _WM_GRID)
    # 离散度：最小块为 0（块内完全没有浅灰）时按「极大」处理 —— 那是最强的条带证据。
    # ★ 不能写成 `if min > 0 else 0.0`：稀疏水印的空块占比就是 0，那样会把
    #   最典型的水印判成「块间无差异」，正好和这条判据想干的事相反。
    spread = (
        max(shares) / max(min(shares), _SHARE_EPS)
        if shares and max(shares) > 0
        else 0.0
    )

    detected = (
        decisions.IMAGE_WATERMARK_MIN_RATIO
        <= light
        <= decisions.IMAGE_WATERMARK_MAX_RATIO
        and coverage >= decisions.IMAGE_WATERMARK_MIN_BLOCK_COVERAGE
        and spread >= decisions.IMAGE_WATERMARK_MIN_BLOCK_SPREAD
    )
    return detected, coverage, light, spread


def _bg_level(gray: Image.Image) -> int:
    """背景电平：灰度直方图的 80 分位（页面大部分是背景，但不是最亮的极值）。

    ★ 取极值会永远得到 255（页面常有白边）；取中位数会被大段深色表格拉低。
      80 分位在「干净页 255 / 灰底复印件 212 / 摩尔纹 251」三种样本上都稳定。
    """
    hist = gray.histogram()
    target = sum(hist) * 0.8
    cum = 0
    for v, c in enumerate(hist):
        cum += c
        if cum >= target:
            return v
    return 255


def _red_stamp_ratio(img: Image.Image, total: int) -> float:
    """红章占比：R 明显高于 G 与 B 的像素。压字的重章只标记、不处理。"""
    r, g, b = img.split()
    thr = 45

    def mask(a: Image.Image, c: Image.Image) -> Image.Image:
        return ImageChops.subtract(a, c).point(lambda p: 255 if p > thr else 0)

    both = ImageChops.multiply(mask(r, g), mask(r, b))
    return both.histogram()[255] / total if total else 0.0


def _high_freq_score(gray: Image.Image, total: int) -> float:
    """边缘能量均值（0–1）。仅作「疑似摩尔纹 / 需重扫」提示，不参与判定。"""
    hist = gray.filter(ImageFilter.FIND_EDGES).histogram()
    return sum(i * c for i, c in enumerate(hist)) / total / 255 if total else 0.0


# ── 主入口 ────────────────────────────────────────────────


def analyze(data: bytes, *, source: str = "image") -> ImageQualityReport:
    """评估一张图像。`source` 取 `image`（用户上传的图）或 `pdf_page`（渲染页）。

    ★ 尺寸下限只对 `image` 生效：PDF 渲染页的 DPI 由 `_SCAN_RENDER_DPI` 决定，
      拿像素数判定会把正常的 150dpi A4 页误杀。
    """
    with Image.open(io.BytesIO(data)) as raw:
        img = ImageOps.exif_transpose(raw).convert("RGB")
        dpi_raw = raw.info.get("dpi")
    dpi = int(round(dpi_raw[0])) if isinstance(dpi_raw, tuple) and dpi_raw else None

    w, h = img.size
    total = w * h
    gray = img.convert("L")

    noise = _noise_ratio(gray, total)
    stain = _stain_ratio(gray, total)
    dark_ratio = sum(gray.histogram()[:128]) / total if total else 0.0
    skew = _estimate_skew(gray, dark_ratio)
    wm_detected, wm_cov, light, wm_spread = _watermark(gray, total)
    stamp = _red_stamp_ratio(img, total)
    hf = _high_freq_score(gray, total)
    bg = _bg_level(gray)
    _bg_lo, _bg_hi = decisions.IMAGE_BG_GRAY_RANGE
    gray_bg = _bg_lo <= bg <= _bg_hi

    reasons: list[str] = []
    if source == "image" and min(w, h) < decisions.IMAGE_MIN_SIDE_PX:
        reasons.append(
            f"分辨率不足：短边 {min(w, h)}px < {decisions.IMAGE_MIN_SIDE_PX}px"
        )
    if noise > decisions.IMAGE_NOISE_REJECT_RATIO:
        reasons.append(
            f"噪点密度过高：{noise:.1%} > {decisions.IMAGE_NOISE_REJECT_RATIO:.0%}，"
            "OCR 结果不可信"
        )
    if abs(skew) > decisions.IMAGE_SKEW_REJECT_DEG:
        reasons.append(
            f"倾斜 {skew:.1f}° 超过 {decisions.IMAGE_SKEW_REJECT_DEG}°，纠偏会裁掉内容"
        )

    if reasons:
        verdict = VERDICT_REJECT
    elif (
        noise > decisions.IMAGE_NOISE_ENHANCE_RATIO
        or stain > decisions.IMAGE_STAIN_ENHANCE_RATIO
        or abs(skew) > decisions.IMAGE_SKEW_ENHANCE_DEG
        or gray_bg
    ):
        verdict = VERDICT_ENHANCE
        if noise > decisions.IMAGE_NOISE_ENHANCE_RATIO:
            reasons.append(f"噪点 {noise:.1%} 超阈值，去噪")
        if stain > decisions.IMAGE_STAIN_ENHANCE_RATIO:
            reasons.append(f"污渍/墨块 {stain:.2%} 超阈值，去污")
        if abs(skew) > decisions.IMAGE_SKEW_ENHANCE_DEG:
            reasons.append(f"倾斜 {skew:.1f}°，纠偏")
        if gray_bg:
            reasons.append(f"底色偏灰（背景电平 {bg}），拉伸对比度并二值化")
    else:
        verdict = VERDICT_PASS

    # 水印只标记，不改变处置（它不影响 OCR 读字，但会污染文本，交上游判断）
    if wm_detected:
        reasons.append(
            f"检测到水印（覆盖 {wm_cov:.0%}、块间离散 {wm_spread:.1f}×），"
            "按策略只标记不擦除"
        )
    elif light > 0.05 and not gray_bg:
        # 有浅灰纹理但不成条带 —— 网纹 / 摩尔纹，提示重扫，绝不判水印
        reasons.append(
            f"整页均匀浅灰纹理（{light:.0%}，块间离散 {wm_spread:.1f}×），"
            "疑似网纹或摩尔纹，建议重扫"
        )

    report = ImageQualityReport(
        verdict=verdict,
        width=w,
        height=h,
        dpi=dpi,
        skew_deg=skew,
        noise_ratio=round(noise, 4),
        stain_ratio=round(stain, 4),
        watermark_detected=wm_detected,
        watermark_coverage=round(wm_cov, 3),
        light_gray_ratio=round(light, 4),
        red_stamp_ratio=round(stamp, 4),
        high_freq_score=round(hf, 4),
        bg_level=bg,
        reasons=tuple(reasons),
    )
    return report


def enhance(data: bytes, report: ImageQualityReport) -> bytes:
    """无损增强：纠偏 → 灰度 → 中值去噪 → 对比度拉伸 → 局部自适应二值化。

    ★ 不做内容级修改（不擦水印、不修复笔画、不做生成式重建）—— 只做
      像素层面的清洗，保证「入库文本可回溯到原图」这一承诺成立。
    ★ 二值化用「局部均值 − 当前像素」比较（Niblack 的均值版，全 C 路径），
      对不均匀光照的扫描页比全局 Otsu 稳；浅色底纹会被压白，但正文保得住。
    """
    with Image.open(io.BytesIO(data)) as raw:
        img = ImageOps.exif_transpose(raw).convert("RGB")

    if abs(report.skew_deg) >= decisions.IMAGE_SKEW_ENHANCE_DEG:
        img = img.rotate(
            report.skew_deg,
            resample=Image.Resampling.BICUBIC,
            expand=True,
            fillcolor=(255, 255, 255),
        )

    gray = img.convert("L")
    if report.noise_ratio > decisions.IMAGE_NOISE_ENHANCE_RATIO:
        gray = gray.filter(
            ImageFilter.MedianFilter(decisions.IMAGE_ENHANCE_MEDIAN_SIZE)
        )
    gray = ImageOps.autocontrast(gray, cutoff=1)

    # 局部自适应二值化：比局部均值明显更暗的像素判为前景
    radius = max(3, min(gray.size) // 100)
    local_mean = gray.filter(ImageFilter.BoxBlur(radius))
    darker = ImageChops.subtract(local_mean, gray)
    binary = darker.point(lambda p: 0 if p > 12 else 255)

    out = io.BytesIO()
    binary.save(out, format="PNG", optimize=True)
    return out.getvalue()


def prepare_for_ocr(
    data: bytes, *, source: str = "image"
) -> tuple[bytes, ImageQualityReport | None]:
    """OCR 前统一门禁：分析 → 判定 → 必要时增强。返回 (送去 OCR 的字节, 报告)。

    · 门禁关闭（image_quality_gate_enabled=false）→ 原样返回、报告为 None
    · 检测异常 → 原样返回 + warning（观测失败不该让解析全挂）
    · verdict=reject_to_manual → 抛 ParseError（显式失败，转人工的唯一表达方式）
    · verdict=enhance 且增强开启 → 返回增强后的 PNG；增强失败 → 回退原图 + warning
    """
    from app.config.settings import get_settings

    settings = get_settings()
    if not settings.image_quality_gate_enabled:
        return data, None

    try:
        report = analyze(data, source=source)
    except Exception as exc:  # noqa: BLE001 —— 见模块 docstring「失败策略」
        logger.warning("图像质量检测失败，回退原图 OCR：%s", exc)
        return data, None

    logger.info("图像质量门禁 %s（%s）", report.summary, source)

    if report.verdict == VERDICT_REJECT:
        raise ParseError(
            "图像质量不足，已转人工处理：" + "；".join(report.reasons)
        )

    if report.verdict == VERDICT_ENHANCE and settings.image_enhance_enabled:
        try:
            return enhance(data, report), report
        except Exception as exc:  # noqa: BLE001
            logger.warning("图像增强失败，回退原图 OCR：%s", exc)
            return data, report

    return data, report


# ── 水印的「知识影响」处理：只清文本，不动像素 ─────────────────
#
# 水印真正的危害不在图像，在**文本**：每页顶部/底部那行「某某公司 内部资料
# 严禁外传」会被 OCR 成一行正文，跟着 chunk 一起切分、一起 embedding。
# 后果是检索时这类垃圾行命中率高、挤占 top-k，甚至被引用进答案 ——
# 用户看到的回答里夹着「严禁外传」，比看不清字难看得多。
#
# 所以处理分两层，都在文本层：
#   ① 密级短语行：整行只由「内部资料 / 机密 / Confidential…」构成 → 删
#   ② 重复短行：同一短行在一页里出现 ≥2 次、或跨页高频出现 → 删（平铺水印）
# ★ 只删「整行」不删「行内片段」：正文里的「公司商业秘密管理办法」删掉短语
#   会变成病句，损伤知识本身 —— 宁可留一行噪声，不可改一句正文。
# ★ 删得太多就放弃（removed_ratio 超上限）→ 说明命中的是正文，回退原文并告警。

_WATERMARK_PHRASES: tuple[str, ...] = (
    "内部资料",
    "严禁外传",
    "请勿外传",
    "不得外传",
    "仅供内部",
    "内部使用",
    "仅供内部使用",
    "受控文件",
    "注意保密",
    "机密",
    "绝密",
    "confidential",
    "internaluseonly",
    "forinternaluse",
    "donotdistribute",
)
# 句末标点：水印行几乎不带句读，带标点的短行更可能是真内容
_SENTENCE_END = "。！？；.!?;"


@dataclass(frozen=True)
class WatermarkTextCleanup:
    """文本层水印清理结果。用于审计：删了什么、删了多少、是否放弃。"""

    applied: bool
    removed_lines: int = 0
    removed_chars: int = 0
    ratio: float = 0.0
    reverted: bool = False
    reasons: tuple[str, ...] = field(default_factory=tuple)


def _norm_line(line: str) -> str:
    """归一化：去空白与常见分隔符，用于重复判定（不影响原行文本）。"""
    return "".join(ch for ch in line if not ch.isspace() and ch not in "|-—_·•*")


def _is_watermark_line(
    line: str, repeat_counts: dict[str, int], *, phrase_rule: bool
) -> bool:
    """单行是否判为水印行。

    三条硬约束同时成立才删，宁可漏删不可误删：
      ① 归一化后长度 <= IMAGE_WATERMARK_TEXT_MAX_LINE_LEN（60）
      ② 不含句末标点（水印不会写成一句话）
      ③ 命中密级短语（行长须 >= PHRASE_MIN_LEN）或在页内 / 页间高频重复

    ★ 密级短语**不再依赖图像侧证据**：图像侧水印检测本身会漏（斜向大字水印、
      页眉单行水印都测不到），若要求双证据，会出现「漏检 → 文本层不清理」的
      连环失效 —— 实测水印行就原样进了 chunk。改为「短语 + 短行 + 无标点」
      三条同时成立，误删风险由行长下限（6 字，保护「机密」这类小标题）兜住。
    ★ `phrase_rule=True`（图像侧也判到水印）时行长下限放宽到 4 字。
    """
    norm = _norm_line(line).lower()
    if not norm:
        return False
    if len(norm) > decisions.IMAGE_WATERMARK_TEXT_MAX_LINE_LEN:
        return False
    if any(ch in line for ch in _SENTENCE_END):
        return False
    min_len = (
        decisions.IMAGE_WATERMARK_PHRASE_MIN_LEN_STRONG
        if phrase_rule
        else decisions.IMAGE_WATERMARK_PHRASE_MIN_LEN
    )
    hit_phrase = len(norm) >= min_len and any(p in norm for p in _WATERMARK_PHRASES)
    repeated = repeat_counts.get(norm, 0) >= decisions.IMAGE_WATERMARK_TEXT_REPEAT
    return hit_phrase or repeated


def strip_watermark_text(
    text: str, *, watermark_detected: bool = True
) -> tuple[str, WatermarkTextCleanup]:
    """剔除 OCR 文本里的水印行（整行级），保护 chunk 与 embedding 不被污染。

    ★ `watermark_detected=False` 时仍会清理「页内高频重复短行」——
      平铺水印在图像层可能因覆盖率高被判成灰底，但文本层的重复特征更可靠。
    ★ 删除比例超过上限时**不必然回退**：只要被删的行都命中密级短语，说明就是水印页
      （实测平铺水印能占全文 40%），回退等于把水印留下；只有「没有任何一行含密级
      短语」的成批删除才判为误伤表格 / 正文，回退原文（`reverted=True`）。
    """
    lines = text.split("\n")
    counts: dict[str, int] = {}
    for line in lines:
        norm = _norm_line(line).lower()
        if norm and len(norm) <= decisions.IMAGE_WATERMARK_TEXT_MAX_LINE_LEN:
            counts[norm] = counts.get(norm, 0) + 1
    counts = {k: v for k, v in counts.items() if v > 1}

    kept: list[str] = []
    removed = 0
    removed_chars = 0
    phrase_hits = 0
    for line in lines:
        norm = _norm_line(line).lower()
        if not norm:  # 空行照留，保证段落结构不失真
            kept.append(line)
            continue
        if _is_watermark_line(line, counts, phrase_rule=watermark_detected):
            removed += 1
            removed_chars += len(norm)
            if any(p in norm for p in _WATERMARK_PHRASES):
                phrase_hits += 1
            continue
        kept.append(line)

    total_chars = sum(len(_norm_line(x)) for x in lines)
    ratio = removed_chars / total_chars if total_chars else 0.0

    if removed == 0:
        return text, WatermarkTextCleanup(applied=False)
    # ★ 只有「成批删除非水印行」才可能是误伤正文（命中整章密级词汇 / 表格重复单元格）。
    #   按纯比例判会两头都错：短文档里一条长水印行就占 38%，而平铺水印整页 6 行
    #   能占 40%（实测 39.8%）—— 比例高恰恰是水印页的正常形态。
    #   判据改为：删的全是水印特征行（命中密级短语）→ 不回退；
    #   删的没有一行含密级短语（纯靠「重复短行」规则）且比例超限 → 判定误伤，回退。
    #   另设 90% 硬顶：连正文都删光了说明判定彻底跑偏，宁可留脏也不交空页。
    if ratio > decisions.IMAGE_WATERMARK_TEXT_MAX_REMOVE_RATIO and (
        phrase_hits == 0 or ratio > 0.90
    ):
        logger.warning(
            "水印文本清理放弃：拟删除 %.1f%%（%d 行，其中含密级短语 %d 行）超过 %.0f%% 上限，"
            "判定命中正文 / 表格重复行，回退原文",
            ratio * 100,
            removed,
            phrase_hits,
            decisions.IMAGE_WATERMARK_TEXT_MAX_REMOVE_RATIO * 100,
        )
        return text, WatermarkTextCleanup(
            applied=False,
            removed_lines=removed,
            removed_chars=removed_chars,
            ratio=round(ratio, 4),
            reverted=True,
            reasons=("删除比例超上限，疑似误伤正文",),
        )

    cleaned = "\n".join(kept)
    logger.info(
        "水印文本清理：删除 %d 行 / %d 字（%.1f%%）", removed, removed_chars, ratio * 100
    )
    return cleaned, WatermarkTextCleanup(
        applied=True,
        removed_lines=removed,
        removed_chars=removed_chars,
        ratio=round(ratio, 4),
    )


def strip_repeated_page_lines(page_texts: list[str]) -> tuple[list[str], int]:
    """跨页重复短行清理（平铺水印 / 页眉页脚）。

    判定：同一归一化短行出现在 >= max(2, 30% 页数) 的页上，且长度 <= 上限。
    ★ 只在扫描件聚合期调用（`blocks_from_page_texts`）：那里才有全文档页序，
      单页视角看不到「这条每页都有」。
    """
    if len(page_texts) < 2:
        return page_texts, 0
    threshold = max(2, int(len(page_texts) * 0.3))
    page_hits: dict[str, int] = {}
    for page in page_texts:
        seen = {
            _norm_line(line).lower()
            for line in page.split("\n")
            if _norm_line(line)
            and len(_norm_line(line)) <= decisions.IMAGE_WATERMARK_TEXT_MAX_LINE_LEN
        }
        for norm in seen:
            page_hits[norm] = page_hits.get(norm, 0) + 1

    repeated = {n for n, c in page_hits.items() if c >= threshold}
    if not repeated:
        return page_texts, 0

    out: list[str] = []
    removed = 0
    for page in page_texts:
        kept_lines = []
        for line in page.split("\n"):
            norm = _norm_line(line).lower()
            if norm in repeated and not any(ch in line for ch in _SENTENCE_END):
                removed += 1
                continue
            kept_lines.append(line)
        out.append("\n".join(kept_lines))
    if removed:
        logger.info(
            "跨页重复行清理：删除 %d 行（%d 页，阈值 %d 页命中）",
            removed,
            len(page_texts),
            threshold,
        )
    return out, removed
