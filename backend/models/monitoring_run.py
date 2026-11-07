"""Trace de chaque cycle de surveillance exécuté."""

from datetime import datetime

from sqlalchemy import Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import Base, UTCDateTime


class MonitoringRun(Base):
    __tablename__ = "monitoring_runs"
    __table_args__ = (Index("ix_monitoring_runs_source_started", "source_tms", "started_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source_tms: Mapped[str] = mapped_column(String(100))
    trigger: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20))
    reference_time: Mapped[datetime] = mapped_column(UTCDateTime)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime] = mapped_column(UTCDateTime)
    missions_evaluated: Mapped[int]
    findings_count: Mapped[int]
    alerts_opened: Mapped[int]
    alerts_resolved: Mapped[int]
    notifications_sent: Mapped[int]
    error: Mapped[str | None] = mapped_column(Text)
