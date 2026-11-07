"""Tests du moteur générique : le même code interroge et normalise Alpha et Beta."""

from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from backend.integrations.errors import (
    TmsAuthError,
    TmsConfigError,
    TmsResponseError,
    TmsUnavailableError,
)
from backend.integrations.generic_client import GenericTmsClient
from backend.integrations.tms_registry import TmsRegistry
from backend.schemas.canonical import EventType, GeoPoint, MissionStatus, Severity
from backend.schemas.tms_config import TmsConfig
from tests.helpers import APPS, REGISTRY, client_for, mock_secret


def stubbed_client(tms_id: str, handler: Callable[[httpx.Request], httpx.Response]) -> GenericTmsClient:
    http = httpx.Client(base_url="http://tms.test", transport=httpx.MockTransport(handler))
    return GenericTmsClient(REGISTRY.get(tms_id), http, secret_provider=mock_secret)


def find(items: list, external_id: str):
    return next(item for item in items if item.external_id == external_id)


@pytest.mark.parametrize("tms_id", APPS)
def test_each_tms_is_fully_normalized(tms_id: str) -> None:
    client = client_for(tms_id)

    missions = client.get_missions()
    incidents = client.get_incidents()

    assert len(missions.items) == 5 and missions.rejected == []
    assert len(incidents.items) == 2 and incidents.rejected == []
    assert all(mission.source_tms == tms_id for mission in missions.items)
    assert {mission.status for mission in missions.items} == {
        MissionStatus.DELIVERED, MissionStatus.IN_TRANSIT, MissionStatus.BLOCKED,
    }


def test_formats_converge_on_the_same_canonical_values() -> None:
    alpha = find(client_for("tms_alpha").get_missions().items, "AL-1002")
    beta = find(client_for("tms_beta").get_missions().items, "BT-7782")

    assert alpha.planned_arrival == datetime(2026, 11, 7, 11, 0, tzinfo=UTC)
    assert beta.planned_arrival == datetime(2026, 11, 7, 10, 15, tzinfo=UTC)
    assert alpha.last_position == GeoPoint(lat=33.0, lon=-7.62)
    assert beta.last_position == GeoPoint(lat=32.73, lon=-9.03)
    assert alpha.status == beta.status == MissionStatus.IN_TRANSIT


def test_omitted_and_null_fields_both_become_none() -> None:
    alpha = find(client_for("tms_alpha").get_missions().items, "AL-1005")
    beta = find(client_for("tms_beta").get_missions().items, "BT-7785")

    for mission in (alpha, beta):
        assert mission.driver_name is None
        assert mission.last_position is None
        assert mission.estimated_arrival is None


@pytest.mark.parametrize("tms_id,mission_id", [("tms_alpha", "AL-1003"), ("tms_beta", "BT-7783")])
def test_events_are_mapped_to_canonical_types(tms_id: str, mission_id: str) -> None:
    events = client_for(tms_id).get_mission_events(mission_id).items

    assert [event.type for event in events] == [EventType.DEPARTED, EventType.STOPPED]


@pytest.mark.parametrize("tms_id,incident_id", [("tms_alpha", "INC-502"), ("tms_beta", "ANO-32")])
def test_open_high_severity_incident_is_recognized(tms_id: str, incident_id: str) -> None:
    incident = find(client_for(tms_id).get_incidents().items, incident_id)

    assert incident.severity == Severity.HIGH
    assert incident.is_open is True


@pytest.mark.parametrize("tms_id", APPS)
def test_all_pages_are_fetched(tms_id: str) -> None:
    config = REGISTRY.get(tms_id)
    small_pages = config.pagination.model_copy(update={"page_size": 2})
    client = client_for(tms_id, config.model_copy(update={"pagination": small_pages}))

    assert len(client.get_missions().items) == 5


def alpha_page(items: list[dict], total: int) -> httpx.Response:
    return httpx.Response(200, json={"data": items, "pagination": {"total": total}})


VALID_ALPHA_MISSION = {
    "mission_id": "AL-1", "status": "IN_TRANSIT", "customer_name": "C", "vehicle_plate": "P",
    "origin": {"city": "A"}, "destination": {"city": "B"},
    "planned_departure": "2026-11-07T08:00:00Z", "planned_arrival": "2026-11-07T09:00:00Z",
    "updated_at": "2026-11-07T08:00:00Z",
}


def test_unknown_status_is_not_guessed() -> None:
    mission = {**VALID_ALPHA_MISSION, "status": "TELEPORTING"}
    client = stubbed_client("tms_alpha", lambda _: alpha_page([mission], 1))

    assert client.get_missions().items[0].status == MissionStatus.UNKNOWN


def test_unreadable_record_is_rejected_without_losing_the_others() -> None:
    naive_date = {**VALID_ALPHA_MISSION, "mission_id": "AL-2", "updated_at": "2026-11-07T08:00:00"}
    no_customer = {key: value for key, value in VALID_ALPHA_MISSION.items() if key != "customer_name"}
    records = [VALID_ALPHA_MISSION, naive_date, no_customer]
    result = stubbed_client("tms_alpha", lambda _: alpha_page(records, 3)).get_missions()

    assert [mission.external_id for mission in result.items] == ["AL-1"]
    assert [rejected.position for rejected in result.rejected] == [1, 2]
    assert "customer_name" in result.rejected[1].reason


def test_incomplete_pagination_raises_instead_of_returning_a_partial_list() -> None:
    client = stubbed_client("tms_alpha", lambda _: alpha_page([], 3))

    with pytest.raises(TmsResponseError, match="pagination incomplète"):
        client.get_missions()


def test_server_error_and_network_failure_mean_unavailable() -> None:
    def network_failure(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(TmsUnavailableError):
        stubbed_client("tms_alpha", lambda _: httpx.Response(503)).get_missions()
    with pytest.raises(TmsUnavailableError):
        stubbed_client("tms_alpha", network_failure).get_missions()


def test_rejected_api_key_raises_auth_error() -> None:
    with pytest.raises(TmsAuthError):
        stubbed_client("tms_alpha", lambda _: httpx.Response(401)).get_missions()


def test_rejected_token_is_renewed_once() -> None:
    tokens = iter(["old", "new"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/token":
            return httpx.Response(200, json={"jeton": next(tokens), "expireDans": 3600})
        if request.headers["Authorization"] == "Bearer old":
            return httpx.Response(401)
        return httpx.Response(200, json={"resultats": [], "total": 0})

    assert stubbed_client("tms_beta", handler).get_missions().items == []


def test_mapping_without_required_field_is_refused_at_load_time() -> None:
    data = REGISTRY.get("tms_alpha").model_dump()
    del data["resources"]["missions"]["mapping"]["updated_at"]

    with pytest.raises(ValueError, match="updated_at"):
        TmsConfig.model_validate(data)


def test_unknown_tms_is_refused() -> None:
    with pytest.raises(TmsConfigError):
        REGISTRY.get("tms_gamma")


def test_demo_data_uses_moroccan_cities_and_time() -> None:
    alpha = find(client_for("tms_alpha").get_missions().items, "AL-1002")
    beta = find(client_for("tms_beta").get_missions().items, "BT-7784")

    assert (alpha.origin_city, alpha.destination_city) == ("Marrakech", "Casablanca")
    assert (beta.origin_city, beta.destination_city) == ("Casablanca", "Marrakech")
    assert beta.planned_departure == datetime(2026, 11, 7, 9, 0, tzinfo=UTC)
