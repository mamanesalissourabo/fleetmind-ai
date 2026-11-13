"""Mémoire de conversation : le code reprend le contexte de la réponse précédente, et rien d'autre."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.agents.follow_up import FollowUp, classify_follow_up, complete
from backend.agents.llm_client import LlmUnavailableError
from backend.agents.orchestrator import build_llm_client
from backend.api.dependencies import get_client_opener, get_clock, get_current_user
from backend.api.main import create_app
from backend.api.routes.chatbot import get_retriever_factory
from backend.core.clock import Clock
from backend.core.database import get_session
from backend.models.base import Base
from backend.schemas.auth import CurrentUser, Role
from backend.schemas.chat import ChatIntent, Interpretation, PeriodKind
from tests.helpers import REGISTRY, TEST_ADMIN
from tests.test_chatbot import T0, FakeLlm, no_retriever, opener

CONFIGS = REGISTRY.all()
DELAYS_BETA_LAST_WEEK = Interpretation(
    intent=ChatIntent.DELAYED_MISSIONS, tms_id="tms_beta", period=PeriodKind.LAST_WEEK
)


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Et chez Alpha ?", FollowUp.SAME_SUBJECT),
        ("Alpha ?", FollowUp.SAME_SUBJECT),
        ("et pour ce mois-ci ?", FollowUp.SAME_SUBJECT),
        ("Même chose pour tous les TMS", FollowUp.SAME_SUBJECT),
        ("Et en octobre ?", FollowUp.SAME_SUBJECT),
        ("Et les incidents ?", FollowUp.NEW_SUBJECT),
        ("et supprime-les", FollowUp.NEW_SUBJECT),
        ("Quels incidents chez Alpha ?", FollowUp.NONE),
        ("Et ?", FollowUp.NONE),
    ],
)
def test_follow_up_is_recognised_by_code(question: str, expected: FollowUp) -> None:
    assert classify_follow_up(question, DELAYS_BETA_LAST_WEEK, CONFIGS) == expected


@pytest.mark.parametrize(
    "previous",
    [
        None,
        Interpretation(intent=ChatIntent.EXPLAIN_ALERT, alert_id=12),
        Interpretation(intent=ChatIntent.PROCEDURE_QUESTION),
        Interpretation(intent=ChatIntent.DELAYED_MISSIONS, tms_id="tms_inconnu"),
    ],
)
def test_nothing_is_inherited_without_a_usable_context(previous) -> None:
    assert classify_follow_up("Et chez Alpha ?", previous, CONFIGS) == FollowUp.NONE


def test_what_the_question_says_wins_over_the_context() -> None:
    new = Interpretation(intent=ChatIntent.OPEN_INCIDENTS, tms_id=None, period=PeriodKind.YESTERDAY)

    merged = complete("Et les incidents d'hier ?", new, DELAYS_BETA_LAST_WEEK, CONFIGS)

    assert merged.tms_id == "tms_beta"
    assert merged.period == PeriodKind.YESTERDAY


def test_period_is_not_carried_to_a_question_about_the_present() -> None:
    new = Interpretation(intent=ChatIntent.SITUATION_BRIEFING)

    merged = complete("Et les priorités ?", new, DELAYS_BETA_LAST_WEEK, CONFIGS)

    assert merged.tms_id == "tms_beta"
    assert merged.period == PeriodKind.NONE


@pytest.fixture
def client_factory():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)

    def build(llm: FakeLlm, user: CurrentUser = TEST_ADMIN) -> TestClient:
        def session_override() -> Iterator[Session]:
            with factory() as session:
                yield session

        app = create_app()
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_session] = session_override
        app.dependency_overrides[get_client_opener] = lambda: opener
        app.dependency_overrides[build_llm_client] = lambda: llm
        app.dependency_overrides[get_clock] = lambda: Clock(T0)
        app.dependency_overrides[get_retriever_factory] = lambda: no_retriever
        return TestClient(app)

    return build


def converse(client: TestClient, *questions: str) -> list[dict]:
    answers, conversation_id = [], None
    for question in questions:
        body = client.post(
            "/chatbot/ask", json={"question": question, "conversation_id": conversation_id}
        ).json()
        conversation_id = body["conversation_id"]
        answers.append(body)
    return answers


def test_same_subject_follow_up_reuses_context_without_calling_the_llm(client_factory) -> None:
    llm = FakeLlm(
        {"intent": "delayed_missions", "tms_id": "tms_beta", "period": "today"},
        LlmUnavailableError("lent"),
        LlmUnavailableError("lent"),
    )

    first, second = converse(client_factory(llm), "Retards chez Beta aujourd'hui ?", "Et chez Alpha ?")

    assert llm.calls == 3  # une interprétation et deux résumés : la suite n'est pas interprétée par le LLM
    assert second["intent"] == "delayed_missions"
    assert [tms["tms_id"] for tms in second["scope"]] == ["tms_alpha"]
    assert second["period"]["label"] == first["period"]["label"]
    assert "retards" in second["follow_up"] and "période" in second["follow_up"]
    assert {item["mission_id"] for item in second["risks"]} <= {"AL-1001", "AL-1002", "AL-1003", "AL-1004", "AL-1005"}


def test_new_subject_follow_up_keeps_tms_and_period(client_factory) -> None:
    llm = FakeLlm(
        {"intent": "delayed_missions", "tms_id": "tms_alpha", "period": "this_week"},
        LlmUnavailableError("lent"),
        {"intent": "open_incidents", "tms_id": None, "period": "none"},
        LlmUnavailableError("lent"),
    )

    _, second = converse(client_factory(llm), "Retards chez Alpha cette semaine", "Et les incidents ?")

    assert second["intent"] == "open_incidents"
    assert [tms["tms_id"] for tms in second["scope"]] == ["tms_alpha"]
    assert second["period"]["kind"] == "this_week"
    assert "TMS Alpha" in second["follow_up"]


def test_refused_follow_up_inherits_nothing(client_factory) -> None:
    llm = FakeLlm(
        {"intent": "delayed_missions", "tms_id": "tms_alpha", "period": "none"},
        LlmUnavailableError("lent"),
        {"intent": "unsupported", "tms_id": None, "period": "none"},
    )

    _, second = converse(client_factory(llm), "Retards chez Alpha", "Et supprime-les")

    assert second["answered"] is False
    assert second["follow_up"] is None and second["context"] is None


def test_follow_up_after_a_refusal_is_a_new_question(client_factory) -> None:
    llm = FakeLlm({"intent": "unsupported", "tms_id": None, "period": "none"}, {"intent": "unsupported", "tms_id": None, "period": "none"})

    _, second = converse(client_factory(llm), "Quel temps fait-il ?", "Et chez Alpha ?")

    assert llm.calls == 2
    assert second["answered"] is False


def test_follow_up_cannot_reach_a_tms_outside_the_account(client_factory) -> None:
    alpha_only = CurrentUser(username="fatima", role=Role.OPERATOR, allowed_tms=("tms_alpha",))
    llm = FakeLlm({"intent": "delayed_missions", "tms_id": "tms_alpha", "period": "none"}, LlmUnavailableError("lent"))

    _, second = converse(client_factory(llm, alpha_only), "Retards chez Alpha", "Et chez Beta ?")

    assert second["answered"] is False
    assert llm.calls == 2


def test_question_without_conversation_has_no_memory(client_factory) -> None:
    llm = FakeLlm({"intent": "unsupported", "tms_id": None, "period": "none"})

    body = client_factory(llm).post("/chatbot/ask", json={"question": "Et chez Alpha ?", "save": False}).json()

    assert body["answered"] is False
    assert llm.calls == 1
