"""WU-M-12 web_fetcher 测试 —— INV-M-06 白名单 / INV-M-08 限速并发 / DoD 鉴权失败中断。

httpx.MockTransport 模拟站点，零真实网络。钉住七件事：
  1. 白名单外 URL 必拒（INV-M-06 核心）：异域 / 伪装子串域 / userinfo / 非 http scheme
  2. 白名单口径：entry_urls host 集合，端口不参与比对，hostname 精确匹配
  3. scope_rules：allow_prefixes 命中保留、deny_prefixes 排除、均不命中丢弃
  4. discover_pages：BFS 收敛在白名单内 + 外链丢弃 + 相对链接补全 + 页数硬截断
  5. probe_source：401/403 → WebAuthError（DoD：不进入后续步骤）；200 通过
  6. fetch_page：带回 ETag / Last-Modified（WU-M-14 增量输入）
  7. 会话构造：basic/bearer/cookie 三形态；声明鉴权但无凭据 fail-closed
"""
from __future__ import annotations

import json
import uuid

import httpx
import pytest

from app.models import SyncSource
from app.services.crypto import encrypt_credential
from app.services.sync import web_fetcher as wf

BASE = "https://wiki.internal"


def _web_source(**overrides) -> SyncSource:
    """构造内存态 web 源（不落库，fetcher 只读属性）。"""
    defaults: dict = {
        "tenant_id": uuid.uuid4(),
        "kb_id": uuid.uuid4(),
        "name": "内网 wiki",
        "source_type": "web",
        "path": BASE,
        "file_patterns": "*.md",
        "level_rank": 20,
        "status": "active",
        "synced_files": {},
        "auth_type": "none",
        "auth_payload": None,
        "entry_urls": [f"{BASE}/docs/"],
        "scope_rules": {},
    }
    defaults.update(overrides)
    return SyncSource(**defaults)


# ── 白名单（INV-M-06）─────────────────────────────────────────────


def test_allowed_domain_passes_for_entry_host():
    wf._assert_allowed_domain(f"{BASE}/docs/a.html", wf.build_allowed_hosts([f"{BASE}/docs/"]))


def test_blocked_for_foreign_host():
    allowed = wf.build_allowed_hosts([f"{BASE}/docs/"])
    with pytest.raises(wf.WebFetchBlocked, match="白名单"):
        wf._assert_allowed_domain("https://evil.com/docs/a.html", allowed)


def test_blocked_for_lookalike_substring_host():
    # 子串伪装域（evil-wiki.internal）绝不能匹配 wiki.internal
    allowed = wf.build_allowed_hosts([f"{BASE}/docs/"])
    with pytest.raises(wf.WebFetchBlocked):
        wf._assert_allowed_domain("https://evil-wiki.internal/docs/", allowed)


def test_blocked_for_userinfo_url():
    allowed = wf.build_allowed_hosts([f"{BASE}/docs/"])
    with pytest.raises(wf.WebFetchError, match="用户信息"):
        wf._assert_allowed_domain("https://admin:pw@wiki.internal/docs/", allowed)


def test_blocked_for_non_http_scheme():
    allowed = wf.build_allowed_hosts([f"{BASE}/docs/"])
    with pytest.raises(wf.WebFetchError, match="scheme"):
        wf._assert_allowed_domain("file:///etc/passwd", allowed)


def test_port_not_part_of_whitelist():
    # 端口不参与比对：同一 host 换端口仍在白名单内（hostname 精确匹配口径）
    allowed = wf.build_allowed_hosts([f"{BASE}:8443/docs/"])
    wf._assert_allowed_domain(f"{BASE}/docs/a.html", allowed)


# ── scope_rules ──────────────────────────────────────────────────


def test_scope_rules_allow_and_deny():
    rules = {"allow_prefixes": ["/docs/"], "deny_prefixes": ["/docs/internal/"]}
    assert wf._passes_scope_rules("/docs/guide.html", rules) is True
    assert wf._passes_scope_rules("/blog/post.html", rules) is False
    assert wf._passes_scope_rules("/docs/internal/hr.html", rules) is False


def test_scope_rules_empty_allows_all():
    assert wf._passes_scope_rules("/any/path", {}) is True


# ── 会话与凭据 ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_open_web_session_basic_auth():
    source = _web_source(
        auth_type="basic",
        auth_payload=encrypt_credential(json.dumps({"username": "u", "password": "p"})),
    )
    client = await wf.open_web_session(source)
    try:
        assert client.auth is not None, "basic 鉴权应构造 httpx.BasicAuth"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_open_web_session_bearer_and_cookie_headers():
    bearer = _web_source(
        auth_type="bearer",
        auth_payload=encrypt_credential(json.dumps({"token": "tok-1"})),
    )
    c1 = await wf.open_web_session(bearer)
    try:
        assert c1.headers["Authorization"] == "Bearer tok-1"
    finally:
        await c1.aclose()

    cookie = _web_source(
        auth_type="cookie",
        auth_payload=encrypt_credential(json.dumps({"name": "sid", "value": "xyz"})),
    )
    c2 = await wf.open_web_session(cookie)
    try:
        assert c2.headers["Cookie"] == "sid=xyz"
    finally:
        await c2.aclose()


@pytest.mark.asyncio
async def test_open_web_session_fails_closed_without_payload():
    # 声明了鉴权却无凭据：必须报错而不是裸连（fail-closed）
    source = _web_source(auth_type="bearer", auth_payload=None)
    with pytest.raises(wf.WebFetchError, match="未配置凭据"):
        await wf.open_web_session(source)


# ── 探活（DoD：鉴权失败不进入后续步骤）────────────────────────────


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


@pytest.mark.asyncio
async def test_probe_401_raises_web_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    async with _mock_client(handler) as client:
        with pytest.raises(wf.WebAuthError, match="鉴权失败"):
            await wf.probe_source(client, _web_source())


@pytest.mark.asyncio
async def test_probe_403_raises_web_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    async with _mock_client(handler) as client:
        with pytest.raises(wf.WebAuthError):
            await wf.probe_source(client, _web_source())


@pytest.mark.asyncio
async def test_probe_200_passes():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body>ok</body></html>")

    async with _mock_client(handler) as client:
        await wf.probe_source(client, _web_source())


# ── 发现（BFS + 白名单 + 截断）────────────────────────────────────


@pytest.mark.asyncio
async def test_discover_keeps_inscope_links_and_drops_external(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(wf.get_settings(), "web_fetch_interval_ms", 0)  # 测试不限速

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=(
            "<html><body>"
            '<a href="guide.html">'          # 相对链接 → 补全
            '<a href="/docs/api.html">'      # 绝对路径
            '<a href="https://evil.com/x">'  # 外域 → 丢弃
            '<a href="/blog/post.html">'     # 白名单内但超出 allow_prefixes → 丢弃
            "</body></html>"
        ))

    source = _web_source(scope_rules={"allow_prefixes": ["/docs/"]})
    async with _mock_client(handler) as client:
        pages = await wf.discover_pages(client, source)

    # 入口 + 相对链接补全的 guide.html + 绝对路径 api.html；
    # 外域（evil.com）与白名单内但超出 allow_prefixes 的 /blog/ 必须丢弃
    assert set(pages) == {f"{BASE}/docs/", f"{BASE}/docs/guide.html", f"{BASE}/docs/api.html"}
    assert all("evil.com" not in p and "/blog/" not in p for p in pages)


@pytest.mark.asyncio
async def test_discover_bfs_multi_level(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(wf.get_settings(), "web_fetch_interval_ms", 0)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/docs/":
            body = '<a href="/docs/page-1.html"><a href="/docs/page-2.html">'
        else:
            body = "<a href='/docs/deep.html'>"  # 单引号形态
        return httpx.Response(200, text=body)

    source = _web_source(scope_rules={"allow_prefixes": ["/docs/"]})
    async with _mock_client(handler) as client:
        pages = await wf.discover_pages(client, source)

    assert set(pages) == {
        f"{BASE}/docs/", f"{BASE}/docs/page-1.html",
        f"{BASE}/docs/page-2.html", f"{BASE}/docs/deep.html",
    }


@pytest.mark.asyncio
async def test_discover_hard_limit(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(wf.get_settings(), "web_fetch_interval_ms", 0)
    monkeypatch.setattr(wf.get_settings(), "web_max_pages_per_sync", 3)

    def handler(request: httpx.Request) -> httpx.Response:
        n = int(request.url.path.rsplit("-", 1)[-1].split(".")[0]) if "page" in request.url.path else 0
        return httpx.Response(200, text=f'<a href="/docs/page-{n + 1}.html"><a href="/docs/page-{n + 2}.html">')

    source = _web_source()
    async with _mock_client(handler) as client:
        pages = await wf.discover_pages(client, source)

    assert len(pages) <= 3, "页数硬上限必须生效"


# ── 抓取 ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_page_returns_increment_headers():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>body</html>", headers={
            "ETag": '"abc123"', "Last-Modified": "Thu, 16 Oct 2026 00:00:00 GMT",
        })

    allowed = wf.build_allowed_hosts([f"{BASE}/docs/"])
    async with _mock_client(handler) as client:
        result = await wf.fetch_page(client, f"{BASE}/docs/a.html", allowed)

    assert result.etag == '"abc123"'
    assert result.last_modified is not None
    assert result.content == b"<html>body</html>"


@pytest.mark.asyncio
async def test_fetch_page_blocked_outside_whitelist():
    allowed = wf.build_allowed_hosts([f"{BASE}/docs/"])
    async with _mock_client(lambda r: httpx.Response(200)) as client:
        with pytest.raises(wf.WebFetchBlocked):
            await wf.fetch_page(client, "https://evil.com/a.html", allowed)
