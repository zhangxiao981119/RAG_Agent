"""M3 RSA 应用层加密 —— 密码密文传输。

后端持有 RSA 私钥，公钥通过 /api/auth/public-key 下发给前端。
前端用 RSA-OAEP(SHA-256) 加密密码，后端私钥解密后再走 bcrypt 校验。
密钥对在进程启动时生成，存内存，不持久化（重启后公钥变化，前端实时获取）。
"""
from __future__ import annotations

import base64
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

_KEY_SIZE = 2048

# 模块级单例：进程启动时生成一次
_private_key: rsa.RSAPrivateKey = rsa.generate_private_key(public_exponent=65537, key_size=_KEY_SIZE)


def get_public_key_spki_b64() -> str:
    """导出公钥为 SPKI DER 格式并 base64 编码，供前端 Web Crypto importKey 直接使用。"""
    public_key = _private_key.public_key()
    der = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return base64.b64encode(der).decode("ascii")


def decrypt_password(ciphertext_b64: str) -> str:
    """用私钥解密前端 RSA-OAEP(SHA-256) 加密的密码，返回明文。"""
    ciphertext = base64.b64decode(ciphertext_b64)
    plaintext = _private_key.decrypt(
        ciphertext,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return plaintext.decode("utf-8")


__all__: list[str] = ["get_public_key_spki_b64", "decrypt_password"]
