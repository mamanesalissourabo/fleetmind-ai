"""Formats d'échange du chatbot : le rapport distingue faits, risques et manques."""

from datetime import date
from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from backend.schemas.findings import FindingNature


class ChatIntent(StrEnum):
    OPEN_INCIDENTS = "open_incidents"
    OPEN_ALERTS = "open_alerts"
    DELIVERY_SUMMARY = "delivery_summary"
    DELAYED_MISSIONS = "delayed_missions"
    MISSING_INFORMATION = "missing_information"
    EXPLAIN_ALERT = "explain_alert"
    SITUATION_BRIEFING = "situation_briefing"
    PROCEDURE_QUESTION = "procedure_question"
    DELAY_RISK = "delay_risk"
    UNSUPPORTED = "unsupported"


class PeriodKind(StrEnum):
    NONE = "none"
    TODAY = "today"
    YESTERDAY = "yesterday"
    THIS_WEEK = "this_week"
    LAST_WEEK = "last_week"
    THIS_MONTH = "this_month"
    LAST_MONTH = "last_month"
    CUSTOM = "custom"


class ReportPeriod(BaseModel):
    """Bornes calculées par le code : début inclus, fin exclue."""

    kind: PeriodKind
    label: str
    start: AwareDatetime
    end: AwareDatetime

    def contains(self, moment: AwareDatetime) -> bool:
        return self.start <= moment < self.end


class SummarySource(StrEnum):
    LLM = "llm"
    DETERMINISTIC = "deterministic"


class Interpretation(BaseModel):
    intent: ChatIntent
    tms_id: str | None = None
    period: PeriodKind = PeriodKind.NONE
    start_date: date | None = None
    end_date: date | None = None
    alert_id: int | None = None


class ChatRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"question": "Quels sont les incidents non résolus ?"}]}
    )

    question: str = Field(min_length=1, max_length=500)


class TmsScope(BaseModel):
    """Ce qui a réellement été consulté pour un TMS, et à quel moment."""

    tms_id: str
    available: bool
    data_as_of: AwareDatetime | None = None
    ruleset_version: int | None = None
    rejected_records: int = 0
    error: str | None = None


class ReportItem(BaseModel):
    nature: FindingNature
    source_tms: str
    mission_id: str | None = None
    text: str


class Report(BaseModel):
    intent: ChatIntent
    reference_time: AwareDatetime
    period: ReportPeriod | None = None
    scope: list[TmsScope]
    items: list[ReportItem] = []
    notes: list[str] = []

    @property
    def complete(self) -> bool:
        return all(tms.available and tms.rejected_records == 0 for tms in self.scope)

    def by_nature(self, nature: FindingNature) -> list[ReportItem]:
        return [item for item in self.items if item.nature == nature]


class DocumentSource(BaseModel):
    """Passage de procédure utilisé, reproduit tel quel pour pouvoir être vérifié."""

    document: str
    title: str
    section: str
    version: int
    origin: str
    score: float
    excerpt: str


class AgentResult(BaseModel):
    """Réponse d'un agent spécialisé ; un brouillon n'est jamais envoyé automatiquement."""

    answered: bool = True
    summary: str
    summary_source: SummarySource
    reference_time: AwareDatetime
    scope: list[TmsScope] = []
    items: list[ReportItem] = []
    draft_message: str | None = None
    suggested_recipient: str | None = None
    sources: list[DocumentSource] = []


class ChatResponse(BaseModel):
    question: str
    intent: ChatIntent
    answered: bool
    summary: str
    summary_source: SummarySource
    reference_time: AwareDatetime
    period: ReportPeriod | None = None
    scope: list[TmsScope] = []
    facts: list[ReportItem] = []
    risks: list[ReportItem] = []
    missing_information: list[ReportItem] = []
    draft_message: str | None = None
    suggested_recipient: str | None = None
    sources: list[DocumentSource] = []
