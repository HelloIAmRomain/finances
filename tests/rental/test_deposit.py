"""Dépôt de garantie (LEG-04 ; loi du 6 juillet 1989, art. 22 et 25-6) : plafond, délai de
restitution, retenues motivées, opérations rattachées, rappels."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import Engine, insert, select

from app.models import Alert, AuditLog, LedgerTransaction, Overpayment, RentAllocation
from app.models.alerts import AlertStatus
from app.models.rental import DepositEventKind, LeaseKind, OverpaymentDecision, PartyRole
from app.rental.deposit import (
    DepositError,
    candidates,
    delete_event,
    deposit_state,
    legal_max,
    record,
    remind_deposits,
    restitution_deadline,
    set_exit,
)
from app.rental.engine import run_rental
from app.rental.leases import LeaseTerms, add_party, create_lease, create_property
from app.tax.calendar import calendar

NOW = datetime(2026, 10, 7, 8, 0, tzinfo=UTC)
TODAY = date(2026, 10, 7)
_ids = iter(range(1, 100_000))


def _lease(engine: Engine, *, furnished: bool = True, kind: LeaseKind = LeaseKind.RENT) -> int:
    property_id = create_property(
        engine, name="Studio", address=None, furnished=furnished, shares=[("moi", 10000)], now=NOW
    )
    lease_id = create_lease(
        engine,
        LeaseTerms(
            kind=kind, label="Bail", account_id=1, start_date=date(2026, 9, 1),
            rent_cents=70_000, charges_cents=5_000, deposit_cents=140_000,
            property_id=property_id if kind is LeaseKind.RENT else None,
        ),
        now=NOW,
    )  # fmt: skip
    add_party(
        engine,
        lease_id,
        role=PartyRole.TENANT if kind is LeaseKind.RENT else PartyRole.BORROWER,
        name="C. Locataire",
        patterns=["LOCATAIRE"],
    )
    return lease_id


def _tx(engine: Engine, amount: int, day: date, label: str = "VIR C LOCATAIRE") -> int:
    tx_id = next(_ids)
    with engine.begin() as connection:
        connection.execute(
            insert(LedgerTransaction).values(
                id=tx_id, account_id=1, dedupe_key=f"ext:d:{tx_id}", fingerprint="x",
                occurrence=1, booking_date=day, amount_cents=amount, currency="EUR",
                label_raw=label, label_clean=label, created_at=NOW,
            )
        )  # fmt: skip
    return tx_id


def _record(engine: Engine, lease_id: int, kind: DepositEventKind, **values: object) -> int:
    return record(engine, lease_id, kind, actor_user_id=None, now=NOW, **values)  # type: ignore[arg-type]


# --- Règles légales ------------------------------------------------------------------------


def test_legal_maximum_is_two_months_furnished_one_month_unfurnished() -> None:
    assert legal_max(70_000, furnished=True) == 140_000
    assert legal_max(70_000, furnished=False) == 70_000


def test_deadline_one_month_if_inspection_conform_otherwise_two() -> None:
    keys = date(2026, 10, 15)
    assert restitution_deadline(keys, inspection_ok=True) == date(2026, 11, 15)
    assert restitution_deadline(keys, inspection_ok=False) == date(2026, 12, 15)
    assert restitution_deadline(keys, inspection_ok=None) == date(2026, 11, 15)  # prudent


# --- Parcours complet ------------------------------------------------------------------------


def test_receive_withhold_return_then_settled(engine: Engine) -> None:
    lease_id = _lease(engine)
    state = deposit_state(engine, lease_id)
    assert state.status(TODAY) == "attendu" and state.expected_cents == 140_000
    assert state.legal_max_cents == 140_000 and not state.above_legal_max

    received = _tx(engine, 140_000, date(2026, 9, 1), "VIR C LOCATAIRE DEPOT")
    _record(engine, lease_id, DepositEventKind.RECEIVED, transaction_id=received)
    state = deposit_state(engine, lease_id)
    assert (state.received_cents, state.status(TODAY)) == (140_000, "encaisse")
    assert state.events[0].on_date == date(2026, 9, 1)  # date de l'opération par défaut

    set_exit(
        engine, lease_id, keys_returned_on=date(2026, 10, 1), inspection_ok=False,
        actor_user_id=None, now=NOW,
    )  # fmt: skip
    _record(
        engine, lease_id, DepositEventKind.WITHHELD, amount_cents=20_000,
        on_date=date(2026, 10, 5), note="Remplacement du canapé taché (devis joint)",
    )  # fmt: skip
    state = deposit_state(engine, lease_id)
    assert state.deadline == date(2026, 12, 1)
    assert state.to_return_cents == 120_000 and state.status(TODAY) == "a_restituer"

    refund = _tx(engine, -120_000, date(2026, 11, 20), "VIR RESTITUTION DEPOT")
    _record(engine, lease_id, DepositEventKind.RETURNED, transaction_id=refund)
    state = deposit_state(engine, lease_id)
    assert state.to_return_cents == 0 and state.status(TODAY) == "solde"
    with engine.connect() as connection:
        actions = set(connection.scalars(select(AuditLog.action)))
    assert {"deposit.received", "deposit.withheld", "deposit.returned", "lease.exit"} <= actions


def test_late_restitution(engine: Engine) -> None:
    lease_id = _lease(engine)
    _record(engine, lease_id, DepositEventKind.RECEIVED, amount_cents=140_000,
            on_date=date(2026, 9, 1))  # fmt: skip
    set_exit(engine, lease_id, keys_returned_on=date(2026, 8, 1), inspection_ok=True,
             actor_user_id=None, now=NOW)  # fmt: skip
    state = deposit_state(engine, lease_id)
    assert state.deadline == date(2026, 9, 1)
    assert state.status(TODAY) == "en_retard"


def test_rules_and_errors(engine: Engine) -> None:
    lease_id = _lease(engine, furnished=False)
    assert deposit_state(engine, lease_id).above_legal_max  # 1 400 € > 1 mois en location nue
    with pytest.raises(DepositError, match="montant"):
        _record(engine, lease_id, DepositEventKind.RECEIVED)
    with pytest.raises(DepositError, match="encaissé"):
        _record(engine, lease_id, DepositEventKind.WITHHELD, amount_cents=1, note="x")
    _record(engine, lease_id, DepositEventKind.RECEIVED, amount_cents=70_000,
            on_date=date(2026, 9, 1))  # fmt: skip
    with pytest.raises(DepositError, match="motif"):
        _record(engine, lease_id, DepositEventKind.WITHHELD, amount_cents=1_000, note=" ")
    with pytest.raises(DepositError, match="dépasse"):
        _record(engine, lease_id, DepositEventKind.RETURNED, amount_cents=70_001)
    debit = _tx(engine, -5_000, date(2026, 9, 3))
    with pytest.raises(DepositError, match="entrée d'argent"):
        _record(engine, lease_id, DepositEventKind.RECEIVED, transaction_id=debit)
    credit = _tx(engine, 5_000, date(2026, 9, 3))
    with pytest.raises(DepositError, match="sortie d'argent"):
        _record(engine, lease_id, DepositEventKind.RETURNED, transaction_id=credit)
    with pytest.raises(DepositError, match="introuvable"):
        _record(engine, lease_id, DepositEventKind.RECEIVED, transaction_id=999_999)
    with pytest.raises(DepositError, match="retenue"):
        _record(engine, lease_id, DepositEventKind.WITHHELD, transaction_id=credit, note="x")
    _record(engine, lease_id, DepositEventKind.RECEIVED, transaction_id=credit)
    with pytest.raises(DepositError, match="déjà rattachée"):
        _record(engine, lease_id, DepositEventKind.RECEIVED, transaction_id=credit)
    loan = _lease(engine, kind=LeaseKind.LOAN)
    with pytest.raises(DepositError, match="bail de location"):
        _record(engine, loan, DepositEventKind.RECEIVED, amount_cents=1, on_date=TODAY)
    with pytest.raises(DepositError, match="bail de location"):
        set_exit(engine, loan, keys_returned_on=TODAY, inspection_ok=None, actor_user_id=None,
                 now=NOW)  # fmt: skip
    with pytest.raises(DepositError, match="introuvable"):
        delete_event(engine, lease_id, 999_999, actor_user_id=None, now=NOW)


def test_rent_allocated_operation_is_refused_and_deposit_is_not_matched_as_rent(
    engine: Engine,
) -> None:
    lease_id = _lease(engine)
    rent = _tx(engine, 75_000, date(2026, 9, 2))
    deposit = _tx(engine, 140_000, date(2026, 9, 1), "VIR C LOCATAIRE DEPOT")
    _record(engine, lease_id, DepositEventKind.RECEIVED, transaction_id=deposit)
    run_rental(engine, now=NOW)
    with engine.connect() as connection:
        allocated = set(connection.scalars(select(RentAllocation.transaction_id)))
    assert rent in allocated and deposit not in allocated  # le dépôt n'est pas un loyer
    with pytest.raises(DepositError, match="affectée au loyer"):
        _record(engine, lease_id, DepositEventKind.RECEIVED, transaction_id=rent)
    listed = [c.id for c in candidates(engine, lease_id, credit=True, today=TODAY)]
    assert rent not in listed and deposit not in listed  # déjà utilisées
    spare = _tx(engine, 1_000, date(2026, 9, 10))
    assert spare in [c.id for c in candidates(engine, lease_id, credit=True, today=TODAY)]
    assert spare not in [c.id for c in candidates(engine, lease_id, credit=False, today=TODAY)]


def test_delete_event_frees_the_operation(engine: Engine) -> None:
    lease_id = _lease(engine)
    deposit = _tx(engine, 140_000, date(2026, 9, 1))
    event_id = _record(engine, lease_id, DepositEventKind.RECEIVED, transaction_id=deposit)
    delete_event(engine, lease_id, event_id, actor_user_id=None, now=NOW)
    assert deposit_state(engine, lease_id).received_cents == 0
    assert deposit in [c.id for c in candidates(engine, lease_id, credit=True, today=TODAY)]


# --- Rappels -------------------------------------------------------------------------------


def _open_alerts(engine: Engine) -> list[tuple[str, str]]:
    with engine.connect() as connection:
        return [
            (row.type, row.title)
            for row in connection.execute(
                select(Alert.type, Alert.title).where(Alert.status != AlertStatus.RESOLVED)
            )
        ]


def test_reminder_a_week_before_then_late_alert_then_resolved(engine: Engine) -> None:
    lease_id = _lease(engine)
    _record(engine, lease_id, DepositEventKind.RECEIVED, amount_cents=140_000,
            on_date=date(2026, 9, 1))  # fmt: skip
    set_exit(engine, lease_id, keys_returned_on=date(2026, 9, 12), inspection_ok=True,
             actor_user_id=None, now=NOW)  # fmt: skip
    assert remind_deposits(engine, now=datetime(2026, 10, 4, 8, tzinfo=UTC)) == 0  # J-8
    assert remind_deposits(engine, now=datetime(2026, 10, 5, 8, tzinfo=UTC)) == 1  # J-7
    assert _open_alerts(engine) == [
        ("deposit", "Dépôt de garantie à restituer avant le 12/10/2026 · Studio")
    ]
    assert remind_deposits(engine, now=datetime(2026, 10, 6, 8, tzinfo=UTC)) == 1  # rejouable
    assert len(_open_alerts(engine)) == 1
    remind_deposits(engine, now=datetime(2026, 10, 13, 8, tzinfo=UTC))
    assert ("deposit_late", "Dépôt de garantie : restitution en retard · Studio") in (
        _open_alerts(engine)
    )
    _record(engine, lease_id, DepositEventKind.RETURNED, amount_cents=140_000,
            on_date=date(2026, 10, 13))  # fmt: skip
    remind_deposits(engine, now=datetime(2026, 10, 14, 8, tzinfo=UTC))
    assert _open_alerts(engine) == []
    for title in (t for _, t in _open_alerts(engine)):
        assert "€" not in title and "Locataire" not in title


def test_restitution_deadline_appears_in_the_calendar_until_settled(engine: Engine) -> None:
    lease_id = _lease(engine)
    _record(engine, lease_id, DepositEventKind.RECEIVED, amount_cents=140_000,
            on_date=date(2026, 9, 1))  # fmt: skip
    assert not [e for e in calendar(engine, TODAY) if "dépôt" in e.title]
    set_exit(engine, lease_id, keys_returned_on=date(2026, 10, 15), inspection_ok=False,
             actor_user_id=None, now=NOW)  # fmt: skip
    [event] = [e for e in calendar(engine, TODAY) if "dépôt" in e.title]
    assert (event.day, event.title) == (
        date(2026, 12, 15),
        "Restituer le dépôt de garantie · Studio",
    )
    assert event.href == f"/loyers/{lease_id}#depot" and event.remind_days == 7
    _record(engine, lease_id, DepositEventKind.RETURNED, amount_cents=140_000,
            on_date=date(2026, 12, 1))  # fmt: skip
    assert not [e for e in calendar(engine, TODAY) if "dépôt" in e.title]


def test_remaining_refusals(engine: Engine) -> None:
    lease_id = _lease(engine)
    with pytest.raises(DepositError, match="Bail introuvable"):
        deposit_state(engine, 999_999)
    _record(engine, lease_id, DepositEventKind.RECEIVED, amount_cents=140_000,
            on_date=date(2026, 9, 1))  # fmt: skip
    with pytest.raises(DepositError, match="trop long"):
        _record(engine, lease_id, DepositEventKind.WITHHELD, amount_cents=1, note="x" * 201)
    surplus = _tx(engine, 2_000, date(2026, 9, 4))
    with engine.begin() as connection:
        connection.execute(
            insert(Overpayment).values(
                lease_id=lease_id, transaction_id=surplus, amount_cents=2_000,
                decision=OverpaymentDecision.PENDING, created_at=NOW,
            )
        )  # fmt: skip
    with pytest.raises(DepositError, match="trop-perçu"):
        _record(engine, lease_id, DepositEventKind.RECEIVED, transaction_id=surplus)
