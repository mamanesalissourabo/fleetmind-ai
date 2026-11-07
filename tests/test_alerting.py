"""Tests du cycle de vie des alertes : doublons, relances, escalades et résolution."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.models.alert import Alert, Notification
from backend.models.base import Base
from backend.schemas.alerts import AlertStatus, NotificationKind
from backend.schemas.canonical import Severity
from backend.schemas.findings import CycleStatus, Finding, FindingNature, MonitoringReport
from backend.services.alerting import AlertingService, AlertTransitionError, CYCLE_FAILED_RULE
from backend.services.notification import NotificationService
from backend.services.rules_registry import RulesRegistry

T0 = datetime(2026, 11, 7, 11, 0, tzinfo=UTC)
SERVICE = AlertingService(RulesRegistry.from_directory(), NotificationService())


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db_session:
        yield db_session


def finding(at: datetime, severity: Severity = Severity.HIGH, tms: str = "tms_alpha") -> Finding:
    return Finding(
        source_tms=tms, mission_external_id="M-1", rule_id="r1", rule_type="estimated_delay",
        ruleset_version=1, nature=FindingNature.RISK, severity=severity, message="retard",
        details={"delay_minutes": 40}, reference_time=at,
    )


def report(
    at: datetime,
    findings: list[Finding] | None = None,
    status: CycleStatus = CycleStatus.COMPLETE,
    tms: str = "tms_alpha",
) -> MonitoringReport:
    return MonitoringReport(
        source_tms=tms, status=status, reference_time=at, retrieved_at=at,
        ruleset_version=1, findings=findings if findings is not None else [finding(at, tms=tms)],
        error="panne" if status == CycleStatus.FAILED else None,
    )


def minutes(value: int) -> datetime:
    return T0 + timedelta(minutes=value)


def alerts(session: Session) -> list[Alert]:
    return list(session.scalars(select(Alert).order_by(Alert.id)))


def sent(session: Session) -> list[tuple[str, str]]:
    rows = session.scalars(select(Notification).order_by(Notification.id))
    return [(row.kind, row.recipient) for row in rows]


def test_repeated_cycles_create_one_alert_and_one_notification(session: Session) -> None:
    for _ in range(3):
        SERVICE.process_report(session, report(T0))

    assert len(alerts(session)) == 1
    assert sent(session) == [("INITIAL", "exploitation-alpha")]


def test_reminders_and_escalations_follow_the_policy_without_repetition(session: Session) -> None:
    for value in (0, 30, 60, 90, 120, 150, 180):
        SERVICE.process_report(session, report(minutes(value)))
        SERVICE.process_report(session, report(minutes(value)))

    assert sent(session) == [
        ("INITIAL", "exploitation-alpha"),
        ("REMINDER", "exploitation-alpha"),
        ("ESCALATION", "responsable-exploitation-alpha"),
        ("REMINDER", "exploitation-alpha"),
        ("ESCALATION", "direction-alpha"),
    ]
    assert alerts(session)[0].escalation_level == 2


def test_acknowledgement_stops_follow_up_but_keeps_alert_active(session: Session) -> None:
    SERVICE.process_report(session, report(T0))
    alert = alerts(session)[0]
    SERVICE.acknowledge(session, alert.id, "sophie", "Je m'en occupe", at=minutes(5))

    SERVICE.process_report(session, report(minutes(300)))

    assert alert.status == AlertStatus.ACKNOWLEDGED
    assert alert.acknowledged_by == "sophie"
    assert sent(session) == [("INITIAL", "exploitation-alpha")]


def test_severity_below_policy_is_tracked_without_notification(session: Session) -> None:
    SERVICE.process_report(session, report(T0, [finding(T0, Severity.LOW)]))

    assert len(alerts(session)) == 1
    assert sent(session) == []


def test_complete_cycle_without_the_finding_resolves_the_alert(session: Session) -> None:
    SERVICE.process_report(session, report(T0))

    outcome = SERVICE.process_report(session, report(minutes(10), findings=[]))

    assert outcome.resolved == 1
    assert alerts(session)[0].status == AlertStatus.RESOLVED
    assert alerts(session)[0].resolved_by == "system"


@pytest.mark.parametrize("status", [CycleStatus.PARTIAL, CycleStatus.FAILED])
def test_incomplete_cycle_never_resolves_an_alert(session: Session, status: CycleStatus) -> None:
    SERVICE.process_report(session, report(T0))

    SERVICE.process_report(session, report(minutes(10), findings=[], status=status))

    assert alerts(session)[0].status == AlertStatus.OPEN


def test_failed_cycle_opens_one_system_alert_closed_after_recovery(session: Session) -> None:
    for value in (0, 5):
        SERVICE.process_report(
            session, report(minutes(value), findings=[], status=CycleStatus.FAILED)
        )

    failures = [alert for alert in alerts(session) if alert.rule_id == CYCLE_FAILED_RULE]
    assert len(failures) == 1
    assert failures[0].status == AlertStatus.OPEN

    SERVICE.process_report(session, report(minutes(10), findings=[], status=CycleStatus.PARTIAL))

    assert failures[0].status == AlertStatus.RESOLVED


def test_tms_without_rules_keeps_alert_without_notification(session: Session) -> None:
    SERVICE.process_report(
        session, report(T0, findings=[], status=CycleStatus.FAILED, tms="tms_gamma")
    )

    assert [alert.source_tms for alert in alerts(session)] == ["tms_gamma"]
    assert sent(session) == []


def test_problem_detected_again_after_resolution_opens_a_new_alert(session: Session) -> None:
    SERVICE.process_report(session, report(T0))
    SERVICE.resolve(session, alerts(session)[0].id, "sophie", at=minutes(1))

    SERVICE.process_report(session, report(minutes(2)))

    assert [alert.status for alert in alerts(session)] == [AlertStatus.RESOLVED, AlertStatus.OPEN]


def test_resolved_alert_cannot_be_acknowledged(session: Session) -> None:
    SERVICE.process_report(session, report(T0))
    alert_id = alerts(session)[0].id
    SERVICE.resolve(session, alert_id, "sophie", at=minutes(1))

    with pytest.raises(AlertTransitionError):
        SERVICE.acknowledge(session, alert_id, "paul")


def test_history_records_who_did_what(session: Session) -> None:
    SERVICE.process_report(session, report(T0))
    alert = alerts(session)[0]
    SERVICE.acknowledge(session, alert.id, "sophie", "En cours", at=minutes(1))
    SERVICE.resolve(session, alert.id, "sophie", "Client prévenu", at=minutes(2))

    assert [(row.action, row.actor) for row in alert.history] == [
        ("OPENED", "system"),
        ("NOTIFIED", "system"),
        ("ACKNOWLEDGED", "sophie"),
        ("RESOLVED", "sophie"),
    ]
    assert alert.history[-1].note == "Client prévenu"


def test_severity_change_is_recorded_and_can_trigger_notification(session: Session) -> None:
    SERVICE.process_report(session, report(T0, [finding(T0, Severity.LOW)]))
    SERVICE.process_report(session, report(minutes(5), [finding(minutes(5), Severity.HIGH)]))

    assert alerts(session)[0].severity == Severity.HIGH
    assert sent(session) == [(NotificationKind.INITIAL.value, "exploitation-alpha")]
