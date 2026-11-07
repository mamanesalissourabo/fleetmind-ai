"""Tests du chatbot avec un LLM simulé : ce qu'il invente ne doit jamais passer."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.dependencies import get_current_user
from tests.helpers import TEST_ADMIN
from backend.agents.llm_client import LlmUnavailableError
from backend.agents.orchestrator import build_llm_client
from backend.agents.periods import resolve_period
from backend.agents.reporting_agent import _late_deliveries
from backend.api.dependencies import get_client_opener, get_clock, resume_simulated_start
from backend.api.main import create_app
from backend.api.routes.chatbot import get_retriever_factory
from backend.core.clock import Clock
from backend.core.config import Settings
from backend.core.database import get_session
from backend.integrations.generic_client import GenericTmsClient
from backend.models.alert import Alert
from backend.rag.retrieval import RetrievalError
from backend.models.base import Base
from backend.models.chat_log import ChatLog
from backend.models.monitoring_run import MonitoringRun
from backend.schemas.canonical import Mission
from backend.schemas.chat import Interpretation
from backend.schemas.findings import FindingNature
from tests.helpers import client_for, mock_secret

T0 = datetime(2026, 11, 7, 11, 0, tzinfo=UTC)


class FakeLlm:
    """Rejoue des réponses prévues ; une exception est levée telle quelle."""

    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls = 0

    def complete_json(self, *_: object, **__: object) -> dict:
        self.calls += 1
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@contextmanager
def opener(config):
    yield client_for(config.id)


@contextmanager
def broken_beta_opener(config):
    if config.id != "tms_beta":
        yield client_for(config.id)
        return
    http = httpx.Client(
        base_url="http://tms.test",
        transport=httpx.MockTransport(lambda _: httpx.Response(503)),
    )
    yield GenericTmsClient(config, http, secret_provider=mock_secret)


@pytest.fixture
def factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def no_retriever():
    raise RetrievalError("Index absent dans ce test")


def ask(
    factory, llm: FakeLlm, question: str = "Question", client_opener=opener, retriever_factory=no_retriever
) -> httpx.Response:
    def session_override() -> Iterator[Session]:
        with factory() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: TEST_ADMIN
    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_client_opener] = lambda: client_opener
    app.dependency_overrides[build_llm_client] = lambda: llm
    app.dependency_overrides[get_clock] = lambda: Clock(T0)
    app.dependency_overrides[get_retriever_factory] = lambda: retriever_factory
    return TestClient(app).post("/chatbot/ask", json={"question": question})


def intent(name: str, tms_id: str | None = None) -> dict:
    return {"intent": name, "tms_id": tms_id}


def test_incident_report_comes_from_tms_data_and_respects_scope(factory) -> None:
    llm = FakeLlm(intent("open_incidents", "tms_alpha"), {"synthese": "L'incident INC-502 est ouvert sur la mission AL-1003."})

    body = ask(factory, llm).json()

    assert body["answered"] is True
    assert body["summary_source"] == "llm"
    assert [tms["tms_id"] for tms in body["scope"]] == ["tms_alpha"]
    assert body["scope"][0]["data_as_of"]
    assert ["INC-502" in fact["text"] for fact in body["facts"]] == [True]
    assert body["risks"] == [] and body["missing_information"] == []


@pytest.mark.parametrize(
    "invented",
    [
        "Incidents INC-999 et AL-1003 ouverts.",
        "12 incidents sont ouverts sur AL-1003.",
        "Un incident sur AL-1003, comme chez Beta.",
        "Un incident est ouvert.",
        "",
    ],
)
def test_unfaithful_summary_is_replaced(factory, invented: str) -> None:
    llm = FakeLlm(intent("open_incidents", "tms_alpha"), {"synthese": invented})

    body = ask(factory, llm).json()

    assert body["summary_source"] == "deterministic"
    assert invented == "" or invented not in body["summary"]
    assert len(body["facts"]) == 1


def test_delays_follow_the_rules_of_each_tms_and_change_nothing(factory) -> None:
    llm = FakeLlm(intent("delayed_missions"), LlmUnavailableError("lent"))

    body = ask(factory, llm).json()

    risky = {item["mission_id"] for item in body["risks"]}
    assert {"AL-1002", "AL-1004", "BT-7782"} == risky
    assert "BT-7784" not in risky
    assert any(item["mission_id"] == "AL-1003" for item in body["facts"])
    assert [tms["ruleset_version"] for tms in body["scope"]] == [1, 1]
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Alert)) == 0


def test_write_request_is_refused_without_reading_data(factory) -> None:
    def must_not_be_called(config):
        raise AssertionError("Aucune donnée ne doit être lue")

    llm = FakeLlm(intent("unsupported", "tms_alpha"))

    body = ask(factory, llm, "Supprime les alertes", must_not_be_called).json()

    assert body["answered"] is False
    assert "Je ne modifie aucune donnée" in body["summary"]
    assert llm.calls == 1


def test_unknown_tms_from_llm_is_refused(factory) -> None:
    body = ask(factory, FakeLlm(intent("open_incidents", "tms_gamma"))).json()

    assert body["answered"] is False


def test_invalid_intent_from_llm_is_refused(factory) -> None:
    body = ask(factory, FakeLlm({"intent": "drop_database", "tms_id": None})).json()

    assert body["answered"] is False


def test_llm_down_during_interpretation_gives_503_without_guess(factory) -> None:
    response = ask(factory, FakeLlm(LlmUnavailableError("LLM local indisponible")))

    assert response.status_code == 503
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ChatLog)) == 0


def test_llm_down_during_summary_still_returns_the_data(factory) -> None:
    llm = FakeLlm(intent("open_incidents", "tms_alpha"), LlmUnavailableError("panne"))

    body = ask(factory, llm).json()

    assert body["summary_source"] == "deterministic"
    assert len(body["facts"]) == 1


def test_unavailable_tms_is_reported_and_llm_does_not_summarize(factory) -> None:
    llm = FakeLlm(intent("open_incidents"))

    body = ask(factory, llm, client_opener=broken_beta_opener).json()

    availability = {tms["tms_id"]: tms["available"] for tms in body["scope"]}
    assert availability == {"tms_alpha": True, "tms_beta": False}
    assert any("tms_beta" in item["text"] for item in body["missing_information"])
    assert "incomplet" in body["summary"] and "tms_beta" in body["summary"]
    assert llm.calls == 1


def test_no_result_is_not_summarized_by_the_llm(factory) -> None:
    llm = FakeLlm(intent("open_alerts", "tms_alpha"))

    body = ask(factory, llm).json()

    assert llm.calls == 1
    assert body["summary_source"] == "deterministic"
    assert any("aucun cycle" in item["text"] for item in body["missing_information"])


def test_each_question_is_logged(factory) -> None:
    ask(factory, FakeLlm(intent("unsupported")), "Écris un poème")

    with factory() as session:
        log = session.scalars(select(ChatLog)).one()
    assert (log.question, log.intent, log.answered) == ("Écris un poème", "unsupported", False)


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:11434", "http://localhost:11434", "http://10.0.0.5:11434", "http://ollama:11434"],
)
def test_internal_llm_addresses_are_accepted(url: str) -> None:
    assert Settings(ollama_base_url=url).ollama_base_url == url


@pytest.mark.parametrize("url", ["https://api.example.com", "http://8.8.8.8:11434"])
def test_external_llm_addresses_are_refused(url: str) -> None:
    with pytest.raises(ValueError, match="local ou privé"):
        Settings(ollama_base_url=url)


def test_summary_forgetting_a_mission_is_replaced(factory) -> None:
    llm = FakeLlm(intent("delayed_missions", "tms_alpha"), {"synthese": "Seule AL-1002 est en retard."})

    body = ask(factory, llm).json()

    assert body["summary_source"] == "deterministic"
    assert "Seule" not in body["summary"]
    assert "AL-1002" in body["summary"] and "AL-1004" in body["summary"]
    assert {"AL-1002", "AL-1004"} <= {item["mission_id"] for item in body["risks"]}


def test_delivery_summary_is_written_by_code_without_llm(factory) -> None:
    llm = FakeLlm(intent("delivery_summary", "tms_alpha"))

    body = ask(factory, llm).json()

    assert llm.calls == 1
    assert body["summary_source"] == "deterministic"
    assert "5 mission(s)" in body["summary"] and "1 livrée(s)" in body["summary"]


def period_intent(name: str, period: str, tms_id: str | None = None, **dates: str) -> dict:
    return {"intent": name, "tms_id": tms_id, "period": period, **dates}


def test_period_outside_data_returns_nothing_and_says_so(factory) -> None:
    llm = FakeLlm(period_intent("delayed_missions", "last_week"))

    body = ask(factory, llm, "Quels retards la semaine dernière ?").json()

    assert body["answered"] is True
    assert body["period"]["label"] == "du 26/10/2026 au 01/11/2026"
    assert body["facts"] == [] and body["risks"] == []
    assert "26/10/2026" in body["summary"]
    assert llm.calls == 1


def test_period_including_data_returns_it(factory) -> None:
    llm = FakeLlm(period_intent("delayed_missions", "today"), LlmUnavailableError("lent"))

    body = ask(factory, llm, "Les retards d'aujourd'hui").json()

    assert body["period"]["label"] == "du 07/11/2026 au 07/11/2026"
    assert {"AL-1002", "BT-7782"} <= {item["mission_id"] for item in body["risks"]}


def test_custom_period_uses_dates_checked_by_code(factory) -> None:
    llm = FakeLlm(
        period_intent("open_incidents", "custom", start_date="2026-11-01", end_date="2026-11-07"),
        LlmUnavailableError("lent"),
    )

    body = ask(factory, llm, "Incidents du 01/11/2026 au 07/11/2026").json()

    assert body["period"]["start"] == "2026-11-01T00:00:00Z"
    assert body["period"]["end"] == "2026-11-08T00:00:00Z"
    assert len(body["facts"]) == 2


@pytest.mark.parametrize("dates", [{}, {"start_date": "2026-11-05", "end_date": "2026-11-01"}])
def test_incoherent_custom_period_asks_for_clarification(factory, dates: dict) -> None:
    def must_not_be_called(config):
        raise AssertionError("Aucune donnée ne doit être lue")

    llm = FakeLlm(period_intent("open_incidents", "custom", **dates))

    body = ask(factory, llm, "Incidents de la période", must_not_be_called).json()

    assert body["answered"] is False
    assert "période" in body["summary"]


def test_date_cited_but_ignored_by_llm_asks_for_clarification(factory) -> None:
    llm = FakeLlm(period_intent("delayed_missions", "none"))

    body = ask(factory, llm, "Quels retards en octobre ?").json()

    assert body["answered"] is False
    assert "période" in body["summary"]


def test_question_about_the_moment_is_not_mistaken_for_a_period(factory) -> None:
    llm = FakeLlm(period_intent("open_alerts", "none", "tms_alpha"))

    body = ask(factory, llm, "Quelles alertes en ce moment ?").json()

    assert body["answered"] is True
    assert body["period"] is None


def test_week_starts_on_monday_in_business_timezone() -> None:
    period = resolve_period(
        Interpretation(intent="open_alerts", period="this_week"), T0, "Africa/Casablanca"
    )

    assert period.label == "du 02/11/2026 au 08/11/2026"
    assert period.contains(T0)


def test_late_delivery_is_reported_as_a_fact() -> None:
    mission = Mission(
        source_tms="t", external_id="M-1", status="DELIVERED", customer_name="C",
        vehicle_plate="P", origin_city="A", destination_city="B", planned_departure=T0,
        planned_arrival=T0, actual_arrival=T0 + timedelta(minutes=25), updated_at=T0,
    )

    items = _late_deliveries("t", [mission])

    assert [(item.nature, item.mission_id) for item in items] == [(FindingNature.FACT, "M-1")]
    assert "25 min" in items[0].text


def test_simulated_clock_resumes_from_last_scheduled_cycle(factory) -> None:
    configured = datetime(2026, 11, 7, 11, 0, tzinfo=UTC)
    later = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)
    assert resume_simulated_start(configured, factory) == configured
    with factory() as session:
        session.add(
            MonitoringRun(
                source_tms="t", trigger="SCHEDULED", status="COMPLETE", reference_time=later,
                started_at=later, finished_at=later, missions_evaluated=0, findings_count=0,
                alerts_opened=0, alerts_resolved=0, notifications_sent=0,
            )
        )
        session.commit()

    assert resume_simulated_start(configured, factory) == later
    assert resume_simulated_start(None, factory) is None


@pytest.mark.parametrize(
    "question,llm_period",
    [
        ("Quels retards en 2025 ?", "this_month"),
        ("Quels retards en 2025 ?", "none"),
        ("Quels retards en octobre ?", "this_month"),
        ("Quels retards le 15/03/2026 ?", "this_week"),
        ("Quels retards avant-hier ?", "none"),
        ("Retards le 45/13 ?", "today"),
    ],
)
def test_period_contradicting_the_question_asks_for_clarification(
    factory, question: str, llm_period: str
) -> None:
    def must_not_be_called(config):
        raise AssertionError("Aucune donnée ne doit être lue")

    llm = FakeLlm(period_intent("delayed_missions", llm_period))

    body = ask(factory, llm, question, must_not_be_called).json()

    assert body["answered"] is False
    assert "période" in body["summary"]


def test_relative_period_is_decided_by_code_when_llm_is_wrong(factory) -> None:
    llm = FakeLlm(period_intent("delayed_missions", "this_month"))

    body = ask(factory, llm, "Quels retards la semaine dernière ?").json()

    assert body["period"]["kind"] == "last_week"
    assert body["period"]["label"] == "du 26/10/2026 au 01/11/2026"


def test_cited_month_and_year_inside_period_are_accepted(factory) -> None:
    llm = FakeLlm(
        period_intent("open_incidents", "custom", start_date="2026-11-01", end_date="2026-11-30"),
        LlmUnavailableError("lent"),
    )

    body = ask(factory, llm, "Incidents de novembre 2026").json()

    assert body["answered"] is True
    assert len(body["facts"]) == 2


def test_refusal_lists_everything_the_chatbot_can_do(factory) -> None:  # noqa: F811
    body = ask(factory, FakeLlm(intent("unsupported"))).json()

    for capability in ("risques de retard", "procédure", "expliquer une alerte", "point de situation"):
        assert capability in body["summary"]
