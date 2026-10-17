"""本地 OCR 服务（B'）—— GOT-OCR2.0 CPU 推理，数据不出域。

对外仅暴露两个端点（仅容器网络内可达，不对外发布端口）：
  · GET  /health：模型就绪返回 200，加载中 503（compose healthcheck 消费）
  · POST /ocr：请求体 = 图片字节（Content-Type: image/png 等），
    返回 {"text": "<转录文本>", "confidence": <0~1>}

设计要点：
  · 单引擎（B' 方案）：GOT-OCR2.0 是唯一引擎，**没有**第二引擎兜底 ——
    服务失败时调用侧（api 的 services/ocr.py）显式失败，任务走重试 → 死信 → 重跑。
  · 置信度代理：GOT 为生成式模型，无识别器式 per-char score；
    用 greedy 解码各步 chosen token 的 log-softmax 概率均值做代理
    （经 LogitsProcessor 流式累积，不保留全量 scores，防超长页内存爆炸）。
  · 串行推理：torch generate 非线程安全且双请求并发易 OOM，
    threading.Lock 串行化；上传解析本身是 worker 单任务节奏，串行不构成瓶颈。
  · ocr_type="format"：输出保留结构（表格 → HTML、公式 → LaTeX），
    比纯文本流更利于入库后的检索与引用。
"""
from __future__ import annotations

import asyncio
import logging
import math
import mimetypes
import os
import tempfile
import threading

import torch
from fastapi import FastAPI, HTTPException, Request
from transformers import LogitsProcessor

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("ocr")

# ── 配置（环境变量，compose 注入）────────────────────────────
MODEL_ID = os.environ.get("OCR_MODEL_ID", "stepfun-ai/GOT-OCR2_0")
HF_ENDPOINT = os.environ.get("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_ENDPOINT", HF_ENDPOINT)  # transformers/hf_hub 读取该变量

# ── 模型句柄：lifespan 启动期加载，加载完成前 /health 返回 503 ──
_model = None
_tokenizer = None
# LogitsProcessor 累积器 → 推理结束后读取该步置信度（单 lock 串行，无竞态）
_conf_box: dict[str, float] = {}
_infer_lock = threading.Lock()

# GOT-OCR2.0 的推理代码在官方仓库（GOT/ 包），构建镜像时 vendored clone，
# PYTHONPATH 指向仓库根（见 Dockerfile），运行时直接 import。
GOT_REPO_DIR = "/app/GOT-OCR2.0"

app = FastAPI(title="kagent-ocr", docs_url=None, redoc_url=None)


class _ConfidenceAccumulator(LogitsProcessor):
    """greedy 解码每步累积 chosen token 的 log 概率（流式，不保留 scores）。"""

    def __init__(self) -> None:
        self.logps: list[float] = []

    def __call__(self, input_ids, scores):  # noqa: ANN001 - transformers 基类签名
        chosen = scores.argmax(dim=-1)
        logp = torch.log_softmax(scores.float(), dim=-1)
        val = logp.gather(-1, chosen.unsqueeze(-1)).item()
        self.logps.append(val)
        return scores


def _wrap_generate_for_confidence(model) -> None:
    """包一层 model.generate：注入置信度累积器，其余行为原样透传。

    GOT 的 model.chat 内部调 self.generate；这里 hook generate 层，
    与 chat 内部实现解耦（不改官方代码）。greedy（do_sample=False）
    下 chosen = argmax，累积结果即转录文本的平均 token 概率。
    """
    orig_generate = model.generate

    def generate_with_conf(*args, **kwargs):  # noqa: ANN002, ANN003
        acc = _ConfidenceAccumulator()
        existing = kwargs.get("logits_processor")
        kwargs["logits_processor"] = [acc] if existing is None else list(existing) + [acc]
        result = orig_generate(*args, **kwargs)
        if acc.logps:
            _conf_box["value"] = math.exp(sum(acc.logps) / len(acc.logps))
        else:
            _conf_box["value"] = 0.0
        return result

    model.generate = generate_with_conf  # type: ignore[method-assign]


@app.on_event("startup")
def load_model() -> None:
    """启动期一次性加载模型（阻塞至就绪；加载完成前健康探针不可达）。"""
    global _model, _tokenizer
    import sys

    if GOT_REPO_DIR not in sys.path:
        sys.path.insert(0, GOT_REPO_DIR)
    try:
        # GOTQwenForCausalLM 定义在 GOT/model/GOT_ocr_2_0.py，经 model 包 __init__ 导出；
        # GOT/__init__.py 为空，不能直接从 GOT 顶层导入
        from GOT.model import GOTQwenForCausalLM  # type: ignore[import-not-found]
        from transformers import AutoTokenizer

        logger.info("开始加载 OCR 模型 %s（CPU 推理）", MODEL_ID)
        # use_flash_attn=False 是 CPU 部署的硬要求（flash-attn 仅支持 CUDA）
        _model = GOTQwenForCausalLM.from_pretrained(
            MODEL_ID,
            low_cpu_mem_usage=True,
            device_map="cpu",
            use_flash_attn=False,
            torch_dtype=torch.float32,
        )
        _model.eval()
        _tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
        _wrap_generate_for_confidence(_model)
        logger.info("OCR 模型加载完成，服务就绪")
    except Exception:
        # fail-fast：模型加载失败容器应退出，由 compose restart 策略拉起重试
        logger.exception("OCR 模型加载失败")
        raise


def _run_ocr(data: bytes, mime: str) -> str:
    """同步执行单页 OCR（在线程池中运行，lock 串行）。返回转录文本。"""
    assert _model is not None and _tokenizer is not None  # 仅 /health 放行后可达
    suffix = mimetypes.guess_extension(mime) or ".png"
    # 官方 chat 接口按文件路径读图，落临时文件（请求结束后立即清理）
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        _conf_box.clear()
        with _infer_lock:
            text = _model.chat(_tokenizer, tmp_path, ocr_type="format")
        return str(text or "").strip()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:  # pragma: no cover - 临时文件清理失败不影响结果
            pass


@app.get("/health")
def health() -> dict[str, str]:
    if _model is None or _tokenizer is None:
        raise HTTPException(status_code=503, detail="模型加载中")
    return {"status": "ok"}


@app.post("/ocr")
async def ocr(request: Request) -> dict[str, object]:
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="请求体为空，未收到图片字节")
    mime = request.headers.get("content-type", "image/png").split(";")[0].strip()
    loop = asyncio.get_running_loop()
    try:
        text = await loop.run_in_executor(None, _run_ocr, data, mime)
    except HTTPException:
        raise
    except Exception as exc:
        # 异常原样透传给调用侧（api services/ocr.py 会包成 OcrError → ParseError）
        logger.exception("OCR 推理失败")
        raise HTTPException(status_code=500, detail=f"OCR 推理失败: {exc}") from exc
    confidence = float(_conf_box.get("value", 0.0))
    return {"text": text, "confidence": confidence}
