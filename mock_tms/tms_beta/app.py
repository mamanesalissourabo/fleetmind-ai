"""API REST du TMS Beta simulé."""

import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, TypeVar

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from mock_tms.common import MockTmsSettings, get_mock_settings, load_dataset
from mock_tms.tms_beta.schemas import (
    Anomalie,
    DemandeJeton,
    EtatTransport,
    JeuDeDonnees,
    Jalon,
    Jeton,
    Resultats,
    Transport,
)

T = TypeVar("T")

DONNEES = JeuDeDonnees.model_validate(load_dataset("tms_beta"))
DUREE_JETON = timedelta(hours=1)

jetons_actifs: dict[str, datetime] = {}
schema_bearer = HTTPBearer(auto_error=False)

Offset = Annotated[int, Query(ge=0)]
Limit = Annotated[int, Query(ge=1, le=50)]


def exiger_jeton(
    identifiants: Annotated[HTTPAuthorizationCredentials | None, Security(schema_bearer)],
) -> None:
    """Refuse la requête si le jeton est absent, inconnu ou expiré."""
    expiration = jetons_actifs.get(identifiants.credentials) if identifiants else None
    if expiration is None or expiration <= datetime.now(UTC):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Jeton absent, invalide ou expiré")


def decouper(elements: list[T], offset: int, limit: int) -> Resultats[T]:
    """Retourne la tranche demandée d'une liste."""
    return Resultats[T](
        resultats=elements[offset : offset + limit],
        total=len(elements),
        offset=offset,
        limit=limit,
    )


def trouver_transport(reference: str) -> Transport:
    """Retourne un transport, ou une erreur 404 s'il n'existe pas."""
    for transport in DONNEES.transports:
        if transport.reference == reference:
            return transport
    raise HTTPException(status.HTTP_404_NOT_FOUND, "Transport introuvable")


routeur_auth = APIRouter(prefix="/auth")
routeur_v2 = APIRouter(prefix="/v2", dependencies=[Depends(exiger_jeton)])


@routeur_auth.post("/token", response_model=Jeton)
def creer_jeton(
    demande: DemandeJeton,
    settings: Annotated[MockTmsSettings, Depends(get_mock_settings)],
) -> Jeton:
    """Délivre un jeton d'accès contre des identifiants valides."""
    id_valide = secrets.compare_digest(
        demande.client_id.encode(), settings.mock_beta_client_id.encode()
    )
    secret_valide = secrets.compare_digest(
        demande.client_secret.encode(), settings.mock_beta_client_secret.encode()
    )
    if not (id_valide and secret_valide):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Identifiants invalides")

    jeton = secrets.token_urlsafe(32)
    jetons_actifs[jeton] = datetime.now(UTC) + DUREE_JETON
    return Jeton(jeton=jeton, expire_dans=int(DUREE_JETON.total_seconds()))


@routeur_v2.get("/transports", response_model=Resultats[Transport])
def lister_transports(
    etat: EtatTransport | None = None,
    offset: Offset = 0,
    limit: Limit = 10,
) -> Resultats[Transport]:
    """Liste les transports, avec un filtre facultatif sur l'état."""
    transports = [t for t in DONNEES.transports if etat is None or t.etat == etat]
    return decouper(transports, offset, limit)


@routeur_v2.get("/transports/{reference}", response_model=Transport)
def lire_transport(reference: str) -> Transport:
    """Retourne le détail d'un transport."""
    return trouver_transport(reference)


@routeur_v2.get("/transports/{reference}/suivi", response_model=list[Jalon])
def lire_suivi(reference: str) -> list[Jalon]:
    """Retourne les jalons de suivi d'un transport."""
    trouver_transport(reference)
    return [jalon for jalon in DONNEES.jalons if jalon.reference_transport == reference]


@routeur_v2.get("/anomalies", response_model=Resultats[Anomalie])
def lister_anomalies(
    ouvertes: bool | None = None,
    offset: Offset = 0,
    limit: Limit = 10,
) -> Resultats[Anomalie]:
    """Liste les anomalies, avec un filtre facultatif sur celles encore ouvertes."""
    anomalies = [
        anomalie
        for anomalie in DONNEES.anomalies
        if ouvertes is None or (anomalie.date_cloture is None) == ouvertes
    ]
    return decouper(anomalies, offset, limit)


def create_app() -> FastAPI:
    """Crée l'application du TMS Beta."""
    application = FastAPI(title="TMS Beta (simulé)", version="2.0.0")
    application.include_router(routeur_auth)
    application.include_router(routeur_v2)
    return application


app = create_app()
