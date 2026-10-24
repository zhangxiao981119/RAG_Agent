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
    # ── 日志 ─────────────────────────────────────────────────
    # 结构化 JSON 日志级别；DEBUG 可见检索/改写等细节，生产默认 INFO
    log_level: str = "INFO"
    # ── 密钥/密码：生产 MUST 通过环境变量覆盖，dev 可用空字符串（seed 自动生成） ──
    # 注：登录链路的应用层加密密钥对（RSA-OAEP）由 services/crypto.py 在 Redis 内部
    #     自动生成与持有（key = auth:rsa_private_key），不经环境变量注入。
    tenant_code: str = "default"
    # ── M3 JWT 认证 ──────────────────────────────────────────
    jwt_secret: str | None = None  # JWT 签名密钥；生产 MUST 显式注入，dev 自动生成随机值
    jwt_expire_minutes: int = 1440  # access token 默认 24 小时
    jwt_refresh_expire_days: int = 7  # refresh token 默认 7 天
    # ── M7 WU-M-11 同步源凭据存储态加密（INV-M-07）────────────
    # Fernet 密钥（urlsafe base64，32 字节）。生成：python -c
    #   "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # dev 留空 = 内置开发密钥；prod MUST 显式注入独立密钥（见 model_post_init）
    credential_encryption_key: str | None = None
    # ── M7 WU-M-12 web 抓取限速与并发（INV-M-08，默认保守）────
    web_fetch_timeout_s: int = 15  # 单请求超时
    web_fetch_interval_ms: int = 1000  # 同步循环相邻请求最小间隔（毫秒）
    web_fetch_concurrency: int = 2  # 抓取并发上限（semaphore，同步任务不打垮内网站点）
    web_max_pages_per_sync: int = 200  # 单次同步最多抓取页面数（防失控膨胀）
    # ── M3 登录安全策略 ──────────────────────────────────────
    bcrypt_rounds: int = 12  # bcrypt 哈希轮数，越大越慢越安全
    login_max_failures: int = 5  # 连续失败多少次后锁定账户
    login_lock_minutes: int = 15  # 账户锁定时长（分钟）
    login_rate_per_minute: int = 10  # 单 IP 每分钟最多登录尝试次数
    database_url: str = "postgresql+asyncpg://user:pass@postgres:5432/kagent"
    # ── DB 连接池（P1-5 压测实测后标定）──────────────────────
    # 每个**在途**问答请求长期占用 1 条连接：get_db 的 yield 覆盖整条 SSE 流，
    # 依赖的 finally 要等响应发完才执行；流式过程中内部还会瞬时再开 1 条
    # （SessionLocal 用于读历史 / 检索 / 落库 / 审计）。
    # ★ 实测依据：pool_size=10 + max_overflow=10（合计 20）时，
    #   c=10 并发 133 请求全成功；c=20 并发 76 请求失败 36 个（47%），
    #   P50 总耗时 60s，日志为 QueuePool limit of size 10 overflow 10 reached。
    #   即：20 恰好卡在「20 在途 × 1~2 条」的临界点上，一旦越界就雪崩。
    # 容量换算：可用连接数 ≈ pool_size + max_overflow，按「每在途请求 2 条」取整 → 支持 ~20 并发。
    # 扩副本 MUST 重算：40 × 副本数 + worker 20 ≤ PG max_connections(100) − 预留。
    db_pool_size: int = 20
    db_max_overflow: int = 20
    # 过载时 MUST 快速失败：pool_timeout 30s 会让请求挂着不动 30 秒再 500
    # （实测 c=20 的 P50 总耗时 60s = 两次 30s 等池），5s 直接 503 更有用。
    db_pool_timeout_s: float = 5.0
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
    # ── 重排开关（显式关闭，消除「白等超时再降级」）──────────
    # ★ 默认 false。实测依据（本机 CPU，torch 引擎，16 核无配额限制）：
    #   bge-reranker-v2-m3 单次重排 8 条耗时 10.5s、25 条 35.4s、50 条 66.0s，
    #   远超 rerank_timeout_seconds(3s) —— 开启也必然逐次超时降级，且降级前
    #   白等满 3s（链式多轮按轮数放大，实测 multi:3 白等约 9s）。
    #   false 时检索层**不发起调用**，直接走 vector 口径：与现状实际行为完全一致，
    #   只是不再空等、不再刷误导性的「重排降级」告警。
    #   换到 GPU / 更强算力后置 RERANK_ENABLED=true 即可启用（启用前 MUST 标定
    #   decisions.RELEVANCE_THRESHOLD_RERANK）。
    rerank_enabled: bool = False
    max_upload_mb: int = 100
    # ★ 图片扩展名与 services/parse/image_parser.IMAGE_EXTS 对齐。
    #   图片能否真正入库还取决于 ocr_enabled：关闭时上传层直接 415 并指向
    #   OCR_ENABLED（见 api/kbs.py 的联动校验），不会落到解析层才失败。
    allowed_ext: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "pdf", "md", "txt", "xls", "xlsx", "docx", "pptx",
            "png", "jpg", "jpeg", "gif", "webp", "bmp",
        ]
    )
    # ── M7 多模态：扫描件 OCR / 视觉抽取 ─────────────────────
    # ★ 默认 false：保持既有语义「仅支持文本层 PDF」，扫描件 PDF 与图片在解析层被拒绝。
    #   置 true 后，扫描件 PDF 与图片改道本地 OCR 服务（apps/ocr，GOT-OCR2.0）转录入库。
    #   真源即此处；decisions.py **不再**持有 OCR_ENABLED（M7 D6）。
    ocr_enabled: bool = False
    # ── B'：本地 OCR 服务地址（fail-closed）────────────────────
    # ocr_enabled=true 时必须显式注入（compose 内为 http://ocr:8000）；
    # 留空且开关开启 → 解析层直接 ParseError，不做任何降级（无双引擎兼容）。
    ocr_base_url: str = ""
    # ── 文档图像质量门禁（去噪 / 去污 + 质量识别）─────────────
    # ★ 门禁本身默认开：纯 CPU（Pillow C 路径，单页 < 200ms），无外部依赖，
    #   只做检测与分流 —— 关掉会失去「转人工」这一唯一的质量兜底。
    image_quality_gate_enabled: bool = True
    # ★ 增强默认开，但只在 verdict=enhance（脏 / 歪 / 灰底）时触发，干净页零改动。
    #   若某批语料的评测基线需要冻结，置 false 只保留检测不动像素 ——
    #   改这个开关会改变入库文本，MUST 重跑全量评估后再回填指标。
    image_enhance_enabled: bool = True
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
        if self.app_env == "prod" and not self.jwt_secret:
            raise ValueError("生产环境必须显式注入: JWT_SECRET（当前值为 None/空）")
        # WU-M-11：凭据存储态加密密钥 —— dev 留空时落到内置开发密钥；
        # prod 必须显式注入独立密钥（禁止开发默认值上线）
        if not self.credential_encryption_key:
            self.credential_encryption_key = (
                "Zz3xQ9wV7tK2pL8mN5jH4bF6dS0aR1eY3uI7oP9gX2k="  # 仅 dev
            )
        elif self.app_env == "prod" and self.credential_encryption_key == "Zz3xQ9wV7tK2pL8mN5jH4bF6dS0aR1eY3uI7oP9gX2k=":
            raise ValueError("生产环境必须显式注入: CREDENTIAL_ENCRYPTION_KEY（禁止使用开发默认值）")


@lru_cache
def get_settings() -> Settings:
    return Settings()
