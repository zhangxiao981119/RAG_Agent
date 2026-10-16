"""视觉抽取服务 —— 图片文字转录（M7 WU-M-06 / INV-M-04 / INV-M-15）。

职责：把一张图片（聊天带图提问、或引用对象存储中的图片）经多模态视觉链路
转录为文本，产出既有 `ParsedBlock` 契约，供上层（聊天 / 解析）复用。

★ 复用 LLMService，MUST NOT 引入第二套 LLM 客户端（DoD）：
  视觉补的是"消息体形态"（多模态 content 数组），不是"另一条调用链"；
  另起客户端会让超时 / 指标 / 鉴权口径在两处分叉。
★ 异常直抛（INV-M-04）：视觉不可用 / 图中无可读内容一律抛错，
  **MUST NOT** 退回"只看文本、凭模型先验作答" —— 那会绕过 grounding 产生
  无引用的幻觉答案。调用方（WU-M-07）负责把异常映射为 400 / SSE refused。
★ 提示词走注册表（INV-M-15）：`resolve_prompt` 拼出锁定区 + 可编辑区，
  锁定区含「不得臆造 / 不得改写原文 / 仅依据给定内容」三条约束。
"""
from __future__ import annotations

import base64
import logging
import mimetypes

from app.config.prompts import NO_ANSWER_TEXT
from app.schemas.chat import ImageRef
from app.services.llm import ChatMessage, LLMService, get_llm_service
from app.services.parse import ParsedBlock
from app.services.prompt_registry import resolve_prompt
from app.services.storage import get_storage

logger = logging.getLogger(__name__)


class VisionError(Exception):
    """视觉抽取失败。调用方据此映射为 400 / SSE refused（INV-M-04）。"""


class VisionNoContentError(VisionError):
    """模型判定图中无可读内容（纯图无字 / 完全不可辨认）。"""


async def _resolve_data_url(image_ref: ImageRef) -> str:
    """把图片引用归一成 OpenAI 兼容的 data URL（`data:<mime>;base64,...`）。

    · data_url：前端已带内联 data URL，原样透传
    · storage_key：从既有对象存储取字节，按 key 的扩展名补 MIME 后内联
      （扩展名是**显式信号**，符合"不做字节嗅探"的口径）
    ★ 对象不存在 / 存储不可用等错误直接向上抛（INV-M-04），不在此处吞掉。
    """
    if image_ref.data_url:
        return image_ref.data_url
    storage = await get_storage()
    key = image_ref.storage_key or ""
    data = await storage.get_object(key)
    mime = mimetypes.guess_type(key)[0] or "application/octet-stream"
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _is_no_readable_content(text: str) -> bool:
    """判定模型是否回复了「无可读内容」哨兵。

    ★ 复刻 generate 层的拒答识别口径（去空白 + 去中英文引号后精确比对），
      保证同一句 NO_ANSWER_TEXT 在全链路只有一种识别方式。
    """
    return text.strip().strip('"').strip("“").strip("”") == NO_ANSWER_TEXT


async def extract_text_from_image(image_ref: ImageRef) -> ParsedBlock:
    """把图片转录为文本块；无可读内容或链路失败一律抛错（INV-M-04）。

    ★ 不写库、不落 chunk —— 本函数只做"图 → 文本"，是否入库由调用方决定
      （聊天提问 MUST NOT 落库，见 INV-M-03 / WU-M-07）。
    """
    data_url = await _resolve_data_url(image_ref)
    template = await resolve_prompt("vision_extract")
    messages: list[ChatMessage] = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": template},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
    ]
    llm: LLMService = get_llm_service()
    raw = await llm.chat(messages, temperature=0.0)
    text = raw.strip()
    if not text or _is_no_readable_content(text):
        logger.info("视觉抽取：模型判定图中无可读内容")
        raise VisionNoContentError("图中未识别到可读内容")
    return ParsedBlock(text=text)
