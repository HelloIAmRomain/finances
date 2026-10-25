"""Alertes et file d'envoi des notifications (ALR-01, ALR-02, ALR-06)."""

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.db_types import UTCDateTime
from app.models._common import created_at_column, text_enum


class Severity(enum.StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertStatus(enum.StrEnum):
    OPEN = "open"
    SEEN = "seen"
    RESOLVED = "resolved"


class Channel(enum.StrEnum):
    NTFY = "ntfy"
    EMAIL = "email"


class NotificationKind(enum.StrEnum):
    INITIAL = "initial"
    REMINDER = "reminder"
    FALLBACK = "fallback"  # e-mail de secours d'une alerte critique non vue (I-19)


class NotificationStatus(enum.StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class Alert(Base):
    __tablename__ = "alert"
    __table_args__ = (Index(None, "status", "severity"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(64))
    severity: Mapped[Severity] = mapped_column(text_enum(Severity, "severity"))
    status: Mapped[AlertStatus] = mapped_column(text_enum(AlertStatus, "alert_status"))
    # Jamais deux alertes pour le même fait (inv. 3) ; un nouvel épisode change de clé.
    dedupe_key: Mapped[str] = mapped_column(String(160), unique=True)
    subject_type: Mapped[str | None] = mapped_column(String(64))
    subject_id: Mapped[int | None] = mapped_column(Integer)
    # Sans montant ni nom : les notifications reprennent ce titre (DT §6).
    title: Mapped[str] = mapped_column(String(160))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    opened_at: Mapped[datetime] = created_at_column()
    seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_reminded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Notification(Base):
    __tablename__ = "notification"
    __table_args__ = (UniqueConstraint("alert_id", "channel", "kind", "sequence"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey("alert.id"))
    channel: Mapped[Channel] = mapped_column(text_enum(Channel, "channel"))
    kind: Mapped[NotificationKind] = mapped_column(text_enum(NotificationKind, "notification_kind"))
    sequence: Mapped[int] = mapped_column(Integer, default=1)  # rang du rappel
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = created_at_column()
    status: Mapped[NotificationStatus] = mapped_column(
        text_enum(NotificationStatus, "notification_status")
    )
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
