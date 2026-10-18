FROM python:3.12-slim

# healthcheck 需要 pgrep (procps)；M6 知识源同步需要 git
RUN apt-get update && apt-get install -y --no-install-recommends procps git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
COPY . .
WORKDIR /workspace/apps/api
# 依赖按 requirements.lock 安装（可复现构建）：锁文件含全部传递依赖的精确版本，
# 不再让 pip 每次构建重新解析 pyproject 的区间。国内镜像源加速（如需可改回官方源）。
# ★ 用阿里云源：清华源在构建容器内持续 SSL EOF（连接握手即断），实测不可用。
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.lock

# ★ 不再 `pip install .` 安装项目自身（2026-10-18 实测踩到的坑）：
#   那样镜像里会有两份 `app` 副本 —— /workspace/apps/api/app 与
#   /usr/local/lib/python3.12/site-packages/app。而 uvicorn / arq 都是 console
#   script，sys.path[0] 是 /usr/local/bin、不含 cwd，运行时**加载的是
#   site-packages 那份构建快照**：改 /workspace 的源码 + 重启容器完全不生效，
#   现象是「代码改了、行为没变」，极难排查（本次端到端验证被它误导了 3 轮）。
#   项目没有 [project.scripts] 入口点，不需要安装自身 —— 用 PYTHONPATH 指向源码，
#   保证唯一真源。
ENV PYTHONPATH=/workspace/apps/api
EXPOSE 8000
# 启动：种子 + uvicorn。迁移不再作为启动副产物 —— 由编排中的 migrate
# 一次性服务保证「先迁移后启动」（dev/prod 两套 compose 均如此）。
CMD ["sh", "-c", "python -m scripts.seed && python -m scripts.seed_eval && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
