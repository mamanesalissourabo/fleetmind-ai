"""Tests du moteur de règles et du cycle de surveillance."""

from datetime import UTC, datetime

import httpx
import pytest

from backend.integrations.errors import TmsConfigError
from backend.integrations.generic_client import GenericTmsClient
from backend.schemas.canonical import GeoPoint, Severity
from backend.schemas.findings import CycleStatus, FindingNature
from backend.schemas.rules import RuleSet
from backend.services.geo import distance_to_route_km
from backend.services.monitoring import MonitoringService
from backend.services.rules_engine import evaluate_mission
from backend.services.rules_registry import RulesRegistry
from tests.helpers import REGISTRY, client_for, mock_secret

REFERENCE = datetime(2026, 11, 7, 11, 0, tzinfo=UTC)
RULES = RulesRegistry.from_directory()
SERVICE = MonitoringService(RULES)

EXPECTED = {
    "tms_alpha": {
        "AL-1002": {"estimated_delay"},
        "AL-1003": {"overdue_arrival", "open_incident"},
        "AL-1004": {"estimated_delay", "route_deviation"},
        "AL-1005": {"overdue_arrival", "missing_information", "stale_data"},
    },
    "tms_beta": {
        "BT-7782": {"estimated_delay", "overdue_arrival"},
        "BT-7783": {"overdue_arrival", "open_incident"},
        "BT-7784": {"route_deviation"},
        "BT-7785": {"overdue_arrival", "missing_information", "stale_data"},
    },
}


@pytest.mark.parametrize("tms_id", EXPECTED)
def test_cycle_detects_expected_scenarios(tms_id: str) -> None:
    report = SERVICE.run_cycle(client_for(tms_id), REFERENCE)

    found: dict[str, set[str]] = {}
    for finding in report.findings:
        found.setdefault(finding.mission_external_id, set()).add(finding.rule_type)

    assert report.status == CycleStatus.COMPLETE
    assert report.missions_evaluated == 5
    assert report.ruleset_origin == "demonstration"
    assert found == EXPECTED[tms_id]


def test_same_mission_is_judged_only_by_the_rules_of_each_ruleset() -> None:
    mission = next(
        m for m in client_for("tms_beta").get_missions().items if m.external_id == "BT-7784"
    )

    beta_types = {f.rule_type for f in evaluate_mission(mission, RULES.get("tms_beta"), REFERENCE)}
    alpha_types = {f.rule_type for f in evaluate_mission(mission, RULES.get("tms_alpha"), REFERENCE)}

    assert "estimated_delay" not in beta_types
    assert "estimated_delay" in alpha_types


def test_findings_separate_facts_risks_and_missing_information() -> None:
    report = SERVICE.run_cycle(client_for("tms_alpha"), REFERENCE)
    natures = {
        (f.mission_external_id, f.rule_type): f.nature for f in report.findings
    }

    assert natures[("AL-1002", "estimated_delay")] == FindingNature.RISK
    assert natures[("AL-1004", "route_deviation")] == FindingNature.FACT
    assert natures[("AL-1005", "missing_information")] == FindingNature.MISSING_INFORMATION


def test_tms_without_rules_fails_instead_of_using_other_rules() -> None:
    only_alpha = MonitoringService(RulesRegistry([RULES.get("tms_alpha")]))

    report = only_alpha.run_cycle(client_for("tms_beta"), REFERENCE)

    assert report.status == CycleStatus.FAILED
    assert report.findings == []
    assert "tms_beta" in (report.error or "")


def stub_client(handler) -> GenericTmsClient:
    http = httpx.Client(base_url="http://tms.test", transport=httpx.MockTransport(handler))
    return GenericTmsClient(REGISTRY.get("tms_alpha"), http, secret_provider=mock_secret)


def alpha_responses(missions: list[dict] = [], incidents: list[dict] = []) -> GenericTmsClient:
    def handler(request: httpx.Request) -> httpx.Response:
        items = missions if request.url.path.endswith("/missions") else incidents
        return httpx.Response(200, json={"data": items, "pagination": {"total": len(items)}})

    return stub_client(handler)


def alpha_mission(**overrides: object) -> dict:
    return {
        "mission_id": "AL-1", "status": "IN_TRANSIT", "customer_name": "C", "vehicle_plate": "P",
        "origin": {"city": "A"}, "destination": {"city": "B"},
        "planned_departure": "2026-11-07T08:00:00Z", "planned_arrival": "2026-11-07T20:00:00Z",
        "updated_at": "2026-11-07T10:55:00Z", **overrides,
    }


def test_unavailable_tms_gives_failed_report_not_an_empty_success() -> None:
    report = SERVICE.run_cycle(stub_client(lambda _: httpx.Response(503)), REFERENCE)

    assert report.status == CycleStatus.FAILED
    assert report.missions_evaluated == 0
    assert "TmsUnavailableError" in (report.error or "")


def test_unknown_status_and_rejected_records_are_reported() -> None:
    missions = [alpha_mission(status="TELEPORTING"), alpha_mission(updated_at="CONFIDENTIEL")]

    report = SERVICE.run_cycle(alpha_responses(missions=missions), REFERENCE)

    assert report.status == CycleStatus.PARTIAL
    assert report.unknown_status_missions == ["AL-1"]
    assert len(report.rejected_records) == 1
    assert "CONFIDENTIEL" not in report.rejected_records[0]


def test_incident_with_unknown_severity_is_never_ignored() -> None:
    incident = {
        "incident_id": "I-1", "mission_id": "AL-1", "type": "X", "severity": "CRITIQUE",
        "description": "d", "opened_at": "2026-11-07T10:00:00Z",
    }

    report = SERVICE.run_cycle(alpha_responses(incidents=[incident]), REFERENCE)

    assert [finding.severity for finding in report.findings] == [Severity.UNKNOWN]


def test_naive_reference_time_is_refused() -> None:
    with pytest.raises(ValueError, match="fuseau"):
        SERVICE.run_cycle(client_for("tms_alpha"), datetime(2026, 11, 7, 11, 0))


def test_distance_to_route() -> None:
    route = [GeoPoint(lat=45.0, lon=4.0), GeoPoint(lat=46.0, lon=4.0)]

    assert distance_to_route_km(GeoPoint(lat=45.5, lon=4.0), route) == pytest.approx(0, abs=0.01)
    assert distance_to_route_km(GeoPoint(lat=45.5, lon=5.0), route) == pytest.approx(78, abs=1)


def test_invalid_rule_field_is_refused() -> None:
    with pytest.raises(ValueError, match="inconnu"):
        RuleSet.model_validate({
            "tms_id": "x", "version": 1, "origin": "demonstration",
            "rules": [{"id": "r", "type": "missing_information", "severity": "LOW",
                       "statuses": ["IN_TRANSIT"], "fields": ["pas_un_champ"]}],
        })


def test_unknown_tms_has_no_rules() -> None:
    with pytest.raises(TmsConfigError):
        RULES.get("tms_gamma")
