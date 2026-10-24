"""多模态路由 —— 全项目唯一的「形态判定点」（M7 / INV-M-01）。

职责：把显式信号（扩展名 / 请求体字段 / 源类型）映射为 `Modality`，
供解析分派、聊天带图、同步分支各自取用。

★ 只看显式信号，MUST NOT 做内容嗅探 / IO / 调模型（INV-M-01）：
  一旦允许"读字节猜类型"，扫描件会被判成文本层 PDF，抽出空文本后
  **静默入库**，直到检索期才暴露 —— 那时错误向量已经污染了检索结果。
★ 本模块是全仓唯一的形态判定点：其它文件 MUST NOT 再出现
  `if ext in (...)` 之类的形态分支，否则口径会在两处分叉。

与 settings.allowed_ext 的关系：后者是**上传白名单**（部署可变项，
由 M-09 扩展以纳入图片扩展名）；本模块是**形态归类**（冻结口径，
含 web 用的 html 扩展名 —— 该扩展名不参与上传白名单）。两者语义不同，
真源各自独立，MUST NOT 互相复制。
"""
from __future__ import annotations

from enum import Enum
from typing import Final

from app.infra import metrics


class Modality(str, Enum):
    """知识 / 提问的形态。禁止在各层散落裸字符串（WU-M-02）。"""

    TEXT = "text"    # 文本类知识：pdf(文本层) / md / txt / docx / pptx / xlsx / xls / html
    IMAGE = "image"  # 图片类：图片文件、扫描件 PDF、聊天带图提问
    WEB = "web"      # 网页源：内部网址抓取到的 html 页面


class UnknownModalityError(ValueError):
    """给定信号无法判定形态时抛出。

    ★ 显式报错而非兜底默认成 TEXT（总规范 §0.5：MUST NOT 发明默认值）——
      把未知输入塞进文本链路会产出一批空 chunk 静默入库。
    """


# 图片扩展名：命中即 IMAGE（M-09 会把它们并入 settings.allowed_ext）
IMAGE_EXTS: Final[frozenset[str]] = frozenset(
    {"png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff"}
)
# 文本类扩展名：含 html/htm —— 网页抓取落盘后的形态（不参与上传白名单）
TEXT_EXTS: Final[frozenset[str]] = frozenset(
    {"pdf", "md", "txt", "docx", "pptx", "xlsx", "xls", "html", "htm"}
)

# 形态 → 处理链路标识，供后续 WU 选择具体处理器：
#   text  → 既有 parse_bytes 链路
#   image → 视觉抽取链路（M-06）
#   web   → html 清洗链路（M-13）
_DISPATCH: Final[dict[Modality, str]] = {
    Modality.TEXT: "text",
    Modality.IMAGE: "image",
    Modality.WEB: "html",
}


def _decide(
    *, ext: str | None, has_images: bool, source_type: str | None
) -> Modality:
    """纯判定逻辑（无指标副作用），供 detect 调用与单测直接覆盖。

    判定优先级（信号互斥，顺序仅用于确定性）：
      1. source_type == "web" → WEB   （网页源，页面为 html）
      2. has_images is True   → IMAGE （聊天带图提问）
      3. ext ∈ IMAGE_EXTS     → IMAGE
      4. ext ∈ TEXT_EXTS      → TEXT
      5. 其余                  → 抛 UnknownModalityError
    """
    if source_type == "web":
        return Modality.WEB
    if has_images:
        return Modality.IMAGE
    key = (ext or "").lower().lstrip(".")
    if key in IMAGE_EXTS:
        return Modality.IMAGE
    if key in TEXT_EXTS:
        return Modality.TEXT
    raise UnknownModalityError(
        f"无法判定形态：ext={ext!r} has_images={has_images} source_type={source_type!r}"
    )


def detect(
    *,
    ext: str | None = None,
    has_images: bool = False,
    source_type: str | None = None,
) -> Modality:
    """按显式信号判定形态；未知输入抛 `UnknownModalityError`。

    每次判定记指标 `rag.router.decision{modality=...}`（INV-M-01 强制检查点）。
    判定失败时标签取 `unknown`，便于观测"有多少输入没接上任何链路"。
    """
    try:
        modality = _decide(ext=ext, has_images=has_images, source_type=source_type)
    except UnknownModalityError:
        metrics.incr("rag.router.decision", modality="unknown")
        raise
    metrics.incr("rag.router.decision", modality=modality.value)
    return modality


def dispatch(modality: Modality) -> str:
    """形态 → 处理链路标识。

    未知形态抛错而非兜底 —— 兜底会让"尚未接入的链路"静默走文本链路。
    """
    try:
        return _DISPATCH[modality]
    except KeyError:
        raise UnknownModalityError(f"未登记的形态: {modality!r}") from None
