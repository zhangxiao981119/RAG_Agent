"""M3 RSA 应用层加密 —— 密码密文传输。

多 worker 安全方案：
  密钥对存储在 Redis（key = "auth:rsa_private_key"，值为 PEM）。
  首次调用时从 Redis 读取，读不到则生成并用 SETNX 写入（多 worker 竞态下只有一个赢家），
  然后再 GET 一次确保所有 worker 使用同一份密钥。

后端持有 RSA 私钥，公钥通过 /api/auth/public-key 下发给前端。
前端用 Web Crypto 以 RSA-OAEP(SHA-256) 加密密码，后端私钥解密后再走 bcrypt 校验。
"""
from __future__ import annotations

import base64
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from redis import Redis

from app.config.settings import get_settings

_KEY_SIZE = 2048
_REDIS_KEY = "auth:rsa_private_key"

# 模块级缓存：每个 worker 进程内只从 Redis 加载一次
_private_key: rsa.RSAPrivateKey | None = None


def _ensure_key() -> rsa.RSAPrivateKey:
    """懒加载私钥：从 Redis 读取，不存在则生成并 SETNX，最终保证拿到同一份密钥。"""
    global _private_key
    if _private_key is not None:
        return _private_key

    settings = get_settings()
    redis: Redis = Redis.from_url(settings.redis_url)
    try:
        pem = redis.get(_REDIS_KEY)
        if pem is None:
            # 本 worker 率先启动，生成密钥并尝试 SETNX
            new_key = rsa.generate_private_key(public_exponent=65537, key_size=_KEY_SIZE)
            new_pem = new_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
            # SETNX：多 worker 同时启动时只有一个成功
            redis.set(_REDIS_KEY, new_pem, nx=True)
            # 无论本 worker 是否成功写入，都 GET 一次确保用 Redis 里那份
            pem = redis.get(_REDIS_KEY)

        if pem is None:
            raise RuntimeError("RSA 私钥初始化失败：Redis 读不到密钥")

        _private_key = serialization.load_pem_private_key(pem, password=None)
        return _private_key
    finally:
        redis.close()


def get_public_key_spki_b64() -> str:
    """导出公钥为 SPKI DER 格式并 base64 编码，供前端 Web Crypto importKey 使用。"""
    key = _ensure_key()
    public_key = key.public_key()
    der = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return base64.b64encode(der).decode("ascii")


def decrypt_password(ciphertext_b64: str) -> str:
    """用私钥解密前端 RSA-OAEP(SHA-256) 加密的密码，返回明文。"""
    key = _ensure_key()
    ciphertext = base64.b64decode(ciphertext_b64)
    plaintext = key.decrypt(
        ciphertext,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return plaintext.decode("utf-8")


def clear_key_cache() -> None:
    """清除进程内的密钥缓存（测试用）。"""
    global _private_key
    _private_key = None


__all__: list[str] = ["get_public_key_spki_b64", "decrypt_password", "clear_key_cache"]
