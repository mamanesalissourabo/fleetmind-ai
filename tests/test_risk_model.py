"""Tests du modèle de risque de retard : mêmes calculs, résultats reproductibles, rôle limité."""

from datetime import UTC, datetime, timedelta
from math import isnan

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.api.dependencies import get_current_user
from tests.helpers import TEST_ADMIN
from backend.agents.orchestrator import build_llm_client
from backend.api.dependencies import get_client_opener, get_clock
from backend.api.main import create_app
from backend.api.routes.chatbot import get_risk_model_factory
from backend.core.clock import Clock
from backend.core.database import get_session
from backend.ml.features import FEATURE_NAMES, extract_features
from backend.ml.predict import DelayRiskModel, ModelUnavailableError
from backend.ml.synthetic import generate_dataset
from backend.ml.train import train_model
from backend.models.alert import Alert
from backend.schemas.canonical import GeoPoint, Mission
from tests.test_chatbot import T0, FakeLlm, factory, opener  # noqa: F401


@pytest.fixture(scope="module")
def trained(tmp_path_factory):
    directory = tmp_path_factory.mktemp("model")
    metadata = train_model(missions=400, output=directory)
    return directory, metadata


def mission(**changes) -> Mission:
    values = dict(
        source_tms="t", external_id="M-1", status="IN_TRANSIT", customer_name="C", vehicle_plate="P",
        origin_city="A", destination_city="B", planned_departure=T0,
        planned_arrival=T0 + timedelta(hours=4),
        planned_route=[GeoPoint(lat=33.0, lon=-7.0), GeoPoint(lat=35.0, lon=-6.0)], updated_at=T0,
    )
    return Mission(**{**values, **changes})


def test_dataset_is_reproducible_and_keeps_two_rows_per_mission() -> None:
    first, second = generate_dataset(20, seed=1), generate_dataset(20, seed=1)

    assert first.labels == second.labels
    assert len(first.features) == 40 and len(set(first.groups)) == 20
    assert all(len(row) == len(FEATURE_NAMES) for row in first.features)


def test_missing_position_stays_missing() -> None:
    row = extract_features(mission(), T0 + timedelta(hours=1), 0)

    assert isnan(row[FEATURE_NAMES.index("progress_ratio")])
    assert isnan(row[FEATURE_NAMES.index("position_age_min")])
    assert row[FEATURE_NAMES.index("elapsed_ratio")] == 0.25


def test_metadata_declares_synthetic_data_and_beats_naive_baseline(trained) -> None:
    _, metadata = trained

    assert metadata["training_data"] == "synthetic"
    assert metadata["metrics"]["brier"] < metadata["metrics"]["baseline_brier"]


def test_stuck_mission_gets_a_higher_risk(trained) -> None:
    model = DelayRiskModel(trained[0])
    reference = T0 + timedelta(hours=3)
    started = dict(actual_departure=T0, last_position_at=reference)

    on_time = model.predict(mission(last_position=GeoPoint(lat=34.5, lon=-6.25), **started), reference, 0)
    stuck = model.predict(mission(last_position=GeoPoint(lat=33.1, lon=-6.95), **started), reference, 1)

    assert 0 <= on_time.probability < stuck.probability <= 1


def test_missing_model_is_reported(tmp_path) -> None:
    with pytest.raises(ModelUnavailableError):
        DelayRiskModel(tmp_path)


def ask_risk(factory, model_factory, at: datetime, client_opener=opener):  # noqa: F811
    def session_override():
        with factory() as session:
            yield session

    llm = FakeLlm({"intent": "delay_risk", "tms_id": "tms_alpha", "period": "none"})
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: TEST_ADMIN
    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_client_opener] = lambda: client_opener
    app.dependency_overrides[build_llm_client] = lambda: llm
    app.dependency_overrides[get_clock] = lambda: Clock(at)
    app.dependency_overrides[get_risk_model_factory] = lambda: model_factory
    body = TestClient(app).post("/chatbot/ask", json={"question": "Quels risques de retard ?"}).json()
    return body, llm


def test_chatbot_reports_risks_as_risks_without_llm_or_alert(factory, trained) -> None:  # noqa: F811
    model = DelayRiskModel(trained[0])
    at = datetime(2026, 11, 7, 9, 30, tzinfo=UTC)

    body, llm = ask_risk(factory, lambda: model, at)

    assert llm.calls == 1
    assert body["summary_source"] == "deterministic"
    assert "données synthétiques" in body["summary"]
    assert body["facts"] == []
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Alert)) == 0


def test_mission_past_its_arrival_is_not_predicted(factory, trained) -> None:  # noqa: F811
    model = DelayRiskModel(trained[0])

    body, _ = ask_risk(factory, lambda: model, datetime(2026, 11, 8, tzinfo=UTC))

    assert body["risks"] == []
    assert "0 mission(s) évaluée(s)" in body["summary"]
    assert "déjà dépassée" in body["summary"]


def test_untrained_model_is_reported_without_guessing(factory) -> None:  # noqa: F811
    def absent():
        raise ModelUnavailableError("Modèle non entraîné")

    body, _ = ask_risk(factory, absent, T0)

    assert body["scope"][0]["available"] is False
    assert body["risks"] == []
    assert "incomplet" in body["summary"]
