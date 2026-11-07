"""Consultation et traitement des alertes, dans le périmètre de chaque compte."""

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.api.dependencies import AlertingDep, CurrentUserDep, OperatorDep, SessionDep
from backend.models.alert import Alert
from backend.schemas.alerts import AlertAction, AlertDetail, AlertRead, AlertStatus
from backend.schemas.auth import CurrentUser
from backend.services.alerting import AlertTransitionError

router = APIRouter(prefix="/alerts", tags=["alerts"])


def _visible_alert(session: Session, alert_id: int, user: CurrentUser) -> Alert:
    """Une alerte hors périmètre répond comme une alerte inexistante."""
    alert = session.get(Alert, alert_id)
    if alert is None or not user.can_access(alert.source_tms):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Alerte introuvable")
    return alert


@router.get("", response_model=list[AlertRead])
def list_alerts(
    session: SessionDep,
    user: CurrentUserDep,
    alert_status: AlertStatus | None = None,
    tms_id: str | None = None,
) -> list[Alert]:
    """Liste les alertes autorisées, des plus récentes aux plus anciennes."""
    query = select(Alert).order_by(Alert.id.desc())
    if alert_status is not None:
        query = query.where(Alert.status == alert_status.value)
    if tms_id is not None:
        query = query.where(Alert.source_tms == tms_id)
    return [alert for alert in session.scalars(query) if user.can_access(alert.source_tms)]


@router.get("/{alert_id}", response_model=AlertDetail)
def get_alert(alert_id: int, session: SessionDep, user: CurrentUserDep) -> Alert:
    """Retourne une alerte avec son historique et ses notifications."""
    return _visible_alert(session, alert_id, user)


@router.post("/{alert_id}/acknowledge", response_model=AlertRead)
def acknowledge_alert(
    alert_id: int, action: AlertAction, session: SessionDep, alerting: AlertingDep, user: OperatorDep
) -> Alert:
    """Enregistre la prise en charge au nom du compte connecté, ce qui arrête les relances."""
    _visible_alert(session, alert_id, user)
    try:
        alert = alerting.acknowledge(session, alert_id, user.username, action.note)
    except AlertTransitionError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    session.commit()
    return alert


@router.post("/{alert_id}/resolve", response_model=AlertRead)
def resolve_alert(
    alert_id: int, action: AlertAction, session: SessionDep, alerting: AlertingDep, user: OperatorDep
) -> Alert:
    """Clôture une alerte ; elle sera rouverte si le problème est de nouveau détecté."""
    _visible_alert(session, alert_id, user)
    alert = alerting.resolve(session, alert_id, user.username, action.note)
    session.commit()
    return alert
