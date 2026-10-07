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
    redis_max_connections: int = 20  # 单进程连接池上限；× 副本数应 ≤ Redis maxclients 的余量
    redis_socket_timeout_s: float = 3.0  # 建连/读写超时（秒）—— 避免 Redis 抖动时请求无限等待
    s3_endpoint: str = "http://minio:9000"
    s3_access_key: str | None = None  # MinIO/S3 access key；None 时 seed_prod 自动生成
    s3_secret_key: str | None = None  # MinIO/S3 secret key；None 时 seed_prod 自动生成
    s3_bucket: str = "kagent-docs"
    llm_base_url: str | None = None
    llm_api_key: str = ""
    # ★ 模型 ID 会退役，别把它当稳定常量：
    #   `deepseek-chat` / `deepseek-reasoner` 已于 2026-07-24 15:59 UTC **永久停服**
    #   （调用直接返回 HTTP 错误，无宽限期）。官方现役 ID 只有 deepseek-v4-flash
    #   与 deepseek-v4-pro；旧别名等价迁到 v4-flash（**同价**，迁到 v4-pro 贵约 3.1 倍）。
    #   ⚠ 换模型时 MUST 同步核对 decisions.MODEL_CONTEXT_WINDOWS —— 启动会校验窗口。
    llm_model: str = "deepseek-v4-flash"
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
    # ★ 单批条数 MUST 与该批在服务端的实际计算时间匹配（embedding.py 的客户端超时 60s）。
    #   实测（CPU 版 bge-m3，约 217 token/s）：32 条真实 chunk（avg 517~589 token）单批
    #   需要 75s+，直接撞 60s 超时 → ReadTimeout → 重试 3 次进死信；8 条约 19s，余量充足。
    #   同一结论见 scripts/reindex_embeddings.py 的 --batch 默认值。
    embedding_batch_size: int = 8
    rerank_timeout_seconds: float = 3.0
    llm_first_token_timeout_seconds: float = 15.0
    llm_total_timeout_seconds: float = 60.0
    # ★ 单次生成的最大输出 token 数。MUST <= decisions.CONTEXT_OUTPUT_RESERVE_TOKENS
    #   —— 后者是从上下文窗口里**预先扣掉**的那部分；两者不一致（预留 4K 却允许多写
    #   8K）会让"预留"名不副实，prompt 体积 + 输出就越过了模型窗口。
    llm_max_output_tokens: int = 4_000
    # ── M2 异步任务 ──────────────────────────────────────────
    arq_queue_name: str = "arq:parse"
    arq_max_attempts: int = 3
    # ★ 单任务最长运行时间，由 workers/parse_job.py 的 WorkerSettings.job_timeout 消费。
    #   arq 自带默认值是 300s，对长文档不够：实测一本 283 页 PDF（482 chunks）按批 8
    #   需要 61 批 ≈ 12 分钟纯嵌入，300s 会在中途把任务杀掉。取 1800s 覆盖该规模。
    parse_job_timeout_seconds: int = 1800
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
