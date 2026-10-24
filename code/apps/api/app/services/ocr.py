"""本地 OCR 客户端 —— 扫描件 PDF 页转录（B'，数据不出域）。

职责：把图片字节送本地 apps/ocr 服务（GOT-OCR2.0）转录为文本，
产出既有 ParsedBlock 契约，供 pdf_parser 的扫描页复用（用户直传的图片文件
已改走视觉链路，见 image_parser）。

★ 单引擎 fail-closed：OCR 服务不可达 / 返回错误 / 空结果一律抛 OcrError，
  由解析层改型为 ParseError —— 任务走 arq 重试 → 死信 → 恢复后重跑。
  **MUST NOT** 在此做第二引擎兜底或纯文本降级（会掩盖故障 + 污染数据口径）。
★ 低置信仅告警（OCR_LOW_CONF）：低于阈值打 warning 进审计日志，仍正常入库；
  数据不出域约束下禁止升级外部 LLM 重转录。
★ 与视觉链路（multimodal/vision.py）的边界：聊天带图、图片文件直传、文档内嵌图
  都走 LLM 视觉转录（能拿到表格结构）；扫描件 PDF 页走本地 OCR（逐字转录保真）。
  两条链路各自 fail-closed，**不互相兜底**。
"""
from __future__ import annotations

import asyncio
import logging
import re

import httpx

from app.config.decisions import OCR_LOW_CONF, OCR_MAX_CONCURRENCY, OCR_TIMEOUT_S
from app.config.settings import get_settings
from app.services.parse import ParsedBlock

logger = logging.getLogger(__name__)

# 进程内并发闸：同一时刻最多 N 个请求在途（见 decisions.OCR_MAX_CONCURRENCY）。
# ★ 按事件循环懒建：Semaphore 跨 loop 复用会在测试（每个用例一个 loop）里抛
#   「bound to a different event loop」，所以 loop 变了就重建一个。
_semaphore: asyncio.Semaphore | None = None
_semaphore_loop: asyncio.AbstractEventLoop | None = None


def _ocr_semaphore() -> asyncio.Semaphore:
    global _semaphore, _semaphore_loop
    loop = asyncio.get_running_loop()
    if _semaphore is None or _semaphore_loop is not loop:
        _semaphore = asyncio.Semaphore(OCR_MAX_CONCURRENCY)
        _semaphore_loop = loop
    return _semaphore

# GOT-OCR 会按文档结构输出 LaTeX 风格标记：单行的 `\title{` / `}`，
# 以及少量转义符号（`\(2026-0917\)`）。这些 token 会原样进入 chunk 与 embedding，
# 在引用里显示成 `\title{`，但没有任何语义 —— 入库前统一剥掉。
_STRUCT_ONLY_RE = re.compile(
    r"^\s*\\?(?:title|chapter|section|subsection|subsubsection|paragraph"
    r"|subparagraph|figure|table)\s*\{?\s*$",
    re.IGNORECASE,
)
_BRACE_ONLY_RE = re.compile(r"^\s*[{}]\s*$")
_STRUCT_WRAP_RE = re.compile(
    r"\\(?:title|chapter|section|subsection|subsubsection|paragraph)\{([^{}]*)\}"
)
_TEX_ESCAPE_RE = re.compile(r"\\([()\[\]%_&$#])")
_BLANK_RUN_RE = re.compile(r"\n{3,}")


def normalize_ocr_text(text: str) -> str:
    """剥离 OCR 引擎的结构标记与转义符，只留下正文（逐字保真，不改语义）。

    ★ 只处理**已知的标记 token**，不做任何「纠错 / 补全 / 改写」——
      OCR 的错字是事实，改它就等于伪造原文，与「答案可回溯」冲突。
    """
    if not text:
        return ""
    out = _STRUCT_WRAP_RE.sub(r"\1", text)  # 单行包好的 \title{X} → X
    lines = []
    for line in out.split("\n"):
        if _STRUCT_ONLY_RE.match(line) or _BRACE_ONLY_RE.match(line):
            continue
        lines.append(line)
    out = "\n".join(lines)
    out = _TEX_ESCAPE_RE.sub(r"\1", out)  # \( → ( ，\_ → _
    return _BLANK_RUN_RE.sub("\n\n", out).strip()


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
        # 并发闸在 httpx 之外：排队时间不计入超时（否则排队 3 张就必然超时）
        async with _ocr_semaphore():
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
    except httpx.TimeoutException as exc:
        # 与「不可达」分开报：超时说明服务活着但太慢，排查方向完全不同
        raise OcrError(
            f"OCR 服务超时（{OCR_TIMEOUT_S:.0f}s 未返回）：{type(exc).__name__}"
        ) from exc
    except httpx.RemoteProtocolError as exc:
        # 最典型的真因是服务端 OOM 被 kill；曾经统一报「不可达」，
        # 排查时全被误导到网络/DNS 上（2026-10-18 实测 12 并发时踩到）
        raise OcrError(
            f"OCR 服务连接被中断（常见原因：服务端 OOM 重启 / 进程退出）：{exc}"
        ) from exc
    except httpx.HTTPError as exc:
        raise OcrError(f"OCR 服务不可达：{exc}") from exc

    text = str(payload.get("text") or "").strip()
    confidence = float(payload.get("confidence") or 0.0)
    # 结构标记（`\title{`、裸 `}`、`\(` 转义）先剥掉：空文本判定也据此复核，
    # 否则一整页只有标记时会被当成「有内容」送进 chunk。
    text = normalize_ocr_text(text)
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
