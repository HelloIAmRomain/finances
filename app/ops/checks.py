"""Alertes techniques (ALR-08, COL-07, NF-RES-04). Chaque contrôle est idempotent : il lève
l'alerte tant que la condition dure (une seule fois) et la résout quand elle disparaît."""

import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import Engine, select, text, update

from app.alerts.engine import raise_alert, resolve_by_key
from app.db import write_transaction
from app.models import Account, AccountSyncState, Alert, Connection
from app.models.alerts import AlertStatus, Severity
from app.models.ledger import ConnectionStatus
from app.timeutil import ensure_utc

SYNC_STALE_AFTER = timedelta(hours=24)
BACKUP_STALE_AFTER = timedelta(hours=1)
DISK_RAISE_PERCENT, DISK_CLEAR_PERCENT = 85, 80
CONSENT_STAGES = (  # (seuil, étape, gravité), du plus urgent au moins urgent
    (timedelta(0), "expired", Severity.CRITICAL),
    (timedelta(days=1), "J-1", Severity.CRITICAL),
    (timedelta(days=7), "J-7", Severity.WARNING),
    (timedelta(days=14), "J-14", Severity.INFO),
)


@dataclass(frozen=True)
class DiskUsage:
    total: int
    used: int


def check_consents(engine: Engine, *, now: datetime) -> None:
    """Consentement bancaire : J-14 (info), J-7 (avertissement), J-1 et expiré (critique)."""
    with engine.connect() as connection:
        connections = connection.execute(
            select(Connection.id, Connection.institution_name, Connection.consent_expires_at).where(
                Connection.status.in_([ConnectionStatus.ACTIVE, ConnectionStatus.EXPIRED]),
                Connection.consent_expires_at.is_not(None),
            )
        ).all()
    for connection_id, _institution, expires_at in connections:
        if expires_at is None:
            continue
        remaining = ensure_utc(expires_at) - now
        stage = next(
            ((name, sev) for limit, name, sev in CONSENT_STAGES if remaining <= limit), None
        )
        current_key = None
        if stage is not None:
            name, severity = stage
            current_key = f"consent:{connection_id}:{expires_at.date()}:{name}"
            title = (
                "Consentement bancaire expiré"
                if name == "expired"
                else "Consentement bancaire à renouveler"
            )
            raise_alert(
                engine,
                type="consent",
                severity=severity,
                key=current_key,
                title=title,
                subject=("connection", connection_id),
                details={"stage": name},
                now=now,
            )
        _resolve_other_consent_alerts(engine, connection_id, keep=current_key, now=now)


def _resolve_other_consent_alerts(
    engine: Engine, connection_id: int, *, keep: str | None, now: datetime
) -> None:
    """Étape dépassée ou consentement renouvelé : les anciennes alertes sont résolues."""
    with write_transaction(engine) as connection:
        connection.execute(
            update(Alert)
            .where(
                Alert.type == "consent",
                Alert.subject_type == "connection",
                Alert.subject_id == connection_id,
                Alert.status != AlertStatus.RESOLVED,
                Alert.dedupe_key != (keep or ""),
            )
            .values(status=AlertStatus.RESOLVED, resolved_at=now)
        )


def check_sync_freshness(engine: Engine, *, now: datetime) -> None:
    """Comptes reliés à une banque active sans synchro réussie depuis 24 h."""
    with engine.connect() as connection:
        accounts = connection.execute(
            select(Account.id, AccountSyncState.last_success_at)
            .join(Connection, Connection.id == Account.connection_id)
            .outerjoin(AccountSyncState, AccountSyncState.account_id == Account.id)
            .where(
                Connection.status == ConnectionStatus.ACTIVE,
                Account.closed_at.is_(None),
                Account.ignored_at.is_(None),
            )
        ).all()
    for account_id, last_success in accounts:
        key = f"sync_stale:{account_id}"
        if last_success is None or now - ensure_utc(last_success) > SYNC_STALE_AFTER:
            raise_alert(
                engine,
                type="sync_stale",
                severity=Severity.WARNING,
                key=key,
                title="Compte non mis à jour depuis 24 h",
                subject=("account", account_id),
                now=now,
            )
        else:
            resolve_by_key(engine, key, now=now)


def disk_usage(path: Path) -> DiskUsage:
    usage = shutil.disk_usage(path)
    return DiskUsage(total=usage.total, used=usage.used)


def check_disk(engine: Engine, *, usage: DiskUsage, now: datetime) -> None:
    """Disque presque plein au-delà de 85 %, résolu sous 80 % (évite les allers-retours)."""
    if usage.used * 100 >= usage.total * DISK_RAISE_PERCENT:
        raise_alert(
            engine,
            type="disk",
            severity=Severity.WARNING,
            key="disk:data",
            title="Disque presque plein",
            details={"percent_used": usage.used * 100 // usage.total},
            now=now,
        )
    elif usage.used * 100 < usage.total * DISK_CLEAR_PERCENT:
        resolve_by_key(engine, "disk:data", now=now)


def check_database_integrity(engine: Engine, *, now: datetime, result: str | None = None) -> None:
    if result is None:
        with engine.connect() as connection:
            result = str(connection.execute(text("PRAGMA integrity_check")).scalar_one())
    if result == "ok":
        resolve_by_key(engine, "db_integrity", now=now)
    else:
        raise_alert(
            engine,
            type="db_integrity",
            severity=Severity.CRITICAL,
            key="db_integrity",
            title="Base de données corrompue : restauration nécessaire",
            details={"integrity_check": result[:500]},
            now=now,
        )


def check_backup_freshness(engine: Engine, *, marker: Path, now: datetime) -> None:
    """La sauvegarde (lancée par l'hôte) inscrit l'heure de sa dernière réussite dans `marker`."""
    try:
        last = ensure_utc(datetime.fromisoformat(marker.read_text(encoding="utf-8").strip()))
    except (OSError, ValueError):
        last = None
    if last is None or now - last > BACKUP_STALE_AFTER:
        raise_alert(
            engine,
            type="backup",
            severity=Severity.CRITICAL,
            key="backup_stale",
            title="Aucune sauvegarde réussie depuis plus d'une heure",
            now=now,
        )
    else:
        resolve_by_key(engine, "backup_stale", now=now)
