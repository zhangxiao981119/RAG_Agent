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
    # ── 密钥/密码：生产 MUST 通过环境变量覆盖，dev 可用空字符串（seed 自动生成） ──
    secret_key: str | None = None  # RSA 私钥 PEM 路径或内容；None 时 seed_prod 自动生成
    tenant_code: str = "default"
    # ── M3 JWT 认证 ──────────────────────────────────────────
    jwt_secret: str | None = None  # JWT 签名密钥；生产 MUST 显式注入，dev 自动生成随机值
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
    s3_access_key: str | None = None  # MinIO/S3 access key；None 时 seed_prod 自动生成
    s3_secret_key: str | None = None  # MinIO/S3 secret key；None 时 seed_prod 自动生成
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
    # ── M5 生产初始化 ────────────────────────────────────────
    admin_password: str | None = None  # seed_prod 创建管理员时使用；None 时自动生成随机密码
    # ── M2 模型调用 ──────────────────────────────────────────
    embedding_batch_size: int = 32
    rerank_timeout_seconds: float = 3.0
    llm_first_token_timeout_seconds: float = 15.0
    llm_total_timeout_seconds: float = 60.0
    # ── M2 异步任务 ──────────────────────────────────────────
    arq_queue_name: str = "arq:parse"
    arq_max_attempts: int = 3
    parse_job_timeout_seconds: int = 600
    # ── M6 安全加固 ──────────────────────────────────────────
    chat_rate_limit_per_minute: int = 20  # 单用户每分钟最多提问次数，超过返回 429
    # ── M6 续篇：配额管理（手册第 14 步 — 双层速率+总量）─────
    default_daily_token_limit: int = 200_000       # 单用户每日默认 token 上限
    default_daily_message_limit: int = 200         # 单用户每日默认问答次数
    default_tenant_daily_token_limit: int = 2_000_000      # 租户每日 token 上限
    default_tenant_monthly_token_limit: int = 50_000_000  # 租户月度 token 上限

    @field_validator("allowed_ext", mode="before")
    @classmethod
    def parse_allowed_ext(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    def model_post_init(self, __context) -> None:
        """生产环境启动时强制校验关键密钥。

        在 model_post_init 里才能拿到所有字段值（app_env + jwt_secret 等）。
        dev 环境 jwt_secret 允许 None（auth.py 运行时自动生成随机值）。
        prod 环境必须显式注入，否则立即阻止启动。
        """
        if self.app_env == "prod":
            missing = []
            if not self.jwt_secret:
                missing.append("JWT_SECRET")
            if not self.secret_key:
                missing.append("SECRET_KEY")
            if missing:
                raise ValueError(
                    f"生产环境必须显式注入: {', '.join(missing)}（当前值为 None/空）"
                )


@lru_cache
def get_settings() -> Settings:
    return Settings()
