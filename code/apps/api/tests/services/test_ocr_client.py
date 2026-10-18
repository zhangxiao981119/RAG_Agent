"""OCR 客户端单测（B'：services/ocr.py，不发起真实 HTTP 请求）。

覆盖：
  · OCR_BASE_URL 未配置 → OcrError（fail-closed，消除 fallback 触发场景）
  · 200 + 文本 → ParsedBlock；空文本 → OcrNoContentError
  · HTTP 4xx/5xx / 连接失败 → OcrError（原因保留，由解析层改型 ParseError）
  · 低置信 → 仅 warning 审计标记，不失败、不重转录（OCR_LOW_CONF 口径）
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.config.settings import get_settings
from app.services import ocr as ocr_mod
from app.services.ocr import OcrError, OcrNoContentError, ocr_image

_OCR_URL = "http://ocr:8000"


def _set_base_url(monkeypatch: pytest.MonkeyPatch, base_url: str) -> None:
    """get_settings 是 lru_cache 单例，直接改实例属性，monkeypatch 负责还原。"""
    monkeypatch.setattr(get_settings(), "ocr_base_url", base_url)


def _http_response(status_code: int, payload: dict | None = None) -> httpx.Response:
    """构造真实 httpx.Response（raise_for_status / json 行为与线上一致）。"""
    content = b"" if payload is None else json.dumps(payload).encode("utf-8")
    return httpx.Response(
        status_code,
        content=content,
        request=httpx.Request("POST", f"{_OCR_URL}/ocr"),
    )


class _FakeAsyncClient:
    """替换 httpx.AsyncClient：按预设返回响应或抛异常，记录请求入参。"""

    calls: list[dict] = []
    _next: list[httpx.Response | Exception] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def __new__(cls, *args: object, **kwargs: object) -> "_FakeAsyncClient":
        return super().__new__(cls)

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def post(self, url: str, **kwargs: object) -> httpx.Response:
        _FakeAsyncClient.calls.append({"url": url, **kwargs})
        item = _FakeAsyncClient._next.pop(0)
        if isinstance(item, Exception):
            raise item
        return item  # type: ignore[no-any-return]


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *responses: httpx.Response | Exception,
) -> list[dict]:
    """把 FakeAsyncClient 装进 ocr 模块的 httpx 引用上（模块内 import httpx）。"""
    _FakeAsyncClient.calls = []
    _FakeAsyncClient._next = list(responses)
    monkeypatch.setattr(ocr_mod.httpx, "AsyncClient", _FakeAsyncClient)
    return _FakeAsyncClient.calls


@pytest.mark.asyncio
async def test_missing_base_url_fails_closed(monkeypatch: pytest.MonkeyPatch):
    """开关已开但未配服务地址 → 直接 OcrError，不发起请求、不做任何替代。"""
    _set_base_url(monkeypatch, "")
    calls = _install(monkeypatch)
    with pytest.raises(OcrError, match="OCR_BASE_URL 未配置"):
        await ocr_image(b"png-bytes")
    assert calls == []


@pytest.mark.asyncio
async def test_success_returns_parsed_block(monkeypatch: pytest.MonkeyPatch):
    _set_base_url(monkeypatch, _OCR_URL)
    calls = _install(
        monkeypatch, _http_response(200, {"text": "部件清单", "confidence": 0.97})
    )
    block = await ocr_image(b"png-bytes", "image/png")
    assert block.text == "部件清单"
    assert calls[0]["url"] == f"{_OCR_URL}/ocr"
    assert calls[0]["content"] == b"png-bytes"
    assert calls[0]["headers"] == {"Content-Type": "image/png"}


@pytest.mark.asyncio
async def test_structure_markup_is_stripped(monkeypatch: pytest.MonkeyPatch):
    """GOT 的结构标记（`\\title{` / 裸 `}` / `\\(`）不得进入 chunk。

    ★ 实测输出长这样：`\\title{` + 标题 + `}`，转义还会出现在编号里
      （`BXYC- \\(2026-0917\\)`）。不剥就会在引用里露出反斜杠脚手架。
    """
    _set_base_url(monkeypatch, _OCR_URL)
    raw = "\\title{\n员工报销管理办法\n}\n第一条 见 BXYC- \\(2026-0917\\) 号文。"
    _install(monkeypatch, _http_response(200, {"text": raw, "confidence": 0.9}))
    block = await ocr_image(b"png-bytes")
    assert "\\title" not in block.text
    assert block.text.splitlines()[0] == "员工报销管理办法"
    assert "BXYC- (2026-0917) 号文" in block.text


@pytest.mark.asyncio
async def test_markup_only_text_maps_to_no_content(monkeypatch: pytest.MonkeyPatch):
    """整页只有结构标记 → 剥完是空文本 → OcrNoContentError，不落库空块。"""
    _set_base_url(monkeypatch, _OCR_URL)
    _install(monkeypatch, _http_response(200, {"text": "\\title{\n}", "confidence": 0.9}))
    with pytest.raises(OcrNoContentError):
        await ocr_image(b"png-bytes")


@pytest.mark.asyncio
async def test_empty_text_maps_to_no_content(monkeypatch: pytest.MonkeyPatch):
    """服务判定图中无可读内容（空文本）→ OcrNoContentError。"""
    _set_base_url(monkeypatch, _OCR_URL)
    _install(monkeypatch, _http_response(200, {"text": "  ", "confidence": 0.9}))
    with pytest.raises(OcrNoContentError):
        await ocr_image(b"png-bytes")


@pytest.mark.asyncio
async def test_http_error_carries_status(monkeypatch: pytest.MonkeyPatch):
    _set_base_url(monkeypatch, _OCR_URL)
    _install(monkeypatch, _http_response(500, {"detail": "模型炸了"}))
    with pytest.raises(OcrError, match="HTTP 500"):
        await ocr_image(b"png-bytes")


@pytest.mark.asyncio
async def test_connection_error_maps_to_ocr_error(monkeypatch: pytest.MonkeyPatch):
    _set_base_url(monkeypatch, _OCR_URL)
    _install(
        monkeypatch,
        httpx.ConnectError("连接被拒绝", request=httpx.Request("POST", f"{_OCR_URL}/ocr")),
    )
    with pytest.raises(OcrError, match="OCR 服务不可达"):
        await ocr_image(b"png-bytes")


@pytest.mark.asyncio
async def test_low_confidence_warns_but_succeeds(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """低于 OCR_LOW_CONF → warning 审计标记，文本仍正常返回（不失败不重转录）。"""
    from app.config.decisions import OCR_LOW_CONF

    _set_base_url(monkeypatch, _OCR_URL)
    low = OCR_LOW_CONF / 2
    _install(
        monkeypatch, _http_response(200, {"text": "模糊页内容", "confidence": low})
    )
    with caplog.at_level("WARNING", logger="app.services.ocr"):
        block = await ocr_image(b"png-bytes")
    assert block.text == "模糊页内容"
    assert any("低于阈值" in rec.message for rec in caplog.records)


# ── 并发闸与错误分类（2026-10-18 实测 12 并发把 OCR 服务打 OOM 后补）──────


@pytest.mark.asyncio
async def test_concurrency_is_capped(monkeypatch: pytest.MonkeyPatch):
    """并发请求数 MUST 被 OCR_MAX_CONCURRENCY 卡住。

    背景：12 张图并发上传时 worker 同时打出 12 个 OCR 请求，
    OCR 服务（4GB 容器上限）内存被打爆、进程被 kill，9/12 张失败。
    没有这道闸，批量上传图片等于对 OCR 服务做一次压力测试。
    """
    import asyncio

    from app.config.decisions import OCR_MAX_CONCURRENCY

    _set_base_url(monkeypatch, _OCR_URL)

    in_flight = 0
    peak = 0

    class _SlowClient(_FakeAsyncClient):
        async def post(self, url: str, **kwargs: object) -> httpx.Response:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.02)  # 模拟真实推理耗时，制造重叠窗口
            in_flight -= 1
            return _http_response(200, {"text": "内容", "confidence": 0.9})

    monkeypatch.setattr(ocr_mod.httpx, "AsyncClient", _SlowClient)

    blocks = await asyncio.gather(*[ocr_image(b"x") for _ in range(8)])

    assert len(blocks) == 8, "限流 MUST NOT 丢请求"
    assert peak <= OCR_MAX_CONCURRENCY, f"并发峰值 {peak} 超过上限 {OCR_MAX_CONCURRENCY}"


@pytest.mark.asyncio
async def test_timeout_reports_timeout_not_unreachable(monkeypatch: pytest.MonkeyPatch):
    """超时与不可达 MUST 分开报：排查方向不同（服务太慢 vs 网络/进程挂了）。"""
    _set_base_url(monkeypatch, _OCR_URL)
    _install(monkeypatch, httpx.ReadTimeout("timed out"))
    with pytest.raises(OcrError, match="超时"):
        await ocr_image(b"png-bytes")


@pytest.mark.asyncio
async def test_disconnect_reports_interrupted(monkeypatch: pytest.MonkeyPatch):
    """连接被中断（典型 OOM 被杀）→ 文案点明真因，不混进「不可达」。"""
    _set_base_url(monkeypatch, _OCR_URL)
    _install(
        monkeypatch,
        httpx.RemoteProtocolError("Server disconnected without sending a response."),
    )
    with pytest.raises(OcrError, match="连接被中断"):
        await ocr_image(b"png-bytes")
