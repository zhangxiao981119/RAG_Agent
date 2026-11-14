"""MCP 工具签名、身份头映射与主 API 转发契约测试。"""
from __future__ import annotations

import inspect
import json

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from apps.mcp import main

_REAL_ASYNC_CLIENT = httpx.AsyncClient
_IDENTITY_HEADERS = {
    "x-user-token": "user-jwt",
    "x-application-id": "application-id",
    "x-application-key": "application-key",
}


@pytest.fixture(autouse=True)
def _block_real_network(monkeypatch):
    """默认禁止创建真实 HTTP 客户端，转发测试必须显式安装内存替身。"""
    def _blocked(*args, **kwargs):
        raise AssertionError("测试不得访问真实网络")

    monkeypatch.setattr(main.httpx, "AsyncClient", _blocked)


def _use_identity_headers(monkeypatch) -> None:
    monkeypatch.setattr(main, "get_http_headers", lambda: dict(_IDENTITY_HEADERS))


def _mock_api(monkeypatch, response_json: dict) -> list[httpx.Request]:
    """使用 HTTPX 内存传输记录请求，不建立任何网络连接。"""
    requests: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=response_json)

    transport = httpx.MockTransport(_handler)

    def _client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(main.httpx, "AsyncClient", _client_factory)
    return requests


def test_identity_headers_are_mapped_to_main_api(monkeypatch):
    monkeypatch.setattr(
        main,
        "get_http_headers",
        lambda: {
            "x-user-token": " user-jwt ",
            "x-application-id": " application-id ",
            "x-application-key": " application-key ",
        },
    )

    assert main._api_headers() == {
        "Authorization": "Bearer user-jwt",
        "X-Application-Id": "application-id",
        "X-Application-Key": "application-key",
    }


def test_tool_signatures_have_no_user_or_permission_parameters():
    ask_parameters = inspect.signature(main.knowledge_ask).parameters
    search_parameters = inspect.signature(main.knowledge_search).parameters

    assert list(ask_parameters) == ["question", "kb_ids", "conversation_id"]
    assert list(search_parameters) == ["query", "kb_ids"]
    forbidden = {"user_id", "tenant_id", "clearance", "subjects", "permissions"}
    assert forbidden.isdisjoint(ask_parameters)
    assert forbidden.isdisjoint(search_parameters)


@pytest.mark.asyncio
async def test_knowledge_ask_forwards_body_headers_and_returns_api_json(monkeypatch):
    _use_identity_headers(monkeypatch)
    api_json = {
        "conversation_id": "conversation-id",
        "message_id": "message-id",
        "answer": "住宿报销标准为 500 元",
        "citations": [{"n": 1}],
        "refused": False,
        "usage": {"prompt_tokens": 3},
        "suggestions": [],
    }
    requests = _mock_api(monkeypatch, api_json)

    result = await main.knowledge_ask(
        "住宿报销标准", ["kb-1", "kb-2"], "conversation-id"
    )

    assert result == api_json
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "http://api:8000/api/integrations/ask"
    assert request.headers["Authorization"] == "Bearer user-jwt"
    assert request.headers["X-Application-Id"] == "application-id"
    assert request.headers["X-Application-Key"] == "application-key"
    assert "X-User-Token" not in request.headers
    assert json.loads(request.content) == {
        "question": "住宿报销标准",
        "kb_ids": ["kb-1", "kb-2"],
        "conversation_id": "conversation-id",
    }


@pytest.mark.asyncio
async def test_knowledge_search_forwards_body_headers_and_returns_api_json(monkeypatch):
    _use_identity_headers(monkeypatch)
    api_json = {"items": [{"content": "检索片段", "score": 0.9}]}
    requests = _mock_api(monkeypatch, api_json)

    result = await main.knowledge_search("报销", ["kb-1"])

    assert result == api_json
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "http://api:8000/api/integrations/search"
    assert request.headers["Authorization"] == "Bearer user-jwt"
    assert request.headers["X-Application-Id"] == "application-id"
    assert request.headers["X-Application-Key"] == "application-key"
    assert "X-User-Token" not in request.headers
    assert json.loads(request.content) == {"query": "报销", "kb_ids": ["kb-1"]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("missing", "display_name"),
    [
        ("x-user-token", "X-User-Token"),
        ("x-application-id", "X-Application-Id"),
        ("x-application-key", "X-Application-Key"),
    ],
)
async def test_tool_rejects_each_missing_identity_header(
    monkeypatch, missing, display_name
):
    headers = dict(_IDENTITY_HEADERS)
    headers.pop(missing)
    monkeypatch.setattr(main, "get_http_headers", lambda: headers)

    with pytest.raises(ToolError) as exc_info:
        await main.knowledge_search("不得放行", ["kb-1"])

    assert display_name in str(exc_info.value)


@pytest.mark.asyncio
async def test_health_returns_ok():
    request = main.Request(
        {"type": "http", "method": "GET", "path": "/health", "headers": []}
    )
    response = await main.health(request)

    assert response.status_code == 200
    assert json.loads(response.body) == {"status": "ok"}


@pytest.mark.asyncio
async def test_mcp_protocol_lists_and_calls_registered_tools(monkeypatch):
    """通过 FastMCP 协议验证工具发现、参数序列化和返回值解码。"""
    _use_identity_headers(monkeypatch)
    api_json = {"items": [{"content": "协议调用结果", "score": 0.9}]}
    requests = _mock_api(monkeypatch, api_json)

    async with Client(main.mcp) as client:
        tools = await client.list_tools()
        assert {tool.name for tool in tools} == {
            "knowledge_ask", "knowledge_search",
        }
        result = await client.call_tool(
            "knowledge_search", {"query": "协议调用", "kb_ids": ["kb-1"]}
        )

    assert result.data == api_json
    assert len(requests) == 1
    assert json.loads(requests[0].content) == {
        "query": "协议调用", "kb_ids": ["kb-1"],
    }
