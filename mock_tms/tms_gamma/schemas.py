"""Formats de données exposés par le TMS Gamma."""

from datetime import datetime
from enum import StrEnum
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class ShipmentState(StrEnum):
    SCHEDULED = "SCHEDULED"
    ON_ROAD = "ON_ROAD"
    COMPLETED = "COMPLETED"
    STUCK = "STUCK"
    ABORTED = "ABORTED"


class HistoryKind(StrEnum):
    LEFT_SITE = "LEFT_SITE"
    HALTED = "HALTED"
    DROPPED_OFF = "DROPPED_OFF"


class IssueLevel(StrEnum):
    MINOR = "MINOR"
    MAJOR = "MAJOR"
    CRITICAL = "CRITICAL"


class GpsFix(BaseModel):
    latitude: float
    longitude: float


class Stop(BaseModel):
    town: str = Field(description="Ville de l'étape")


class Schedule(BaseModel):
    pickup_at: datetime = Field(description="Enlèvement prévu")
    dropoff_at: datetime = Field(description="Livraison prévue")
    eta: datetime | None = Field(default=None, description="Heure de livraison estimée")


class Execution(BaseModel):
    picked_up_at: datetime | None = Field(default=None, description="Enlèvement réel")
    dropped_off_at: datetime | None = Field(default=None, description="Livraison réelle")


class Shipment(BaseModel):
    ref: str = Field(description="Référence unique de l'expédition")
    state: ShipmentState = Field(description="État de l'expédition")
    shipper: str = Field(description="Client donneur d'ordre")
    truck: str = Field(description="Immatriculation du camion")
    operator: str | None = Field(default=None, description="Nom du conducteur")
    start: Stop = Field(description="Lieu de départ")
    end: Stop = Field(description="Lieu d'arrivée")
    schedule: Schedule
    execution: Execution
    waypoints: list[GpsFix] = Field(description="Itinéraire prévu")
    gps: GpsFix | None = Field(default=None, description="Dernière position connue")
    gps_time: datetime | None = Field(default=None, description="Heure de la dernière position")
    modified_at: datetime = Field(description="Dernière modification de l'expédition")


class HistoryEntry(BaseModel):
    uid: str = Field(description="Identifiant de l'entrée d'historique")
    shipment_ref: str = Field(description="Référence de l'expédition")
    kind: HistoryKind = Field(description="Nature de l'événement")
    at: datetime = Field(description="Heure de l'événement")
    label: str = Field(description="Description de l'événement")


class Issue(BaseModel):
    code: str = Field(description="Identifiant unique du problème")
    shipment_ref: str = Field(description="Référence de l'expédition concernée")
    category: str = Field(description="Catégorie du problème")
    level: IssueLevel = Field(description="Gravité du problème")
    summary: str = Field(description="Description du problème")
    raised_at: datetime = Field(description="Date d'ouverture du problème")
    closed_at: datetime | None = Field(default=None, description="Date de clôture du problème")


class Meta(BaseModel):
    count: int
    page: int
    per_page: int


class Listing(BaseModel, Generic[T]):
    items: list[T]
    meta: Meta


class GammaDataset(BaseModel):
    shipments: list[Shipment]
    history: list[HistoryEntry]
    issues: list[Issue]
