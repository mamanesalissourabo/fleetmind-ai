"""Statuts et formats d'échange des alertes."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from backend.schemas.findings import MonitoringReport


class AlertStatus(StrEnum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"


class HistoryAction(StrEnum):
    OPENED = "OPENED"
    SEVERITY_CHANGED = "SEVERITY_CHANGED"
    NOTIFIED = "NOTIFIED"
    REMINDED = "REMINDED"
    ESCALATED = "ESCALATED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"
    AUTO_RESOLVED = "AUTO_RESOLVED"


class NotificationKind(StrEnum):
    INITIAL = "INITIAL"
    REMINDER = "REMINDER"
    ESCALATION = "ESCALATION"


class ReadModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class AlertRead(ReadModel):
    id: int
    source_tms: str
    mission_external_id: str
    rule_id: str
    rule_type: str
    ruleset_version: int
    nature: str
    severity: str
    message: str
    details: dict[str, Any]
    status: AlertStatus
    escalation_level: int
    reminders_sent: int
    opened_at: datetime
    last_seen_at: datetime
    acknowledged_by: str | None
    resolved_by: str | None
    resolved_at: datetime | None


class HistoryRead(ReadModel):
    action: str
    actor: str
    occurred_at: datetime
    note: str | None


class NotificationRead(ReadModel):
    kind: str
    recipient: str
    channel: str
    status: str
    message: str
    created_at: datetime


class AlertDetail(AlertRead):
    history: list[HistoryRead]
    notifications: list[NotificationRead]


class AlertAction(BaseModel):
    """L'auteur de l'action vient du compte connecté, jamais de la requête."""

    model_config = ConfigDict(json_schema_extra={"examples": [{"note": "Je m'en occupe"}]})

    note: str | None = Field(default=None, max_length=1000)


class CycleRequest(BaseModel):
    """Sans `tms_id`, tous les TMS ; sans `reference_time`, l'heure de l'horloge de surveillance."""

    model_config = ConfigDict(json_schema_extra={"examples": [{"tms_id": "tms_alpha"}]})

    tms_id: str | None = None
    reference_time: AwareDatetime | None = None


class AlertingOutcome(BaseModel):
    opened: int = 0
    resolved: int = 0
    notifications: int = 0


class CycleResult(BaseModel):
    report: MonitoringReport
    alerts: AlertingOutcome
