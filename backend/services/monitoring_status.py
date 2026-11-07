"""État de la surveillance d'un TMS : depuis quand a-t-il été surveillé avec succès ?"""

from datetime import datetime, timedelta

from pydantic import AwareDatetime, BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.monitoring_run import MonitoringRun
from backend.schemas.findings import CycleStatus
from backend.schemas.tms_config import TmsConfig

OVERDUE_AFTER_INTERVALS = 3


class TmsMonitoringStatus(BaseModel):
    source_tms: str
    polling_interval_seconds: int
    last_run_at: AwareDatetime | None
    last_run_status: CycleStatus | None
    last_run_trigger: str | None
    last_success_at: AwareDatetime | None
    overdue: bool
    error: str | None


def get_tms_status(session: Session, config: TmsConfig, now: datetime) -> TmsMonitoringStatus:
    latest = select(MonitoringRun).where(MonitoringRun.source_tms == config.id).order_by(
        MonitoringRun.started_at.desc()
    )
    last = session.scalars(latest.limit(1)).first()
    success = session.scalars(
        latest.where(MonitoringRun.status != CycleStatus.FAILED.value).limit(1)
    ).first()
    tolerance = timedelta(seconds=config.polling_interval_seconds * OVERDUE_AFTER_INTERVALS)
    return TmsMonitoringStatus(
        source_tms=config.id,
        polling_interval_seconds=config.polling_interval_seconds,
        last_run_at=last.started_at if last else None,
        last_run_status=last.status if last else None,
        last_run_trigger=last.trigger if last else None,
        last_success_at=success.started_at if success else None,
        overdue=success is None or now - success.started_at > tolerance,
        error=last.error if last else None,
    )
