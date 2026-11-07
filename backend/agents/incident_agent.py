"""Agent d'incident : explique une alerte et prépare un brouillon, sans jamais l'envoyer."""

from collections.abc import Callable
from datetime import datetime
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import Session

from backend.agents.common import describe_alert, is_faithful, other_tms_names, to_source
from backend.agents.llm_client import LlmUnavailableError, OllamaClient
from backend.integrations.errors import TmsConfigError, TmsError
from backend.models.alert import Alert
from backend.schemas.alerts import AlertStatus
from backend.schemas.canonical import Mission
from backend.rag.embeddings import EmbeddingError
from backend.rag.retrieval import DocumentRetriever, RetrievalError
from backend.schemas.chat import AgentResult, DocumentSource, ReportItem, SummarySource, TmsScope
from backend.schemas.findings import FindingNature
from backend.schemas.tms_config import TmsConfig
from backend.services.cycle_runner import ClientOpener
from backend.services.rules_registry import RulesRegistry

SYSTEM_MISSION = "*"
# Sujet de recherche par type de règle : choisi par le code, pas par le LLM.
PROCEDURE_TOPICS = {
    "estimated_delay": "retard de livraison",
    "overdue_arrival": "retard de livraison",
    "route_deviation": "déviation d'itinéraire",
    "missing_information": "mission sans nouvelles, informations manquantes",
    "stale_data": "mission sans nouvelles, informations manquantes",
}
WRITER_PROMPT = """Tu expliques une alerte à un exploitant de transport, en français et en deux phrases courtes, à partir des seules données fournies. N'invente aucune cause, aucun chiffre ni conseil. Cite la mission.

Exemple de données : Alerte 3 (OPEN, gravité MEDIUM), mission XX-1 : Arrivée estimée 40 min après l'heure prévue ; Mission XX-1 : Rabat vers Salé, statut IN_TRANSIT
Exemple de réponse : {"explication": "La mission XX-1 de Rabat vers Salé est en transit. Son arrivée est estimée 40 min après l'heure prévue."}"""
WRITER_SCHEMA = {
    "type": "object",
    "properties": {"explication": {"type": "string"}},
    "required": ["explication"],
}


def _describe_mission(mission: Mission) -> str:
    parts = [
        f"Mission {mission.external_id} : {mission.origin_city} vers {mission.destination_city}",
        f"statut {mission.status.value}",
        f"client {mission.customer_name}",
        f"arrivée prévue {mission.planned_arrival:%d/%m/%Y %H:%M} UTC",
    ]
    if mission.estimated_arrival:
        parts.append(f"arrivée estimée {mission.estimated_arrival:%d/%m/%Y %H:%M} UTC")
    if mission.driver_name:
        parts.append(f"chauffeur {mission.driver_name}")
    return ", ".join(parts)


class IncidentState(TypedDict, total=False):
    alert_id: int
    tms_id: str | None
    alert: Alert
    refusal: str
    items: list[ReportItem]
    scope: list[TmsScope]
    recipient: str | None
    complete: bool
    summary: str
    draft: str | None
    source: SummarySource
    procedure: list[DocumentSource]


class IncidentAgent:
    def __init__(
        self,
        configs: list[TmsConfig],
        open_client: ClientOpener,
        rules: RulesRegistry,
        session: Session,
        now: Callable[[], datetime],
        llm: OllamaClient,
        retriever_factory: Callable[[], DocumentRetriever],
    ) -> None:
        self._configs = {config.id: config for config in configs}
        self._open_client = open_client
        self._rules = rules
        self._session = session
        self._now = now
        self._llm = llm
        self._retriever_factory = retriever_factory
        graph = StateGraph(IncidentState)
        graph.add_node("gather", self._gather)
        graph.add_node("write", self._write)
        graph.add_node("fallback", self._fallback)
        graph.add_node("procedure", self._find_procedure)
        graph.add_edge(START, "gather")
        graph.add_conditional_edges(
            "gather", self._route, {"refuse": END, "write": "write", "fallback": "fallback"}
        )
        graph.add_edge("write", "procedure")
        graph.add_edge("fallback", "procedure")
        graph.add_edge("procedure", END)
        self._graph = graph.compile()

    def run(self, alert_id: int, tms_id: str | None = None) -> AgentResult:
        state = self._graph.invoke({"alert_id": alert_id, "tms_id": tms_id})
        if "refusal" in state:
            return AgentResult(
                answered=False, summary=state["refusal"],
                summary_source=SummarySource.DETERMINISTIC, reference_time=self._now(),
            )
        return AgentResult(
            summary=state["summary"], summary_source=state["source"],
            reference_time=self._now(), scope=state["scope"], items=state["items"],
            draft_message=state.get("draft"), suggested_recipient=state.get("recipient"),
            sources=state.get("procedure", []),
        )

    @staticmethod
    def _route(state: IncidentState) -> str:
        if "refusal" in state:
            return "refuse"
        return "write" if state["complete"] else "fallback"

    def _find_procedure(self, state: IncidentState) -> IncidentState:
        """Joint la procédure telle quelle : elle ne passe pas par le LLM."""
        alert = state["alert"]
        if alert.status == AlertStatus.RESOLVED.value or alert.mission_external_id == SYSTEM_MISSION:
            return {"procedure": []}
        topic = self._procedure_topic(alert)
        try:
            hits = self._retriever_factory().search(topic, alert.source_tms)
        except (EmbeddingError, RetrievalError):
            note = "procédures non consultées, base documentaire indisponible"
            return {"procedure": [], "items": [*state["items"], self._missing(alert, note)]}
        if not hits:
            note = "aucune procédure trouvée pour cette alerte"
            return {"procedure": [], "items": [*state["items"], self._missing(alert, note)]}
        return {"procedure": [to_source(hit) for hit in hits]}

    def _procedure_topic(self, alert: Alert) -> str:
        """Le code d'incident, propre à chaque TMS, est plus fiable que sa description."""
        try:
            topics = self._rules.get(alert.source_tms).incident_procedure_topics
        except TmsConfigError:
            topics = {}
        incident_type = alert.details.get("incident_type")
        return topics.get(incident_type) or PROCEDURE_TOPICS.get(alert.rule_type, alert.message)

    def _gather(self, state: IncidentState) -> IncidentState:
        """Rassemble l'alerte et l'état de sa mission dans son propre TMS, et rien d'autre."""
        alert = self._session.get(Alert, state["alert_id"])
        if alert is None or alert.source_tms not in self._configs:
            return {"refusal": f"L'alerte {state['alert_id']} n'existe pas."}
        if state.get("tms_id") not in (None, alert.source_tms):
            return {"refusal": f"L'alerte {alert.id} n'appartient pas au TMS demandé."}
        items = [
            ReportItem(
                nature=FindingNature(alert.nature), source_tms=alert.source_tms,
                mission_id=alert.mission_external_id, text=describe_alert(alert),
            )
        ]
        scope, complete = self._add_mission_context(alert, items)
        return {
            "alert": alert, "items": items, "scope": scope, "complete": complete,
            "recipient": self._recipient(alert),
        }

    def _add_mission_context(
        self, alert: Alert, items: list[ReportItem]
    ) -> tuple[list[TmsScope], bool]:
        config = self._configs.get(alert.source_tms)
        if alert.mission_external_id == SYSTEM_MISSION or config is None:
            return [TmsScope(tms_id=alert.source_tms, available=config is not None)], True
        try:
            with self._open_client(config) as client:
                missions = client.get_missions()
                events = client.get_mission_events(alert.mission_external_id)
                incidents = client.get_incidents()
        except (TmsError, LookupError) as error:
            items.append(self._missing(alert, "données de la mission indisponibles"))
            unavailable = TmsScope(
                tms_id=config.id, available=False, error=f"{type(error).__name__}: {error}"
            )
            return [unavailable], False

        scope = [
            TmsScope(
                tms_id=config.id, available=True, data_as_of=self._now(),
                rejected_records=len(missions.rejected) + len(events.rejected),
            )
        ]
        mission = next(
            (m for m in missions.items if m.external_id == alert.mission_external_id), None
        )
        if mission is None:
            items.append(self._missing(alert, "mission introuvable dans le TMS"))
            return scope, False

        def fact(text: str) -> ReportItem:
            return ReportItem(
                nature=FindingNature.FACT, source_tms=config.id,
                mission_id=mission.external_id, text=text,
            )

        items.append(fact(_describe_mission(mission)))
        items.extend(
            fact(f"Événement {event.occurred_at:%d/%m/%Y %H:%M} UTC : {event.description}")
            for event in events.items
        )
        items.extend(
            fact(f"Incident {incident.external_id} ouvert, gravité {incident.severity.value} : {incident.description}")
            for incident in incidents.items
            if incident.is_open and incident.mission_external_id == mission.external_id
        )
        return scope, not missions.rejected and not events.rejected

    @staticmethod
    def _missing(alert: Alert, text: str) -> ReportItem:
        return ReportItem(
            nature=FindingNature.MISSING_INFORMATION, source_tms=alert.source_tms,
            mission_id=alert.mission_external_id, text=f"Mission {alert.mission_external_id} : {text}",
        )

    def _recipient(self, alert: Alert) -> str | None:
        """Le destinataire vient des règles du TMS et de l'escalade atteinte, jamais du LLM."""
        try:
            policy = self._rules.get(alert.source_tms).notifications
        except TmsConfigError:
            return None
        level = min(alert.escalation_level, len(policy.escalations))
        return policy.escalations[level - 1].recipient if level else policy.recipients[0]

    def _write(self, state: IncidentState) -> IncidentState:
        """Le LLM n'écrit que l'explication, gardée seulement si elle reste fidèle aux données."""
        alert = state["alert"]
        texts = [item.text for item in state["items"]]
        try:
            result = self._llm.complete_json(
                WRITER_PROMPT, "Données : " + " ; ".join(texts), WRITER_SCHEMA, max_tokens=90
            )
        except LlmUnavailableError:
            return self._fallback(state)
        explanation = str(result.get("explication", "")).strip()
        required = set() if alert.mission_external_id == SYSTEM_MISSION else {alert.mission_external_id}
        forbidden = other_tms_names(self._configs.values(), {alert.source_tms})
        if not explanation or not is_faithful(explanation, " ".join(texts), required, forbidden):
            return self._fallback(state)
        return {"summary": explanation, "draft": _draft(alert), "source": SummarySource.LLM}

    @staticmethod
    def _fallback(state: IncidentState) -> IncidentState:
        alert = state["alert"]
        suffix = "" if state["complete"] else " Contexte incomplet : voir les informations manquantes."
        return {
            "summary": describe_alert(alert) + "." + suffix,
            "draft": _draft(alert),
            "source": SummarySource.DETERMINISTIC,
        }


def _draft(alert: Alert) -> str | None:
    """Brouillon jamais envoyé, et sans objet pour une alerte résolue ou une panne de surveillance."""
    if alert.status == AlertStatus.RESOLVED.value or alert.mission_external_id == SYSTEM_MISSION:
        return None
    return (
        f"Bonjour, concernant la mission {alert.mission_external_id} : {alert.message}. "
        "Merci de confirmer la situation et la prochaine étape."
    )
