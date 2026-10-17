"""本地 OCR 客户端 —— 扫描件 / 图片入库转录（B'，数据不出域）。

职责：把图片字节送本地 apps/ocr 服务（GOT-OCR2.0）转录为文本，
产出既有 ParsedBlock 契约，供 pdf_parser / image_parser 复用。

★ 单引擎 fail-closed：OCR 服务不可达 / 返回错误 / 空结果一律抛 OcrError，
  由解析层改型为 ParseError —— 任务走 arq 重试 → 死信 → 恢复后重跑。
  **MUST NOT** 在此做第二引擎兜底或纯文本降级（会掩盖故障 + 污染数据口径）。
★ 低置信仅告警（OCR_LOW_CONF）：低于阈值打 warning 进审计日志，仍正常入库；
  数据不出域约束下禁止升级外部 LLM 重转录。
★ 与视觉链路（multimodal/vision.py）的边界：聊天带图走 LLM 视觉转录
  （理解型提问），入库解析走本地 OCR（逐字转录保真）—— 两条链路独立。
"""
from __future__ import annotations

import logging

import httpx

from app.config.decisions import OCR_LOW_CONF, OCR_TIMEOUT_S
from app.config.settings import get_settings
from app.services.parse import ParsedBlock

logger = logging.getLogger(__name__)


class OcrError(Exception):
    """OCR 链路失败（服务不可达 / 返回错误 / 未配置）。解析层据此改型 ParseError。"""


class OcrNoContentError(OcrError):
    """OCR 判定图中无可读内容（空白页 / 纯图形无文字）。"""


async def ocr_image(data: bytes, mime: str = "image/png") -> ParsedBlock:
    """把图片字节送本地 OCR 服务转录为文本块。

    ★ fail-closed：OCR_BASE_URL 为空（开关已开但服务未部署）直接抛错，
      不尝试任何替代路径 —— 消除 fallback 触发场景，而不是加兜底。
    ★ 返回前完成低置信审计标记：confidence < OCR_LOW_CONF → warning 日志，
      文本仍正常返回（不失败、不重转录）。
    """
    settings = get_settings()
    if not settings.ocr_base_url:
        raise OcrError(
            "OCR_BASE_URL 未配置：请部署 apps/ocr 服务并注入环境变量（http://ocr:8000）"
        )
    base = settings.ocr_base_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=OCR_TIMEOUT_S) as client:
            resp = await client.post(
                f"{base}/ocr",
                content=data,
                headers={"Content-Type": mime},
            )
            resp.raise_for_status()
            payload = resp.json()
    except httpx.HTTPStatusError as exc:
        detail = (exc.response.text or "")[:200]
        raise OcrError(
            f"OCR 服务返回错误：HTTP {exc.response.status_code} {detail}"
        ) from exc
    except httpx.HTTPError as exc:
        raise OcrError(f"OCR 服务不可达：{exc}") from exc

    text = str(payload.get("text") or "").strip()
    confidence = float(payload.get("confidence") or 0.0)
    if not text:
        logger.info("OCR：服务判定图中无可读内容")
        raise OcrNoContentError("图中未识别到可读内容")
    if confidence < OCR_LOW_CONF:
        logger.warning(
            "OCR 单页置信度 %.3f 低于阈值 %.2f（已入库，仅审计标记，不重转录）",
            confidence,
            OCR_LOW_CONF,
        )
    return ParsedBlock(text=text)
