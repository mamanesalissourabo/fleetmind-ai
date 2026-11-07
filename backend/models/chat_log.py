"""Trace de chaque question posée au chatbot."""

from datetime import datetime

from sqlalchemy import JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import Base, UTCDateTime


class ChatLog(Base):
    __tablename__ = "chat_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    asked_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    username: Mapped[str | None] = mapped_column(String(100), index=True)
    question: Mapped[str] = mapped_column(Text)
    intent: Mapped[str] = mapped_column(String(30))
    answered: Mapped[bool]
    summary: Mapped[str] = mapped_column(Text)
    summary_source: Mapped[str] = mapped_column(String(20))
    scope: Mapped[list[str]] = mapped_column(JSON)
    duration_ms: Mapped[int]
