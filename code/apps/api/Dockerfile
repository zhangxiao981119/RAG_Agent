FROM python:3.12-slim

# healthcheck 需要 pgrep (procps)；M6 知识源同步需要 git
RUN apt-get update && apt-get install -y --no-install-recommends procps git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
COPY . .
WORKDIR /workspace/apps/api
# 依赖按 requirements.lock 安装（可复现构建）：锁文件含全部传递依赖的精确版本，
# 不再让 pip 每次构建重新解析 pyproject 的区间。国内镜像源加速（如需可改回官方源）。
# 项目自身用 --no-deps 安装，避免 pip 据 pyproject 重新解析并覆盖锁定版本。
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.lock \
    && pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple --no-deps .
EXPOSE 8000
# 启动：迁移 + 种子 + uvicorn
CMD ["sh", "-c", "alembic upgrade head && python -m scripts.seed && python -m scripts.seed_eval && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
