"""多模态视觉能力包（M7）—— 对外只暴露视觉抽取入口。

跨模块调用 MUST 走 `extract_text_from_image`，MUST NOT 自行拼多模态消息体
或另起 LLM 客户端（那会让超时 / 指标 / 鉴权口径在两处分叉）。
"""
from __future__ import annotations

from app.services.multimodal.vision import (
    VisionError,
    VisionNoContentError,
    extract_text_from_image,
)

__all__ = [
    "VisionError",
    "VisionNoContentError",
    "extract_text_from_image",
]
