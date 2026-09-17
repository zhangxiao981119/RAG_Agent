"""M3 认证 API schema —— 登录 / refresh / 登出 请求响应。"""
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
    """登录成功响应：access + refresh token + 用户基本信息。

    access token 用于业务 API 鉴权（24h）；refresh token 仅用于 /api/auth/refresh
    换新 token（7d）。前端需同时存储两个 token。
    """

    token: str
    refresh_token: str
    token_type: str = "Bearer"
    user: UserInfo


class RefreshRequest(BaseModel):
    """POST /api/auth/refresh 请求体：传入 refresh token。"""

    refresh_token: str = Field(min_length=1)


class RefreshResponse(BaseModel):
    """refresh 成功响应：新的 access + refresh token（rotation）。"""

    token: str
    refresh_token: str
    token_type: str = "Bearer"


class LogoutRequest(BaseModel):
    """POST /api/auth/logout 请求体（可选传 refresh token 一并吊销）。"""

    refresh_token: str | None = Field(default=None, min_length=1)


class LogoutResponse(BaseModel):
    """登出响应。"""

    revoked: bool = True


class MeResponse(BaseModel):
    """GET /api/me 响应：当前用户主体解析结果（M3 任务 2）。

    `subjects` 已含 §3.2.3 双向展开（祖先 ∪ 自己 ∪ 子孙）；
    `authorized_kb_ids` 含公开库自动成员（§3.2.7）；
    `acl_epoch` 为当前权限缓存版本号，调试用。
    """

    user_id: uuid.UUID
    username: str
    display_name: str
    dept_path: str
    clearance: int
    subjects: list[str]
    authorized_kb_ids: list[uuid.UUID]
    acl_epoch: int
