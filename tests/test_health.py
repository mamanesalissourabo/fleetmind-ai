"""Tests de la route de santé du backend."""

from fastapi.testclient import TestClient

from backend.api.main import app


def test_health_returns_ok() -> None:
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
