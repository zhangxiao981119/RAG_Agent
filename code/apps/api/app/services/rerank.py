"""重排服务 —— OpenAI 兼容 /v1/rerank 协议调 bge-reranker-v2-m3（手册 §3.3.2 ④ + §3.4 故障 2）。

端点：POST {RERANK_BASE_URL}/rerank（BASE_URL 含 /v1 版本前缀）
超时：3s（手册 §3.4 故障 2，超时则跳过重排用 RRF 分当阈值分）

★ 跳过重排是手册明确允许的降级，不算 fallback——是规格的一部分。
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from app.config.settings import get_settings

logger = logging.getLogger(__name__)


class RerankTimeout(Exception):
    """重排超时。调用方应跳过重排。"""


class RerankService:
    async def rerank(
        self, query: str, documents: list[str], top_n: int
    ) -> list[tuple[int, float]]:
        """重排。返回 [(原 index, 分数)]，已按分数降序。

        超时或调用失败 → 抛 RerankTimeout / RerankError，调用方降级。
        """
        settings = get_settings()
        if not settings.rerank_base_url:
            raise RerankTimeout("RERANK_BASE_URL 未配置，跳过重排")
        if not documents:
            return []
        try:
            async with httpx.AsyncClient(timeout=settings.rerank_timeout_seconds) as client:
                response = await client.post(
                    f"{settings.rerank_base_url.rstrip('/').rstrip('/v1')}/v1/rerank",
                    json={
                        "model": settings.rerank_model,
                        "query": query,
                        "documents": documents,
                        "top_n": top_n,
                    },
                )
                if response.status_code != 200:
                    logger.warning(
                        "rerank 返回 %s: %s，跳过重排",
                        response.status_code,
                        response.text[:200],
                    )
                    raise RerankTimeout(f"rerank 状态码 {response.status_code}")
                payload = response.json()
                results = payload.get("results", [])
                return [(item["index"], float(item["relevance_score"])) for item in results]
        except (httpx.TimeoutException, asyncio.TimeoutError) as exc:
            logger.warning("rerank 超时，跳过重排: %s", exc)
            raise RerankTimeout("rerank 超时") from exc
        except httpx.HTTPError as exc:
            # 容器挂了/DNS 失败/连接拒绝等，也降级跳过重排
            logger.warning("rerank 连接失败，跳过重排: %s", exc)
            raise RerankTimeout("rerank 不可用") from exc


def get_rerank_service() -> RerankService:
    return RerankService()
