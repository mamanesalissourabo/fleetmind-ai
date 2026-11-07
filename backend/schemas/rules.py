"""Format des fichiers de règles métier, propres à chaque TMS."""

from typing import Annotated, Literal

from pydantic import Field, NonNegativeInt, PositiveFloat, PositiveInt, model_validator

from backend.schemas.canonical import Mission, MissionStatus, Severity
from backend.schemas.tms_config import StrictConfig


class MissionRule(StrictConfig):
    """Règle évaluée sur une mission dont le statut figure dans `statuses`."""

    id: str
    severity: Severity
    statuses: list[MissionStatus]


class EstimatedDelayRule(MissionRule):
    type: Literal["estimated_delay"]
    min_delay_minutes: PositiveInt


class OverdueArrivalRule(MissionRule):
    type: Literal["overdue_arrival"]
    grace_minutes: NonNegativeInt


class RouteDeviationRule(MissionRule):
    type: Literal["route_deviation"]
    max_distance_km: PositiveFloat


class MissingInformationRule(MissionRule):
    type: Literal["missing_information"]
    fields: list[str]

    @model_validator(mode="after")
    def check_fields_exist(self) -> "MissingInformationRule":
        unknown = set(self.fields) - set(Mission.model_fields)
        if unknown:
            raise ValueError(f"Champs de mission inconnus : {sorted(unknown)}")
        return self


class StaleDataRule(MissionRule):
    type: Literal["stale_data"]
    max_age_minutes: PositiveInt


class OpenIncidentRule(StrictConfig):
    """Règle évaluée sur les incidents ouverts, avec leur propre gravité."""

    type: Literal["open_incident"]
    id: str
    min_severity: Severity


Rule = Annotated[
    EstimatedDelayRule
    | OverdueArrivalRule
    | RouteDeviationRule
    | MissingInformationRule
    | StaleDataRule
    | OpenIncidentRule,
    Field(discriminator="type"),
]


class Escalation(StrictConfig):
    after_minutes: PositiveInt
    recipient: str


class NotificationPolicy(StrictConfig):
    """Qui prévenir, à partir de quelle gravité, et quand relancer ou escalader."""

    min_severity: Severity
    recipients: list[str] = Field(min_length=1)
    reminder_interval_minutes: PositiveInt
    max_reminders: NonNegativeInt
    escalations: list[Escalation] = []

    @model_validator(mode="after")
    def check_escalation_order(self) -> "NotificationPolicy":
        delays = [escalation.after_minutes for escalation in self.escalations]
        if delays != sorted(set(delays)):
            raise ValueError("Les délais d'escalade doivent être strictement croissants")
        return self


class RuleSet(StrictConfig):
    """Ensemble versionné des règles d'un TMS."""

    tms_id: str
    version: PositiveInt
    origin: Literal["demonstration", "business"]
    rules: list[Rule]
    notifications: NotificationPolicy
    incident_procedure_topics: dict[str, str] = {}

    @model_validator(mode="after")
    def check_unique_rule_ids(self) -> "RuleSet":
        ids = [rule.id for rule in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("Identifiants de règles en double")
        return self
