"""M7 WU-M-12：web 源抓取器 —— 探活 / 发现 / 抓取。

三层职责（对应规范 §4.2 WU-M-12）：
  1. open_web_session —— 按 auth_type（basic/bearer/cookie）建立 httpx.AsyncClient
  2. probe_source     —— 探活入口页；401/403 抛 WebAuthError（DoD：鉴权失败不进入后续步骤）
  3. discover_pages   —— 从 entry_urls 出发按 scope_rules 枚举页面（BFS，限速 + 页数硬上限）
  4. fetch_page       —— 抓取单页正文字节，带回 etag / last-modified（WU-M-14 增量判定输入）

不变量：
  - INV-M-06：出站域名白名单 —— 所有请求 URL 必须通过 _assert_allowed_domain
    （白名单 = entry_urls 的 host 集合，源配置即白名单），单点强制检查
  - INV-M-08：限速与并发可配，默认保守（settings.web_fetch_* 四项）
  - INV-M-07：凭据只在本模块内存中还原使用，MUST NOT 写日志
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx

from app.config.settings import get_settings
from app.models import SyncSource

# 白名单只接受 web scheme；file://gopher:// 等一律拒绝
_ALLOWED_SCHEMES = ("http", "https")

# <a href="..."> 提取：属性值双/单引号两种形态（WU-M-13 的 bs4 才做正文清洗，
# 发现阶段只需要链接，不值得为此引入解析器依赖）
_HREF_RE = re.compile(r"""<a\s[^>]*?href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


class WebFetchError(Exception):
    """web 抓取失败基类（探活失败 / 白名单拦截 / 非 2xx）。"""


class WebAuthError(WebFetchError):
    """鉴权失败（401/403）—— sync_service 捕获后记 last_error 并中断，不进入发现/抓取。"""


class WebFetchBlocked(WebFetchError):
    """URL 不在源白名单内（INV-M-06 拦截）。"""


# ── 白名单（INV-M-06 单点）────────────────────────────────────────


def _extract_host(url: str) -> str:
    """提取并归一化 URL 的 hostname（小写）；无 host 或 scheme 非法即抛错。"""
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise WebFetchError(f"不支持的 URL scheme: {parsed.scheme or '缺失'}（仅 http/https）")
    # 带用户信息的 URL（user:pass@host）在日志与白名单比对上都危险，直接拒绝
    if parsed.username or parsed.password:
        raise WebFetchError("URL 不得携带用户信息（user:pass@host 形态）")
    host = (parsed.hostname or "").lower()
    if not host:
        raise WebFetchError(f"URL 缺少 host: {url}")
    return host


def build_allowed_hosts(entry_urls: list[str]) -> set[str]:
    """白名单 = 源配置 entry_urls 的 host 集合（小写 hostname，不含端口）。"""
    return {_extract_host(u) for u in entry_urls}


def _assert_allowed_domain(url: str, allowed_hosts: set[str]) -> None:
    """INV-M-06 强制检查点：目标 host 不在白名单内 MUST 拒绝（防 SSRF / 出域抓取）。

    注意比较的是精确 hostname（evil-wiki.internal 不会误匹配 wiki.internal）。
    """
    host = _extract_host(url)
    if host not in allowed_hosts:
        raise WebFetchBlocked(f"URL 域名不在源白名单内: {host}")


# ── 会话与凭据（INV-M-07：凭据只内存使用，不写日志）────────────────


def _build_auth(source: SyncSource, decrypted_payload: dict[str, str]) -> httpx.Auth | dict[str, str]:
    """按 auth_type 构造 httpx 认证或请求头。payload 结构由 WU-M-11 schema 约定。"""
    if source.auth_type == "basic":
        return httpx.BasicAuth(decrypted_payload["username"], decrypted_payload["password"])
    if source.auth_type == "bearer":
        return {"Authorization": f"Bearer {decrypted_payload['token']}"}
    if source.auth_type == "cookie":
        return {"Cookie": f"{decrypted_payload['name']}={decrypted_payload['value']}"}
    return {}


async def open_web_session(source: SyncSource) -> httpx.AsyncClient:
    """按源配置建立会话。凭据经 crypto 解密进内存；调用方负责 aclose()。"""
    headers: dict[str, str] = {}
    auth: httpx.Auth | None = None
    if source.auth_type != "none":
        payload = _require_auth_payload(source)
        built = _build_auth(source, payload)
        if isinstance(built, dict):
            headers.update(built)
        else:
            auth = built

    settings = get_settings()
    return httpx.AsyncClient(
        timeout=settings.web_fetch_timeout_s,
        follow_redirects=True,
        auth=auth,
        headers=headers,
    )


def _require_auth_payload(source: SyncSource) -> dict[str, str]:
    """解密 auth_payload；源声明了鉴权但没有凭据 → 明确报错（fail-closed，不裸连）。"""
    import json

    from app.services.crypto import decrypt_credential

    if not source.auth_payload:
        raise WebFetchError(f"源声明 auth_type={source.auth_type} 但未配置凭据")
    payload = json.loads(decrypt_credential(source.auth_payload))
    if not isinstance(payload, dict) or not payload:
        raise WebFetchError("凭据 payload 必须是非空 JSON 对象")
    return payload


# ── 探活 / 发现 / 抓取 ────────────────────────────────────────────


async def probe_source(client: httpx.AsyncClient, source: SyncSource) -> None:
    """探活：GET 入口页。401/403 → WebAuthError（DoD：鉴权失败不进入后续步骤）。"""
    allowed = build_allowed_hosts(list(source.entry_urls or []))
    target = list(source.entry_urls or [source.path])[0]
    _assert_allowed_domain(target, allowed)

    resp = await client.get(target)
    if resp.status_code in (401, 403):
        raise WebAuthError(f"入口页鉴权失败（HTTP {resp.status_code}）: {target}")
    if resp.status_code >= 400:
        raise WebFetchError(f"入口页探活失败（HTTP {resp.status_code}）: {target}")


def _passes_scope_rules(url_path: str, scope_rules: dict) -> bool:
    """路径级范围规则：allow_prefixes 非空时必须命中其一；deny_prefixes 命中即排除。"""
    deny = list(scope_rules.get("deny_prefixes") or [])
    if any(url_path.startswith(p) for p in deny):
        return False
    allow = list(scope_rules.get("allow_prefixes") or [])
    if allow and not any(url_path.startswith(p) for p in allow):
        return False
    return True


@dataclass
class _Page:
    url: str
    content: str


async def _get_page(client: httpx.AsyncClient, url: str) -> _Page:
    resp = await client.get(url)
    if resp.status_code >= 400:
        raise WebFetchError(f"页面抓取失败（HTTP {resp.status_code}）: {url}")
    return _Page(url=url, content=resp.text)


async def discover_pages(client: httpx.AsyncClient, source: SyncSource) -> list[str]:
    """从 entry_urls 出发 BFS 枚举站内页面。

    约束（INV-M-06 / INV-M-08）：
      - 每个待访问 URL 都过域名白名单（发现层 + 访问层双重校验）
      - scope_rules 的 allow/deny 前缀过滤路径
      - 相邻请求 sleep(web_fetch_interval_ms) 限速；页数达 web_max_pages_per_sync 硬截断
    """
    settings = get_settings()
    allowed = build_allowed_hosts(list(source.entry_urls or []))
    scope_rules = dict(source.scope_rules or {})

    queue: list[str] = []
    for entry in list(source.entry_urls or [source.path]):
        _assert_allowed_domain(entry, allowed)
        queue.append(entry)

    visited: set[str] = set()
    result: list[str] = []
    interval_s = settings.web_fetch_interval_ms / 1000

    while queue and len(result) < settings.web_max_pages_per_sync:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)

        # 访问层再校验一次白名单（INV-M-06：不信任队列内容，只信任函数出口）
        _assert_allowed_domain(url, allowed)
        await asyncio.sleep(interval_s)
        page = await _get_page(client, url)
        result.append(page.url)

        # 提取链接入队：相对路径补全 → 白名单 + scope 双过滤
        for href in _HREF_RE.findall(page.content):
            absolute = urljoin(page.url, href.split("#", 1)[0])
            try:
                _assert_allowed_domain(absolute, allowed)
            except WebFetchError:
                continue  # 外域 / 非法 URL：发现层直接丢弃，不算错误
            parsed = urlparse(absolute)
            if not _passes_scope_rules(parsed.path or "/", scope_rules):
                continue
            if absolute not in visited and absolute not in queue:
                queue.append(absolute)

    return result


@dataclass
class FetchResult:
    """单页抓取结果：正文字节 + 增量判定头（WU-M-14 消费）。"""

    url: str
    content: bytes
    etag: str | None = None
    last_modified: str | None = None


async def fetch_page(client: httpx.AsyncClient, url: str, allowed_hosts: set[str]) -> FetchResult:
    """抓取单页。INV-M-06：入口在函数内强制过白名单。"""
    _assert_allowed_domain(url, allowed_hosts)
    resp = await client.get(url)
    if resp.status_code >= 400:
        raise WebFetchError(f"页面抓取失败（HTTP {resp.status_code}）: {url}")
    return FetchResult(
        url=url,
        content=resp.content,
        etag=resp.headers.get("ETag"),
        last_modified=resp.headers.get("Last-Modified"),
    )
