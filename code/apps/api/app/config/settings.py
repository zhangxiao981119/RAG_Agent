from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[4]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "dev"
    secret_key: str = "change-me"
    tenant_code: str = "default"
    # ── M3 JWT 认证 ──────────────────────────────────────────
    jwt_secret: str = "change-me-jwt"
    jwt_expire_minutes: int = 1440  # access token 默认 24 小时
    jwt_refresh_expire_days: int = 7  # refresh token 默认 7 天
    # ── M3 登录安全策略 ──────────────────────────────────────
    bcrypt_rounds: int = 12  # bcrypt 哈希轮数，越大越慢越安全
    login_max_failures: int = 5  # 连续失败多少次后锁定账户
    login_lock_minutes: int = 15  # 账户锁定时长（分钟）
    login_rate_per_minute: int = 10  # 单 IP 每分钟最多登录尝试次数
    database_url: str = "postgresql+asyncpg://user:pass@postgres:5432/kagent"
    redis_url: str = "redis://redis:6379/0"
    s3_endpoint: str = "http://minio:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "kagent-docs"
    llm_base_url: str | None = None
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    embedding_base_url: str | None = None
    embedding_model: str = "bge-m3"
    embedding_dim: int = 1024
    rerank_base_url: str | None = None
    rerank_model: str = "bge-reranker-v2-m3"
    max_upload_mb: int = 100
    allowed_ext: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["pdf", "md", "txt", "xls", "xlsx", "docx"]
    )
    audit_retention_days: int = 365
    multi_tenant: bool = False
    # ── M2 模型调用 ──────────────────────────────────────────
    embedding_batch_size: int = 32
    rerank_timeout_seconds: float = 3.0
    llm_first_token_timeout_seconds: float = 15.0
    llm_total_timeout_seconds: float = 60.0
    # ── M2 异步任务 ──────────────────────────────────────────
    arq_queue_name: str = "arq:parse"
    arq_max_attempts: int = 3
    parse_job_timeout_seconds: int = 600

    @field_validator("allowed_ext", mode="before")
    @classmethod
    def parse_allowed_ext(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
