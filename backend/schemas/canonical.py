"""Modèle canonique commun : la représentation unique des données de tous les TMS."""

from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, computed_field


class MissionStatus(StrEnum):
    PLANNED = "PLANNED"
    IN_TRANSIT = "IN_TRANSIT"
    DELIVERED = "DELIVERED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class EventType(StrEnum):
    DEPARTED = "DEPARTED"
    STOPPED = "STOPPED"
    DELIVERED = "DELIVERED"
    UNKNOWN = "UNKNOWN"


class Severity(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"


class GeoPoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    lat: float
    lon: float


class CanonicalRecord(BaseModel):
    """Base des données normalisées : origine et identifiant dans le TMS source."""

    model_config = ConfigDict(frozen=True)

    source_tms: str
    external_id: str


class Mission(CanonicalRecord):
    status: MissionStatus
    customer_name: str
    vehicle_plate: str
    driver_name: str | None = None
    origin_city: str
    destination_city: str
    planned_departure: AwareDatetime
    planned_arrival: AwareDatetime
    actual_departure: AwareDatetime | None = None
    actual_arrival: AwareDatetime | None = None
    estimated_arrival: AwareDatetime | None = None
    planned_route: list[GeoPoint] = []
    last_position: GeoPoint | None = None
    last_position_at: AwareDatetime | None = None
    updated_at: AwareDatetime


class MissionEvent(CanonicalRecord):
    mission_external_id: str
    type: EventType
    occurred_at: AwareDatetime
    description: str


class Incident(CanonicalRecord):
    mission_external_id: str
    type: str
    severity: Severity
    description: str
    opened_at: AwareDatetime
    resolved_at: AwareDatetime | None = None

    @computed_field
    @property
    def is_open(self) -> bool:
        return self.resolved_at is None
