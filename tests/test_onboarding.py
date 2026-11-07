"""Tests de la génération de configuration depuis OpenAPI, avec un LLM simulé."""

import pytest
from fastapi.testclient import TestClient

from backend.integrations.generic_client import GenericTmsClient
from backend.integrations.onboard_cli import probe
from backend.integrations.openapi_parser import FieldKind, OpenApiError, infer_pagination, parse_openapi
from backend.integrations.tms_onboarding import RESOURCES, generate_draft
from backend.integrations.tms_registry import TmsRegistry
from backend.schemas.canonical import Incident, Mission, MissionEvent, MissionStatus
from backend.schemas.tms_config import TmsConfig
from mock_tms.common import get_mock_settings
from mock_tms.tms_alpha.app import app as alpha_app
from mock_tms.tms_beta.app import app as beta_app
from mock_tms.tms_gamma.app import app as gamma_app

ENDPOINTS = {"missions": "/api/shipments", "events": "/api/shipments/{ref}/history", "incidents": "/api/issues"}
MISSIONS = {
    "external_id": "ref", "status": "state", "customer_name": "shipper", "vehicle_plate": "truck",
    "driver_name": "operator", "origin_city": "start.town", "destination_city": "end.town",
    "planned_departure": "schedule.pickup_at", "planned_arrival": "schedule.dropoff_at",
    "actual_departure": "execution.picked_up_at", "actual_arrival": "execution.dropped_off_at",
    "estimated_arrival": "schedule.eta", "planned_route": "waypoints", "last_position": "gps",
    "last_position_at": "gps_time", "updated_at": "modified_at",
}
STATES = {"SCHEDULED": "PLANNED", "ON_ROAD": "IN_TRANSIT", "COMPLETED": "DELIVERED", "STUCK": "BLOCKED", "ABORTED": "CANCELLED"}
EVENTS = {"external_id": "uid", "mission_external_id": "shipment_ref", "type": "kind", "occurred_at": "at", "description": "label"}
KINDS = {"LEFT_SITE": "DEPARTED", "HALTED": "STOPPED", "DROPPED_OFF": "DELIVERED"}
INCIDENTS = {
    "external_id": "code", "mission_external_id": "shipment_ref", "type": "category", "severity": "level",
    "description": "summary", "opened_at": "raised_at", "resolved_at": "closed_at",
}
LEVELS = {"MINOR": "LOW", "MAJOR": "MEDIUM", "CRITICAL": "HIGH"}
CORRECT = [ENDPOINTS, MISSIONS, STATES, EVENTS, KINDS, INCIDENTS, LEVELS]


class ScriptedLlm:
    def __init__(self, *answers: dict) -> None:
        self.answers = list(answers)
        self.schemas: list[dict] = []

    def complete_json(self, system: str, user: str, schema: dict, max_tokens: int = 200) -> dict:
        self.schemas.append(schema)
        return self.answers.pop(0)


def spec_of(app) -> dict:
    return TestClient(app).get("/openapi.json").json()


def generate(*answers: dict):
    return generate_draft(
        ScriptedLlm(*answers), parse_openapi(spec_of(gamma_app)),
        "tms_gamma", "TMS Gamma", "http://gamma.test", "MOCK_GAMMA_API_KEY",
    )


def gamma_client(config: TmsConfig) -> GenericTmsClient:
    return GenericTmsClient(
        config, TestClient(gamma_app), secret_provider=lambda _: get_mock_settings().mock_gamma_api_key
    )


def test_parser_deduces_auth_pagination_and_field_kinds() -> None:
    api = parse_openapi(spec_of(gamma_app))
    shipments = next(e for e in api.endpoints if e.path == "/api/shipments")
    kinds = {f.path: f.kind for f in shipments.fields}

    assert api.auth_header == "X-Gamma-Token"
    assert infer_pagination(shipments)["size_param"] == "per_page"
    assert infer_pagination(shipments)["total_path"] == "meta.count"
    assert kinds["schedule.eta"] == FieldKind.DATETIME
    assert kinds["waypoints"] == FieldKind.GEO_LIST
    assert kinds["gps"] == FieldKind.GEO


def test_parser_also_reads_the_other_tms() -> None:
    alpha = parse_openapi(spec_of(alpha_app))

    missions = next(e for e in alpha.endpoints if e.path == "/api/v1/missions")
    assert alpha.auth_header == "X-API-Key"
    assert infer_pagination(missions)["page_param"] == "page"
    assert missions.items_path == "data"


def test_token_authentication_is_not_guessed() -> None:
    with pytest.raises(OpenApiError, match="clé d'API"):
        parse_openapi(spec_of(beta_app))


def test_expected_fields_match_the_canonical_models() -> None:
    models = {"missions": Mission, "events": MissionEvent, "incidents": Incident}
    for resource, model in models.items():
        fields = set(model.model_fields) - {"source_tms"}
        assert set(RESOURCES[resource]) == fields


def test_correct_answers_produce_a_config_that_reads_gamma() -> None:
    draft = generate(*CORRECT)
    config = TmsConfig.model_validate(draft.config)

    report, issues = probe(gamma_client(config))

    assert draft.issues == [] and issues == []
    assert "Missions : 3 lues, 0 rejetées" in report
    assert config.resources.events.path == "/api/shipments/{mission_id}/history"
    missions = gamma_client(config).get_missions().items
    assert {m.status for m in missions} == {MissionStatus.DELIVERED, MissionStatus.IN_TRANSIT, MissionStatus.BLOCKED}
    assert missions[1].last_position.lat == 33.0


def test_invented_field_is_dropped_and_reported() -> None:
    answers = [ENDPOINTS, {**MISSIONS, "driver_name": "pas_un_champ"}, STATES, EVENTS, KINDS, INCIDENTS, LEVELS]

    draft = generate(*answers)

    assert "driver_name" not in draft.config["resources"]["missions"]["mapping"]
    assert any("driver_name" in issue for issue in draft.issues)


def test_missing_required_field_makes_the_draft_invalid() -> None:
    first = {**MISSIONS, "updated_at": None}
    answers = [ENDPOINTS, first, {"updated_at": None}, STATES, EVENTS, KINDS, INCIDENTS, LEVELS]

    draft = generate(*answers)

    with pytest.raises(ValueError, match="updated_at"):
        TmsConfig.model_validate(draft.config)


def test_same_field_used_twice_is_reported() -> None:
    mapping = {**MISSIONS, "planned_arrival": "schedule.pickup_at"}

    draft = generate(ENDPOINTS, mapping, STATES, EVENTS, KINDS, INCIDENTS, LEVELS)

    assert any("schedule.pickup_at" in issue for issue in draft.issues)


def test_inverted_dates_are_detected_by_the_real_probe() -> None:
    mapping = {**MISSIONS, "planned_departure": "schedule.dropoff_at", "planned_arrival": "schedule.pickup_at"}

    draft = generate(ENDPOINTS, mapping, STATES, EVENTS, KINDS, INCIDENTS, LEVELS)
    _, issues = probe(gamma_client(TmsConfig.model_validate(draft.config)))

    assert any("Départ prévu après" in issue for issue in issues)


def test_unknown_translation_is_kept_unknown_and_reported() -> None:
    states = {**STATES, "ON_ROAD": "UNKNOWN"}

    draft = generate(ENDPOINTS, MISSIONS, states, EVENTS, KINDS, INCIDENTS, LEVELS)
    _, issues = probe(gamma_client(TmsConfig.model_validate(draft.config)))

    assert any("Statut non traduit" in issue and "GM-302" in issue for issue in issues)


def test_missing_route_stops_the_generation() -> None:
    draft = generate({**ENDPOINTS, "incidents": None})

    assert draft.config == {}
    assert any("incidents" in issue for issue in draft.issues)


def test_llm_can_only_choose_existing_fields_of_the_right_kind() -> None:
    llm = ScriptedLlm(*CORRECT)
    generate_draft(llm, parse_openapi(spec_of(gamma_app)), "g", "G", "http://g.test", "K")

    choices = llm.schemas[1]["properties"]["planned_arrival"]["anyOf"][0]["enum"]
    assert "schedule.dropoff_at" in choices and "shipper" not in choices


def test_drafts_are_never_loaded_by_the_registry() -> None:
    assert "tms_gamma" not in {config.id for config in TmsRegistry.from_directory().all()}


def test_required_field_left_empty_gets_a_second_short_question() -> None:
    first = {**MISSIONS, "destination_city": None}
    llm = ScriptedLlm(
        ENDPOINTS, first, {"destination_city": "end.town"}, STATES, EVENTS, KINDS, INCIDENTS, LEVELS
    )

    draft = generate_draft(llm, parse_openapi(spec_of(gamma_app)), "g", "G", "http://g.test", "K")

    assert draft.config["resources"]["missions"]["mapping"]["destination_city"] == "end.town"
    retry_choices = llm.schemas[2]["properties"]["destination_city"]["anyOf"][0]["enum"]
    assert "start.town" not in retry_choices


def test_optional_field_left_empty_is_not_asked_again() -> None:
    first = {**MISSIONS, "driver_name": None}
    llm = ScriptedLlm(ENDPOINTS, first, STATES, EVENTS, KINDS, INCIDENTS, LEVELS)

    draft = generate_draft(llm, parse_openapi(spec_of(gamma_app)), "g", "G", "http://g.test", "K")

    assert "driver_name" not in draft.config["resources"]["missions"]["mapping"]
    assert any("driver_name" in issue for issue in draft.issues)


def test_nested_field_keeps_the_meaning_given_by_its_parent() -> None:
    api = parse_openapi(spec_of(gamma_app))
    shipments = next(e for e in api.endpoints if e.path == "/api/shipments")
    descriptions = {f.path: f.description for f in shipments.fields}

    assert "arrivée" in descriptions["end.town"]
    assert "départ" in descriptions["start.town"]
