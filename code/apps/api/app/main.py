import json
import logging
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
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
from app.api.quota import router as quota_router
from app.api.rate_limit import check_chat_rate_limit  # noqa: F401 — 限流依赖
from app.api.roles import router as roles_router
from app.api.sensitive_words import router as sensitive_words_router
from app.api.sync import router as sync_router
from app.api.users import router as users_router
from app.config import decisions

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


@asynccontextmanager
async def lifespan(_: FastAPI):
    decisions.self_check()
    yield


app = FastAPI(title="知识库问答 Agent API", version="0.2.0", lifespan=lifespan)


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
