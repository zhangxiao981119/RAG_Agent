"""知识库 MCP 协议适配服务，仅转换请求头和转发主 API。"""
from __future__ import annotations

import os
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from starlette.requests import Request
from starlette.responses import JSONResponse

KNOWLEDGE_API_BASE_URL = os.environ.get(
    "KNOWLEDGE_API_BASE_URL", "http://api:8000/api"
).rstrip("/")

mcp = FastMCP("knowledge-mcp")


def _api_headers() -> dict[str, str]:
    """将 MCP 入站身份头映射为主 API 双认证头，缺任一项即拒绝。"""
    inbound = get_http_headers()
    values = {
        name: inbound.get(name, "").strip()
        for name in ("x-user-token", "x-application-id", "x-application-key")
    }
    display_names = {
        "x-user-token": "X-User-Token",
        "x-application-id": "X-Application-Id",
        "x-application-key": "X-Application-Key",
    }
    missing = [display_names[name] for name, value in values.items() if not value]
    if missing:
        raise ToolError("缺少必需身份请求头: " + ", ".join(missing))
    return {
        "Authorization": f"Bearer {values['x-user-token']}",
        "X-Application-Id": values["x-application-id"],
        "X-Application-Key": values["x-application-key"],
    }


async def _post_integration(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """转发到主 API，不在适配层实现任何检索、问答或权限逻辑。"""
    headers = _api_headers()
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            f"{KNOWLEDGE_API_BASE_URL}/integrations/{path}",
            json=payload,
            headers=headers,
        )
        response.raise_for_status()
        return response.json()


async def knowledge_ask(
    question: str,
    kb_ids: list[str],
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """使用当前请求绑定的用户和应用身份进行知识库问答。"""
    return await _post_integration(
        "ask",
        {
            "question": question,
            "kb_ids": kb_ids,
            "conversation_id": conversation_id,
        },
    )


async def knowledge_search(query: str, kb_ids: list[str]) -> dict[str, Any]:
    """使用当前请求绑定的用户和应用身份检索知识库。"""
    return await _post_integration("search", {"query": query, "kb_ids": kb_ids})


# 显式注册以保留原函数对象，便于检查工具签名和进行离线单元测试。
mcp.tool(knowledge_ask)
mcp.tool(knowledge_search)


@mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health(_request: Request) -> JSONResponse:
    """容器健康检查不依赖外部服务，API 就绪由 Compose 依赖条件保证。"""
    return JSONResponse({"status": "ok"})


# 官方 FastMCP Streamable HTTP ASGI 入口，同时挂载 /mcp 与 /health。
app = mcp.http_app(path="/mcp", transport="streamable-http")
