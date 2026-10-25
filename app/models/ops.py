"""Exécutions planifiées et journal d'audit (inv. 3, NF-SEC-06)."""

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.db_types import UTCDateTime
from app.models._common import created_at_column, text_enum
from app.timeutil import utcnow

# Le journal d'audit ne se modifie ni ne s'efface. Utilisé par create_all ET par les migrations.
AUDIT_LOG_APPEND_ONLY_SQL = (
    """
CREATE TRIGGER audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log est en ajout seul (NF-SEC-06)');
END
""",
    """
CREATE TRIGGER audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log est en ajout seul (NF-SEC-06)');
END
""",
)


class JobStatus(enum.StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class JobRun(Base):
    """Une exécution par tâche et par créneau : garantit l'idempotence des tâches planifiées."""

    __tablename__ = "job_run"
    __table_args__ = (UniqueConstraint("job_name", "slot_start"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    job_name: Mapped[str] = mapped_column(String(64))
    slot_start: Mapped[datetime] = mapped_column(UTCDateTime)
    status: Mapped[JobStatus] = mapped_column(text_enum(JobStatus, "job_status"), index=True)
    started_at: Mapped[datetime] = created_at_column()
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    error: Mapped[str | None] = mapped_column(Text)
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    action: Mapped[str] = mapped_column(String(64))
    object_type: Mapped[str | None] = mapped_column(String(64))
    object_id: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class AppSetting(Base):
    """Réglage de l'instance modifiable depuis l'interface (PROD-01) : valeur JSON, défaut dans
    le code du module qui le lit. Toute modification est auditée par ce module."""

    __tablename__ = "app_setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    updated_at: Mapped[datetime] = created_at_column()
