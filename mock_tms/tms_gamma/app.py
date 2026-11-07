"""API REST du TMS Gamma simulé."""

import secrets
from typing import Annotated, TypeVar

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Security, status
from fastapi.security import APIKeyHeader

from mock_tms.common import MockTmsSettings, get_mock_settings, load_dataset
from mock_tms.tms_gamma.schemas import GammaDataset, HistoryEntry, Issue, Listing, Meta, Shipment

T = TypeVar("T")
DATA = GammaDataset.model_validate(load_dataset("tms_gamma"))
header = APIKeyHeader(name="X-Gamma-Token", auto_error=False)
PageNumber = Annotated[int, Query(ge=1)]
PerPage = Annotated[int, Query(ge=1, le=25)]


def check_token(
    token: Annotated[str | None, Security(header)],
    settings: Annotated[MockTmsSettings, Depends(get_mock_settings)],
) -> None:
    expected = settings.mock_gamma_api_key.encode()
    if token is None or not secrets.compare_digest(token.encode(), expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")


def listing(items: list[T], page: int, per_page: int) -> Listing[T]:
    start = (page - 1) * per_page
    return Listing[T](
        items=items[start : start + per_page],
        meta=Meta(count=len(items), page=page, per_page=per_page),
    )


router = APIRouter(prefix="/api", dependencies=[Depends(check_token)])


@router.get("/shipments", response_model=Listing[Shipment], summary="Liste des expéditions")
def list_shipments(page: PageNumber = 1, per_page: PerPage = 10) -> Listing[Shipment]:
    """Retourne les expéditions de transport, c'est-à-dire les missions en cours et passées."""
    return listing(DATA.shipments, page, per_page)


@router.get("/shipments/{ref}/history", response_model=list[HistoryEntry], summary="Historique d'une expédition")
def shipment_history(ref: str) -> list[HistoryEntry]:
    """Retourne les événements de suivi d'une expédition."""
    if all(shipment.ref != ref for shipment in DATA.shipments):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown shipment")
    return [entry for entry in DATA.history if entry.shipment_ref == ref]


@router.get("/issues", response_model=Listing[Issue], summary="Liste des problèmes")
def list_issues(page: PageNumber = 1, per_page: PerPage = 10) -> Listing[Issue]:
    """Retourne les incidents signalés sur les expéditions."""
    return listing(DATA.issues, page, per_page)


def create_app() -> FastAPI:
    application = FastAPI(title="TMS Gamma (simulé)", version="3.0.0")
    application.include_router(router)
    return application


app = create_app()
