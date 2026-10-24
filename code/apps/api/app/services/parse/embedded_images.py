"""文档内嵌图转录编排 —— docx / PDF 文本层共用（口径单点）。

定位：parser 从文档里抽出的图片字节 → 过滤（小图 / 重复 / 超限）→
视觉 LLM 转录 → ParsedBlock（插回文档流由调用方负责位置）。

★ 引擎复用聊天带图链路（app.services.multimodal.vision + LLMService），
  MUST NOT 引入第二套 LLM 调用实现；**延迟导入** vision 模块断循环 ——
  vision.py 顶部 `from app.services.parse import ParsedBlock`，本模块在
  parse 包内若顶部反向 import 即成环（与 image_parser 延迟导入 ocr
  client 同一手法）。
★ 失败语义（决策真源 decisions.EMBEDDED_IMAGE_MAX_PER_DOC）：
  · 小图 / 重复图 / 超限图 / 模型判定无可读内容（纯 K 线、装饰图）
    → 跳过 + 计数，文档主体照常入库；
  · 转录链路本身失败（LLM 不可用 / 超时 / VisionError）
    → ParseError，fail-closed —— 不做"只入文本"降级，
      与「视觉抽取不可用不得降级为纯文本」的全局口径一致。
★ 开发期接多模态 API 代理即可验证；生产部署 MUST 把 LLM_BASE_URL /
  LLM_MODEL 切换为**本地部署的视觉模型**（数据不出域硬约束）。
"""
from __future__ import annotations

import base64
import hashlib
import io
import logging

from PIL import Image

from app.config import decisions
from app.services.parse.base import ParsedBlock, ParseError

logger = logging.getLogger(__name__)


def _image_size(data: bytes) -> tuple[int, int] | None:
    """Pillow 读图片宽高；解码失败返回 None（按 insignificant 跳过转录）。"""
    try:
        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception:
        return None


class EmbeddedImageRunner:
    """单份文档的内嵌图转录编排器：去重、过滤、上限护栏、串行转录、计数。

    每份解析任务实例化一个 —— 去重集与上限计数都是**文档级**状态。
    """

    def __init__(self) -> None:
        self._seen: set[str] = set()  # 已处理图片的 sha1（重复图不烧转录算力）
        self._transcribed = 0
        self._skipped_small = 0
        self._skipped_dup = 0
        self._skipped_no_content = 0
        self._skipped_over_cap = 0

    async def process(
        self,
        data: bytes,
        mime: str,
        heading_path: str,
        page_no: int | None,
    ) -> ParsedBlock | None:
        """一张内嵌图 → 转录块或 None（被过滤/跳过）。

        ★ 过滤顺序即成本顺序：先字节/面积（零成本）→ sha1 去重（零成本）
          → 上限护栏，最后才发起转录。
        """
        size = _image_size(data)
        if (
            size is None
            or size[0] * size[1] < decisions.EMBEDDED_IMAGE_MIN_AREA_PX
            or len(data) < decisions.EMBEDDED_IMAGE_MIN_BYTES
        ):
            self._skipped_small += 1
            return None

        digest = hashlib.sha1(data).hexdigest()
        if digest in self._seen:
            self._skipped_dup += 1
            return None
        self._seen.add(digest)

        if self._transcribed >= decisions.EMBEDDED_IMAGE_MAX_PER_DOC:
            self._skipped_over_cap += 1
            return None

        # 延迟导入断 parse<->multimodal 循环（见模块 docstring）
        from app.schemas.chat import ImageRef
        from app.services.multimodal import vision

        data_url = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
        try:
            block = await vision.extract_text_from_image(ImageRef(data_url=data_url))
        except vision.VisionNoContentError:
            # 纯图形无字（K 线 / 示意图 / 装饰图）：合法情形，跳过不废文档
            self._skipped_no_content += 1
            logger.info("内嵌图无可读内容，跳过转录（page=%s, heading=%s）", page_no, heading_path)
            return None
        except vision.VisionError as exc:
            raise ParseError(f"内嵌图转录失败（视觉链路不可用，fail-closed）：{exc}") from exc

        self._transcribed += 1
        block.heading_path = heading_path
        block.page_no = page_no
        block.text = f"[图片转录] {block.text}"
        return block

    def log_summary(self, source: str) -> None:
        """收尾计数日志：让运维能看到「有图但没变成知识」的规模。"""
        if not (self._transcribed or self._skipped_small or self._skipped_dup
                or self._skipped_no_content or self._skipped_over_cap):
            return
        logger.info(
            "内嵌图转录汇总（%s）：转录 %d 张；跳过 —— 小图 %d / 重复 %d / 无字 %d / 超上限 %d",
            source,
            self._transcribed,
            self._skipped_small,
            self._skipped_dup,
            self._skipped_no_content,
            self._skipped_over_cap,
        )
