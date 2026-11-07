"""Route du chatbot : consultation uniquement, dans le périmètre du compte, et tracée."""

import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from backend.agents.incident_agent import IncidentAgent
from backend.agents.knowledge_agent import KnowledgeAgent
from backend.agents.llm_client import LlmUnavailableError, OllamaClient
from backend.agents.monitoring_agent import MonitoringAgent
from backend.agents.orchestrator import ChatOrchestrator, build_llm_client
from backend.agents.reporting_agent import ReportingAgent
from backend.api.dependencies import (
    ClockDep,
    CurrentUserDep,
    SessionDep,
    TmsRegistryDep,
    get_client_opener,
    get_rules_registry,
    visible_tms,
)
from backend.ml.predict import DelayRiskModel, get_delay_risk_model
from backend.models.chat_log import ChatLog
from backend.rag.retrieval import DocumentRetriever, get_retriever
from backend.schemas.chat import ChatIntent, ChatRequest, ChatResponse, SummarySource
from backend.schemas.tms_config import TmsConfig
from backend.services.monitoring import MonitoringService
from backend.services.rules_registry import RulesRegistry

FORBIDDEN_MESSAGE = "Cette question cite un TMS hors de votre périmètre : je ne peux pas y répondre."

router = APIRouter(prefix="/chatbot", tags=["chatbot"])


def get_risk_model_factory() -> Callable[[], DelayRiskModel]:
    """Le modèle n'est chargé que si une question en a besoin."""
    return get_delay_risk_model


def get_retriever_factory() -> Callable[[], DocumentRetriever]:
    """L'index n'est ouvert que si une question en a besoin."""
    return get_retriever


def cites_hidden_tms(question: str, hidden: list[TmsConfig]) -> bool:
    """Contrôle fait par le code, avant le LLM, qui ignore jusqu'au nom de ces TMS."""
    names = [name for config in hidden for name in (config.id, config.name.removeprefix("TMS "))]
    return any(re.search(rf"\b{re.escape(name)}\b", question, re.IGNORECASE) for name in names)


@router.post("/ask", response_model=ChatResponse)
def ask(
    request: ChatRequest,
    session: SessionDep,
    registry: TmsRegistryDep,
    clock: ClockDep,
    user: CurrentUserDep,
    rules: Annotated[RulesRegistry, Depends(get_rules_registry)],
    open_client: Annotated[object, Depends(get_client_opener)],
    llm: Annotated[OllamaClient, Depends(build_llm_client)],
    retriever_factory: Annotated[Callable[[], DocumentRetriever], Depends(get_retriever_factory)],
    risk_model_factory: Annotated[Callable[[], DelayRiskModel], Depends(get_risk_model_factory)],
) -> ChatResponse:
    """Répond à une question ; sans LLM local, la question n'est pas interprétée au hasard."""
    started = time.perf_counter()
    configs = visible_tms(registry, user)
    if not configs:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Aucun TMS n'est autorisé pour ce compte")
    hidden = [config for config in registry.all() if config not in configs]
    if cites_hidden_tms(request.question, hidden):
        response = ChatResponse(
            question=request.question, intent=ChatIntent.UNSUPPORTED, answered=False,
            summary=FORBIDDEN_MESSAGE, summary_source=SummarySource.DETERMINISTIC,
            reference_time=clock.now(),
        )
    else:
        reporting = ReportingAgent(
            configs, open_client, MonitoringService(rules), session, clock.now, llm, risk_model_factory
        )
        incident = IncidentAgent(configs, open_client, rules, session, clock.now, llm, retriever_factory)
        monitoring = MonitoringAgent(configs, session, clock.now)
        knowledge = KnowledgeAgent(configs, retriever_factory, clock.now, llm)
        orchestrator = ChatOrchestrator(configs, llm, reporting, incident, monitoring, knowledge)
        try:
            response = orchestrator.ask(request.question, clock.now())
        except LlmUnavailableError as error:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(error)) from error
    session.add(
        ChatLog(
            asked_at=datetime.now(UTC),
            username=user.username,
            question=request.question,
            intent=response.intent.value,
            answered=response.answered,
            summary=response.summary,
            summary_source=response.summary_source.value,
            scope=[tms.tms_id for tms in response.scope],
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
    )
    session.commit()
    return response
