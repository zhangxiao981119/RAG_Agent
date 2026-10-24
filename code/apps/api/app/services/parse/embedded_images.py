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
★ 预算分两类且分开计：段落内嵌图（inline）与表格单元格图（table_cell）；
  去重集则**跨类共享**（同一张 logo 可能出现两处，只该转录一次）。
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


def _flatten_cell_text(text: str) -> str:
    """把单元格转录压成单行：换行 → ` / `，Tab → ` | `。

    ★ 单元格转录会被拼进外层表格的 TSV 行里，换行与 Tab 都是行列分隔符 ——
      原样写入会把一格内容撑成多行多列，整表的行内绑定直接被打散。
    """
    lines: list[str] = []
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.split("\t") if cell.strip()]
        if cells:
            lines.append(" | ".join(cells))
    return " / ".join(lines)


class EmbeddedImageRunner:
    """单份文档的内嵌图转录编排器：去重、过滤、分类预算、串行转录、计数。

    每份解析任务实例化一个 —— 去重集与预算计数都是**文档级**状态。
    ★ 去重集**跨类别共享**：同一张 logo 既可能出现在段落也可能出现在表格里，
      按类别分开去重等于让同一张图烧两次算力、入库两段相同文本。
    """

    # 类别 → 预算。两类图的单文档上限分开计（理由见 decisions 对应常量）
    _CAPS: dict[str, int] = {
        "inline": decisions.EMBEDDED_IMAGE_MAX_PER_DOC,
        "table_cell": decisions.TABLE_EMBEDDED_IMAGE_MAX_PER_DOC,
    }

    def __init__(self) -> None:
        self._seen: set[str] = set()  # 已处理图片的 sha1（重复图不烧转录算力）
        self._transcribed: dict[str, int] = {}  # 类别 → 已转录张数
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
        category: str = "inline",
    ) -> ParsedBlock | None:
        """一张内嵌图 → 转录块或 None（被过滤/跳过）。

        ★ 过滤顺序即成本顺序：先字节/面积（零成本）→ sha1 去重（零成本）
          → 本类别预算护栏，最后才发起转录。
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

        if self._transcribed.get(category, 0) >= self._CAPS.get(category, 0):
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

        self._transcribed[category] = self._transcribed.get(category, 0) + 1
        block.heading_path = heading_path
        block.page_no = page_no
        if category == "table_cell":
            # 单元格转录要回填外层表格：压平行列分隔符，并撤销"表格块"标记 ——
            # 单元格里的图片表格嵌套不进 TSV，只能退化成一行描述
            block.text = f"[图片转录] {_flatten_cell_text(block.text)}"
            block.is_table = False
            block.table_header = []
        elif not block.is_table:
            # 段落里的表格图不加前缀：前缀会落在表头行首，chunk 层据此识别表头，
            # 加了就变成"一个叫 [图片转录] 型号 的列名"，表头语义被污染
            block.text = f"[图片转录] {block.text}"
        return block

    def log_summary(self, source: str) -> None:
        """收尾计数日志：让运维能看到「有图但没变成知识」的规模。"""
        total = sum(self._transcribed.values())
        if not (total or self._skipped_small or self._skipped_dup
                or self._skipped_no_content or self._skipped_over_cap):
            return
        logger.info(
            "内嵌图转录汇总（%s）：转录 %d 张（段落 %d / 单元格 %d）；"
            "跳过 —— 小图 %d / 重复 %d / 无字 %d / 超上限 %d",
            source,
            total,
            self._transcribed.get("inline", 0),
            self._transcribed.get("table_cell", 0),
            self._skipped_small,
            self._skipped_dup,
            self._skipped_no_content,
            self._skipped_over_cap,
        )
