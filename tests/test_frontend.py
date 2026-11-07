"""Tests de l'interface, connectée au vrai backend avec une base en mémoire."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from streamlit.testing.v1 import AppTest

from backend.agents.orchestrator import build_llm_client
from backend.api.dependencies import get_client_opener, get_clock
from backend.api.main import create_app
from backend.api.routes.chatbot import get_retriever_factory
from backend.core.clock import Clock
from backend.core.database import get_session
from backend.schemas.auth import Role
from backend.services.auth import create_user
from frontend import api_client
from frontend.api_client import ApiError, FleetMindApi
from frontend.formatting import format_time, sort_alerts
from tests.helpers import client_for
from tests.test_chatbot import T0, FakeLlm, factory, no_retriever  # noqa: F401

CYCLE = {"reference_time": "2026-11-07T11:00:00Z"}
APP_FILE = str(Path(__file__).resolve().parents[1] / "frontend" / "app.py")
PASSWORD = "mot-de-passe-solide"
ACCOUNTS = {
    "admin": (Role.ADMIN, ["*"]),
    "fatima": (Role.OPERATOR, ["tms_alpha"]),
    "direction": (Role.EXECUTIVE, ["tms_alpha"]),
}


@contextmanager
def opener(config):
    yield client_for(config.id)


def connect(monkeypatch, factory, llm=None, cycle=True) -> TestClient:  # noqa: F811
    def session_override() -> Iterator[Session]:
        with factory() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_client_opener] = lambda: opener
    app.dependency_overrides[build_llm_client] = lambda: llm or FakeLlm()
    app.dependency_overrides[get_clock] = lambda: Clock(T0)
    app.dependency_overrides[get_retriever_factory] = lambda: no_retriever
    with factory() as session:
        for username, (role, tms) in ACCOUNTS.items():
            create_user(session, username, PASSWORD, role, tms, datetime.now(UTC))
        session.commit()
    client = TestClient(app)
    monkeypatch.setattr(api_client, "get_api", lambda: FleetMindApi(client))
    if cycle:
        client.post("/monitoring/cycles", json=CYCLE, headers=auth_headers(client, "admin"))
    return client


def auth_headers(client: TestClient, username: str) -> dict[str, str]:
    body = client.post("/auth/login", json={"username": username, "password": PASSWORD}).json()
    return {"Authorization": f"Bearer {body['access_token']}"}


def logged_in(username: str, page_name: str) -> AppTest:
    page = AppTest.from_file(APP_FILE, default_timeout=30).run()
    page.text_input[0].set_value(username)
    page.text_input[1].set_value(PASSWORD)
    next(b for b in page.button if b.label == "Se connecter").click().run()
    assert not page.exception and not page.error
    page.sidebar.radio[0].set_value(page_name).run()
    assert not page.exception
    return page


def test_nothing_is_visible_before_login(monkeypatch, factory) -> None:  # noqa: F811
    connect(monkeypatch, factory)

    page = AppTest.from_file(APP_FILE, default_timeout=30).run()

    assert not page.sidebar.radio
    assert [t.label for t in page.text_input] == ["Identifiant", "Mot de passe"]


def test_wrong_password_shows_a_neutral_message(monkeypatch, factory) -> None:  # noqa: F811
    connect(monkeypatch, factory)
    page = AppTest.from_file(APP_FILE, default_timeout=30).run()
    page.text_input[0].set_value("fatima")
    page.text_input[1].set_value("mauvais")

    next(b for b in page.button if b.label == "Se connecter").click().run()

    assert any("Identifiant ou mot de passe incorrect" in e.value for e in page.error)
    assert not page.sidebar.radio


def test_dashboard_without_cycle_warns_instead_of_reassuring(monkeypatch, factory) -> None:  # noqa: F811
    connect(monkeypatch, factory, cycle=False)

    page = logged_in("admin", "Tableau de bord")

    warnings = " ".join(w.value for w in page.warning)
    assert "surveillance automatique est arrêtée" in warnings
    assert "ne signifie pas absence de problème" in warnings
    assert not page.success


def test_dashboard_counts_follow_the_authorized_scope(monkeypatch, factory) -> None:  # noqa: F811
    connect(monkeypatch, factory)

    assert logged_in("admin", "Tableau de bord").metric[0].value == "16"
    assert logged_in("fatima", "Tableau de bord").metric[0].value == "8"


def test_operator_acknowledges_under_their_own_name(monkeypatch, factory) -> None:  # noqa: F811
    client = connect(monkeypatch, factory)
    page = logged_in("fatima", "Alertes")
    alert_id = page.selectbox[2].value

    next(b for b in page.button if b.label == "Prendre en charge").click().run()

    assert not page.exception
    stored = client.get(f"/alerts/{alert_id}", headers=auth_headers(client, "fatima")).json()
    assert stored["status"] == "ACKNOWLEDGED"
    assert stored["acknowledged_by"] == "fatima"


def test_executive_has_no_action_buttons(monkeypatch, factory) -> None:  # noqa: F811
    connect(monkeypatch, factory)

    labels = {b.label for b in logged_in("direction", "Alertes").button}

    assert "Prendre en charge" not in labels and "Résoudre" not in labels


def test_admin_tools_are_only_shown_to_admin(monkeypatch, factory) -> None:  # noqa: F811
    connect(monkeypatch, factory)

    admin_labels = {b.label for b in logged_in("admin", "Tableau de bord").button}
    operator_labels = {b.label for b in logged_in("fatima", "Tableau de bord").button}

    assert "Lancer un cycle maintenant" in admin_labels
    assert "Lancer un cycle maintenant" not in operator_labels


def test_missions_page_shows_only_the_authorized_tms(monkeypatch, factory) -> None:  # noqa: F811
    connect(monkeypatch, factory)

    page = logged_in("fatima", "Missions")

    assert [s.value for s in page.subheader] == ["tms_alpha"]


def test_chat_shows_summary_facts_and_its_origin(monkeypatch, factory) -> None:  # noqa: F811
    llm = FakeLlm(
        {"intent": "open_incidents", "tms_id": "tms_alpha", "period": "none"},
        {"synthese": "L'incident INC-502 touche AL-1003."},
    )
    connect(monkeypatch, factory, llm)
    page = logged_in("fatima", "Chatbot")

    page.chat_input[0].set_value("Incidents chez Alpha ?").run()

    assert not page.exception
    text = " ".join(m.value for m in page.markdown)
    assert "INC-502 touche AL-1003" in text
    assert "**Faits**" in text
    assert any("rédigé par le LLM local" in c.value for c in page.caption)


def test_backend_down_gives_a_clear_message(monkeypatch) -> None:
    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    http = httpx.Client(base_url="http://backend.test", transport=httpx.MockTransport(refused))
    monkeypatch.setattr(api_client, "get_api", lambda: FleetMindApi(http))
    page = AppTest.from_file(APP_FILE, default_timeout=30).run()
    page.text_input[0].set_value("fatima")
    page.text_input[1].set_value(PASSWORD)

    next(b for b in page.button if b.label == "Se connecter").click().run()

    assert any("injoignable" in e.value for e in page.error)
    assert not page.exception


def test_api_error_detail_is_shown() -> None:
    response = httpx.Response(409, json={"detail": "Alerte déjà résolue"})
    http = httpx.Client(base_url="http://b.test", transport=httpx.MockTransport(lambda _: response))

    with pytest.raises(ApiError, match="Alerte déjà résolue"):
        FleetMindApi(http).alert(1)


def test_formatting_keeps_missing_dates_visible_and_orders_alerts() -> None:
    alerts = [
        {"id": 1, "status": "ACKNOWLEDGED", "severity": "HIGH", "escalation_level": 2, "opened_at": "2026-11-07T09:00:00Z"},
        {"id": 2, "status": "OPEN", "severity": "LOW", "escalation_level": 0, "opened_at": "2026-11-07T09:00:00Z"},
        {"id": 3, "status": "OPEN", "severity": "HIGH", "escalation_level": 1, "opened_at": "2026-11-07T10:00:00Z"},
    ]

    assert [a["id"] for a in sort_alerts(alerts)] == [3, 2, 1]
    assert format_time(None) == "Non renseigné"
    assert format_time("2026-11-07T09:30:00Z") == "07/11/2026 09:30 UTC"


def test_sidebar_keeps_description_on_top_and_logout_in_its_footer(monkeypatch, factory) -> None:  # noqa: F811
    connect(monkeypatch, factory)

    page = logged_in("fatima", "Alertes")

    assert "Agent d'exploitation local" in page.sidebar.caption[0].value
    assert "fatima" in page.sidebar.caption[1].value
    assert page.sidebar.button[-1].label == "Se déconnecter"
