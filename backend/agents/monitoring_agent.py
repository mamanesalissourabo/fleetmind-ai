"""Agent de surveillance : le code classe les priorités et rédige le point de situation."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.agents.common import describe_alert
from backend.models.alert import Alert
from backend.schemas.alerts import AlertStatus
from backend.schemas.canonical import Severity
from backend.schemas.chat import AgentResult, ReportItem, SummarySource, TmsScope
from backend.schemas.findings import FindingNature
from backend.schemas.tms_config import TmsConfig
from backend.services.monitoring_status import get_tms_status
from backend.services.rules_engine import SEVERITY_RANK

TOP_PRIORITIES = 5
REQUIRED_IN_SUMMARY = 3
def priority_key(alert: Alert) -> tuple:
    """Une alerte non prise en charge, grave, escaladée et ancienne passe en premier."""
    return (
        alert.status != AlertStatus.OPEN.value,
        -SEVERITY_RANK[Severity(alert.severity)],
        -alert.escalation_level,
        alert.opened_at,
    )


class BriefingState(TypedDict, total=False):
    tms_id: str | None
    alerts: list[Alert]
    scope: list[TmsScope]
    summary: str
    source: SummarySource


class MonitoringAgent:
    def __init__(
        self,
        configs: list[TmsConfig],
        session: Session,
        now: Callable[[], datetime],
    ) -> None:
        self._configs = configs
        self._session = session
        self._now = now
        graph = StateGraph(BriefingState)
        graph.add_node("gather", self._gather)
        graph.add_node("write", self._write)
        graph.add_edge(START, "gather")
        graph.add_edge("gather", "write")
        graph.add_edge("write", END)
        self._graph = graph.compile()

    def run(self, tms_id: str | None = None) -> AgentResult:
        state = self._graph.invoke({"tms_id": tms_id})
        unavailable = [
            ReportItem(
                nature=FindingNature.MISSING_INFORMATION,
                source_tms=tms.tms_id,
                text=f"{tms.tms_id} : surveillance en retard ou sans cycle réussi",
            )
            for tms in state["scope"]
            if not tms.available
        ]
        priorities = [
            ReportItem(
                nature=FindingNature(alert.nature),
                source_tms=alert.source_tms,
                mission_id=alert.mission_external_id,
                text=describe_alert(alert),
            )
            for alert in state["alerts"][:TOP_PRIORITIES]
        ]
        return AgentResult(
            summary=state["summary"],
            summary_source=state["source"],
            reference_time=self._now(),
            scope=state["scope"],
            items=priorities + unavailable,
        )

    def _selected(self, tms_id: str | None) -> list[TmsConfig]:
        return [config for config in self._configs if tms_id in (None, config.id)]

    def _gather(self, state: BriefingState) -> BriefingState:
        configs = self._selected(state.get("tms_id"))
        now = datetime.now(UTC)
        statuses = [get_tms_status(self._session, config, now) for config in configs]
        alerts = self._session.scalars(
            select(Alert).where(
                Alert.status != AlertStatus.RESOLVED.value,
                Alert.source_tms.in_([config.id for config in configs]),
            )
        )
        return {
            "alerts": sorted(alerts, key=priority_key),
            "scope": [
                TmsScope(
                    tms_id=status.source_tms,
                    available=not status.overdue,
                    data_as_of=status.last_success_at,
                    error=status.error,
                )
                for status in statuses
            ],
        }

    @staticmethod
    def _write(state: BriefingState) -> BriefingState:
        alerts = state["alerts"]
        late = [tms.tms_id for tms in state["scope"] if not tms.available]
        if alerts:
            first = ", ".join(
                f"alerte {alert.id} (mission {alert.mission_external_id})"
                for alert in alerts[:REQUIRED_IN_SUMMARY]
            )
            text = (
                f"{len(alerts)} alerte(s) active(s), dont {_urgent_count(alerts)} grave(s) "
                f"non prise(s) en charge. À traiter d'abord : {first}."
            )
        else:
            text = "Aucune alerte active enregistrée."
        if late:
            text += f" Situation possiblement incomplète : surveillance en retard pour {', '.join(late)}."
        return {"summary": text, "source": SummarySource.DETERMINISTIC}


def _urgent_count(alerts: list[Alert]) -> int:
    return sum(
        alert.status == AlertStatus.OPEN.value
        and SEVERITY_RANK[Severity(alert.severity)] >= SEVERITY_RANK[Severity.HIGH]
        for alert in alerts
    )
