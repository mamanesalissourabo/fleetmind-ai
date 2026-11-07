"""Cycle de vie des alertes : création, relances, escalades et résolution."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.integrations.errors import TmsConfigError
from backend.models.alert import Alert, AlertHistory
from backend.schemas.alerts import AlertingOutcome, AlertStatus, HistoryAction, NotificationKind
from backend.schemas.canonical import Severity
from backend.schemas.findings import CycleStatus, Finding, FindingNature, MonitoringReport
from backend.schemas.rules import NotificationPolicy
from backend.services.notification import NotificationService
from backend.services.rules_engine import SEVERITY_RANK
from backend.services.rules_registry import RulesRegistry

SYSTEM_ACTOR = "system"
CYCLE_FAILED_RULE = "system-cycle-failed"


class AlertNotFoundError(Exception):
    """L'alerte demandée n'existe pas."""


class AlertTransitionError(Exception):
    """L'action demandée n'est pas possible dans l'état actuel de l'alerte."""


def _record(
    alert: Alert, action: HistoryAction, actor: str, at: datetime, note: str | None = None
) -> None:
    alert.history.append(AlertHistory(action=action.value, actor=actor, occurred_at=at, note=note))


def _cycle_failure_finding(report: MonitoringReport) -> Finding:
    return Finding(
        source_tms=report.source_tms,
        mission_external_id="*",
        rule_id=CYCLE_FAILED_RULE,
        rule_type="cycle_failed",
        ruleset_version=report.ruleset_version or 0,
        nature=FindingNature.MISSING_INFORMATION,
        severity=Severity.HIGH,
        message=f"Surveillance impossible : {report.error}",
        details={"error": report.error or "inconnue"},
        reference_time=report.reference_time,
    )


class AlertingService:
    """Transforme les rapports de surveillance en alertes suivies, sans doublon."""

    def __init__(self, rules: RulesRegistry, notifications: NotificationService) -> None:
        self._rules = rules
        self._notifications = notifications

    def process_report(self, session: Session, report: MonitoringReport) -> AlertingOutcome:
        """Applique un rapport ; l'appelant valide la transaction."""
        outcome = AlertingOutcome()
        reference = report.reference_time
        active = {
            alert.dedup_key: alert
            for alert in session.scalars(
                select(Alert).where(
                    Alert.source_tms == report.source_tms,
                    Alert.status != AlertStatus.RESOLVED.value,
                )
            )
        }

        failed = report.status == CycleStatus.FAILED
        findings = [_cycle_failure_finding(report)] if failed else report.findings
        seen = {finding.dedup_key for finding in findings}
        for finding in findings:
            if finding.dedup_key in active:
                self._refresh(active[finding.dedup_key], finding, reference)
            else:
                active[finding.dedup_key] = self._open(session, finding, reference)
                outcome.opened += 1

        for key, alert in list(active.items()):
            if key not in seen and self._may_auto_resolve(alert, report.status):
                alert.status = AlertStatus.RESOLVED.value
                alert.resolved_at = reference
                alert.resolved_by = SYSTEM_ACTOR
                _record(alert, HistoryAction.AUTO_RESOLVED, SYSTEM_ACTOR, reference)
                outcome.resolved += 1
                del active[key]

        policy = self._policy(report.source_tms)
        if policy is not None:
            for alert in active.values():
                outcome.notifications += self._follow_up(session, alert, policy, reference)
        session.flush()
        return outcome

    def acknowledge(
        self, session: Session, alert_id: int, actor: str,
        note: str | None = None, at: datetime | None = None,
    ) -> Alert:
        alert = self._get(session, alert_id)
        if alert.status == AlertStatus.RESOLVED.value:
            raise AlertTransitionError("Une alerte résolue ne peut pas être prise en charge")
        if alert.status == AlertStatus.OPEN.value:
            moment = at or datetime.now(UTC)
            alert.status = AlertStatus.ACKNOWLEDGED.value
            alert.acknowledged_at = moment
            alert.acknowledged_by = actor
            _record(alert, HistoryAction.ACKNOWLEDGED, actor, moment, note)
            session.flush()
        return alert

    def resolve(
        self, session: Session, alert_id: int, actor: str,
        note: str | None = None, at: datetime | None = None,
    ) -> Alert:
        alert = self._get(session, alert_id)
        if alert.status != AlertStatus.RESOLVED.value:
            moment = at or datetime.now(UTC)
            alert.status = AlertStatus.RESOLVED.value
            alert.resolved_at = moment
            alert.resolved_by = actor
            _record(alert, HistoryAction.RESOLVED, actor, moment, note)
            session.flush()
        return alert

    @staticmethod
    def _get(session: Session, alert_id: int) -> Alert:
        alert = session.get(Alert, alert_id)
        if alert is None:
            raise AlertNotFoundError(f"Alerte introuvable : {alert_id}")
        return alert

    @staticmethod
    def _may_auto_resolve(alert: Alert, status: CycleStatus) -> bool:
        """Un constat absent ne prouve une résolution que si le cycle est complet."""
        if alert.rule_id == CYCLE_FAILED_RULE:
            return status != CycleStatus.FAILED
        return status == CycleStatus.COMPLETE

    def _policy(self, tms_id: str) -> NotificationPolicy | None:
        try:
            return self._rules.get(tms_id).notifications
        except TmsConfigError:
            return None

    @staticmethod
    def _open(session: Session, finding: Finding, reference: datetime) -> Alert:
        alert = Alert(
            dedup_key=finding.dedup_key,
            source_tms=finding.source_tms,
            mission_external_id=finding.mission_external_id,
            rule_id=finding.rule_id,
            rule_type=finding.rule_type,
            ruleset_version=finding.ruleset_version,
            nature=finding.nature.value,
            severity=finding.severity.value,
            message=finding.message,
            details=finding.details,
            status=AlertStatus.OPEN.value,
            escalation_level=0,
            reminders_sent=0,
            opened_at=reference,
            last_seen_at=reference,
        )
        session.add(alert)
        _record(alert, HistoryAction.OPENED, SYSTEM_ACTOR, reference)
        session.flush()
        return alert

    @staticmethod
    def _refresh(alert: Alert, finding: Finding, reference: datetime) -> None:
        if alert.severity != finding.severity.value:
            note = f"{alert.severity} -> {finding.severity.value}"
            alert.severity = finding.severity.value
            _record(alert, HistoryAction.SEVERITY_CHANGED, SYSTEM_ACTOR, reference, note)
        alert.message = finding.message
        alert.details = finding.details
        alert.ruleset_version = finding.ruleset_version
        alert.last_seen_at = reference

    def _follow_up(
        self, session: Session, alert: Alert, policy: NotificationPolicy, reference: datetime
    ) -> int:
        """Déclenche ce qui est dû ; une alerte prise en charge n'est plus relancée."""
        severity = Severity(alert.severity)
        if (
            alert.status != AlertStatus.OPEN.value
            or SEVERITY_RANK[severity] < SEVERITY_RANK[policy.min_severity]
        ):
            return 0

        sent = 0

        def send(kind: NotificationKind, recipient: str, step: str, action: HistoryAction) -> None:
            nonlocal sent
            if self._notifications.notify(session, alert, kind, recipient, step, reference):
                sent += 1
                alert.last_notified_at = reference
                _record(alert, action, SYSTEM_ACTOR, reference, recipient)

        for recipient in policy.recipients:
            send(NotificationKind.INITIAL, recipient, "initial", HistoryAction.NOTIFIED)

        for level, escalation in enumerate(policy.escalations, start=1):
            due = reference - alert.opened_at >= timedelta(minutes=escalation.after_minutes)
            if due and alert.escalation_level < level:
                alert.escalation_level = level
                send(NotificationKind.ESCALATION, escalation.recipient, f"escalation:{level}",
                     HistoryAction.ESCALATED)

        interval = timedelta(minutes=policy.reminder_interval_minutes)
        last = alert.last_notified_at
        if alert.reminders_sent < policy.max_reminders and last and reference - last >= interval:
            alert.reminders_sent += 1
            for recipient in policy.recipients:
                send(NotificationKind.REMINDER, recipient, f"reminder:{alert.reminders_sent}",
                     HistoryAction.REMINDED)
        return sent
