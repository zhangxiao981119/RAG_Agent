FROM python:3.12-slim

# healthcheck 需要 pgrep (procps)
RUN apt-get update && apt-get install -y --no-install-recommends procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
COPY . .
WORKDIR /workspace/apps/api
# 国内镜像源加速（如需可改回官方源）
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple .
EXPOSE 8000
# 启动：迁移 + 种子 + uvicorn
CMD ["sh", "-c", "alembic upgrade head && python -m scripts.seed && python -m scripts.seed_eval && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
