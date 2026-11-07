"""API REST du TMS Alpha simulé."""

import secrets
from typing import Annotated, TypeVar

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Security, status
from fastapi.security import APIKeyHeader

from mock_tms.common import MockTmsSettings, get_mock_settings, load_dataset
from mock_tms.tms_alpha.schemas import (
    Dataset,
    Incident,
    IncidentStatus,
    Item,
    Items,
    Mission,
    MissionEvent,
    MissionStatus,
    Page,
    Pagination,
)

T = TypeVar("T")

DATASET = Dataset.model_validate(load_dataset("tms_alpha"))

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

PageNumber = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]


def require_api_key(
    api_key: Annotated[str | None, Security(api_key_header)],
    settings: Annotated[MockTmsSettings, Depends(get_mock_settings)],
) -> None:
    """Refuse la requête si la clé d'API est absente ou incorrecte."""
    expected = settings.mock_alpha_api_key.encode()
    if api_key is None or not secrets.compare_digest(api_key.encode(), expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing API key")


def paginate(items: list[T], page: int, page_size: int) -> Page[T]:
    """Retourne la page demandée d'une liste."""
    start = (page - 1) * page_size
    return Page[T](
        data=items[start : start + page_size],
        pagination=Pagination(page=page, page_size=page_size, total=len(items)),
    )


def find_mission(mission_id: str) -> Mission:
    """Retourne une mission, ou une erreur 404 si elle n'existe pas."""
    for mission in DATASET.missions:
        if mission.mission_id == mission_id:
            return mission
    raise HTTPException(status.HTTP_404_NOT_FOUND, "Mission not found")


router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])


@router.get("/missions", response_model=Page[Mission], response_model_exclude_none=True)
def list_missions(
    mission_status: Annotated[MissionStatus | None, Query(alias="status")] = None,
    page: PageNumber = 1,
    page_size: PageSize = 20,
) -> Page[Mission]:
    """Liste les missions, avec un filtre facultatif sur le statut."""
    missions = [
        mission
        for mission in DATASET.missions
        if mission_status is None or mission.status == mission_status
    ]
    return paginate(missions, page, page_size)


@router.get(
    "/missions/{mission_id}",
    response_model=Item[Mission],
    response_model_exclude_none=True,
)
def get_mission(mission_id: str) -> Item[Mission]:
    """Retourne le détail d'une mission."""
    return Item[Mission](data=find_mission(mission_id))


@router.get("/missions/{mission_id}/events", response_model=Items[MissionEvent])
def list_mission_events(mission_id: str) -> Items[MissionEvent]:
    """Retourne l'historique des événements d'une mission."""
    find_mission(mission_id)
    events = [event for event in DATASET.events if event.mission_id == mission_id]
    return Items[MissionEvent](data=events)


@router.get("/incidents", response_model=Page[Incident], response_model_exclude_none=True)
def list_incidents(
    incident_status: Annotated[IncidentStatus | None, Query(alias="status")] = None,
    page: PageNumber = 1,
    page_size: PageSize = 20,
) -> Page[Incident]:
    """Liste les incidents, avec un filtre facultatif sur le statut."""
    incidents = [
        incident
        for incident in DATASET.incidents
        if incident_status is None or incident.status == incident_status
    ]
    return paginate(incidents, page, page_size)


def create_app() -> FastAPI:
    """Crée l'application du TMS Alpha."""
    application = FastAPI(title="TMS Alpha (simulé)", version="1.0.0")
    application.include_router(router)
    return application


app = create_app()
