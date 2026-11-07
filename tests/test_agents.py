"""Tests des agents d'incident et de surveillance, avec un LLM simulé."""

from datetime import timedelta

import pytest
from qdrant_client import QdrantClient
from sqlalchemy import select

from backend.agents.llm_client import LlmUnavailableError
from backend.api.dependencies import build_cycle_runner
from backend.models.alert import Alert, Notification
from backend.models.monitoring_run import MonitoringRun
from backend.rag.retrieval import DocumentRetriever
from backend.schemas.alerts import AlertStatus
from backend.services.cycle_runner import CycleTrigger
from backend.services.rules_registry import RulesRegistry
from tests.helpers import REGISTRY
from tests.test_rag import KeywordEmbeddings
from tests.test_chatbot import T0, FakeLlm, ask, broken_beta_opener, factory, opener  # noqa: F401


@pytest.fixture
def seeded(factory):  # noqa: F811
    """Remplit la base avec un vrai cycle sur chaque TMS."""
    runner = build_cycle_runner(RulesRegistry.from_directory(), opener)
    with factory() as session:
        for config in REGISTRY.all():
            runner.run(session, config, CycleTrigger.SCHEDULED, T0)
    return factory


def alert_of(factory, mission: str, rule_type: str) -> Alert:  # noqa: F811
    with factory() as session:
        return session.scalars(
            select(Alert).where(
                Alert.mission_external_id == mission, Alert.rule_type == rule_type
            )
        ).one()


def explain(alert_id: int, tms_id: str | None = None) -> dict:
    return {"intent": "explain_alert", "tms_id": tms_id, "period": "none", "alert_id": alert_id}


def test_incident_agent_explains_with_mission_context_and_draft(seeded) -> None:
    alert = alert_of(seeded, "AL-1003", "open_incident")
    llm = FakeLlm(
        explain(alert.id),
        {"explication": "La mission AL-1003 est bloquée."},
    )

    body = ask(seeded, llm, f"Explique l'alerte {alert.id}").json()

    assert body["answered"] is True
    assert body["summary_source"] == "llm"
    assert body["summary"] == "La mission AL-1003 est bloquée."
    assert "AL-1003" in body["draft_message"]
    assert body["suggested_recipient"] == "exploitation-alpha"
    texts = " ".join(item["text"] for item in body["facts"])
    assert "Fès vers Oujda" in texts and "INC-502" in texts and "Taza" in texts


def test_explaining_changes_nothing_and_sends_nothing(seeded) -> None:
    alert = alert_of(seeded, "AL-1003", "open_incident")
    with seeded() as session:
        before = len(list(session.scalars(select(Notification))))
    llm = FakeLlm(explain(alert.id), LlmUnavailableError("lent"))

    ask(seeded, llm, f"Explique l'alerte {alert.id}")

    with seeded() as session:
        assert len(list(session.scalars(select(Notification)))) == before
        assert session.get(Alert, alert.id).status == AlertStatus.OPEN.value


@pytest.mark.parametrize(
    "bad",
    [
        {"explication": "La mission AL-1003 a 99 min de retard."},
        {"explication": "La mission est bloquée."},
        {"explication": "AL-1003 comme chez Beta."},
    ],
)
def test_unfaithful_explanation_or_draft_is_replaced(seeded, bad: dict) -> None:
    alert = alert_of(seeded, "AL-1003", "open_incident")

    body = ask(seeded, FakeLlm(explain(alert.id), bad), f"Explique l'alerte {alert.id}").json()

    assert body["summary_source"] == "deterministic"
    assert "AL-1003" in body["draft_message"]


def test_alert_number_must_be_in_the_question(seeded) -> None:
    alert = alert_of(seeded, "AL-1003", "open_incident")

    body = ask(seeded, FakeLlm(explain(alert.id)), "Explique cette alerte").json()

    assert body["answered"] is False
    assert "numéro" in body["summary"]


def test_unknown_alert_is_reported(seeded) -> None:
    body = ask(seeded, FakeLlm(explain(999999)), "Explique l'alerte 999999").json()

    assert body["answered"] is False
    assert "999999" in body["summary"]


def test_alert_of_another_tms_is_refused(seeded) -> None:
    alert = alert_of(seeded, "AL-1003", "open_incident")
    question = f"Explique l'alerte {alert.id} de Beta"

    body = ask(seeded, FakeLlm(explain(alert.id, "tms_beta")), question).json()

    assert body["answered"] is False
    assert body["facts"] == []


def test_tms_down_gives_template_and_says_context_is_incomplete(seeded) -> None:
    alert = alert_of(seeded, "BT-7783", "open_incident")
    llm = FakeLlm(explain(alert.id))

    body = ask(seeded, llm, f"Explique l'alerte {alert.id}", broken_beta_opener).json()

    assert llm.calls == 1
    assert body["summary_source"] == "deterministic"
    assert "incomplet" in body["summary"]
    assert body["missing_information"]
    assert body["scope"][0]["available"] is False


def test_recipient_follows_the_escalation_reached(seeded) -> None:
    alert = alert_of(seeded, "AL-1003", "open_incident")
    with seeded() as session:
        session.get(Alert, alert.id).escalation_level = 2
        session.commit()
    llm = FakeLlm(explain(alert.id), LlmUnavailableError("x"))

    body = ask(seeded, llm, f"Explique l'alerte {alert.id}").json()

    assert body["suggested_recipient"] == "direction-alpha"


def test_no_draft_for_resolved_alert(seeded) -> None:
    alert = alert_of(seeded, "AL-1003", "open_incident")
    with seeded() as session:
        session.get(Alert, alert.id).status = AlertStatus.RESOLVED.value
        session.commit()
    llm = FakeLlm(explain(alert.id), LlmUnavailableError("x"))

    body = ask(seeded, llm, f"Explique l'alerte {alert.id}").json()

    assert body["draft_message"] is None


def briefing(tms_id: str | None = None) -> dict:
    return {"intent": "situation_briefing", "tms_id": tms_id, "period": "none"}


def test_priorities_are_ordered_by_code(seeded) -> None:
    llm = FakeLlm(briefing())

    body = ask(seeded, llm, "Que traiter en priorité ?").json()

    items = body["facts"] + body["risks"] + body["missing_information"]
    assert len(items) == 5
    assert all("gravité HIGH" in item["text"] for item in items)
    assert body["summary_source"] == "deterministic"
    assert "16 alerte(s)" in body["summary"]


def test_acknowledged_alert_goes_after_open_ones(seeded) -> None:
    first = ask(seeded, FakeLlm(briefing("tms_alpha")), "Priorités Alpha ?").json()
    top_id = int(first["summary"].split("alerte ")[1].split(" ")[0])
    with seeded() as session:
        session.get(Alert, top_id).status = AlertStatus.ACKNOWLEDGED.value
        session.commit()

    second = ask(seeded, FakeLlm(briefing("tms_alpha")), "Priorités Alpha ?").json()

    assert f"alerte {top_id} " not in second["summary"].split("À traiter d'abord")[1]


def test_briefing_only_uses_llm_to_classify_the_question(seeded) -> None:
    llm = FakeLlm(briefing())

    body = ask(seeded, llm, "Quelles urgences ?").json()

    assert llm.calls == 1
    assert body["summary_source"] == "deterministic"


def test_briefing_without_any_cycle_does_not_claim_all_is_well(factory) -> None:  # noqa: F811
    llm = FakeLlm(briefing())

    body = ask(factory, llm, "Quelles urgences ?").json()

    assert llm.calls == 1
    assert "possiblement incomplète" in body["summary"]
    assert all(tms["available"] is False for tms in body["scope"])


def test_stale_monitoring_is_reported(seeded) -> None:
    with seeded() as session:
        for run in session.scalars(select(MonitoringRun)):
            run.started_at = run.started_at - timedelta(days=2)
        session.commit()
    llm = FakeLlm(briefing())

    body = ask(seeded, llm, "Quelles urgences ?").json()

    assert llm.calls == 1
    assert "surveillance en retard" in body["summary"]
    assert body["missing_information"]


def test_briefing_with_past_period_asks_for_reformulation(seeded) -> None:
    question = "Quelles étaient les priorités la semaine dernière ?"

    body = ask(seeded, FakeLlm(briefing()), question).json()

    assert body["answered"] is False
    assert "situation actuelle" in body["summary"]


def test_briefing_today_is_accepted(seeded) -> None:
    llm = FakeLlm(briefing())

    body = ask(seeded, llm, "Quelles priorités aujourd'hui ?").json()

    assert body["answered"] is True


@pytest.fixture
def retriever() -> DocumentRetriever:
    instance = DocumentRetriever(QdrantClient(":memory:"), KeywordEmbeddings(), min_score=0.3)
    instance.reindex()
    return instance


def explain_with_procedures(factory, retriever, alert_id: int) -> dict:  # noqa: F811
    llm = FakeLlm(explain(alert_id), LlmUnavailableError("x"))
    question = f"Explique l'alerte {alert_id}"
    return ask(factory, llm, question, retriever_factory=lambda: retriever).json()


def test_explanation_attaches_the_procedure_for_the_rule_type(seeded, retriever) -> None:
    alert = alert_of(seeded, "AL-1004", "route_deviation")

    body = explain_with_procedures(seeded, retriever, alert.id)

    assert {source["document"] for source in body["sources"]} == {"deviation_itineraire.md"}
    assert all(source["excerpt"] for source in body["sources"])


def test_procedure_of_another_tms_is_not_attached(seeded, retriever) -> None:
    alert = alert_of(seeded, "AL-1003", "open_incident")
    with seeded() as session:
        session.get(Alert, alert.id).message = "Panne du groupe frigorifique"
        session.commit()

    body = explain_with_procedures(seeded, retriever, alert.id)

    assert "rupture_froid.md" not in {source["document"] for source in body["sources"]}


def test_no_procedure_for_resolved_alert(seeded, retriever) -> None:
    alert = alert_of(seeded, "AL-1004", "route_deviation")
    with seeded() as session:
        session.get(Alert, alert.id).status = AlertStatus.RESOLVED.value
        session.commit()

    body = explain_with_procedures(seeded, retriever, alert.id)

    assert body["sources"] == []


def test_unavailable_documents_do_not_block_the_explanation(seeded) -> None:
    alert = alert_of(seeded, "AL-1004", "route_deviation")
    llm = FakeLlm(explain(alert.id), {"explication": "La mission AL-1004 a dévié."})

    body = ask(seeded, llm, f"Explique l'alerte {alert.id}").json()

    assert body["summary_source"] == "llm"
    assert body["sources"] == []
    assert any("base documentaire indisponible" in item["text"] for item in body["missing_information"])


def test_incident_code_selects_the_procedure_whatever_the_language(seeded, retriever) -> None:
    alert = alert_of(seeded, "AL-1003", "open_incident")

    body = explain_with_procedures(seeded, retriever, alert.id)

    assert "Engine failure" in alert.message
    assert {source["document"] for source in body["sources"]} == {"panne_vehicule.md"}


def test_decimal_comma_written_by_llm_is_accepted(seeded) -> None:
    alert = alert_of(seeded, "AL-1004", "route_deviation")
    distance = str(alert.details["distance_km"]).replace(".", ",")
    llm = FakeLlm(explain(alert.id), {"explication": f"La mission AL-1004 est à {distance} km."})

    body = ask(seeded, llm, f"Explique l'alerte {alert.id}").json()

    assert body["summary_source"] == "llm"
