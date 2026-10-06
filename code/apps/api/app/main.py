import json
import logging
import os
import time
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse

from app.api.auth import router as auth_router
from app.api.audit import router as audit_router
from app.api.chat import router as chat_router
from app.api.departments import router as departments_router
from app.api.documents import router as documents_router
from app.api.eval import router as eval_router
from app.api.feature_flags import router as feature_flags_router
from app.api.finetune import router as finetune_router
from app.api.groups import router as groups_router
from app.api.health import router as health_router
from app.api.jobs import router as jobs_router
from app.api.kbs import router as kbs_router
from app.api.me import router as me_router
from app.api.messages import router as messages_router
from app.api.metrics import router as metrics_router
from app.api.quota import router as quota_router
from app.api.rate_limit import check_chat_rate_limit  # noqa: F401 — 限流依赖
from app.api.roles import router as roles_router
from app.api.sensitive_words import router as sensitive_words_router
from app.api.sync import router as sync_router
from app.api.users import router as users_router
from app.config import decisions
from app.config.settings import get_settings
from app.infra import metrics, trace
from app.infra.arq_pool import close_arq_pool, get_arq_pool
from app.infra.redis_client import close_redis

logger = logging.getLogger(__name__)

# ── M6 可观测：结构化 JSON 日志 ──────────────────────────
# 每条日志输出为单行 JSON，方便 ELK/Loki 等日志聚合系统采集


class JsonFormatter(logging.Formatter):
    """单行 JSON 日志格式化器，支持 extra 中的结构化字段。"""

    _RESERVED = {
        "name", "msg", "args", "levelname", "levelno", "pathname",
        "filename", "module", "exc_info", "exc_text", "stack_info",
        "lineno", "funcName", "created", "msecs", "relativeCreated",
        "thread", "threadName", "processName", "process",
    }

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%03d"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # extra 中的非保留字段全部带上
        for key, value in record.__dict__.items():
            if key not in self._RESERVED and not key.startswith("_"):
                entry[key] = value
        if record.exc_info:
            entry["traceback"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


# 手动配置（不用 dictConfig，避免模块加载时序问题）
_json_handler = logging.StreamHandler()
_json_handler.setFormatter(JsonFormatter())
logging.root.handlers = [_json_handler]
logging.root.setLevel(logging.INFO)
for _name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    _logger = logging.getLogger(_name)
    _logger.handlers = [_json_handler]
    _logger.propagate = False

# trace_id 注入：给所有 handler 挂过滤器，之后每条日志自动带 trace_id 字段
trace.install_log_filter()


@asynccontextmanager
async def lifespan(_: FastAPI):
    decisions.self_check()
    # LLM 预算校验：窗口上限 / 输出预留 / 模型窗口三者必须对齐。
    # 模型名来自配置（.env），只有这里拿得到，故不放进 decisions.self_check() 内部。
    decisions.check_llm_budget(
        get_settings().llm_model, get_settings().llm_max_output_tokens
    )

    # ── crypto 私钥预热 ────────────────────────────────────
    # 见 app/services/crypto.py::warm_up —— 该模块用同步 Redis 客户端，
    # 首次调用若发生在请求期会阻塞整个事件循环。提前到启动阶段执行。
    from app.services import crypto as crypto_service

    crypto_service.warm_up()

    # ── arq 连接池（进程级单例）──────────────────────────────
    # 见 app/infra/arq_pool.py —— 请求路径与后台任务共用同一个池，
    # 不再每次入队 create_pool + close
    app.state.arq_pool = await get_arq_pool()

    # ── 重排服务启动探活 ──────────────────────────────────
    # 降级是设计允许的，因此探活失败**不拒绝启动**，只告警 + 打点。
    # 目的：让「重排服务不可用」这件事在启动时就被看见，
    # 而不是等某个请求静默降级后才在日志里浮现。
    try:
        from app.services.rerank import get_rerank_service
        _rerank_ok = await get_rerank_service().healthcheck()
        if not _rerank_ok:
            metrics.incr("rag.rerank.startup_probe", result="unhealthy")
            logger.warning(
                "启动探活：重排服务不可用，检索将走降级链路（用向量余弦分）",
                extra={"hint": "检查 RERANK_BASE_URL 与推理服务；降级态下阈值口径为 vector"},
            )
        else:
            metrics.incr("rag.rerank.startup_probe", result="healthy")
            logger.info("启动探活：重排服务可用")
    except Exception:
        logger.warning("重排启动探活异常", exc_info=True)

    try:
        yield
    finally:
        # 连接池都是进程级共享资源，统一在这里释放。
        # 请求级调用点拿到的 aclose() / close() 都是 no-op
        # （见 app/infra/redis_client.py 与 app/infra/arq_pool.py）
        await close_arq_pool()
        await close_redis()


app = FastAPI(title="知识库问答 Agent API", version="0.2.0", lifespan=lifespan)

# ── CORS + 可信主机（安全基线）────────────────────────────
settings = get_settings()
# dev 环境允许 localhost + 局域网 IP；prod 环境应通过 CORS_ORIGINS 环境变量显式注入
_dev_origins = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]
# prod 可通过环境变量 CORS_ORIGINS="https://foo.com,https://bar.com" 覆盖
_cors_origins_env = os.environ.get("CORS_ORIGINS", "").strip()
cors_origins = (
    [o.strip() for o in _cors_origins_env.split(",") if o.strip()]
    if _cors_origins_env
    else _dev_origins
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 可信主机：dev 允许任意（含 nginx 代理透传的 Host）；prod 应通过 TRUSTED_HOSTS 环境变量注入
_trusted_hosts_env = os.environ.get("TRUSTED_HOSTS", "").strip()
trusted_hosts = (
    [h.strip() for h in _trusted_hosts_env.split(",") if h.strip()]
    if _trusted_hosts_env
    else ["*"]  # dev 宽松，prod 必须显式配置
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)


@app.middleware("http")
async def trace_id_middleware(request: Request, call_next):
    """为每个请求建立 trace_id，贯穿全部环节并回写响应头。

    放在最外层（最后注册 = 最外层）：即便请求在 CORS / 可信主机环节被拒，
    也能留下可追溯的 trace_id。

    客户端可传 X-Trace-Id 串联上下游（如网关已生成）；未传则新生成。
    """
    incoming = request.headers.get("X-Trace-Id")
    trace_id = incoming.strip()[:64] if incoming and incoming.strip() else trace.new_trace_id()
    token = trace.set_trace_id(trace_id)
    started = time.perf_counter()
    try:
        response = await call_next(request)
        response.headers["X-Trace-Id"] = trace_id
        metrics.observe_ms(
            "http.request.duration",
            (time.perf_counter() - started) * 1000,
            path=request.url.path,
            status=str(response.status_code),
        )
        return response
    finally:
        trace.reset_trace_id(token)


@app.exception_handler(ValueError)
async def _value_error_handler(request: Request, exc: ValueError):
    """全局 ValueError → 400。

    M4 任务 3：models 层 event listener（before_insert/before_update）校验
    acl_tags 后抛 ValueError（原 AclTagError 转义），此处统一映射为 400，
    让 API 调用方拿到可读的拒绝原因而不是 500。
    """
    logger.error("ValueError on %s %s: %s\n%s", request.method, request.url.path, exc, traceback.format_exc())
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    """全局兜底异常 → 500，仅返回通用文案。

    避免未捕获异常向客户端泄露内部路径/SQL/堆栈（P2 安全修复）。
    完整堆栈进日志，由运维侧排查。
    """
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "INTERNAL_SERVER_ERROR"},
    )


app.include_router(health_router, prefix="/api")
app.include_router(auth_router, prefix="/api")
app.include_router(me_router, prefix="/api")
app.include_router(departments_router, prefix="/api")
app.include_router(users_router, prefix="/api")
app.include_router(groups_router, prefix="/api")
app.include_router(roles_router, prefix="/api")
app.include_router(kbs_router, prefix="/api")
app.include_router(documents_router, prefix="/api")
app.include_router(jobs_router, prefix="/api")
app.include_router(chat_router, prefix="/api")
app.include_router(messages_router, prefix="/api")
app.include_router(finetune_router, prefix="/api")
app.include_router(audit_router, prefix="/api")
app.include_router(sync_router, prefix="/api")
app.include_router(eval_router, prefix="/api")
# ── M6 续篇：配额 / 敏感词 / 灰度开关 ─────────────────────
app.include_router(quota_router, prefix="/api")
app.include_router(sensitive_words_router, prefix="/api")
app.include_router(feature_flags_router, prefix="/api")
app.include_router(metrics_router, prefix="/api")
