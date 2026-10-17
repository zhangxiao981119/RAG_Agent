"""M6 任务 1：知识源同步 — pydantic schema（§8 D-07）；M7 WU-M-11 增补 web 源。"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, model_validator

# web 源鉴权方式（迁移 0017 auth_type CHECK 同口径）
AUTH_TYPES = ("none", "basic", "bearer", "cookie")


class SyncSourceCreate(BaseModel):
    """创建同步源。

    web 源凭据（auth）以明文进请求体、加密落库（INV-M-07）：
    响应中永远不回显（Out 只给 has_auth 布尔），日志只记 auth_type。
    """

    name: str = Field(min_length=1, max_length=100)
    source_type: str = Field(default="local", pattern="^(local|git|web)$")
    # local: /data/sync 下的子目录名；git: 仓库 URL；web: 主入口 URL
    path: str = Field(min_length=1, max_length=500)
    branch: str | None = None  # 仅 git 类型
    file_patterns: str = Field(default="*.md,*.pdf,*.txt,*.docx")
    level_rank: int = Field(default=20, ge=1, le=40)
    # ── web 源（M7 WU-M-11）──
    auth_type: str = Field(default="none", pattern="^(none|basic|bearer|cookie)$")
    # 凭据内容：basic={"username","password"} / bearer={"token"} / cookie={"name","value"}
    # 仅 web 源可给；序列化为 JSON 后经 crypto 加密落库
    auth: dict[str, str] | None = None
    # 抓取入口 URL 列表（web 源必填 ≥1 条）
    entry_urls: list[str] = []
    # 抓取范围规则：{"allow_prefixes": ["/docs/"], "deny_prefixes": ["/api/"]}
    scope_rules: dict[str, list[str]] = {}

    @model_validator(mode="after")
    def _web_source_consistency(self) -> SyncSourceCreate:
        if self.source_type == "web":
            if not self.entry_urls:
                raise ValueError("web 源必须提供至少一个 entry_url")
            if self.auth_type != "none" and not self.auth:
                raise ValueError("auth_type 非 none 时必须提供 auth 凭据")
        else:
            # 非 web 源不得携带 web 专属字段（显式信号，不猜）
            if self.auth_type != "none" or self.auth or self.entry_urls or self.scope_rules:
                raise ValueError("仅 web 源可配置 auth_type/auth/entry_urls/scope_rules")
        return self


class SyncSourceUpdate(BaseModel):
    """更新同步源（部分字段）。

    auth 语义：None = 不动现有凭据；clear_auth=true = 清除凭据。
    不提供"传空 dict 清除"的隐式约定 —— 清除必须是显式动作。
    """

    name: str | None = Field(default=None, min_length=1, max_length=100)
    file_patterns: str | None = None
    level_rank: int | None = Field(default=None, ge=1, le=40)
    status: str | None = Field(default=None, pattern="^(active|paused)$")
    # ── web 源（M7 WU-M-11）──
    auth_type: str | None = Field(default=None, pattern="^(none|basic|bearer|cookie)$")
    auth: dict[str, str] | None = None
    entry_urls: list[str] | None = None
    scope_rules: dict[str, list[str]] | None = None
    clear_auth: bool = False


class SyncSourceOut(BaseModel):
    """同步源详情。凭据永远不出接口：只有 has_auth 布尔（INV-M-07）。"""

    id: uuid.UUID
    kb_id: uuid.UUID
    name: str
    source_type: str
    path: str
    branch: str | None = None
    file_patterns: str
    level_rank: int
    status: str
    last_synced_at: datetime | None = None
    last_sync_count: int = 0
    last_error: str | None = None
    synced_file_count: int = 0  # 已同步的文件数
    created_at: datetime
    updated_at: datetime
    # ── web 源（M7 WU-M-11）──
    auth_type: str = "none"
    has_auth: bool = False
    entry_urls: list[str] = []
    scope_rules: dict[str, list[str]] = {}


class SyncResult(BaseModel):
    """手动触发同步的返回。"""

    source_id: uuid.UUID
    new_count: int
    message: str
