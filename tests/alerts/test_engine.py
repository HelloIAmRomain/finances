"""Moteur d'alertes : dédoublonnage, épisodes, cycle de vie, rappels (ALR-01, ALR-03, ALR-06)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, func, select

from app.alerts.engine import (
    AlertTransitionError,
    mark_seen,
    open_keys,
    raise_alert,
    resolve,
    resolve_by_key,
    send_due_reminders,
)
from app.models import Alert, Notification
from app.models.alerts import AlertStatus, Channel, NotificationKind, Severity

T0 = datetime(2026, 9, 27, 8, 0, tzinfo=UTC)


def _raise(
    engine: Engine, key: str = "sync:1", severity: Severity = Severity.WARNING, when: datetime = T0
) -> int:
    return raise_alert(
        engine,
        type="sync_failed",
        severity=severity,
        key=key,
        title="Synchro bancaire en échec",
        subject=("account", 1),
        now=when,
    ).alert_id


def _alerts(engine: Engine) -> list[tuple[str, str]]:
    with engine.connect() as connection:
        rows = connection.execute(select(Alert.dedupe_key, Alert.status).order_by(Alert.id)).all()
    return [(key, str(status)) for key, status in rows]


def _notifications(engine: Engine) -> list[tuple[str, str, int]]:
    with engine.connect() as connection:
        rows = connection.execute(
            select(Notification.channel, Notification.kind, Notification.sequence).order_by(
                Notification.id
            )
        ).all()
    return [(str(channel), str(kind), sequence) for channel, kind, sequence in rows]


def test_same_fact_raised_many_times_gives_one_alert(engine: Engine) -> None:
    first = raise_alert(engine, type="t", severity=Severity.WARNING, key="k", title="x", now=T0)
    again = raise_alert(engine, type="t", severity=Severity.WARNING, key="k", title="x", now=T0)
    assert first.created and not again.created
    assert first.alert_id == again.alert_id
    assert _alerts(engine) == [("k", "open")]


def test_notifications_follow_severity(engine: Engine) -> None:
    _raise(engine, key="info", severity=Severity.INFO)
    _raise(engine, key="warn", severity=Severity.WARNING)
    _raise(engine, key="crit", severity=Severity.CRITICAL)
    assert _notifications(engine) == [
        ("ntfy", "initial", 1),
        ("ntfy", "initial", 1),
        ("ntfy", "initial", 1),
        ("email", "initial", 1),  # le critique part aussi par e-mail (ALR-02)
    ]


def test_new_episode_after_resolution(engine: Engine) -> None:
    alert_id = _raise(engine)
    resolve(engine, alert_id, now=T0 + timedelta(hours=1))
    _raise(engine, when=T0 + timedelta(days=1))
    _raise(engine, when=T0 + timedelta(days=1, hours=1))  # toujours le même épisode
    assert _alerts(engine) == [("sync:1", "resolved"), ("sync:1#2", "open")]


def test_one_off_fact_is_never_raised_again_once_resolved(engine: Engine) -> None:
    first = raise_alert(
        engine, type="t", severity=Severity.INFO, key="anomaly:1", title="x", now=T0, once=True
    )
    resolve(engine, first.alert_id, now=T0)
    again = raise_alert(
        engine, type="t", severity=Severity.INFO, key="anomaly:1", title="x", now=T0, once=True
    )
    assert not again.created and again.alert_id == first.alert_id
    assert _alerts(engine) == [("anomaly:1", "resolved")]


def test_lifecycle(engine: Engine) -> None:
    alert_id = _raise(engine)
    mark_seen(engine, alert_id, now=T0 + timedelta(minutes=5))
    resolve(engine, alert_id, now=T0 + timedelta(minutes=10))
    with engine.connect() as connection:
        alert = connection.execute(select(Alert).where(Alert.id == alert_id)).one()
    assert alert.status is AlertStatus.RESOLVED
    assert alert.seen_at == T0 + timedelta(minutes=5)
    assert alert.resolved_at == T0 + timedelta(minutes=10)
    with pytest.raises(AlertTransitionError):
        mark_seen(engine, alert_id, now=T0 + timedelta(minutes=20))


def test_resolve_by_key_closes_current_episode_only(engine: Engine) -> None:
    _raise(engine)
    assert resolve_by_key(engine, "sync:1", now=T0 + timedelta(hours=1)) is True
    assert resolve_by_key(engine, "sync:1", now=T0 + timedelta(hours=2)) is False  # déjà résolue
    assert _alerts(engine) == [("sync:1", "resolved")]


def test_single_reminder_after_48_hours(engine: Engine) -> None:
    _raise(engine)
    assert send_due_reminders(engine, now=T0 + timedelta(hours=47)) == 0
    assert send_due_reminders(engine, now=T0 + timedelta(hours=48)) == 1
    assert send_due_reminders(engine, now=T0 + timedelta(hours=100)) == 0  # rappel unique
    assert _notifications(engine)[-1] == ("ntfy", "reminder", 1)


def test_seen_or_resolved_alerts_are_not_reminded(engine: Engine) -> None:
    seen = _raise(engine, key="a")
    done = _raise(engine, key="b")
    mark_seen(engine, seen, now=T0 + timedelta(hours=1))
    resolve(engine, done, now=T0 + timedelta(hours=1))
    assert send_due_reminders(engine, now=T0 + timedelta(days=3)) == 0


def test_title_must_not_contain_amounts() -> None:
    with pytest.raises(ValueError, match="montant"):
        raise_alert(
            None,  # type: ignore[arg-type]
            type="rent",
            severity=Severity.WARNING,
            key="k",
            title="Loyer : 850,00 € manquants",
            now=T0,
        )


def test_idempotent_counts(engine: Engine) -> None:
    for _ in range(5):
        _raise(engine, severity=Severity.CRITICAL)
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(Notification)).scalar_one() == 2
    assert Channel.EMAIL.value == "email" and NotificationKind.REMINDER.value == "reminder"


def test_open_keys_ignore_resolved_and_episode_numbers(engine: Engine) -> None:
    for key in ("rent-match:1", "rent-match:2", "overpayment:1", "rent_match:3"):
        _raise(engine, key)
    resolve_by_key(engine, "rent-match:2", now=T0)
    _raise(engine, "rent-match:2", when=T0 + timedelta(hours=1))  # nouvel épisode « #2 »
    resolve_by_key(engine, "rent-match:1", now=T0)
    assert open_keys(engine, "rent-match:") == {"rent-match:2"}  # « _ » n'est pas un joker
    assert open_keys(engine, "overpayment:") == {"overpayment:1"}
