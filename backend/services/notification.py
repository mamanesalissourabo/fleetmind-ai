"""Notifications simulées : enregistrées en base, sans aucun envoi réel."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.alert import Alert, Notification
from backend.schemas.alerts import NotificationKind

SIMULATED_CHANNEL = "simulated"
SIMULATED_STATUS = "SIMULATED"


class NotificationService:
    """Enregistre une notification, une seule fois pour une même clé d'idempotence."""

    def notify(
        self,
        session: Session,
        alert: Alert,
        kind: NotificationKind,
        recipient: str,
        step: str,
        at: datetime,
    ) -> bool:
        """Retourne True si la notification est nouvelle, False si elle existait déjà."""
        key = f"alert:{alert.id}:{step}:{recipient}"
        if session.scalar(select(Notification.id).where(Notification.idempotency_key == key)):
            return False
        session.add(
            Notification(
                alert_id=alert.id,
                idempotency_key=key,
                kind=kind.value,
                recipient=recipient,
                channel=SIMULATED_CHANNEL,
                status=SIMULATED_STATUS,
                message=f"{alert.source_tms} / {alert.mission_external_id} : {alert.message}",
                created_at=at,
            )
        )
        session.flush()
        return True
