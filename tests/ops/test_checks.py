"""Alertes techniques (ALR-08, COL-07, NF-RES-04)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, insert, select, update

from app.models import Account, AccountSyncState, Alert, Connection
from app.models.ledger import ConnectionStatus, Provider
from app.ops.checks import (
    DiskUsage,
    check_backup_freshness,
    check_consents,
    check_database_integrity,
    check_disk,
    check_sync_freshness,
)

T0 = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def _alerts(engine: Engine) -> list[tuple[str, str, str]]:
    with engine.connect() as connection:
        rows = connection.execute(
            select(Alert.dedupe_key, Alert.severity, Alert.status).order_by(Alert.id)
        ).all()
    return [(key, str(severity), str(status)) for key, severity, status in rows]


@pytest.fixture
def bank(engine: Engine) -> Engine:
    with engine.begin() as connection:
        connection.execute(
            insert(Connection).values(
                id=1,
                provider=Provider.ENABLE_BANKING,
                institution_name="Banque Exemple",
                country="FR",
                status=ConnectionStatus.ACTIVE,
                consent_expires_at=T0 + timedelta(days=30),
                created_at=T0,
                updated_at=T0,
            )
        )
        connection.execute(update(Account).where(Account.id == 1).values(connection_id=1))
        connection.execute(
            insert(AccountSyncState).values(account_id=1, last_success_at=T0 - timedelta(hours=2))
        )
    return engine


def _set_expiry(engine: Engine, expires: datetime) -> None:
    with engine.begin() as connection:
        connection.execute(update(Connection).values(consent_expires_at=expires))


# --- Consentement ---------------------------------------------------------------------------


def test_no_alert_far_from_expiry(bank: Engine) -> None:
    check_consents(bank, now=T0)
    assert _alerts(bank) == []


def test_consent_expiring_in_seven_days(bank: Engine) -> None:
    """Scénario d'acceptation du CDC : « Consentement bancaire qui expire dans 7 jours »."""
    _set_expiry(bank, T0 + timedelta(days=7))
    check_consents(bank, now=T0)
    assert _alerts(bank) == [("consent:1:2026-10-04:J-7", "warning", "open")]


@pytest.mark.parametrize(
    ("days", "stage", "severity"),
    [
        (14, "J-14", "info"),
        (8, "J-14", "info"),
        (1, "J-1", "critical"),
        (-1, "expired", "critical"),
    ],
)
def test_consent_stages(bank: Engine, days: int, stage: str, severity: str) -> None:
    expires = T0 + timedelta(days=days)
    _set_expiry(bank, expires)
    check_consents(bank, now=T0)
    assert _alerts(bank) == [(f"consent:1:{expires.date()}:{stage}", severity, "open")]


def test_consent_alerts_are_idempotent_and_escalate(bank: Engine) -> None:
    expires = T0 + timedelta(days=14)
    _set_expiry(bank, expires)
    for day in range(0, 8):
        check_consents(bank, now=T0 + timedelta(days=day))
        check_consents(bank, now=T0 + timedelta(days=day, hours=1))
    keys = [key for key, _, _ in _alerts(bank)]
    assert keys == ["consent:1:2026-10-11:J-14", "consent:1:2026-10-11:J-7"]


def test_renewal_resolves_consent_alerts(bank: Engine) -> None:
    _set_expiry(bank, T0 + timedelta(days=5))
    check_consents(bank, now=T0)
    _set_expiry(bank, T0 + timedelta(days=180))  # consentement renouvelé
    check_consents(bank, now=T0 + timedelta(minutes=1))
    assert [status for _, _, status in _alerts(bank)] == ["resolved"]


def test_revoked_connection_is_ignored(bank: Engine) -> None:
    with bank.begin() as connection:
        connection.execute(update(Connection).values(status=ConnectionStatus.REVOKED))
    _set_expiry(bank, T0 + timedelta(days=3))
    check_consents(bank, now=T0)
    assert _alerts(bank) == []


# --- Fraîcheur des synchros -------------------------------------------------------------------


def test_stale_sync_raises_then_resolves(bank: Engine) -> None:
    check_sync_freshness(bank, now=T0)
    assert _alerts(bank) == []
    check_sync_freshness(bank, now=T0 + timedelta(hours=23))
    assert _alerts(bank) == [("sync_stale:1", "warning", "open")]
    with bank.begin() as connection:
        connection.execute(
            update(AccountSyncState).values(last_success_at=T0 + timedelta(hours=24))
        )
    check_sync_freshness(bank, now=T0 + timedelta(hours=24, minutes=5))
    assert _alerts(bank) == [("sync_stale:1", "warning", "resolved")]


def test_never_synced_linked_account_is_stale(bank: Engine) -> None:
    with bank.begin() as connection:
        connection.execute(update(AccountSyncState).values(last_success_at=None))
    check_sync_freshness(bank, now=T0)
    assert _alerts(bank) == [("sync_stale:1", "warning", "open")]


def test_file_only_accounts_are_not_monitored(engine: Engine) -> None:
    check_sync_freshness(engine, now=T0)  # comptes sans connexion : alimentés par fichier
    assert _alerts(engine) == []


# --- Disque, base, sauvegarde -------------------------------------------------------------------


def test_disk_threshold_with_hysteresis(engine: Engine) -> None:
    check_disk(engine, usage=DiskUsage(total=100, used=86), now=T0)
    check_disk(engine, usage=DiskUsage(total=100, used=82), now=T0)  # entre 80 et 85 : inchangé
    assert _alerts(engine) == [("disk:data", "warning", "open")]
    check_disk(engine, usage=DiskUsage(total=100, used=79), now=T0)
    assert _alerts(engine) == [("disk:data", "warning", "resolved")]


def test_integrity_check_ok_and_corrupted(engine: Engine, tmp_path: Path) -> None:
    check_database_integrity(engine, now=T0)
    assert _alerts(engine) == []
    check_database_integrity(engine, now=T0, result="*** in database main ***")
    assert _alerts(engine) == [("db_integrity", "critical", "open")]


def test_backup_freshness(engine: Engine, tmp_path: Path) -> None:
    marker = tmp_path / ".backup-last-success"
    check_backup_freshness(engine, marker=marker, now=T0)
    assert _alerts(engine) == [("backup_stale", "critical", "open")]  # jamais sauvegardé
    marker.write_text((T0 - timedelta(minutes=20)).isoformat())
    check_backup_freshness(engine, marker=marker, now=T0)
    assert _alerts(engine) == [("backup_stale", "critical", "resolved")]
    check_backup_freshness(engine, marker=marker, now=T0 + timedelta(hours=2))
    assert _alerts(engine)[-1] == ("backup_stale#2", "critical", "open")
