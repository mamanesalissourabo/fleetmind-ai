"""Lancement manuel d'un cycle et état de la surveillance."""

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from backend.api.dependencies import (
    AdminDep,
    CurrentUserDep,
    ClockDep,
    CycleRunnerDep,
    SessionDep,
    TmsRegistryDep,
    select_tms,
    visible_tms,
)
from backend.schemas.alerts import CycleRequest, CycleResult
from backend.services.cycle_runner import CycleTrigger
from backend.services.monitoring_status import TmsMonitoringStatus, get_tms_status

router = APIRouter(prefix="/monitoring", tags=["monitoring"])


class MonitoringStatus(BaseModel):
    scheduler_running: bool
    tms: list[TmsMonitoringStatus]


@router.post("/cycles", response_model=list[CycleResult])
def run_cycles(
    request: CycleRequest,
    session: SessionDep,
    registry: TmsRegistryDep,
    runner: CycleRunnerDep,
    clock: ClockDep,
    user: AdminDep,
) -> list[CycleResult]:
    """Surveille un TMS ou tous les TMS ; la panne de l'un n'empêche pas les autres."""
    reference = request.reference_time or clock.now()
    results = []
    for config in select_tms(registry, user, request.tms_id):
        result = runner.run(session, config, CycleTrigger.MANUAL, reference)
        if result is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"Un cycle est déjà en cours pour {config.id}"
            )
        results.append(result)
    return results


@router.get("/status", response_model=MonitoringStatus)
def get_status(
    http_request: Request, session: SessionDep, registry: TmsRegistryDep, user: CurrentUserDep
) -> MonitoringStatus:
    """Indique si la surveillance fonctionne, et depuis quand chaque TMS est surveillé."""
    now = datetime.now(UTC)
    scheduler = getattr(http_request.app.state, "scheduler", None)
    statuses = [get_tms_status(session, config, now) for config in visible_tms(registry, user)]
    return MonitoringStatus(
        scheduler_running=bool(scheduler and scheduler.running), tms=statuses
    )
