"""图片解析器 —— 视觉链路文本化后入库（M7 WU-M-09 / INV-M-02 / INV-M-04）。

职责：把一张图片文件（对象存储中的字节）经视觉链路转录为文本，
产出既有 ParsedBlock 契约，走既有 chunk → embedding 路径写入 `chunks.embedding`。

★ 不新增向量列 / 第二向量空间（INV-M-02）：图片知识文本化后与文本知识
  同构，embedding 维度仍是 1024，RRF / rerank / 阈值口径全部不变。
★ fail-closed（M7 D6）：`settings.ocr_enabled=false` 时直接 ParseError，
  不做任何降级 —— 上传层的 ALLOWED_EXT 是第一道门，这里是解析层最后一道，
  两层任一命中都给出明确错误而非猜测。
★ 异常映射边界（INV-M-04）：vision 的 Vision*Error 直抛语义不变，
  本模块是"解析层"边界，统一按 worker 契约改型为 ParseError（保留原因），
  **不是**吞掉后降级 —— 无内容 / 不可用仍然失败，job 进 last_error。
"""
from __future__ import annotations

import base64
import mimetypes

from app.config.settings import get_settings
from app.services.parse.base import ParsedBlock, ParseError

# 图片扩展名 —— 显式信号（INV-M-01：按扩展名分派，不做字节嗅探）
IMAGE_EXTS: tuple[str, ...] = ("png", "jpg", "jpeg", "gif", "webp", "bmp")


async def parse_image(data: bytes, ext: str) -> list[ParsedBlock]:
    """把图片字节经视觉链路转录为单个文本块。

    ★ 延迟导入 vision：vision.py 顶部 `from app.services.parse import ParsedBlock`
      会触发包初始化；base.py 又在包初始化中途 import 本模块，顶部互引会成环。
      与 base.py 的既有模式一致：数据类先定义，parser 的重导入放使用处。
    """
    # 局部导入避免循环依赖（见上）
    from app.services.multimodal.vision import (
        VisionError,
        VisionNoContentError,
        extract_text_from_image,
    )
    from app.schemas.chat import ImageRef

    if not get_settings().ocr_enabled:
        raise ParseError(
            "图片解析未启用：OCR_ENABLED=false（M7 D6）。"
            "如需支持图片入库，置 OCR_ENABLED=true 并把图片扩展名加入 ALLOWED_EXT。"
        )

    mime = mimetypes.guess_type(f"img.{ext}")[0] or "application/octet-stream"
    encoded = base64.b64encode(data).decode("ascii")
    image_ref = ImageRef(data_url=f"data:{mime};base64,{encoded}")

    try:
        block = await extract_text_from_image(image_ref)
    except (VisionNoContentError, VisionError) as exc:
        # 边界改型：解析层契约统一 ParseError，原因原样保留（INV-M-04 不降级）
        raise ParseError(f"视觉抽取失败：{exc}") from exc

    return [block]
