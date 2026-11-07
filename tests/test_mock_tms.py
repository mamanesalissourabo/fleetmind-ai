"""Tests des TMS simulés : authentification, pagination et différences de format."""

import pytest
from fastapi.testclient import TestClient

from mock_tms.common import get_mock_settings
from mock_tms.tms_alpha.app import app as alpha_app
from mock_tms.tms_beta.app import app as beta_app


@pytest.fixture
def alpha() -> TestClient:
    headers = {"X-API-Key": get_mock_settings().mock_alpha_api_key}
    return TestClient(alpha_app, headers=headers)


@pytest.fixture
def beta() -> TestClient:
    settings = get_mock_settings()
    client = TestClient(beta_app)
    response = client.post(
        "/auth/token",
        json={
            "clientId": settings.mock_beta_client_id,
            "clientSecret": settings.mock_beta_client_secret,
        },
    )
    client.headers["Authorization"] = f"Bearer {response.json()['jeton']}"
    return client


def test_alpha_rejects_missing_or_wrong_api_key() -> None:
    client = TestClient(alpha_app)

    assert client.get("/api/v1/missions").status_code == 401
    assert client.get("/api/v1/missions", headers={"X-API-Key": "wrong"}).status_code == 401


def test_alpha_paginates_by_page(alpha: TestClient) -> None:
    body = alpha.get("/api/v1/missions", params={"page": 3, "page_size": 2}).json()

    assert [mission["mission_id"] for mission in body["data"]] == ["AL-1005"]
    assert body["pagination"] == {"page": 3, "page_size": 2, "total": 5}


def test_alpha_filters_missions_by_status(alpha: TestClient) -> None:
    body = alpha.get("/api/v1/missions", params={"status": "ON_HOLD"}).json()

    assert [mission["mission_id"] for mission in body["data"]] == ["AL-1003"]


def test_alpha_omits_missing_information(alpha: TestClient) -> None:
    mission = alpha.get("/api/v1/missions/AL-1005").json()["data"]

    assert "driver" not in mission
    assert "last_position" not in mission
    assert "estimated_arrival" not in mission


def test_alpha_returns_404_for_unknown_mission(alpha: TestClient) -> None:
    assert alpha.get("/api/v1/missions/UNKNOWN").status_code == 404
    assert alpha.get("/api/v1/missions/UNKNOWN/events").status_code == 404


def test_alpha_filters_open_incidents(alpha: TestClient) -> None:
    body = alpha.get("/api/v1/incidents", params={"status": "OPEN"}).json()

    assert [incident["incident_id"] for incident in body["data"]] == ["INC-502"]


def test_beta_rejects_invalid_credentials_and_missing_token() -> None:
    client = TestClient(beta_app)
    response = client.post("/auth/token", json={"clientId": "x", "clientSecret": "y"})

    assert response.status_code == 401
    assert client.get("/v2/transports").status_code == 401
    assert client.get("/v2/transports", headers={"Authorization": "Bearer x"}).status_code == 401


def test_beta_paginates_by_offset(beta: TestClient) -> None:
    body = beta.get("/v2/transports", params={"offset": 4, "limit": 2}).json()

    assert [transport["reference"] for transport in body["resultats"]] == ["BT-7785"]
    assert (body["total"], body["offset"], body["limit"]) == (5, 4, 2)


def test_beta_filters_transports_by_numeric_state(beta: TestClient) -> None:
    body = beta.get("/v2/transports", params={"etat": 40}).json()

    assert [transport["reference"] for transport in body["resultats"]] == ["BT-7783"]


def test_beta_exposes_missing_information_as_null(beta: TestClient) -> None:
    transport = beta.get("/v2/transports/BT-7785").json()

    assert transport["chauffeur"] is None
    assert transport["dernierePosition"] is None
    assert transport["arriveeEstimee"] is None


def test_beta_returns_404_for_unknown_transport(beta: TestClient) -> None:
    assert beta.get("/v2/transports/INCONNU").status_code == 404
    assert beta.get("/v2/transports/INCONNU/suivi").status_code == 404


def test_beta_filters_open_anomalies(beta: TestClient) -> None:
    body = beta.get("/v2/anomalies", params={"ouvertes": True}).json()

    assert [anomalie["idAnomalie"] for anomalie in body["resultats"]] == ["ANO-32"]


def test_each_tms_publishes_its_own_openapi_contract() -> None:
    alpha_spec = TestClient(alpha_app).get("/openapi.json").json()
    beta_spec = TestClient(beta_app).get("/openapi.json").json()

    assert "/api/v1/missions" in alpha_spec["paths"]
    assert "/v2/transports" in beta_spec["paths"]
    assert "APIKeyHeader" in alpha_spec["components"]["securitySchemes"]
    assert "HTTPBearer" in beta_spec["components"]["securitySchemes"]
