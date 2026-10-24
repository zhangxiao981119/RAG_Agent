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
★ 表格图归一为表格块：锁定区约定图片式表格用 `<TABLE>` 包裹 + 首行表头 +
  Tab 分列；本模块据此返回 `is_table=True` 的 ParsedBlock，让图片表格走
  与 docx 表格完全相同的切片路径（整表不切 / 行组切带表头 / 跨片回捞）。
"""
from __future__ import annotations

import base64
import logging
import mimetypes

from app.config.prompts import NO_ANSWER_TEXT, TABLE_CLOSE, TABLE_OPEN
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


def _expand_table(text: str) -> tuple[str, list[str] | None]:
    """解析 `<TABLE>` 标记，返回 (正文, 表头)；非表格图返回 (原文, None)。

    ★ 标记协议由 vision_extract 锁定区约束：图片式表格 MUST 整体包裹在
      `<TABLE>...</TABLE>` 里，行间换行、单元格间制表符 —— 与 docx 表格块的
      TSV 口径完全一致。于是图片表格在上层获得与文本表格相同的待遇
      （chunk 整表不切 / 超限按行组切且每组带表头 / 跨片回捞），
      不必再为"图片转出来的表"写第二套切分逻辑。
    ★ 标记 MUST 剥离：它是给解析层的信号，不是用户可见内容，留在正文里
      会污染 embedding 文本与检索词。
    ★ 形态校验（>=2 行、>=2 列）失败时只剥离标记、按普通文本返回 ——
      模型偶尔会把单行文字也套上标记，那不是表格，不该进表格切片分支。
    ★ 参差行补齐到最大列数：TSV 行必须等宽，否则下游按 Tab 数推列会错位。
    """
    start = text.find(TABLE_OPEN)
    end = text.rfind(TABLE_CLOSE)
    if start < 0 or end < start:
        return text, None

    body = text[start + len(TABLE_OPEN) : end].strip()
    rows: list[list[str]] = []
    for line in body.splitlines():
        if not line.strip():
            continue
        rows.append([cell.strip() for cell in line.split("\t")])

    if len(rows) < 2 or max(len(r) for r in rows) < 2:
        return body, None

    width = max(len(r) for r in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    tsv = "\n".join("\t".join(row) for row in rows)
    return tsv, rows[0]


async def extract_text_from_image(image_ref: ImageRef) -> ParsedBlock:
    """把图片转录为文本块；无可读内容或链路失败一律抛错（INV-M-04）。

    ★ 不写库、不落 chunk —— 本函数只做"图 → 文本"，是否入库由调用方决定
      （聊天提问 MUST NOT 落库，见 INV-M-03 / WU-M-07）。
    ★ 表格图在此处一次性归一为「is_table=True + TSV 正文 + 表头」：
      这里是 chat 与 parse 两个消费者的唯一交汇点，在这里定结构，
      两边拿到的都是干净文本 + 结构标记，不必各自再解析一遍模型输出。
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

    body, header = _expand_table(text)
    if header is None:
        return ParsedBlock(text=body)
    return ParsedBlock(text=body, is_table=True, table_header=header)
