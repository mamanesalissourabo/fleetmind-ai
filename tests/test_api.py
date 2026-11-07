"""Tests de l'API : du cycle de surveillance au traitement d'une alerte."""

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.api.dependencies import get_current_user
from tests.helpers import TEST_ADMIN
from backend.api.dependencies import get_client_opener
from backend.api.main import create_app
from backend.core.database import DatabaseNotConfiguredError, get_session
from backend.models.base import Base
from tests.helpers import client_for

REFERENCE = "2026-11-07T11:00:00Z"


@pytest.fixture
def api() -> TestClient:
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)

    def session_override() -> Iterator[Session]:
        with Session(engine, expire_on_commit=False) as session:
            yield session

    @contextmanager
    def opener(config):
        yield client_for(config.id)

    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: TEST_ADMIN
    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_client_opener] = lambda: opener
    return TestClient(app)


def run_cycle(api: TestClient, **body: object) -> list[dict]:
    response = api.post("/monitoring/cycles", json={"reference_time": REFERENCE, **body})
    assert response.status_code == 200
    return response.json()


def test_cycle_creates_alerts_for_each_tms_without_duplicates(api: TestClient) -> None:
    first = run_cycle(api)
    second = run_cycle(api)

    assert [result["alerts"]["opened"] for result in first] == [8, 8]
    assert [result["alerts"]["opened"] for result in second] == [0, 0]
    assert [result["alerts"]["notifications"] for result in second] == [0, 0]
    assert len(api.get("/alerts").json()) == 16


def test_alerts_can_be_filtered_by_tms_and_status(api: TestClient) -> None:
    run_cycle(api, tms_id="tms_beta")

    assert {a["source_tms"] for a in api.get("/alerts").json()} == {"tms_beta"}
    assert api.get("/alerts", params={"tms_id": "tms_alpha"}).json() == []
    assert api.get("/alerts", params={"alert_status": "RESOLVED"}).json() == []


def test_operator_can_acknowledge_then_resolve_an_alert(api: TestClient) -> None:
    run_cycle(api, tms_id="tms_alpha")
    alert_id = api.get("/alerts").json()[0]["id"]

    acknowledged = api.post(f"/alerts/{alert_id}/acknowledge", json={"actor": "sophie"})
    resolved = api.post(f"/alerts/{alert_id}/resolve", json={"actor": "sophie", "note": "Fait"})
    conflict = api.post(f"/alerts/{alert_id}/acknowledge", json={"actor": "paul"})
    detail = api.get(f"/alerts/{alert_id}").json()

    assert acknowledged.json()["status"] == "ACKNOWLEDGED"
    assert resolved.json()["status"] == "RESOLVED"
    assert conflict.status_code == 409
    assert [row["action"] for row in detail["history"]][-2:] == ["ACKNOWLEDGED", "RESOLVED"]


def test_unknown_alert_and_unknown_tms_return_404(api: TestClient) -> None:
    assert api.get("/alerts/999").status_code == 404
    assert api.post("/alerts/999/resolve", json={"actor": "x"}).status_code == 404
    assert api.get("/missions", params={"tms_id": "tms_gamma"}).status_code == 404


def test_missions_route_returns_normalized_data_with_freshness(api: TestClient) -> None:
    result = api.get("/missions").json()

    assert [(item["source_tms"], len(item["missions"])) for item in result] == [
        ("tms_alpha", 5), ("tms_beta", 5),
    ]
    assert all(item["available"] and item["retrieved_at"] for item in result)


def test_missing_database_gives_503_but_health_still_works() -> None:
    def unconfigured() -> Iterator[Session]:
        raise DatabaseNotConfiguredError("DATABASE_URL absente")
        yield

    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: TEST_ADMIN
    app.dependency_overrides[get_session] = unconfigured
    client = TestClient(app)

    response = client.get("/alerts")

    assert response.status_code == 503
    assert "postgresql" not in response.text.lower()
    assert client.get("/health").status_code == 200


def test_docs_propose_valid_examples(api: TestClient) -> None:
    schemas = api.get("/openapi.json").json()["components"]["schemas"]
    cycle_example = schemas["CycleRequest"]["examples"][0]
    action_example = schemas["AlertAction"]["examples"][0]

    assert api.post("/monitoring/cycles", json=cycle_example).status_code == 200
    alert_id = api.get("/alerts").json()[0]["id"]
    assert api.post(f"/alerts/{alert_id}/acknowledge", json=action_example).status_code == 200
