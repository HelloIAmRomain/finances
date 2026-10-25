"""Calendrier des échéances (FIS-04, LOC-09, LOC-13, COL-07) et export ICS (RFC 5545)."""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import Engine, insert, select

from app.models import Alert, Connection
from app.models.alerts import AlertStatus, Severity
from app.models.ledger import ConnectionStatus, Provider
from app.models.rental import LeaseKind
from app.models.tax import DeadlineKind
from app.rental.leases import LeaseTerms, create_lease, create_property
from app.tax.calendar import CalendarEvent, anniversary, calendar, remind_leases
from app.tax.deadlines import DeadlineInput, create, mark_done
from app.tax.ics import to_ics

NOW = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
TODAY = date(2026, 10, 1)


def _lease(engine: Engine, *, start: date, end: date | None = None, name: str = "Studio") -> int:
    property_id = create_property(
        engine, name=name, address=None, furnished=True, shares=[("moi", 10000)], now=NOW
    )
    return create_lease(
        engine,
        LeaseTerms(
            kind=LeaseKind.RENT,
            label="Bail Alice Exemple",  # nom du locataire : jamais dans le calendrier
            account_id=1,
            start_date=start,
            rent_cents=80_000,
            property_id=property_id,
            end_date=end,
        ),
        now=NOW,
    )


def _connection(engine: Engine, expires: datetime, status: ConnectionStatus) -> None:
    with engine.begin() as connection:
        connection.execute(
            insert(Connection).values(
                provider=Provider.ENABLE_BANKING,
                institution_name="Banque Démo",
                country="FR",
                status=status,
                consent_expires_at=expires,
                created_at=NOW,
                updated_at=NOW,
            )
        )


def test_calendar_merges_deadlines_consents_and_leases(engine: Engine) -> None:
    lease_id = _lease(engine, start=date(2025, 11, 1), end=date(2027, 3, 31))
    _connection(engine, datetime(2026, 12, 20, 23, 30, tzinfo=UTC), ConnectionStatus.ACTIVE)
    _connection(engine, datetime(2026, 11, 1, tzinfo=UTC), ConnectionStatus.REVOKED)
    tax = create(
        engine,
        DeadlineInput(
            kind=DeadlineKind.PROPERTY_TAX,
            due_date=date(2026, 10, 15),
            property_id=1,
            amount_cents=100_000,
            label="avis 2026",
        ),
        actor_user_id=None,
        now=NOW,
    )
    events = calendar(engine, TODAY)
    assert [(e.day, e.title, e.href) for e in events] == [
        (date(2026, 10, 15), "Taxe foncière · Studio (avis 2026)", f"/echeances/{tax}"),
        (date(2026, 11, 1), "Révision annuelle du loyer · Studio", f"/loyers/{lease_id}"),
        # Expiration à 23 h 30 UTC : déjà le 21 à Paris.
        (date(2026, 12, 21), "Renouveler l'accès à la banque · Banque Démo", "/connexions"),
        (date(2027, 3, 31), "Fin du bail · Studio", f"/loyers/{lease_id}"),
    ]
    assert all("Alice" not in e.title for e in events)
    assert events[0].days_left(TODAY) == 14
    assert events[0].amount_cents == 100_000

    # Réglée : encore affichée 60 jours (cochée), puis plus du tout.
    mark_done(engine, tax, actor_user_id=None, now=NOW)
    shown = [e for e in calendar(engine, TODAY) if e.deadline_id == tax]
    assert shown[0].done
    assert [e for e in calendar(engine, date(2027, 1, 1)) if e.deadline_id == tax] == []


def test_anniversary_of_a_lease() -> None:
    assert anniversary(date(2025, 11, 1), date(2026, 10, 1)) == date(2026, 11, 1)
    assert anniversary(date(2025, 11, 1), date(2026, 11, 1)) == date(2026, 11, 1)  # le jour même
    assert anniversary(date(2025, 11, 1), date(2026, 11, 2)) == date(2027, 11, 1)
    assert anniversary(date(2026, 9, 15), date(2026, 9, 20)) == date(2027, 9, 15)  # 1re année
    assert anniversary(date(2024, 2, 29), date(2026, 1, 1)) == date(2026, 2, 28)
    assert anniversary(date(2024, 2, 29), date(2027, 3, 1)) == date(2028, 2, 29)


def test_lease_reminders_thirty_days_before_then_resolved(engine: Engine) -> None:
    _lease(engine, start=date(2025, 10, 20))
    assert remind_leases(engine, now=NOW) == 1
    assert remind_leases(engine, now=NOW + timedelta(hours=1)) == 1
    with engine.connect() as connection:
        alert = connection.execute(select(Alert)).one()
    assert (alert.severity, alert.status, alert.title) == (
        Severity.INFO,
        AlertStatus.OPEN,
        "Révision annuelle du loyer · Studio le 20/10/2026",
    )
    remind_leases(engine, now=datetime(2026, 10, 21, 8, 0, tzinfo=UTC))
    with engine.connect() as connection:
        assert connection.execute(select(Alert.status)).scalar_one() is AlertStatus.RESOLVED


def test_lease_reminder_falls_back_when_the_property_name_looks_like_an_amount(
    engine: Engine,
) -> None:
    _lease(engine, start=date(2025, 10, 20), name="Lot 12,50")
    remind_leases(engine, now=NOW)
    with engine.connect() as connection:
        assert "12,50" not in connection.execute(select(Alert.title)).scalar_one()


def test_ics_export_is_valid_and_minimal() -> None:
    long_title = "Taxe foncière · " + "é" * 80
    events = [
        CalendarEvent(
            day=date(2026, 10, 15),
            title=long_title,
            category="fiscal",
            href="/echeances/1",
            uid="echeance-1-2026-10-15",
            remind_days=7,
            amount_cents=123_400,
        ),
        CalendarEvent(
            day=date(2026, 11, 1),
            title="Révision; loyer, Studio",
            category="bail",
            href="/loyers/2",
            uid="bail-2",
            remind_days=0,
        ),
        CalendarEvent(
            day=date(2026, 9, 1),
            title="Déjà réglée",
            category="fiscal",
            href="/echeances/3",
            uid="echeance-3",
            remind_days=7,
            done=True,
        ),
    ]
    text = to_ics(events, base_url="https://pi.example.ts.net/", now=NOW)
    assert text.startswith("BEGIN:VCALENDAR\r\n") and text.endswith("END:VCALENDAR\r\n")
    assert all(len(line.encode()) <= 75 for line in text.split("\r\n"))
    unfolded = text.replace("\r\n ", "")
    assert f"SUMMARY:{long_title}" in unfolded
    assert "DTSTART;VALUE=DATE:20261015\r\nDTEND;VALUE=DATE:20261016" in unfolded
    assert "TRIGGER:-P6DT15H" in unfolded  # 7 jours avant, à 9 h
    assert "URL:https://pi.example.ts.net/echeances/1" in unfolded
    assert "SUMMARY:Révision\\; loyer\\, Studio" in unfolded
    assert unfolded.count("BEGIN:VALARM") == 1
    assert "Déjà réglée" not in unfolded
    assert "1234" not in unfolded  # aucun montant
    assert "DTSTAMP:20261001T080000Z" in unfolded


@pytest.mark.parametrize("separator", ["\r", "\n", "\r\n"])
def test_ics_titles_cannot_inject_properties(separator: str) -> None:
    event = CalendarEvent(
        day=date(2026, 10, 15),
        title=f"Taxe{separator}ATTENDEE:mailto:x@example.org",
        category="fiscal",
        href="/echeances/1",
        uid="echeance-1",
        remind_days=0,
    )
    lines = to_ics([event], base_url="https://pi.example.ts.net/", now=NOW).split("\r\n")
    assert not any(line.startswith("ATTENDEE") for line in lines)
    assert all("\r" not in line and "\n" not in line for line in lines)
