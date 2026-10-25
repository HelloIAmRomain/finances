"""Récapitulatif annuel d'un bien (FIS-01) et répartition entre indivisaires (LOC-12)."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import Engine, delete, insert, select

from app.models import DuplicateLink, LedgerTransaction, OwnerShare
from app.models.ledger import DuplicateReason, DuplicateStatus, LinkMode
from app.models.rental import ExpenseKind, LeaseKind, PartyRole
from app.models.tax import DeadlineKind
from app.rental.engine import run_rental
from app.rental.expenses import EXPENSE_LABELS, link
from app.rental.leases import LeaseTerms, add_party, create_lease, create_property
from app.spreadsheet import cell
from app.tax.deadlines import DeadlineInput, create
from app.tax.summary import SummaryError, property_year, summary_csv_rows

NOW = datetime(2026, 12, 31, 12, 0, tzinfo=UTC)
_ids = iter(range(1, 1_000_000))


def _tx(engine: Engine, day: date, amount: int, label: str) -> int:
    tx_id = next(_ids)
    with engine.begin() as connection:
        connection.execute(
            insert(LedgerTransaction).values(
                id=tx_id, account_id=1, dedupe_key=f"ext:t:{tx_id}", fingerprint="x",
                occurrence=1, booking_date=day, amount_cents=amount, currency="EUR",
                label_raw=label, label_clean=label, created_at=NOW,
            )
        )  # fmt: skip
    return tx_id


def _setup(engine: Engine, shares: list[tuple[str, int]]) -> int:
    property_id = create_property(
        engine, name="Studio", address=None, furnished=True, shares=shares, now=NOW
    )
    lease = create_lease(
        engine,
        LeaseTerms(
            kind=LeaseKind.RENT, label="Bail", account_id=1, start_date=date(2026, 10, 1),
            rent_cents=70_001, charges_cents=5_000, property_id=property_id,
        ),
        now=NOW,
    )  # fmt: skip
    add_party(
        engine, lease, role=PartyRole.TENANT, name="Alice Exemple", iban=None,
        patterns=["LOYER STUDIO"],
    )  # fmt: skip
    for month in (10, 11, 12):
        _tx(engine, date(2026, month, 2), 75_001, "VIR LOYER STUDIO")
    _tx(engine, date(2027, 1, 2), 75_001, "VIR LOYER STUDIO")  # année suivante
    tax = _tx(engine, date(2026, 10, 14), -100_001, "DGFIP TAXE FONCIERE")
    link(engine, property_id, tax, kind=ExpenseKind.PROPERTY_TAX, actor_user_id=None, now=NOW)
    syndic = _tx(engine, date(2026, 11, 3), -30_000, "SYNDIC")
    link(engine, property_id, syndic, kind=ExpenseKind.CO_OWNERSHIP, actor_user_id=None, now=NOW)
    create(
        engine,
        DeadlineInput(
            kind=DeadlineKind.PROPERTY_TAX, due_date=date(2026, 10, 15), property_id=property_id,
            amount_cents=100_001, recoverable_cents=15_000,
        ),
        actor_user_id=None,
        now=NOW,
    )  # fmt: skip
    run_rental(engine, now=NOW)
    return property_id


def test_year_summary_split_between_co_owners(engine: Engine) -> None:
    property_id = _setup(engine, [("moi", 5000), ("co-indivisaire", 5000)])
    summary = property_year(engine, property_id, 2026)
    assert (summary.due_cents, summary.received_cents) == (3 * 75_001, 3 * 75_001)
    assert summary.charges_by_kind == {
        ExpenseKind.PROPERTY_TAX: -100_001,
        ExpenseKind.CO_OWNERSHIP: -30_000,
    }
    assert summary.result_cents == 225_003 - 130_001
    assert summary.recoverable_cents == 15_000
    [co, me] = summary.owners
    assert (co.label, me.label) == ("co-indivisaire", "moi")
    # Sommes exactes au centime : 225 003 = 112 502 + 112 501.
    assert co.received_cents + me.received_cents == summary.received_cents
    assert co.charges_cents + me.charges_cents == summary.charges_cents
    assert {co.received_cents, me.received_cents} == {112_502, 112_501}
    assert co.result_cents + me.result_cents == summary.result_cents

    rows = [[cell(value) for value in row] for row in summary_csv_rows(summary, EXPENSE_LABELS)]
    assert rows[0] == ["Poste", "Total", "co-indivisaire (50 %)", "moi (50 %)"]
    assert ["Loyers encaissés", "2250,03", "1125,02", "1125,01"] in rows
    assert ["Charges : taxe foncière", "-1000,01", "-500,01", "-500,00"] in rows
    assert rows[-1][0] == "Mention" and "indicatif" in rows[-1][1]


def test_unassigned_share_and_empty_year(engine: Engine) -> None:
    property_id = _setup(engine, [("moi", 6000), ("autre", 4000)])
    with engine.begin() as connection:  # quote-part retirée hors de l'application
        connection.execute(delete(OwnerShare).where(OwnerShare.owner_label == "autre"))
    summary = property_year(engine, property_id, 2025)
    assert [(o.label, o.share_bp) for o in summary.owners] == [
        ("moi", 6000),
        ("non attribuée", 4000),
    ]
    assert summary.received_cents == summary.charges_cents == summary.recoverable_cents == 0
    with pytest.raises(SummaryError):
        property_year(engine, 99, 2026)


def test_rent_paid_by_a_payment_later_found_duplicate_still_counts(engine: Engine) -> None:
    """L-12 : le virement affecté reste affecté même reconnu plus tard comme doublon d'une
    ligne de relevé ; le récapitulatif fiscal le compte (il le perdait avec `is_effective`)."""
    property_id = _setup(engine, [("moi", 10_000)])
    with engine.connect() as connection:
        paid = connection.scalar(
            select(LedgerTransaction.id).where(LedgerTransaction.booking_date == date(2026, 10, 2))
        )
    statement_line = _tx(engine, date(2026, 10, 2), 75_001, "VIR LOYER STUDIO (RELEVE)")
    with engine.begin() as connection:
        connection.execute(
            insert(DuplicateLink).values(
                canonical_tx_id=statement_line, duplicate_tx_id=paid, mode=LinkMode.AUTO,
                reason=DuplicateReason.EXACT, status=DuplicateStatus.CONFIRMED, created_at=NOW,
            )
        )  # fmt: skip
    for _ in range(2):
        run_rental(engine, now=NOW)
    assert property_year(engine, property_id, 2026).received_cents == 3 * 75_001
