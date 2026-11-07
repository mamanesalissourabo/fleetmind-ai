"""Exécution d'un cycle complet, commune au planificateur et au lancement manuel."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from enum import StrEnum
from zlib import crc32

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from backend.integrations.generic_client import GenericTmsClient
from backend.models.monitoring_run import MonitoringRun
from backend.schemas.alerts import CycleResult
from backend.schemas.tms_config import TmsConfig
from backend.services.alerting import AlertingService
from backend.services.monitoring import MonitoringService

ClientOpener = Callable[[TmsConfig], AbstractContextManager[GenericTmsClient]]


class CycleTrigger(StrEnum):
    SCHEDULED = "SCHEDULED"
    MANUAL = "MANUAL"


def try_lock_tms(session: Session, tms_id: str) -> bool:
    """Réserve un TMS jusqu'à la fin de la transaction ; False s'il est déjà occupé."""
    if session.get_bind().dialect.name != "postgresql":
        return True
    key = crc32(f"fleetmind-cycle:{tms_id}".encode())
    return bool(session.scalar(text("select pg_try_advisory_xact_lock(:key)"), {"key": key}))


def last_scheduled_reference(session: Session) -> datetime | None:
    """Retourne l'heure de référence du dernier cycle planifié."""
    return session.scalar(
        select(func.max(MonitoringRun.reference_time)).where(
            MonitoringRun.trigger == CycleTrigger.SCHEDULED.value
        )
    )


class CycleRunner:
    def __init__(
        self,
        open_client: ClientOpener,
        monitoring: MonitoringService,
        alerting: AlertingService,
    ) -> None:
        self._open_client = open_client
        self._monitoring = monitoring
        self._alerting = alerting

    def run(
        self,
        session: Session,
        config: TmsConfig,
        trigger: CycleTrigger,
        reference_time: datetime,
    ) -> CycleResult | None:
        """Exécute et enregistre un cycle ; retourne None si le TMS est déjà en cours de cycle."""
        if not try_lock_tms(session, config.id):
            return None

        started_at = datetime.now(UTC)
        with self._open_client(config) as client:
            report = self._monitoring.run_cycle(client, reference_time)
        outcome = self._alerting.process_report(session, report)
        session.add(
            MonitoringRun(
                source_tms=config.id,
                trigger=trigger.value,
                status=report.status.value,
                reference_time=reference_time,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                missions_evaluated=report.missions_evaluated,
                findings_count=len(report.findings),
                alerts_opened=outcome.opened,
                alerts_resolved=outcome.resolved,
                notifications_sent=outcome.notifications,
                error=report.error,
            )
        )
        session.commit()
        return CycleResult(report=report, alerts=outcome)
