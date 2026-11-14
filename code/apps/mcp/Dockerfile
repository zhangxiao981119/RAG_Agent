# 独立 MCP 协议适配服务，仅在容器内网运行
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY apps/mcp/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY apps/mcp/main.py ./

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
