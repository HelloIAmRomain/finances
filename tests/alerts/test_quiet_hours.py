"""Heures silencieuses et regroupement des alertes non urgentes (ALR-03, ALR-04)."""

from datetime import UTC, datetime, time, timedelta

import pytest
from sqlalchemy import Engine, select

from app.alerts.dispatch import dispatch
from app.alerts.engine import raise_alert
from app.alerts.outbox import process_outbox
from app.alerts.preferences import PreferenceError, QuietHours, quiet_hours, set_quiet_hours
from app.models import AuditLog, Notification
from app.models.alerts import Channel, NotificationStatus, Severity
from tests.alerts.test_outbox import BASE_URL, FakeSender

NIGHT = datetime(2026, 10, 7, 21, 30, tzinfo=UTC)  # 23 h 30 à Paris
MORNING = datetime(2026, 10, 8, 6, 5, tzinfo=UTC)  # 8 h 05 à Paris


def _raise(engine: Engine, key: str, severity: Severity, when: datetime = NIGHT) -> None:
    raise_alert(engine, type="t", severity=severity, key=key, title=f"Alerte {key}", now=when)


def test_quiet_hours_contain_overnight_and_daytime_ranges() -> None:
    night = QuietHours()
    assert night.contains(NIGHT)
    assert night.contains(datetime(2026, 10, 8, 5, 59, tzinfo=UTC))  # 7 h 59
    assert not night.contains(MORNING)
    assert not night.contains(datetime(2026, 10, 7, 19, 59, tzinfo=UTC))  # 21 h 59
    nap = QuietHours(start=time(13, 0), end=time(14, 0))
    assert nap.contains(datetime(2026, 10, 7, 11, 30, tzinfo=UTC))  # 13 h 30
    assert not nap.contains(NIGHT)
    assert not QuietHours(enabled=False).contains(NIGHT)
    assert not QuietHours(start=time(9, 0), end=time(9, 0)).contains(NIGHT)
    # Heure d'hiver : 23 h 30 à Paris = 22 h 30 UTC.
    assert night.contains(datetime(2026, 12, 1, 22, 30, tzinfo=UTC))


def test_only_critical_alerts_pass_at_night_others_are_grouped_in_the_morning(
    engine: Engine,
) -> None:
    _raise(engine, "a", Severity.INFO)
    _raise(engine, "b", Severity.WARNING)
    _raise(engine, "c", Severity.CRITICAL)
    ntfy = FakeSender()
    quiet = QuietHours()
    assert (
        process_outbox(
            engine,
            {Channel.NTFY: ntfy},
            base_url=BASE_URL,
            now=NIGHT,
            quiet=quiet,
            min_interval={},
        )
        == 1
    )
    assert [m.title for m in ntfy.sent] == ["Alerte c"]
    assert (
        process_outbox(
            engine,
            {Channel.NTFY: ntfy},
            base_url=BASE_URL,
            now=MORNING,
            quiet=quiet,
            min_interval={},
        )
        == 1
    )
    grouped = ntfy.sent[1]
    assert grouped.title == "2 nouvelles alertes"
    assert grouped.severity is Severity.WARNING
    assert "• Alerte a\n• Alerte b" in grouped.body
    assert grouped.body.endswith(f"{BASE_URL}/alertes")
    with engine.connect() as connection:
        statuses = set(
            connection.scalars(
                select(Notification.status).where(Notification.channel == Channel.NTFY)
            )
        )
    assert statuses == {NotificationStatus.SENT}


def test_grouped_message_failure_retries_every_notification(engine: Engine) -> None:
    _raise(engine, "a", Severity.INFO, MORNING)
    _raise(engine, "b", Severity.INFO, MORNING)
    ntfy = FakeSender(failures=1)
    assert process_outbox(engine, {Channel.NTFY: ntfy}, base_url=BASE_URL, now=MORNING) == 0
    with engine.connect() as connection:
        rows = connection.execute(select(Notification.status, Notification.attempt_count)).all()
    assert [(str(s), n) for s, n in rows] == [("pending", 1), ("pending", 1)]
    later = MORNING + timedelta(minutes=2)
    assert process_outbox(engine, {Channel.NTFY: ntfy}, base_url=BASE_URL, now=later) == 1
    assert ntfy.sent[0].severity is Severity.INFO


def test_settings_are_stored_audited_and_used_by_dispatch(engine: Engine) -> None:
    assert quiet_hours(engine) == QuietHours()
    set_quiet_hours(
        engine, enabled=False, start="23:15", end="06:45", actor_user_id=None, now=NIGHT
    )
    assert quiet_hours(engine) == QuietHours(False, time(23, 15), time(6, 45))
    set_quiet_hours(engine, enabled=True, start="23:15", end="06:45", actor_user_id=None, now=NIGHT)
    with engine.connect() as connection:
        assert connection.scalar(select(AuditLog.details).order_by(AuditLog.id.desc())) == {
            "actif": True, "debut": "23:15", "fin": "06:45",
        }  # fmt: skip
    with pytest.raises(PreferenceError, match="format 22:00"):
        set_quiet_hours(
            engine, enabled=True, start="22h", end="06:45", actor_user_id=None, now=NIGHT
        )
    _raise(engine, "a", Severity.INFO)
    ntfy = FakeSender()
    assert dispatch(engine, {Channel.NTFY: ntfy}, base_url=BASE_URL, now=NIGHT)["sent"] == 0


def test_at_night_with_daily_cadence_only_criticals_leave(engine: Engine) -> None:
    _raise(engine, "a", Severity.WARNING)
    _raise(engine, "c", Severity.CRITICAL)
    ntfy = FakeSender()
    process_outbox(engine, {Channel.NTFY: ntfy}, base_url=BASE_URL, now=NIGHT, quiet=QuietHours())
    assert [m.title for m in ntfy.sent] == ["Alerte c"]  # l'avertissement attend
