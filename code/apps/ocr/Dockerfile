# 本地 OCR 服务（B'）：GOT-OCR2.0 CPU 推理，数据不出域
FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    HF_HOME=/app/.cache/huggingface

WORKDIR /app

# 依赖全部走国内镜像源（见 requirements.txt）；构建期不再需要 apt 包 ——
# 官方推理代码已 vendored 进仓库，无需 git clone GitHub
COPY apps/ocr/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# GOT-OCR2.0 官方推理代码 + 自定义 tokenizer，已固化进仓库（apps/ocr/vendor）。
# 权重不烧镜像，运行时经 HF_ENDPOINT 下载到挂载的 hf_cache。
# ★ 不用 transformers trust_remote_code 动态加载模型：check_imports 会因
#   modeling_GOT.py 引用 verovio（仅 render 分支用）拒绝加载模块。
COPY apps/ocr/vendor ./vendor
COPY apps/ocr/main.py ./

EXPOSE 8000

# 单 worker：模型句柄进程内共享，多 worker 会成倍占用内存
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
