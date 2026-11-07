"""Route du chatbot : consultation uniquement, dans le périmètre du compte, et tracée."""

import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

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
from backend.models.conversation import Conversation, ConversationMessage
from backend.rag.retrieval import DocumentRetriever, get_retriever
from backend.schemas.auth import CurrentUser
from backend.schemas.chat import (
    ChatIntent,
    ChatRequest,
    ChatResponse,
    ConversationDetail,
    ConversationRead,
    SummarySource,
)
from backend.schemas.tms_config import TmsConfig
from backend.services.monitoring import MonitoringService
from backend.services.rules_registry import RulesRegistry

TITLE_LENGTH = 60
MAX_LISTED_CONVERSATIONS = 50
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
    conversation = _own_conversation(session, request.conversation_id, user)
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
    if request.save:
        response.conversation_id = _save_exchange(session, conversation, request.question, response, user)
    session.commit()
    return response


def _own_conversation(
    session: Session, conversation_id: int | None, user: CurrentUser
) -> Conversation | None:
    """Une conversation d'un autre compte répond comme une conversation inexistante."""
    if conversation_id is None:
        return None
    conversation = session.get(Conversation, conversation_id)
    if conversation is None or conversation.username != user.username:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation introuvable")
    return conversation


def _save_exchange(
    session: Session,
    conversation: Conversation | None,
    question: str,
    response: ChatResponse,
    user: CurrentUser,
) -> int:
    now = datetime.now(UTC)
    if conversation is None:
        title = question if len(question) <= TITLE_LENGTH else f"{question[:TITLE_LENGTH].rstrip()}..."
        conversation = Conversation(username=user.username, title=title, created_at=now, updated_at=now)
        session.add(conversation)
    conversation.updated_at = now
    conversation.messages.append(
        ConversationMessage(question=question, answer=response.model_dump(mode="json"), created_at=now)
    )
    session.flush()
    return conversation.id


@router.get("/conversations", response_model=list[ConversationRead])
def list_conversations(session: SessionDep, user: CurrentUserDep) -> list[Conversation]:
    """Liste les conversations du compte connecté, de la plus récente à la plus ancienne."""
    query = (
        select(Conversation)
        .where(Conversation.username == user.username)
        .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
        .limit(MAX_LISTED_CONVERSATIONS)
    )
    return list(session.scalars(query))


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
def get_conversation(conversation_id: int, session: SessionDep, user: CurrentUserDep) -> Conversation:
    return _own_conversation(session, conversation_id, user)


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(conversation_id: int, session: SessionDep, user: CurrentUserDep) -> None:
    """Efface la conversation et ses messages ; la trace d'audit chat_logs est conservée."""
    session.delete(_own_conversation(session, conversation_id, user))
    session.commit()
