"""图片解析器 —— 图片文件经视觉 LLM 转录后入库（M7 WU-M-09 / INV-M-02 / INV-M-04 / B'）。

职责：把一张图片文件（对象存储中的字节）经视觉 LLM（多模态链路）转录为
文本，产出既有 ParsedBlock 契约，走既有 chunk → embedding 路径写入
`chunks.embedding`。

★ 与文档内嵌图同一条视觉链（app.services.multimodal.vision）：图片文件
  直传不再走独立的 OCR 服务接口，而是与 docx/pdf 内嵌图共用同一个
  「图 → ParsedBlock」出口 —— 表格截图因此拿到 is_table / table_header，
  与文本表格走完全相同的整表切片与回捞路径（INV-M-02 单一知识空间）。
★ 不新增向量列 / 第二向量空间（INV-M-02）：图片知识文本化后与文本知识
  同构，embedding 维度仍是 1024，RRF / rerank / 阈值口径全部不变。
★ fail-closed（M7 D6 / B'）：`ocr_enabled=false` 时直接 ParseError，不做任何
  降级 —— 上传层的 ALLOWED_EXT 是第一道门，这里是解析层最后一道，两层任一
  命中都给出明确错误而非猜测。
★ 异常映射边界（INV-M-04）：vision 的 VisionError 直抛语义不变，本模块是
  "解析层"边界，统一改型为 ParseError（保留原因），**不是**吞掉后降级 ——
  无内容 / 不可用仍然失败，job 进 last_error。
"""
from __future__ import annotations

import base64
import dataclasses
import mimetypes

from app.config.settings import get_settings
from app.services.parse.base import ParsedBlock, ParseError

# 图片扩展名 —— 显式信号（INV-M-01：按扩展名分派，不做字节嗅探）
IMAGE_EXTS: tuple[str, ...] = ("png", "jpg", "jpeg", "gif", "webp", "bmp")


async def parse_image(data: bytes, ext: str) -> list[ParsedBlock]:
    """把图片字节经视觉 LLM 转录为单个块（表格图带 is_table / table_header）。

    ★ 延迟导入 vision / ImageRef：vision.py 顶部 `from app.services.parse import
      ParsedBlock` 会触发包初始化；base.py 又在包初始化中途 import 本模块，
      顶部互引会成环。与 embedded_images 的既有手法一致：数据类先定义，
      parser 的重导入放使用处。
    """
    if not get_settings().ocr_enabled:
        raise ParseError(
            "图片解析未启用：OCR_ENABLED=false（M7 D6）。"
            "如需支持图片入库，置 OCR_ENABLED=true 并配置视觉模型（LLM_BASE_URL / LLM_MODEL）。"
        )

    # MIME 由扩展名显式映射（INV-M-01：显式信号，不做字节嗅探）
    mime = mimetypes.guess_type(f"img.{ext}")[0] or "application/octet-stream"

    # 质量门禁：先检测（必要时去噪/去污/纠偏）再送视觉模型。
    # verdict=reject_to_manual → ParseError（显式转人工，不静默入库错字）。
    from app.services.parse.image_quality import prepare_for_ocr

    original = data
    data, quality = prepare_for_ocr(data, source="image")
    if data is not original:
        # 增强输出统一为 PNG，MIME 必须跟着改，否则模型侧按原 MIME 解码会失败
        mime = "image/png"

    from app.schemas.chat import ImageRef
    from app.services.multimodal import vision

    data_url = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    try:
        block = await vision.extract_text_from_image(ImageRef(data_url=data_url))
    except vision.VisionNoContentError as exc:
        # 图片**文件**（不是文档内嵌示意图）转录不出内容 = 这份文件没有可用知识
        raise ParseError(f"图片未识别到可读内容（视觉转录无输出）：{exc}") from exc
    except vision.VisionError as exc:
        # 边界改型：解析层契约统一 ParseError，原因原样保留（INV-M-04 不降级）
        raise ParseError(f"视觉转录失败：{exc}") from exc

    # 文本层水印清理：把「内部资料 严禁外传」这类行剔掉，别让它跟着 chunk
    # 进 embedding（水印的伤害在文本，不在像素 —— 见 image_quality 模块说明）。
    # ★ 表格块不做清理：清理依据是「短行 / 重复行」，而 TSV 的数据行天然短且
    #   可能重复，删除等于是把数据行当水印删掉（结构还在，知识没了）。
    if not block.is_table:
        from app.services.parse.image_quality import strip_watermark_text

        cleaned, _cleanup = strip_watermark_text(
            block.text,
            watermark_detected=bool(quality and quality.watermark_detected),
        )
        if cleaned != block.text:
            block = dataclasses.replace(block, text=cleaned)

    return [block]
