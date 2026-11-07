"""Tests de la surveillance automatique, de l'horloge et de l'état de la surveillance."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.dependencies import get_current_user
from tests.helpers import TEST_ADMIN
from backend.api.dependencies import build_cycle_runner, get_client_opener, get_cycle_runner
from backend.api.main import create_app
from backend.core.clock import Clock
from backend.core.database import get_session
from backend.models.alert import Alert
from backend.models.base import Base
from backend.models.monitoring_run import MonitoringRun
from backend.services.rules_registry import RulesRegistry
from backend.services.scheduler import MonitoringScheduler
from tests.helpers import REGISTRY, client_for

T0 = datetime(2026, 11, 7, 11, 0, tzinfo=UTC)


@contextmanager
def opener(config):
    yield client_for(config.id)


@pytest.fixture
def factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def scheduler_for(factory, session_factory=None) -> MonitoringScheduler:
    return MonitoringScheduler(
        REGISTRY.all(),
        build_cycle_runner(RulesRegistry.from_directory(), opener),
        session_factory or factory,
        Clock(T0),
    )


def test_each_tms_has_its_own_job_and_interval(factory) -> None:
    scheduler = scheduler_for(factory)

    assert scheduler.job_ids() == ["tms_alpha", "tms_beta"]
    assert REGISTRY.get("tms_alpha").polling_interval_seconds == 60
    assert REGISTRY.get("tms_beta").polling_interval_seconds == 120


def test_scheduled_cycle_creates_alerts_and_records_the_run(factory) -> None:
    scheduler = scheduler_for(factory)

    scheduler.run_tms(REGISTRY.get("tms_alpha"))
    scheduler.run_tms(REGISTRY.get("tms_alpha"))

    with factory() as session:
        runs = list(session.scalars(select(MonitoringRun)))
        alerts = list(session.scalars(select(Alert)))
    assert [(run.trigger, run.status) for run in runs] == [("SCHEDULED", "COMPLETE")] * 2
    assert [run.alerts_opened for run in runs] == [8, 0]
    assert len(alerts) == 8


def test_database_failure_is_logged_without_stopping_the_scheduler(factory, caplog) -> None:
    def broken_session() -> Session:
        raise OperationalError("select 1", {}, Exception("postgresql://secret@host"))

    scheduler = scheduler_for(factory, broken_session)

    scheduler.run_tms(REGISTRY.get("tms_alpha"))

    assert "OperationalError" in caplog.text
    assert "secret" not in caplog.text


def test_scheduler_starts_and_stops(factory) -> None:
    scheduler = MonitoringScheduler(
        [], build_cycle_runner(RulesRegistry.from_directory(), opener), factory, Clock(T0)
    )

    scheduler.start()
    assert scheduler.running
    scheduler.shutdown()
    assert not scheduler.running


def test_simulated_clock_advances_at_the_requested_speed() -> None:
    real = [T0]
    clock = Clock(datetime(2026, 1, 1, tzinfo=UTC), speed=10, real_now=lambda: real[0])

    real[0] = T0 + timedelta(minutes=3)

    assert clock.now() == datetime(2026, 1, 1, 0, 30, tzinfo=UTC)


def test_real_clock_is_used_without_simulation() -> None:
    assert Clock(real_now=lambda: T0).now() == T0


def api_for(factory) -> TestClient:
    def session_override() -> Iterator[Session]:
        with factory() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: TEST_ADMIN
    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_client_opener] = lambda: opener
    return TestClient(app)


def test_status_reveals_tms_that_were_never_monitored(factory) -> None:
    body = api_for(factory).get("/monitoring/status").json()

    assert body["scheduler_running"] is False
    assert all(item["overdue"] and item["last_run_at"] is None for item in body["tms"])


def test_status_shows_recent_success_after_a_cycle(factory) -> None:
    api = api_for(factory)
    api.post("/monitoring/cycles", json={"reference_time": "2026-11-07T11:00:00Z"})

    items = api.get("/monitoring/status").json()["tms"]

    assert all(not item["overdue"] and item["last_run_status"] == "COMPLETE" for item in items)
    assert {item["last_run_trigger"] for item in items} == {"MANUAL"}


def test_manual_cycle_returns_409_when_tms_is_busy(factory) -> None:
    class BusyRunner:
        def run(self, *_: object) -> None:
            return None

    api = api_for(factory)
    api.app.dependency_overrides[get_cycle_runner] = lambda: BusyRunner()

    assert api.post("/monitoring/cycles", json={"tms_id": "tms_alpha"}).status_code == 409
