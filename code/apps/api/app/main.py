from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.chat import router as chat_router
from app.api.documents import router as documents_router
from app.api.health import router as health_router
from app.api.jobs import router as jobs_router
from app.api.kbs import router as kbs_router
from app.config import decisions


@asynccontextmanager
async def lifespan(_: FastAPI):
    decisions.self_check()
    yield


app = FastAPI(title="知识库问答 Agent API", version="0.2.0", lifespan=lifespan)
app.include_router(health_router, prefix="/api")
app.include_router(kbs_router, prefix="/api")
app.include_router(documents_router, prefix="/api")
app.include_router(jobs_router, prefix="/api")
app.include_router(chat_router, prefix="/api")
