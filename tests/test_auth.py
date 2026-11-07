"""Tests de l'authentification et des droits, sans raccourci sur la connexion."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.agents.orchestrator import build_llm_client
from backend.api.dependencies import get_client_opener, get_clock
from backend.api.main import create_app
from backend.api.routes.chatbot import get_retriever_factory
from backend.core.clock import Clock
from backend.core.database import get_session
from backend.models.alert import Alert
from backend.models.chat_log import ChatLog
from backend.models.user import AuthSession, User
from backend.schemas.auth import Role
from backend.services.auth import create_user, login
from tests.helpers import client_for
from tests.test_chatbot import T0, FakeLlm, factory, no_retriever  # noqa: F401

PASSWORD = "mot-de-passe-solide"
CYCLE = {"reference_time": "2026-11-07T11:00:00Z"}


class SpyLlm(FakeLlm):
    def __init__(self, *responses: object) -> None:
        super().__init__(*responses)
        self.seen: list[str] = []

    def complete_json(self, system: str, user: str, schema: dict, max_tokens: int = 200) -> dict:
        self.seen.append(system + user + str(schema))
        return super().complete_json()


@contextmanager
def opener(config):
    yield client_for(config.id)


def build(factory, llm=None) -> TestClient:  # noqa: F811
    def session_override() -> Iterator[Session]:
        with factory() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_client_opener] = lambda: opener
    app.dependency_overrides[build_llm_client] = lambda: llm or FakeLlm()
    app.dependency_overrides[get_clock] = lambda: Clock(T0)
    app.dependency_overrides[get_retriever_factory] = lambda: no_retriever
    return TestClient(app)


def add_user(factory, username: str, role: Role, tms: list[str]) -> None:  # noqa: F811
    with factory() as session:
        create_user(session, username, PASSWORD, role, tms, datetime.now(UTC))
        session.commit()


def headers_for(client: TestClient, username: str) -> dict[str, str]:
    response = client.post("/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
def world(factory):  # noqa: F811
    """Un administrateur remplit la base ; chaque autre compte a un périmètre limité."""
    add_user(factory, "admin", Role.ADMIN, ["*"])
    add_user(factory, "alpha-op", Role.OPERATOR, ["tms_alpha"])
    add_user(factory, "alpha-boss", Role.EXECUTIVE, ["tms_alpha"])
    client = build(factory)
    assert client.post("/monitoring/cycles", json=CYCLE, headers=headers_for(client, "admin")).status_code == 200
    return factory, client


def alert_id(factory, tms: str) -> int:  # noqa: F811
    with factory() as session:
        return session.scalars(select(Alert.id).where(Alert.source_tms == tms)).first()


@pytest.mark.parametrize(
    "method,path",
    [("GET", "/alerts"), ("GET", "/alerts/1"), ("POST", "/alerts/1/resolve"), ("GET", "/missions"),
     ("GET", "/monitoring/status"), ("POST", "/monitoring/cycles"), ("POST", "/chatbot/ask"),
     ("POST", "/documents/reindex"), ("GET", "/auth/me")],
)
def test_every_route_requires_a_session(factory, method: str, path: str) -> None:  # noqa: F811
    response = build(factory).request(method, path, json={})

    assert response.status_code in (401, 422)
    if response.status_code == 422:
        valid = build(factory).request(method, path, json={"question": "x", "note": None})
        assert valid.status_code == 401


def test_health_stays_public(factory) -> None:  # noqa: F811
    assert build(factory).get("/health").status_code == 200


def test_forged_token_is_refused(factory) -> None:  # noqa: F811
    client = build(factory)

    assert client.get("/alerts", headers={"Authorization": "Bearer faux"}).status_code == 401


def test_secrets_are_not_stored_in_clear(factory) -> None:  # noqa: F811
    add_user(factory, "admin", Role.ADMIN, ["*"])
    client = build(factory)
    token = client.post("/auth/login", json={"username": "admin", "password": PASSWORD}).json()["access_token"]

    with factory() as session:
        assert PASSWORD not in session.scalars(select(User)).one().password_hash
        assert session.scalars(select(AuthSession)).one().token_hash != token


def test_login_error_does_not_reveal_if_account_exists(factory) -> None:  # noqa: F811
    add_user(factory, "admin", Role.ADMIN, ["*"])
    client = build(factory)

    wrong = client.post("/auth/login", json={"username": "admin", "password": "faux"})
    unknown = client.post("/auth/login", json={"username": "personne", "password": "faux"})

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_account_is_locked_after_five_failures(factory) -> None:  # noqa: F811
    add_user(factory, "admin", Role.ADMIN, ["*"])
    client = build(factory)
    for _ in range(5):
        client.post("/auth/login", json={"username": "admin", "password": "faux"})

    locked = client.post("/auth/login", json={"username": "admin", "password": PASSWORD})

    assert locked.status_code == 401
    with factory() as session:
        later = datetime.now(UTC) + timedelta(minutes=16)
        assert login(session, "admin", PASSWORD, later)[0]


def test_short_password_is_refused(factory) -> None:  # noqa: F811
    with factory() as session, pytest.raises(ValueError, match="10 caractères"):
        create_user(session, "x", "court", Role.ADMIN, ["*"], datetime.now(UTC))


def test_expired_session_and_logout_stop_access(world) -> None:
    factory, client = world
    headers = headers_for(client, "alpha-op")
    assert client.get("/auth/me", headers=headers).json()["username"] == "alpha-op"

    assert client.post("/auth/logout", headers=headers).status_code == 204
    assert client.get("/auth/me", headers=headers).status_code == 401

    headers = headers_for(client, "alpha-op")
    with factory() as session:
        for stored in session.scalars(select(AuthSession)):
            stored.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        session.commit()
    assert client.get("/auth/me", headers=headers).status_code == 401


def test_deactivated_account_loses_access_immediately(world) -> None:
    factory, client = world
    headers = headers_for(client, "alpha-op")
    with factory() as session:
        session.scalars(select(User).where(User.username == "alpha-op")).one().is_active = False
        session.commit()

    assert client.get("/alerts", headers=headers).status_code == 401


def test_user_sees_only_the_authorized_tms(world) -> None:
    _, client = world
    headers = headers_for(client, "alpha-op")

    alerts = client.get("/alerts", headers=headers).json()
    missions = client.get("/missions", headers=headers).json()
    status = client.get("/monitoring/status", headers=headers).json()

    assert alerts and {a["source_tms"] for a in alerts} == {"tms_alpha"}
    assert [m["source_tms"] for m in missions] == ["tms_alpha"]
    assert [t["source_tms"] for t in status["tms"]] == ["tms_alpha"]


def test_forbidden_tms_looks_like_it_does_not_exist(world) -> None:
    factory, client = world
    headers = headers_for(client, "alpha-op")
    beta_alert = alert_id(factory, "tms_beta")

    assert client.get(f"/alerts/{beta_alert}", headers=headers).status_code == 404
    assert client.post(f"/alerts/{beta_alert}/resolve", json={}, headers=headers).status_code == 404
    assert client.get("/missions", params={"tms_id": "tms_beta"}, headers=headers).status_code == 404
    assert client.get("/alerts", params={"tms_id": "tms_beta"}, headers=headers).json() == []
    with factory() as session:
        assert session.get(Alert, beta_alert).status == "OPEN"


def test_author_comes_from_the_session_not_the_request(world) -> None:
    factory, client = world
    headers = headers_for(client, "alpha-op")
    target = alert_id(factory, "tms_alpha")

    response = client.post(
        f"/alerts/{target}/acknowledge", json={"actor": "direction", "note": "Vu"}, headers=headers
    )

    assert response.json()["acknowledged_by"] == "alpha-op"
    history = client.get(f"/alerts/{target}", headers=headers).json()["history"]
    assert history[-1]["actor"] == "alpha-op" and history[-1]["note"] == "Vu"


def test_executive_can_read_but_not_act(world) -> None:
    factory, client = world
    headers = headers_for(client, "alpha-boss")
    target = alert_id(factory, "tms_alpha")

    assert client.get(f"/alerts/{target}", headers=headers).status_code == 200
    assert client.post(f"/alerts/{target}/acknowledge", json={}, headers=headers).status_code == 403
    assert client.post(f"/alerts/{target}/resolve", json={}, headers=headers).status_code == 403


@pytest.mark.parametrize("username", ["alpha-op", "alpha-boss"])
def test_admin_actions_are_reserved(world, username: str) -> None:
    _, client = world
    headers = headers_for(client, username)

    assert client.post("/monitoring/cycles", json=CYCLE, headers=headers).status_code == 403
    assert client.post("/documents/reindex", headers=headers).status_code == 403


def test_question_citing_a_forbidden_tms_never_reaches_the_llm(world) -> None:
    factory, _ = world
    llm = SpyLlm()
    client = build(factory, llm)

    body = client.post(
        "/chatbot/ask", json={"question": "Quels retards chez BETA ?"}, headers=headers_for(client, "alpha-op")
    ).json()

    assert llm.seen == []
    assert body["answered"] is False
    assert "hors de votre périmètre" in body["summary"]


def test_llm_is_not_even_told_about_forbidden_tms(world) -> None:
    factory, _ = world
    llm = SpyLlm({"intent": "unsupported", "tms_id": None, "period": "none"})
    client = build(factory, llm)

    client.post("/chatbot/ask", json={"question": "Bonjour"}, headers=headers_for(client, "alpha-op"))

    assert "tms_alpha" in llm.seen[0] and "tms_beta" not in llm.seen[0]
    assert "Beta" not in llm.seen[0]


def test_tms_id_invented_by_llm_outside_scope_is_refused(world) -> None:
    factory, _ = world
    llm = FakeLlm({"intent": "open_incidents", "tms_id": "tms_beta", "period": "none"})
    client = build(factory, llm)

    body = client.post(
        "/chatbot/ask", json={"question": "Incidents ?"}, headers=headers_for(client, "alpha-op")
    ).json()

    assert body["answered"] is False
    assert body["facts"] == []


def test_explaining_a_forbidden_alert_says_it_does_not_exist(world) -> None:
    factory, _ = world
    beta_alert = alert_id(factory, "tms_beta")
    llm = FakeLlm({"intent": "explain_alert", "tms_id": None, "period": "none", "alert_id": beta_alert})
    client = build(factory, llm)

    body = client.post(
        "/chatbot/ask", json={"question": f"Explique l'alerte {beta_alert}"},
        headers=headers_for(client, "alpha-op"),
    ).json()

    assert body["answered"] is False
    assert "n'existe pas" in body["summary"]
    assert body["facts"] == [] and body["draft_message"] is None


def test_report_without_tms_covers_only_the_authorized_scope(world) -> None:
    factory, _ = world
    llm = FakeLlm({"intent": "delivery_summary", "tms_id": None, "period": "none"})
    client = build(factory, llm)

    body = client.post(
        "/chatbot/ask", json={"question": "Bilan des livraisons"}, headers=headers_for(client, "alpha-op")
    ).json()

    assert [tms["tms_id"] for tms in body["scope"]] == ["tms_alpha"]
    assert "tms_beta" not in body["summary"]


def test_question_is_traced_with_its_author(world) -> None:
    factory, _ = world
    client = build(factory, FakeLlm({"intent": "unsupported", "tms_id": None, "period": "none"}))

    client.post("/chatbot/ask", json={"question": "Bonjour"}, headers=headers_for(client, "alpha-boss"))

    with factory() as session:
        assert session.scalars(select(ChatLog)).one().username == "alpha-boss"


def test_user_without_valid_tms_cannot_use_the_chatbot(factory) -> None:  # noqa: F811
    add_user(factory, "vide", Role.OPERATOR, ["tms_supprime"])
    client = build(factory, SpyLlm())

    response = client.post("/chatbot/ask", json={"question": "Bonjour"}, headers=headers_for(client, "vide"))

    assert response.status_code == 403
