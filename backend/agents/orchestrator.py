"""Graphe LangGraph du chatbot : comprendre la question, puis appeler un seul agent."""

import re
from datetime import datetime
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from backend.agents.incident_agent import IncidentAgent
from backend.agents.knowledge_agent import KnowledgeAgent
from backend.agents.llm_client import OllamaClient
from backend.agents.monitoring_agent import MonitoringAgent
from backend.agents.periods import (
    InvalidPeriodError,
    correct_relative_period,
    mentions_period,
    period_matches_question,
    resolve_period,
)
from backend.agents.reporting_agent import ReportingAgent
from backend.core.config import get_settings
from backend.schemas.chat import (
    AgentResult,
    ChatIntent,
    ChatResponse,
    Interpretation,
    PeriodKind,
    Report,
    ReportPeriod,
    SummarySource,
)
from backend.schemas.findings import FindingNature
from backend.schemas.tms_config import TmsConfig

UNSUPPORTED_MESSAGE = (
    "Je peux consulter les incidents ouverts, les alertes actives, le bilan des missions, les "
    "retards et les informations manquantes, estimer les risques de retard, expliquer une alerte, "
    "faire un point de situation et retrouver une procédure. Je ne modifie aucune donnée et je "
    "n'envoie aucun message."
)
CLARIFY_PERIOD_MESSAGE = (
    "Je n'ai pas compris la période demandée. Précisez-la, par exemple « cette semaine », "
    "« le mois dernier » ou « du 01/11/2026 au 07/11/2026 »."
)
CURRENT_ONLY_MESSAGE = (
    "Cette demande porte uniquement sur la situation actuelle. Reformulez-la sans période, "
    "ou demandez par exemple les retards ou les incidents de cette période."
)
ALERT_ID_MESSAGE = "Précisez le numéro de l'alerte à expliquer, par exemple « Explique l'alerte 12 »."
CURRENT_ONLY_INTENTS = {
    ChatIntent.EXPLAIN_ALERT,
    ChatIntent.SITUATION_BRIEFING,
    ChatIntent.PROCEDURE_QUESTION,
    ChatIntent.DELAY_RISK,
}
NUMBER_IN_QUESTION = re.compile(r"\d+")


class ChatState(TypedDict, total=False):
    question: str
    reference_time: datetime
    interpretation: Interpretation
    period: ReportPeriod | None
    clarification: str
    report: Report
    agent_result: AgentResult
    summary: str
    summary_source: SummarySource


def build_interpreter_prompt(configs: list[TmsConfig]) -> str:
    """Construit la consigne à partir des TMS configurés, sans en citer aucun dans le code."""
    tms_lines = "\n".join(
        f'- "{config.id}" si {config.name.removeprefix("TMS ")} est cité' for config in configs
    )
    first = configs[0]
    name = first.name.removeprefix("TMS ")
    empty = '"period": "none"'
    return f"""Tu classes une question posée par un exploitant de transport.

Catégories de "intent" :
- open_incidents : incidents, pannes ou anomalies non résolus
- open_alerts : liste des alertes en cours
- delivery_summary : bilan ou état des livraisons et des missions
- delayed_missions : retards, missions en retard
- missing_information : informations manquantes, missions sans nouvelles
- explain_alert : expliquer une alerte précise, désignée par son numéro
- situation_briefing : priorités, urgences, ce qu'il faut traiter d'abord
- delay_risk : prévision, missions qui risquent d'être en retard, sans être encore en retard
- procedure_question : que faire, comment réagir, quelle procédure suivre face à une situation
- unsupported : tout le reste, y compris les demandes de modifier, résoudre, supprimer ou envoyer quelque chose

"tms_id" :
{tms_lines}
- null sinon

"period" : période citée par la question.
- none si aucune date ni période n'est citée
- today, yesterday, this_week, last_week, this_month, last_month selon le cas
- custom pour des dates précises : renseigne alors start_date et end_date au format AAAA-MM-JJ, en utilisant la date du jour si l'année manque
N'écris start_date, end_date et alert_id que s'ils sont utiles.

"alert_id" : numéro de l'alerte pour explain_alert.

Exemples :
Question : Quels retards la semaine dernière ?
{{"intent": "delayed_missions", "tms_id": null, "period": "last_week"}}
Question : Retards du 01/11/2026 au 05/11/2026
{{"intent": "delayed_missions", "tms_id": null, "period": "custom", "start_date": "2026-11-01", "end_date": "2026-11-05"}}
Question : Quelles pannes sont encore ouvertes chez {name} ?
{{"intent": "open_incidents", "tms_id": "{first.id}", {empty}}}
Question : Fais-moi le point sur les livraisons.
{{"intent": "delivery_summary", "tms_id": null, {empty}}}
Question : Pourquoi l'alerte 7 a-t-elle été levée ?
{{"intent": "explain_alert", "tms_id": null, "period": "none", "alert_id": 7}}
Question : Que dois-je traiter en priorité ?
{{"intent": "situation_briefing", "tms_id": null, {empty}}}
Question : Quelles missions risquent d'arriver en retard ?
{{"intent": "delay_risk", "tms_id": null, {empty}}}
Question : Que faire en cas de panne de véhicule ?
{{"intent": "procedure_question", "tms_id": null, {empty}}}
Question : Résous l'alerte 7.
{{"intent": "unsupported", "tms_id": null, {empty}}}
Question : Quel temps fait-il à Rabat ?
{{"intent": "unsupported", "tms_id": null, {empty}}}
Question : Supprime les alertes de {name}.
{{"intent": "unsupported", "tms_id": "{first.id}", {empty}}}"""


def _interpretation_schema(configs: list[TmsConfig]) -> dict:
    nullable_date = {"anyOf": [{"type": "string", "format": "date"}, {"type": "null"}]}
    return {
        "type": "object",
        "properties": {
            "intent": {"type": "string", "enum": [intent.value for intent in ChatIntent]},
            "tms_id": {
                "anyOf": [
                    {"type": "string", "enum": [config.id for config in configs]},
                    {"type": "null"},
                ]
            },
            "period": {"type": "string", "enum": [kind.value for kind in PeriodKind]},
            "start_date": nullable_date,
            "end_date": nullable_date,
            "alert_id": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        },
        "required": ["intent", "tms_id", "period"],
    }


class ChatOrchestrator:
    """Le LLM propose, le code valide, et un seul agent traite la question."""

    def __init__(
        self,
        configs: list[TmsConfig],
        llm: OllamaClient,
        reporting: ReportingAgent,
        incident: IncidentAgent,
        monitoring: MonitoringAgent,
        knowledge: KnowledgeAgent,
    ) -> None:
        self._configs = configs
        self._llm = llm
        self._reporting = reporting
        self._incident = incident
        self._monitoring = monitoring
        self._knowledge = knowledge
        graph = StateGraph(ChatState)
        graph.add_node("interpret", self._interpret)
        graph.add_node("report", self._report)
        graph.add_node("summarize", self._summarize)
        graph.add_node("incident", self._run_incident)
        graph.add_node("monitoring", self._run_monitoring)
        graph.add_node("knowledge", self._run_knowledge)
        graph.add_edge(START, "interpret")
        graph.add_conditional_edges(
            "interpret",
            self._route,
            {
                "report": "report",
                "incident": "incident",
                "monitoring": "monitoring",
                "knowledge": "knowledge",
                "refuse": END,
                "clarify": END,
            },
        )
        graph.add_edge("report", "summarize")
        graph.add_edge("summarize", END)
        graph.add_edge("incident", END)
        graph.add_edge("monitoring", END)
        graph.add_edge("knowledge", END)
        self._graph = graph.compile()

    def ask(self, question: str, reference_time: datetime) -> ChatResponse:
        state = self._graph.invoke({"question": question, "reference_time": reference_time})
        intent = state["interpretation"].intent
        common = {"question": question, "intent": intent}
        if "clarification" in state or intent == ChatIntent.UNSUPPORTED:
            return ChatResponse(
                **common, answered=False,
                summary=state.get("clarification", UNSUPPORTED_MESSAGE),
                summary_source=SummarySource.DETERMINISTIC, reference_time=reference_time,
            )
        result = state.get("agent_result")
        if result is not None:
            return ChatResponse(
                **common, **result.model_dump(exclude={"items"}),
                **_split_by_nature(result.items),
            )
        report = state["report"]
        return ChatResponse(
            **common, answered=True, summary=state["summary"],
            summary_source=state["summary_source"], reference_time=report.reference_time,
            period=report.period, scope=report.scope, **_split_by_nature(report.items),
        )

    def _interpret(self, state: ChatState) -> ChatState:
        """Une réponse non conforme, ou un TMS inconnu, conduit à un refus, jamais à une supposition."""
        raw = self._llm.complete_json(
            build_interpreter_prompt(self._configs),
            f"Date du jour : {state['reference_time']:%Y-%m-%d}\nQuestion : {state['question']}",
            _interpretation_schema(self._configs),
            max_tokens=100,
        )
        try:
            interpretation = Interpretation.model_validate(raw)
        except ValidationError:
            interpretation = Interpretation(intent=ChatIntent.UNSUPPORTED)
        known_ids = {config.id for config in self._configs}
        if interpretation.tms_id is not None and interpretation.tms_id not in known_ids:
            interpretation = Interpretation(intent=ChatIntent.UNSUPPORTED)
        if interpretation.intent == ChatIntent.UNSUPPORTED:
            return {"interpretation": interpretation}
        if interpretation.intent == ChatIntent.EXPLAIN_ALERT:
            cited = NUMBER_IN_QUESTION.findall(state["question"])
            if interpretation.alert_id is None or str(interpretation.alert_id) not in cited:
                return {"interpretation": interpretation, "clarification": ALERT_ID_MESSAGE}
        return {"interpretation": interpretation, **self._resolve_period(state, interpretation)}

    @staticmethod
    def _resolve_period(state: ChatState, interpretation: Interpretation) -> ChatState:
        """Une date citée mais non comprise conduit à une question, jamais à l'état actuel."""
        question = state["question"]
        timezone = get_settings().business_timezone
        interpretation = correct_relative_period(question, interpretation)
        if interpretation.intent in CURRENT_ONLY_INTENTS:
            if mentions_period(question) and interpretation.period != PeriodKind.TODAY:
                return {"clarification": CURRENT_ONLY_MESSAGE}
            return {"period": None}
        if interpretation.period == PeriodKind.NONE:
            if mentions_period(question):
                return {"clarification": CLARIFY_PERIOD_MESSAGE}
            return {"period": None}
        try:
            period = resolve_period(interpretation, state["reference_time"], timezone)
        except InvalidPeriodError:
            return {"clarification": CLARIFY_PERIOD_MESSAGE}
        if not period_matches_question(question, period, timezone):
            return {"clarification": CLARIFY_PERIOD_MESSAGE}
        return {"period": period}

    @staticmethod
    def _route(state: ChatState) -> str:
        intent = state["interpretation"].intent
        if intent == ChatIntent.UNSUPPORTED:
            return "refuse"
        if "clarification" in state:
            return "clarify"
        if intent == ChatIntent.EXPLAIN_ALERT:
            return "incident"
        if intent == ChatIntent.PROCEDURE_QUESTION:
            return "knowledge"
        return "monitoring" if intent == ChatIntent.SITUATION_BRIEFING else "report"

    def _report(self, state: ChatState) -> ChatState:
        interpretation = state["interpretation"]
        return {
            "report": self._reporting.build_report(
                interpretation.intent, interpretation.tms_id, state.get("period")
            )
        }

    def _summarize(self, state: ChatState) -> ChatState:
        summary, source = self._reporting.summarize(state["question"], state["report"])
        return {"summary": summary, "summary_source": source}

    def _run_incident(self, state: ChatState) -> ChatState:
        interpretation = state["interpretation"]
        return {"agent_result": self._incident.run(interpretation.alert_id, interpretation.tms_id)}

    def _run_knowledge(self, state: ChatState) -> ChatState:
        return {
            "agent_result": self._knowledge.run(state["question"], state["interpretation"].tms_id)
        }

    def _run_monitoring(self, state: ChatState) -> ChatState:
        return {"agent_result": self._monitoring.run(state["interpretation"].tms_id)}


def _split_by_nature(items: list) -> dict:
    return {
        "facts": [item for item in items if item.nature == FindingNature.FACT],
        "risks": [item for item in items if item.nature == FindingNature.RISK],
        "missing_information": [
            item for item in items if item.nature == FindingNature.MISSING_INFORMATION
        ],
    }


def build_llm_client() -> OllamaClient:
    settings = get_settings()
    return OllamaClient(settings.ollama_base_url, settings.llm_model, settings.llm_timeout_seconds)
