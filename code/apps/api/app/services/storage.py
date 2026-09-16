"""对象存储服务 —— MinIO/S3 异步访问（手册 §5.1 上传链路）。

职责：
  · 启动时确保 bucket 存在
  · API 层接收 multipart 后 put_object 落 MinIO
  · 解析 worker 通过 get_object 拉回字节流
  · 文档软删时清理对象

MUST NOT 在本模块做业务校验（扩展名 / 大小 / 权限）—— 那些在 API 层。
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import aioboto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.config.settings import get_settings


class StorageService:
    """MinIO/S3 异步客户端。每次调用创建独立 client，避免跨事件循环复用。"""

    def __init__(self) -> None:
        self._settings = get_settings()

    def _session(self) -> aioboto3.Session:
        return aioboto3.Session()

    def _client_kwargs(self) -> dict[str, object]:
        settings = self._settings
        return {
            "endpoint_url": settings.s3_endpoint,
            "aws_access_key_id": settings.s3_access_key,
            "aws_secret_access_key": settings.s3_secret_key,
            "config": Config(
                # MinIO 用 path-style，S3 用 virtual-host；私有化默认 path-style
                s3={"addressing_style": "path"},
                retries={"max_attempts": 3, "mode": "standard"},
            ),
            "region_name": "us-east-1",
        }

    async def ensure_bucket(self) -> None:
        async with self._session().client("s3", **self._client_kwargs()) as client:
            try:
                await client.head_bucket(Bucket=self._settings.s3_bucket)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") == "404":
                    await client.create_bucket(Bucket=self._settings.s3_bucket)
                else:
                    raise

    async def put_object(self, key: str, data: bytes, content_type: str) -> None:
        async with self._session().client("s3", **self._client_kwargs()) as client:
            await client.put_object(
                Bucket=self._settings.s3_bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
            )

    async def get_object(self, key: str) -> bytes:
        async with self._session().client("s3", **self._client_kwargs()) as client:
            response = await client.get_object(Bucket=self._settings.s3_bucket, Key=key)
            # aioboto3 的 Body 是 aiohttp ClientResponse，直接 read() 取全部
            body = response["Body"]
            if hasattr(body, "read"):
                return await body.read()
            # 兜底：botocore StreamingBody
            return body.read()

    async def stream_object(self, key: str) -> AsyncIterator[bytes]:
        async with self._session().client("s3", **self._client_kwargs()) as client:
            response = await client.get_object(Bucket=self._settings.s3_bucket, Key=key)
            body = response["Body"]
            if hasattr(body, "read"):
                data = await body.read()
                yield data
            else:
                yield body.read()

    async def delete_object(self, key: str) -> None:
        async with self._session().client("s3", **self._client_kwargs()) as client:
            await client.delete_object(Bucket=self._settings.s3_bucket, Key=key)


_storage_singleton: StorageService | None = None
_storage_lock = asyncio.Lock()


async def get_storage() -> StorageService:
    """单例 + 懒初始化 ensure_bucket。启动后第一次调用完成 bucket 自检。"""
    global _storage_singleton
    if _storage_singleton is None:
        async with _storage_lock:
            if _storage_singleton is None:
                service = StorageService()
                await service.ensure_bucket()
                _storage_singleton = service
    return _storage_singleton
