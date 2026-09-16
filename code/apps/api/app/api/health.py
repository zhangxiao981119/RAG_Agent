from fastapi import APIRouter, Response, status

from app.config.settings import get_settings
from app.database import engine
from app.schemas.health import HealthResponse
from app.services.health import collect_health

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health(response: Response) -> HealthResponse:
    result = await collect_health(engine, get_settings())
    if result.status == "error":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return result
