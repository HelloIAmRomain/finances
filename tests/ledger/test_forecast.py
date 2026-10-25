"""Prévision de trésorerie par compte (ANA-10) et alerte « solde bas prévu » (ALR-07)."""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import Engine, insert, select, update

from app.ledger.forecast import (
    ForecastError,
    ForecastEvent,
    check_low_balances,
    forecast,
    project,
    set_threshold,
    total_projection,
)
from app.models import Account, Alert, BalanceSnapshot, LedgerTransaction
from app.models.alerts import AlertStatus, Severity
from app.models.ledger import AccountType, BalanceType
from app.models.rental import LeaseKind
from app.rental.engine import run_rental
from app.rental.leases import LeaseTerms, create_lease

NOW = datetime(2026, 10, 7, 8, 0, tzinfo=UTC)
TODAY = date(2026, 10, 7)
_ids = iter(range(1, 10_000_000))


def _tx(engine: Engine, day: date, amount: int, label: str, account: int = 1) -> None:
    tx_id = next(_ids)
    with engine.begin() as connection:
        connection.execute(
            insert(LedgerTransaction).values(
                id=tx_id, account_id=account, dedupe_key=f"ext:t:{tx_id}", fingerprint="x",
                occurrence=1, booking_date=day, amount_cents=amount, currency="EUR",
                label_raw=label, label_clean=label, created_at=NOW,
            )
        )  # fmt: skip


def _monthly(engine: Engine, label: str, amount: int, last: date, account: int = 1) -> None:
    for k in range(4):
        _tx(engine, last - timedelta(days=30 * k), amount, f"{label} {k:02d}/10", account)


def _balance(engine: Engine, account: int, amount: int) -> None:
    with engine.begin() as connection:
        connection.execute(
            insert(BalanceSnapshot).values(
                account_id=account, as_of_date=TODAY, balance_type=BalanceType.BOOKED,
                amount_cents=amount, currency="EUR", fetched_at=NOW,
            )
        )  # fmt: skip


def test_projection_is_a_running_balance() -> None:
    events = [
        ForecastEvent(date(2026, 10, 10), -50_000, "loyer payé", 1, "récurrent"),
        ForecastEvent(date(2026, 10, 9), -20_000, "assurance", 1, "récurrent"),
        ForecastEvent(date(2026, 10, 28), 250_000, "salaire", 1, "récurrent"),
    ]
    projection = project(60_000, events, TODAY)
    assert (projection.end_cents, projection.low_cents, projection.low_date) == (
        240_000,
        -10_000,
        date(2026, 10, 10),
    )
    assert project(1_000, [], TODAY).low_date == TODAY


def test_forecast_per_account_with_transfers_and_rent(engine: Engine) -> None:
    _balance(engine, 1, 150_000)
    _balance(engine, 2, 20_000)
    _monthly(engine, "VIR SALAIRE", 250_000, date(2026, 9, 28))
    _monthly(engine, "PRLV NETFLIX", -1_399, date(2026, 9, 15))
    # Virement mensuel du compte 1 vers le compte joint (2) : il compte pour chacun.
    _monthly(engine, "VIR VERS JOINT", -60_000, date(2026, 10, 2))
    _monthly(engine, "VIR RECU COURANT", 60_000, date(2026, 10, 2), account=2)
    _monthly(engine, "PRLV EDF", -9_000, date(2026, 9, 20), account=2)
    create_lease(
        engine,
        LeaseTerms(
            kind=LeaseKind.LOAN, label="Prêt", account_id=2, start_date=date(2026, 1, 1),
            rent_cents=30_000, due_day=5,
        ),
        now=NOW,
    )  # fmt: skip
    run_rental(engine, now=NOW)  # échéances du prêt (octobre : pas encore reçue)
    first, joint = forecast(engine, TODAY, days=30)
    assert [(e.day, e.amount_cents, e.label) for e in first.events] == [
        (date(2026, 10, 15), -1_399, "PRLV NETFLIX"),
        (date(2026, 10, 28), 250_000, "VIR SALAIRE"),
        (date(2026, 11, 1), -60_000, "VIR VERS JOINT"),
    ]
    assert first.projection is not None
    assert first.projection.end_cents == 150_000 - 1_399 + 250_000 - 60_000
    assert [(e.day, e.amount_cents, e.source) for e in joint.events] == [
        (date(2026, 10, 7), 30_000, "loyer"),  # échéance d'octobre non reçue
        (date(2026, 10, 20), -9_000, "récurrent"),
        (date(2026, 11, 1), 60_000, "récurrent"),
        (date(2026, 11, 5), 30_000, "loyer"),
    ]
    total = total_projection([first, joint], TODAY)
    assert total is not None and total.start_cents == 170_000


def test_lease_starting_later_is_forecast_from_its_start(engine: Engine) -> None:
    _balance(engine, 1, 10_000)
    create_lease(
        engine,
        LeaseTerms(
            kind=LeaseKind.LOAN, label="Prêt", account_id=1, start_date=date(2027, 1, 1),
            rent_cents=50_000, due_day=5,
        ),
        now=NOW,
    )  # fmt: skip
    run_rental(engine, now=NOW)
    account = next(f for f in forecast(engine, TODAY, days=120) if f.account_id == 1)
    assert [(e.day, e.source) for e in account.events] == [(date(2027, 1, 5), "loyer")]


def test_cards_ignored_and_unknown_balances(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(update(Account).where(Account.id == 2).values(type=AccountType.CARD))
    [only] = forecast(engine, TODAY)
    assert (only.account_id, only.projection) == (1, None)
    assert total_projection([only], TODAY) is None


def test_low_balance_alert_follows_the_projection(engine: Engine) -> None:
    _balance(engine, 1, 30_000)
    _monthly(engine, "PRLV CREDIT AUTO", -25_000, date(2026, 9, 12))
    assert check_low_balances(engine, now=NOW) == 0  # aucun seuil
    set_threshold(engine, 1, 10_000, actor_user_id=None, now=NOW)
    assert check_low_balances(engine, now=NOW) == 1
    assert check_low_balances(engine, now=NOW) == 1
    with engine.connect() as connection:
        alert = connection.execute(select(Alert)).one()
    assert (alert.title, alert.severity, alert.dedupe_key) == (
        "Solde bas prévu",
        Severity.WARNING,
        "low_balance:1:2026-10-12",
    )
    assert alert.details["solde_prevu"].startswith("50,00")
    set_threshold(engine, 1, None, actor_user_id=None, now=NOW)
    check_low_balances(engine, now=NOW)
    with engine.connect() as connection:
        assert connection.scalar(select(Alert.status)) is AlertStatus.RESOLVED
    with pytest.raises(ForecastError, match="introuvable"):
        set_threshold(engine, 99, 1, actor_user_id=None, now=NOW)
