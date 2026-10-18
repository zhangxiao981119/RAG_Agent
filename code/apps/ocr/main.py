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
  · 推理代码固化：官方 modeling_GOT.py / got_vision_b.py / 自定义 QWenTokenizer 已
    vendored 进仓库（apps/ocr/vendor），运行期不执行 HF 远程代码、不依赖 verovio。
    上游 chat 写死 CUDA，CPU 版在 _chat_cpu 复刻（去 .cuda()/.half()/autocast）。
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

# GOT-OCR2.0 官方推理代码已固化成镜像内的本地包（apps/ocr/vendor → /app/vendor，见 Dockerfile）：
#   · got_inference/ : modeling_GOT.py + got_vision_b.py（GOTQwenForCausalLM 等）
#   · got_tokenizer/ : tokenization_qwen.py + qwen.tiktoken + tokenizer_config.json
VENDOR_DIR = "/app/vendor"
GOT_TOKENIZER_DIR = f"{VENDOR_DIR}/got_tokenizer"

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

    # vendored 包位于 /app/vendor，不在脚本目录（/app）的包搜索路径内，显式加入
    if VENDOR_DIR not in sys.path:
        sys.path.insert(0, VENDOR_DIR)
    try:
        from transformers import AutoTokenizer

        # 直接从本地包 import，不经 transformers 的 trust_remote_code 动态加载 ——
        # 后者会对 modeling_GOT.py 做 check_imports，缺 verovio 会把整个模块拦下
        from got_inference import GOTQwenForCausalLM

        logger.info("开始加载 OCR 模型 %s（CPU 推理）", MODEL_ID)
        # CPU 部署硬要求：flash-attn 仅支持 CUDA，显式锁定 eager 注意力后端。
        # ★ 不能写 use_flash_attn=False —— transformers 4.37 只识别 use_flash_attention_2
        #   与 attn_implementation，未识别的 kwarg 会被透传给 GOTQwenForCausalLM.__init__(config)，
        #   直接抛 TypeError；attn_implementation 会被 from_pretrained 消费并写入 config。
        # ★ 不传 trust_remote_code：config 走 GOTConfig.from_pretrained（GOTConfig 由本地包
        #   import，不触发 auto_map 的远程解析），模型本体同理
        _model = GOTQwenForCausalLM.from_pretrained(
            MODEL_ID,
            low_cpu_mem_usage=True,
            device_map="cpu",
            attn_implementation="eager",
            torch_dtype=torch.float32,
        )
        _model.eval()
        # 分词器同样固化在镜像内。本地目录仍须 trust_remote_code=True ——
        # tokenizer_config.json 的 auto_map 指向自定义类 QWenTokenizer，不在 transformers
        # 内置映射里；放行后加载的是本地 vendored 文件，无网络请求、无远程代码
        _tokenizer = AutoTokenizer.from_pretrained(
            GOT_TOKENIZER_DIR, trust_remote_code=True
        )
        _wrap_generate_for_confidence(_model)
        logger.info("OCR 模型加载完成，服务就绪")
    except Exception:
        # fail-fast：模型加载失败容器应退出，由 compose restart 策略拉起重试
        logger.exception("OCR 模型加载失败")
        raise


def _chat_cpu(image_path: str, ocr_type: str = "format") -> str:
    """上游 GOTQwenForCausalLM.chat 的 CPU 复刻版（语义不变，只替换 CUDA 相关写法）。

    与 vendor 内 modeling_GOT.py 的 chat 逐处差异：
      · input_ids 不再 .cuda()；图像张量不再 .half().cuda()，保持模型加载时的 float32
      · 推理上下文由 torch.autocast("cuda", bfloat16) 换成 torch.no_grad()
        （CPU 上 autocast 无收益，fp32 下强转 bf16 反而损失精度）
      · 不实现 render 分支 —— 上游仅 render=True 时才 import verovio / render_tools，
        本服务只取转录文本
      · 不调用 disable_torch_init()：它把 torch.nn 的 reset_parameters 改成空实现，
        只在「从零构造模型」时提速；本服务模型已由 from_pretrained 构造完毕
    保留不变：MPT 会话模板、<img> + 256×<imgpad> + </img> 提示词、KeywordsStoppingCriteria，
              以及 do_sample=False / num_beams=1 / no_repeat_ngram_size=20 / max_new_tokens=4096。
    """
    from got_inference import (
        DEFAULT_IMAGE_PATCH_TOKEN,
        DEFAULT_IM_END_TOKEN,
        DEFAULT_IM_START_TOKEN,
        Conversation,
        GOTImageEvalProcessor,
        KeywordsStoppingCriteria,
        SeparatorStyle,
    )

    image = _model.load_image(image_path)
    qs = "OCR with format: " if ocr_type == "format" else "OCR: "
    qs = (
        DEFAULT_IM_START_TOKEN
        + DEFAULT_IMAGE_PATCH_TOKEN * 256
        + DEFAULT_IM_END_TOKEN
        + "\n"
        + qs
    )

    conv_mpt = Conversation(
        system="""<|im_start|>system
        You should follow the instructions carefully and explain your answers in detail.""",
        roles=("<|im_start|>user\n", "<|im_start|>assistant\n"),
        version="mpt",
        messages=(),
        offset=0,
        sep_style=SeparatorStyle.MPT,
        sep="<|im_end|>",
    )
    conv = conv_mpt.copy()
    conv.append_message(conv.roles[0], qs)
    conv.append_message(conv.roles[1], None)
    prompt = conv.get_prompt()

    inputs = _tokenizer([prompt])
    image_tensor = GOTImageEvalProcessor(image_size=1024)(image).unsqueeze(0)
    input_ids = torch.as_tensor(inputs.input_ids)

    stop_str = conv.sep
    stopping_criteria = KeywordsStoppingCriteria([stop_str], _tokenizer, input_ids)
    # 必须走 self.generate：main.py 在 generate 层挂了置信度累积器，
    # 换成自定义采样循环会让置信度钩子失效
    with torch.no_grad():
        output_ids = _model.generate(
            input_ids,
            images=[image_tensor],
            do_sample=False,
            num_beams=1,
            no_repeat_ngram_size=20,
            max_new_tokens=4096,
            stopping_criteria=[stopping_criteria],
        )
    outputs = _tokenizer.decode(output_ids[0, input_ids.shape[1] :]).strip()
    if outputs.endswith(stop_str):
        outputs = outputs[: -len(stop_str)]
    return outputs.strip()


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
            text = _chat_cpu(tmp_path, ocr_type="format")
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
