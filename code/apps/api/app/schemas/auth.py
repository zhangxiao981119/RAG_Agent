"""M3 认证 API schema —— 登录请求/响应。"""
from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    """POST /api/auth/login 请求体。

    password 为前端用 RSA-OAEP(SHA-256) 加密后的 base64 密文，非明文。
    """

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=512)  # RSA 2048 密文 base64 约 344 字符


class PublicKeyResponse(BaseModel):
    """GET /api/auth/public-key 响应：SPKI DER 格式公钥的 base64 编码。"""

    public_key: str


class UserInfo(BaseModel):
    """登录响应中的用户信息（前端展示用）。"""

    id: uuid.UUID
    display_name: str
    dept_path: str
    clearance: int


class LoginResponse(BaseModel):
    """登录成功响应：JWT + 用户基本信息。"""

    token: str
    token_type: str = "Bearer"
    user: UserInfo
