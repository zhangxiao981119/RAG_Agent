"""多模态路由包（M7）—— 对外只暴露形态判定入口。

跨模块调用 MUST 走 `detect()` / `dispatch()`，MUST NOT 直接读
`IMAGE_EXTS` / `TEXT_EXTS` 自行判定（那会绕开唯一判定点）。
"""
from __future__ import annotations

from app.services.router.modality import (
    IMAGE_EXTS,
    TEXT_EXTS,
    Modality,
    UnknownModalityError,
    detect,
    dispatch,
)

__all__ = [
    "IMAGE_EXTS",
    "TEXT_EXTS",
    "Modality",
    "UnknownModalityError",
    "detect",
    "dispatch",
]
