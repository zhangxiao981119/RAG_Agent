import json
import logging
import os
import time
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse

from app.api.auth import router as auth_router
from app.api.audit import router as audit_router
from app.api.chat import router as chat_router
from app.api.departments import router as departments_router
from app.api.documents import router as documents_router
from app.api.eval import router as eval_router
from app.api.system import router as system_router
from app.api.wiki import router as wiki_router
from app.api.feature_flags import router as feature_flags_router
from app.api.finetune import router as finetune_router
from app.api.groups import router as groups_router
from app.api.health import router as health_router
from app.api.jobs import router as jobs_router
from app.api.integrations import router as integrations_router
from app.api.kbs import router as kbs_router
from app.api.me import router as me_router
from app.api.messages import router as messages_router
from app.api.metrics import router as metrics_router
from app.api.needs_reindex import router as needs_reindex_router
from app.api.prompts import router as prompts_router
from app.api.quota import router as quota_router
from app.api.rate_limit import check_chat_rate_limit  # noqa: F401 — 限流依赖
from app.api.roles import router as roles_router
from app.api.scenarios import router as scenarios_router
from app.api.sensitive_words import router as sensitive_words_router
from app.api.sync import router as sync_router
from app.api.tenant_governance import router as tenant_governance_router
from app.api.users import router as users_router
from app.config import decisions
from app.config.settings import get_settings
from app.infra import metrics, trace
from app.services.db_scope import (
    DB_SCOPE_NOT_ALLOWED,
    DbScopeError,
    error_detail,
)
from app.infra.arq_pool import close_arq_pool, get_arq_pool
from app.infra.redis_client import close_redis

logger = logging.getLogger(__name__)

def _iso_with_millis(formatter: logging.Formatter, record: logging.LogRecord) -> str:
    """把 `created` 格式化为 `YYYY-MM-DDTHH:MM:SS.mmm`。

    ★ 为什么不能写 `formatter.formatTime(record, "%Y-%m-%dT%H:%M:%S.%03d")`：
      `%03d` 是 C `strftime` 的**宽度修饰**，Python 的 `time.strftime` 不支持，
      在 3.12+ 上直接抛 `ValueError: Invalid format string`。
      日志格式化器抛异常 = 整条日志链路（含场景解析的 diagnostic、
      trace_id、审计字段）全部丢掉 —— 排障通道被自己的格式化器掐断，
      而且**每条日志都会炸**，不是偶发。

    ★ 为什么用 `formatter.converter` 而不是 `time.localtime`：
      `converter` 是 Formatter 的既有配置（默认 `time.localtime`，
      可被改造成 UTC/本地时区），沿用它才不丢时区语义。

    ★ 毫秒取 `record.msecs` 而不是 `created % 1`：前者是 logging 在构造
      LogRecord 时算好的（与 `created` 同源），避免两次浮点运算出现进位差。
    """
    converted = formatter.converter(record.created)
    return time.strftime("%Y-%m-%dT%H:%M:%S", converted) + f".{int(record.msecs):03d}"


# ── M6 可观测：结构化 JSON 日志 ──────────────────────────
# 每条日志输出为单行 JSON，方便 ELK/Loki 等日志聚合系统采集


class JsonFormatter(logging.Formatter):
    """单行 JSON 日志格式化器，支持 extra 中的结构化字段。"""

    _RESERVED = {
        "name", "msg", "args", "levelname", "levelno", "pathname",
        "filename", "module", "exc_info", "exc_text", "stack_info",
        "lineno", "funcName", "created", "msecs", "relativeCreated",
        "thread", "threadName", "processName", "process",
        # Python 3.12 给 LogRecord 新增了 taskName（asyncio 任务名）。
        # 它是 logging 自身的字段而不是业务 extra —— 不排除的话每条日志都会
        # 多出一个 "taskName": null。此前不可见是因为格式化器本身在抛异常。
        "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": _iso_with_millis(self, record),
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
# 级别由配置驱动（.env 的 LOG_LEVEL），生产默认 INFO
logging.root.setLevel(get_settings().log_level.upper())
for _name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    _logger = logging.getLogger(_name)
    _logger.handlers = [_json_handler]
    _logger.propagate = False

# trace_id 注入：给所有 handler 挂过滤器，之后每条日志自动带 trace_id 字段
trace.install_log_filter()


@asynccontextmanager
async def lifespan(_: FastAPI):
    decisions.self_check()

    # ── 场景化配置自检（Step 3）────────────────────────────────────────
    # 分两类，动作刻意不同（设计文档 §C.3「代码错了就别启动，数据错了就喊出来」）：
    #   T1 常量类（无 DB）→ 违反 raise，拒绝启动；
    #   T2 数据类（有 DB）→ 只 WARN + metric，**永不 raise**。
    # ★ 为什么 T2 不放进 check_capabilities()：那个函数在「图与 wiki 都关」时**提前
    #   return**（self_check_ext.py:322-325），而本机默认两者全关 —— 放进去等于
    #   「自检写了一行、永远是绿的」。
    # ★ 为什么 T2 只在 api 侧：worker 不检索，跑全场景扫描是纯加成本与 DB 压力。
    from app.database import SessionLocal
    from app.services.scenario.effective import WIRED_RETRIEVAL_STEP
    from app.services.scenario.self_check import (
        check_scenario_constants,
        check_scenario_data,
    )

    check_scenario_constants()  # T1：违反即拒绝启动

    # ── 数据层连接预算闸门（Step 5）──
    # T1：纯算术、无 IO，超预算直接拒绝启动；关闭态不创建任何引擎。
    from app.infra.db_router import (
        check_connection_budget,
        log_db_router_banner,
        register_from_settings,
    )

    check_connection_budget()
    register_from_settings()

    async with SessionLocal() as _scenario_check_session:
        # T2：返回各判据的命中计数（已由 self_check 内部打点/记日志），此处只兜底
        await check_scenario_data(_scenario_check_session, max_step=WIRED_RETRIEVAL_STEP)

    # LLM 预算校验：窗口上限 / 输出预留 / 模型窗口三者必须对齐。
    # 模型名来自配置（.env），只有这里拿得到，故不放进 decisions.self_check() 内部。
    decisions.check_llm_budget(
        get_settings().llm_model, get_settings().llm_max_output_tokens
    )

    # ── 运行时配置口径（方案三：配置版本）──
    # 打印脱敏配置 + 指纹，让「这台机器按什么配置跑」在启动时留痕 ——
    # 出问题时可拿预发 / 生产的指纹直接比对，无需逐项核对 .env。
    from app.config.config_profile import config_fingerprint, runtime_config_snapshot

    _cfg_snapshot = runtime_config_snapshot()
    logger.info(
        "启动配置口径",
        extra={
            "config_fingerprint": config_fingerprint(_cfg_snapshot),
            "runtime_config": _cfg_snapshot,
        },
    )

    # ── 能力自检 + 启动横幅（GraphRAG 图召回 / LLM wiki 导航）──
    # 与 worker 的 on_startup 调**同一个函数**：抽取在 worker、召回在 api，
    # 只检一侧等于不检（另一侧的缺失只在「用户提问后什么都没发生」时才被发现）。
    # ★ fail-closed：开了能力却依赖缺失 → 这里抛错，进程启动失败（不静默降级）。
    # ★ 能力全关时本函数直接返回，**不查库**、不打任何图相关日志。
    from app.config.capabilities import log_capability_banner
    from app.config.self_check_ext import check_capabilities

    await check_capabilities()
    await log_capability_banner(role="api")

    # ── chunk 口径分布横幅（Step 4B-1）──────────────────────────────
    # ★ 异步 + 超时降级（默认 5s，站位值已登记到 placeholders）：
    #   这条统计是 `chunks` 上的一次全表扫（口径三列无索引，U-4B-7）。既然把它放进了
    #   启动路径，就必须给启动路径一个**上界** —— 否则「离线动作、不必加索引」这个
    #   论证不成立（设计 §B.5.2 与 U-4B-7 原本互相矛盾，本实现按"启动期有界成本 +
    #   超时护栏"解决）。超时/异常都只打提示，**绝不阻塞启动、绝不抛**。
    from app.services.scenario.needs_reindex import log_needs_reindex_banner

    await log_needs_reindex_banner()

    # ── DB 路由横幅（关闭态必打：这是「开关当时是开还是关」的唯一留痕）──
    await log_db_router_banner(SessionLocal)

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
    # ★ 重排显式关闭（settings.rerank_enabled=false）时**跳过探活** —— 否则每次启动
    #   都会为一个已停用的组件白等最多 rerank_timeout_seconds 并刷出误导性 unhealthy 告警。
    if get_settings().rerank_enabled:
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
    else:
        logger.info("启动探活：重排已关闭（RERANK_ENABLED=false），跳过探活")

    # ── 索引口径启动检查（方案一：索引/模型版本）──────────
    # 统计口径与当前不一致的 chunk（换模型 / 改向量公式后未重算的那批）。
    # 不拒绝启动：服务需在线继续服务旧数据，同时给运维窗口去重算；
    # 但必须让「有过期 chunk」这件事在启动时就被看见，而不是等检索悄悄查空才浮现。
    try:
        from sqlalchemy import text as _sa_text

        from app.config.index_profile import current_index_profile
        from app.database import SessionLocal

        _profile = current_index_profile()
        async with SessionLocal() as _session:
            _stale = (
                await _session.execute(
                    _sa_text(
                        "SELECT count(*) FROM chunks WHERE is_latest = true "
                        "AND (embedding_model <> :m OR embedding_text_version <> :tv)"
                    ),
                    {
                        "m": _profile.embedding_model,
                        "tv": _profile.embedding_text_version,
                    },
                )
            ).scalar_one()
        if _stale:
            metrics.incr("rag.index.stale_chunks_found")
            logger.warning(
                "启动检查：%d 个 chunk 的索引口径与当前不一致，向量路径将排除它们",
                _stale,
                extra={
                    "current_embedding_model": _profile.embedding_model,
                    "current_embedding_text_version": _profile.embedding_text_version,
                    "hint": "执行 python scripts/reindex_embeddings.py 重算",
                },
            )
        else:
            logger.info("启动检查：索引口径一致，无过期 chunk")
    except Exception:
        # 首次启动表可能尚未迁移 / DB 未就绪 —— 检查失败不阻断启动，仅告警
        logger.warning("索引口径启动检查跳过", exc_info=True)

    # ── schema 版本一致性启动检查（发布流程固化）──────────
    # 迁移由编排中的 migrate 一次性服务保证「先迁移后启动」；此处只做兜底：
    # 漏跑 migrate、手工改库、或回滚只换了镜像没降级时，让版本错位在启动时可见，
    # 而不是等某次查询抛 UndefinedColumn 才暴露。不阻断启动（旧数据仍需在线服务）。
    try:
        from alembic.config import Config as _AlembicConfig
        from alembic.script import ScriptDirectory
        from sqlalchemy import text as _sa_text

        from app.database import SessionLocal

        _alembic_ini = Path(__file__).resolve().parents[1] / "alembic.ini"
        _code_heads = set(
            ScriptDirectory.from_config(_AlembicConfig(str(_alembic_ini))).get_heads()
        )
        async with SessionLocal() as _session:
            _db_rev = (
                await _session.execute(_sa_text("SELECT version_num FROM alembic_version"))
            ).scalar_one_or_none()
        if _db_rev not in _code_heads:
            logger.warning(
                "启动检查：数据库 schema 版本与代码不一致",
                extra={
                    "db_revision": _db_rev,
                    "code_heads": sorted(_code_heads),
                    "hint": "执行 docker compose -p kagent -f docker-compose.prod.yml "
                    "run --rm migrate alembic upgrade head",
                },
            )
        else:
            logger.info("启动检查：schema 版本一致", extra={"revision": _db_rev})
    except Exception:
        # alembic.ini 缺失 / 表尚未创建 —— 不阻断启动，仅告警
        logger.warning("schema 版本启动检查跳过", exc_info=True)

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
        # path 标签取**路由模板**（如 /api/documents/{document_id}），不是原始 URL：
        # 原始 path 带资源 ID，会让时间序列基数随数据量无限膨胀（Prometheus 大忌）。
        # 未命中任何路由（404）统一归到 __unmatched__，避免把任意路径当标签写进去。
        _route = request.scope.get("route")
        metrics.observe_ms(
            "http.request.duration",
            (time.perf_counter() - started) * 1000,
            path=getattr(_route, "path", None) or "__unmatched__",
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


@app.exception_handler(DbScopeError)
async def _db_scope_error_handler(request: Request, exc: DbScopeError):
    """库位异常的统一出口（Step 7 S7-3）。

    ★ 只有 `DB_SCOPE_NOT_ALLOWED` 在这里映射 400：它是治理拒绝（租户库白名单），
      不是"同一 kb 命中多个库"的冲突。后者由各调用点显式 409/500 并附业务文案，
      不会走到这里；走到这里的其它码说明调用点没捕获，维持 500 且不泄露细节。
    """
    if exc.code == DB_SCOPE_NOT_ALLOWED:
        return JSONResponse(status_code=400, content={"detail": error_detail(exc)})
    logger.error(
        "Unhandled DbScopeError on %s %s: %s", request.method, request.url.path, exc
    )
    return JSONResponse(status_code=500, content={"detail": "INTERNAL_SERVER_ERROR"})


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


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics() -> PlainTextResponse:
    """暴露 Prometheus 文本指标，供监控栈抓取（P0-4 可观测性）。

    为什么挂在根路径而非 /api 下：web 的 nginx 只反代 /api/，
    挂在 /api 外可确保这条内部指标不经唯一对外入口 gateway(Caddy) 暴露。
    api 本身不发布宿主端口、网关只反代 web:80，故 /metrics 仅容器内网可达，
    不额外做鉴权（监控抓取亦不便携带凭证）。
    """
    return PlainTextResponse(
        content=metrics.render(),
        media_type=metrics.CONTENT_TYPE_LATEST,
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
app.include_router(integrations_router, prefix="/api")
app.include_router(messages_router, prefix="/api")
app.include_router(finetune_router, prefix="/api")
app.include_router(audit_router, prefix="/api")
app.include_router(sync_router, prefix="/api")
app.include_router(eval_router, prefix="/api")
app.include_router(system_router, prefix="/api")
app.include_router(wiki_router, prefix="/api")
# ── M6 续篇：配额 / 敏感词 / 灰度开关 ─────────────────────
app.include_router(quota_router, prefix="/api")
app.include_router(sensitive_words_router, prefix="/api")
app.include_router(feature_flags_router, prefix="/api")
app.include_router(metrics_router, prefix="/api")
app.include_router(prompts_router, prefix="/api")
# ── Step 4A：场景管理写接口 ─────────────────────────────
# ★ 漏这一行 = 全部 9 个场景接口不可达，而且**不会报任何错**（import 正确、
#   应用能起、测试里直接测 service 也全绿）—— 只有对着文档调接口才会发现 404。
#   守护用例：`tests/scenario/test_step4_routes.py::test_scenarios_router_is_mounted`。
app.include_router(scenarios_router, prefix="/api")
# ── Step 4B-1：chunk 口径分布（admin 只读）────────────────────
# ★ 与 `/api/admin/metrics` 同前缀不同模块：那边是进程内计数，这边是**查库**的
#   实时口径分布。合进 metrics.py 会让"一个模块两种数据源"，回滚时一起回滚。
app.include_router(needs_reindex_router, prefix="/api")
# ── Step 7 S7-5：租户治理面（库白名单 / 场景分级 / 钉定 / 上限）──
app.include_router(tenant_governance_router, prefix="/api")
