"""嵌入服务 —— OpenAI 兼容协议调 bge-m3（手册 §6 M2 任务 5 + §8 D-05）。

端点：POST {EMBEDDING_BASE_URL}/embeddings（BASE_URL 含 /v1 版本前缀）
维度：MUST == settings.embedding_dim（默认 1024，与 chunks.embedding vector(1024) 对齐）
批量：按 settings.embedding_batch_size 分批，超限会被模型静默截断

★ 不做 fallback：服务挂了直接抛错，让 worker 重试（手册 §3.4 故障表里
  embedding 失败不在"降级"列表里，属于硬失败）。
"""
from __future__ import annotations

import httpx

from app.config.settings import get_settings


class EmbeddingError(Exception):
    """嵌入调用失败。"""


class EmbeddingDimensionError(EmbeddingError):
    """返回维度与 chunks.embedding 维度不一致——会破坏 HNSW 索引。"""


class EmbeddingCountError(EmbeddingError):
    """返回向量条数与输入文本条数不一致（模型静默截断/跳过输入）。"""


class EmbeddingService:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """批量嵌入。返回向量列表，顺序与输入一致。"""
        if not texts:
            return []
        settings = get_settings()
        if not settings.embedding_base_url:
            raise EmbeddingError("EMBEDDING_BASE_URL 未配置")
        batch_size = settings.embedding_batch_size
        results: list[list[float]] = []
        async with httpx.AsyncClient(timeout=60) as client:
            for start in range(0, len(texts), batch_size):
                batch = texts[start : start + batch_size]
                response = await client.post(
                    f"{settings.embedding_base_url.rstrip('/').removesuffix('/v1')}/v1/embeddings",
                    json={"model": settings.embedding_model, "input": batch},
                )
                if response.status_code != 200:
                    raise EmbeddingError(
                        f"embedding 返回 {response.status_code}: {response.text[:200]}"
                    )
                payload = response.json()
                # 按 index 排序，保证顺序
                data = sorted(payload["data"], key=lambda item: item["index"])
                # 条数校验：模型可能静默截断/跳过输入，少返回会让上层 zip(strict=True)
                # 抛含糊的 ValueError；在此提前给出可定位的明确错误
                if len(data) != len(batch):
                    raise EmbeddingCountError(
                        f"embedding 批次返回 {len(data)} 条向量，期望 {len(batch)} 条"
                        f"（批次起始 start={start}，模型可能截断或跳过输入）"
                    )
                for item in data:
                    vec = item["embedding"]
                    if len(vec) != settings.embedding_dim:
                        raise EmbeddingDimensionError(
                            f"embedding 维度 {len(vec)} != 配置 {settings.embedding_dim}；"
                            "改维度必须重灌全库（手册 §8 D-05）"
                        )
                    results.append(vec)
        return results

    async def embed_query(self, query: str) -> list[float]:
        """单条查询嵌入。"""
        result = await self.embed([query])
        return result[0]


def get_embedding_service() -> EmbeddingService:
    return EmbeddingService()
