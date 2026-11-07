"""Formats de données exposés par le TMS Beta."""

from enum import IntEnum
from typing import Annotated, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

T = TypeVar("T")

# Date locale (Africa/Casablanca) au format JJ/MM/AAAA HH:MM.
DateLocale = Annotated[str, Field(pattern=r"^\d{2}/\d{2}/\d{4} \d{2}:\d{2}$")]
# Position GPS au format "latitude,longitude".
PositionGps = Annotated[str, Field(pattern=r"^-?\d+(\.\d+)?,-?\d+(\.\d+)?$")]


class EtatTransport(IntEnum):
    """10 planifié, 20 en cours, 30 livré, 40 bloqué, 90 annulé."""

    PLANIFIE = 10
    EN_COURS = 20
    LIVRE = 30
    BLOQUE = 40
    ANNULE = 90


class Gravite(IntEnum):
    """1 faible, 2 moyenne, 3 élevée."""

    FAIBLE = 1
    MOYENNE = 2
    ELEVEE = 3


class ModeleBeta(BaseModel):
    """Modèle de base : les champs sont exposés en camelCase."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class Transport(ModeleBeta):
    reference: str
    etat: EtatTransport
    client: str
    immatriculation: str
    chauffeur: str | None
    ville_depart: str
    ville_arrivee: str
    depart_prevu: DateLocale
    arrivee_prevue: DateLocale
    depart_reel: DateLocale | None
    arrivee_reelle: DateLocale | None
    arrivee_estimee: DateLocale | None
    itineraire_prevu: list[PositionGps]
    derniere_position: PositionGps | None
    date_derniere_position: DateLocale | None
    date_maj: DateLocale


class Jalon(ModeleBeta):
    id_jalon: str
    reference_transport: str
    code: str
    date: DateLocale
    commentaire: str


class Anomalie(ModeleBeta):
    id_anomalie: str
    reference_transport: str
    code: str
    gravite: Gravite
    libelle: str
    date_ouverture: DateLocale
    date_cloture: DateLocale | None


class Resultats(ModeleBeta, Generic[T]):
    resultats: list[T]
    total: int
    offset: int
    limit: int


class DemandeJeton(ModeleBeta):
    client_id: str
    client_secret: str


class Jeton(ModeleBeta):
    jeton: str
    expire_dans: int


class JeuDeDonnees(ModeleBeta):
    """Contenu du fichier de données synthétiques."""

    transports: list[Transport]
    jalons: list[Jalon]
    anomalies: list[Anomalie]
