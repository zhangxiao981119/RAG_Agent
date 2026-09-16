from fastapi.testclient import TestClient

from app.main import app


def test_openapi_contains_health() -> None:
    with TestClient(app) as client:
        response = client.get("/openapi.json")
    assert response.status_code == 200
    assert "/api/health" in response.json()["paths"]
