"""Constats produits par les règles et rapport d'un cycle de surveillance."""

from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict

from backend.schemas.canonical import Severity

DetailValue = str | int | float | list[str]


class FindingNature(StrEnum):
    """Distingue ce qui est établi, ce qui est probable et ce qui manque."""

    FACT = "FACT"
    RISK = "RISK"
    MISSING_INFORMATION = "MISSING_INFORMATION"


class Finding(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_tms: str
    mission_external_id: str
    subject_id: str | None = None
    rule_id: str
    rule_type: str
    ruleset_version: int
    nature: FindingNature
    severity: Severity
    message: str
    details: dict[str, DetailValue]
    reference_time: AwareDatetime

    @property
    def dedup_key(self) -> str:
        """Identifie le problème suivi : même clé, même alerte active."""
        parts = [self.source_tms, self.mission_external_id, self.rule_id, self.subject_id]
        return ":".join(part for part in parts if part)


class CycleStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class MonitoringReport(BaseModel):
    """Résultat d'un cycle : un échec ne signifie jamais qu'il n'y a aucun problème."""

    model_config = ConfigDict(frozen=True)

    source_tms: str
    status: CycleStatus
    reference_time: AwareDatetime
    retrieved_at: AwareDatetime
    ruleset_version: int | None = None
    ruleset_origin: str | None = None
    missions_evaluated: int = 0
    findings: list[Finding] = []
    unknown_status_missions: list[str] = []
    rejected_records: list[str] = []
    error: str | None = None
