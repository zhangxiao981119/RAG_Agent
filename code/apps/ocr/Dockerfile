# 本地 OCR 服务（B'）：GOT-OCR2.0 CPU 推理，数据不出域
FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    HF_HOME=/app/.cache/huggingface \
    PYTHONPATH=/app/GOT-OCR2.0

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# torch 走 CPU 专用源；transformers 等走 PyPI
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# GOT-OCR2.0 官方推理代码（GOT/ 包）vendored 进镜像；
# 模型权重不烧镜像，运行时经 HF_ENDPOINT（默认 hf-mirror.com）下载到挂载的 hf_cache。
# ★ GitHub 国内直连不可达：Clash 开启时构建须传代理 ——
#   docker compose build ocr --build-arg HTTPS_PROXY=http://host.docker.internal:7897
#   （Docker 内建代理 ARG 对 RUN 的 git clone 自动生效）
RUN git clone --depth 1 https://github.com/Ucas-HaoranWei/GOT-OCR2.0.git /app/GOT-OCR2.0

COPY main.py ./

EXPOSE 8000

# 单 worker：模型句柄进程内共享，多 worker 会成倍占用内存
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
