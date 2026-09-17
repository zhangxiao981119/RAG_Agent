from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.auth import router as auth_router
from app.api.audit import router as audit_router
from app.api.chat import router as chat_router
from app.api.departments import router as departments_router
from app.api.documents import router as documents_router
from app.api.finetune import router as finetune_router
from app.api.groups import router as groups_router
from app.api.health import router as health_router
from app.api.jobs import router as jobs_router
from app.api.kbs import router as kbs_router
from app.api.me import router as me_router
from app.api.messages import router as messages_router
from app.api.roles import router as roles_router
from app.api.users import router as users_router
from app.config import decisions


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
    return JSONResponse(status_code=400, content={"detail": str(exc)})


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
