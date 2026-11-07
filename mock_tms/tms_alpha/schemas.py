"""Formats de données exposés par le TMS Alpha."""

from datetime import datetime
from enum import StrEnum
from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class MissionStatus(StrEnum):
    PLANNED = "PLANNED"
    IN_TRANSIT = "IN_TRANSIT"
    DELIVERED = "DELIVERED"
    ON_HOLD = "ON_HOLD"
    CANCELLED = "CANCELLED"


class IncidentStatus(StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"


class Severity(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class Coordinates(BaseModel):
    lat: float
    lon: float


class Location(Coordinates):
    city: str


class Position(Coordinates):
    recorded_at: datetime


class Driver(BaseModel):
    name: str
    phone: str | None = None


class Mission(BaseModel):
    mission_id: str
    status: MissionStatus
    customer_name: str
    vehicle_plate: str
    driver: Driver | None = None
    origin: Location
    destination: Location
    planned_departure: datetime
    planned_arrival: datetime
    actual_departure: datetime | None = None
    actual_arrival: datetime | None = None
    estimated_arrival: datetime | None = None
    planned_route: list[Coordinates]
    last_position: Position | None = None
    updated_at: datetime


class MissionEvent(BaseModel):
    event_id: str
    mission_id: str
    type: str
    occurred_at: datetime
    description: str


class Incident(BaseModel):
    incident_id: str
    mission_id: str
    type: str
    severity: Severity
    status: IncidentStatus
    description: str
    opened_at: datetime
    resolved_at: datetime | None = None


class Pagination(BaseModel):
    page: int
    page_size: int
    total: int


class Page(BaseModel, Generic[T]):
    data: list[T]
    pagination: Pagination


class Item(BaseModel, Generic[T]):
    data: T


class Items(BaseModel, Generic[T]):
    data: list[T]


class Dataset(BaseModel):
    """Contenu du fichier de données synthétiques."""

    missions: list[Mission]
    events: list[MissionEvent]
    incidents: list[Incident]
