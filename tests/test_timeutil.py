"""Instants en UTC, dates calendaires sans fuseau, affichage Europe/Paris (inv. 9)."""

from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, insert, select, text

from app.db import make_engine
from app.db_types import UTCDateTime
from app.timeutil import PARIS, ensure_utc, format_date_fr, paris_date, to_paris, utcnow


def test_utcnow_is_aware_utc() -> None:
    now = utcnow()
    assert now.tzinfo is UTC


def test_ensure_utc_converts_aware() -> None:
    paris_summer = datetime(2026, 7, 14, 12, 0, tzinfo=PARIS)
    assert ensure_utc(paris_summer) == datetime(2026, 7, 14, 10, 0, tzinfo=UTC)
    assert ensure_utc(paris_summer).tzinfo is UTC


def test_ensure_utc_rejects_naive() -> None:
    with pytest.raises(ValueError, match="sans fuseau"):
        ensure_utc(datetime(2026, 1, 1))  # noqa: DTZ001


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        # Heure d'hiver (UTC+1) et d'été (UTC+2).
        (datetime(2026, 1, 31, 23, 30, tzinfo=UTC), date(2026, 2, 1)),
        (datetime(2026, 6, 30, 21, 59, tzinfo=UTC), date(2026, 6, 30)),
        (datetime(2026, 6, 30, 22, 0, tzinfo=UTC), date(2026, 7, 1)),
    ],
)
def test_paris_date_crosses_midnight(instant: datetime, expected: date) -> None:
    assert paris_date(instant) == expected


def test_to_paris_handles_dst_change() -> None:
    # Passage à l'heure d'été le 29/03/2026 à 01:00 UTC.
    before = to_paris(datetime(2026, 3, 29, 0, 59, tzinfo=UTC))
    after = to_paris(datetime(2026, 3, 29, 1, 0, tzinfo=UTC))
    assert before.utcoffset() == timedelta(hours=1)
    assert after.utcoffset() == timedelta(hours=2)


def test_format_date_fr() -> None:
    assert format_date_fr(date(2026, 9, 7)) == "07/09/2026"


@pytest.fixture
def table(tmp_path: Path) -> tuple[Table, object]:
    metadata = MetaData()
    events = Table(
        "events",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("at", UTCDateTime, nullable=False),
    )
    engine = make_engine(tmp_path / "t.db")
    metadata.create_all(engine)
    return events, engine


def test_utc_datetime_roundtrip(table: tuple[Table, object]) -> None:
    events, engine = table
    instant = datetime(2026, 7, 14, 12, 30, 15, 123456, tzinfo=timezone(timedelta(hours=2)))
    with engine.begin() as connection:  # type: ignore[attr-defined]
        connection.execute(insert(events).values(id=1, at=instant))
        stored = connection.execute(text("SELECT at FROM events")).scalar_one()
        loaded = connection.execute(select(events.c.at)).scalar_one()
    assert stored == "2026-07-14 10:30:15.123456"  # UTC en base, triable
    assert loaded == instant
    assert loaded.tzinfo is UTC


def test_utc_datetime_rejects_naive(table: tuple[Table, object]) -> None:
    events, engine = table
    with engine.begin() as connection, pytest.raises(Exception, match="sans fuseau"):  # type: ignore[attr-defined]
        connection.execute(insert(events).values(id=1, at=datetime(2026, 1, 1)))  # noqa: DTZ001
