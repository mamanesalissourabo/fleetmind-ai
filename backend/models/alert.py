"""Tables des alertes, de leur historique d'audit et des notifications."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.models.base import Base, UTCDateTime

NOT_RESOLVED = text("status <> 'RESOLVED'")


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (
        # Garantit en base une seule alerte active par sujet, même en cas d'appels simultanés.
        Index(
            "uq_alerts_active_dedup_key",
            "dedup_key",
            unique=True,
            postgresql_where=NOT_RESOLVED,
            sqlite_where=NOT_RESOLVED,
        ),
        Index("ix_alerts_source_status", "source_tms", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    dedup_key: Mapped[str] = mapped_column(String(300))
    source_tms: Mapped[str] = mapped_column(String(100))
    mission_external_id: Mapped[str] = mapped_column(String(100))
    rule_id: Mapped[str] = mapped_column(String(100))
    rule_type: Mapped[str] = mapped_column(String(50))
    ruleset_version: Mapped[int]
    nature: Mapped[str] = mapped_column(String(30))
    severity: Mapped[str] = mapped_column(String(20))
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20))
    escalation_level: Mapped[int] = mapped_column(default=0)
    reminders_sent: Mapped[int] = mapped_column(default=0)
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime)
    last_notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    acknowledged_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    acknowledged_by: Mapped[str | None] = mapped_column(String(100))
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    resolved_by: Mapped[str | None] = mapped_column(String(100))

    history: Mapped[list["AlertHistory"]] = relationship(
        back_populates="alert", order_by="AlertHistory.id", cascade="all, delete-orphan"
    )
    notifications: Mapped[list["Notification"]] = relationship(
        back_populates="alert", order_by="Notification.id", cascade="all, delete-orphan"
    )


class AlertHistory(Base):
    __tablename__ = "alert_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey("alerts.id"), index=True)
    action: Mapped[str] = mapped_column(String(30))
    actor: Mapped[str] = mapped_column(String(100))
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime)
    note: Mapped[str | None] = mapped_column(Text)

    alert: Mapped[Alert] = relationship(back_populates="history")


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey("alerts.id"), index=True)
    # Rend chaque envoi idempotent : la même notification ne peut exister qu'une fois.
    idempotency_key: Mapped[str] = mapped_column(String(300), unique=True)
    kind: Mapped[str] = mapped_column(String(20))
    recipient: Mapped[str] = mapped_column(String(100))
    channel: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)

    alert: Mapped[Alert] = relationship(back_populates="notifications")
