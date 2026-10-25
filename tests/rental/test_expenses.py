"""Charges du bien (LOC-10) : règles de libellé rejouables, rattachement manuel, totaux."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import Engine, insert, select

from app.models import LedgerTransaction, PropertyExpense
from app.models.rental import ExpenseKind
from app.rental.engine import run_rental
from app.rental.expenses import (
    ExpenseError,
    add_rule,
    expenses,
    link,
    totals_by_kind,
    unlink,
)
from app.rental.leases import create_property

NOW = datetime(2026, 9, 2, 10, 0, tzinfo=UTC)
_ids = iter(range(1, 100_000))


def _property(engine: Engine) -> int:
    return create_property(
        engine, name="Bien", address=None, furnished=True, shares=[("moi", 10000)], now=NOW
    )


def _tx(engine: Engine, amount: int, label: str, day: date = date(2026, 3, 1)) -> int:
    tx_id = next(_ids)
    with engine.begin() as connection:
        connection.execute(
            insert(LedgerTransaction).values(
                id=tx_id,
                account_id=1,
                dedupe_key=f"ext:t:{tx_id}",
                fingerprint="x",
                occurrence=1,
                booking_date=day,
                amount_cents=amount,
                currency="EUR",
                label_raw=label,
                label_clean=label,
                created_at=NOW,
            )
        )
    return tx_id


def test_rule_links_expenses_once_and_respects_manual_unlink(engine: Engine) -> None:
    bien = _property(engine)
    syndic = _tx(engine, -45000, "PRLV SEPA Syndic Démo")
    _tx(engine, -1200, "CARTE BOULANGERIE")
    _tx(engine, 45000, "VIR SYNDIC DEMO REMBOURSEMENT")  # un crédit n'est pas une charge
    add_rule(
        engine,
        bien,
        kind=ExpenseKind.CO_OWNERSHIP,
        pattern="syndic démo",
        actor_user_id=None,
        now=NOW,
    )
    for _ in range(2):
        run_rental(engine, now=NOW)
    rows = expenses(engine, bien, 2026)
    assert [(r.label, r.amount_cents, r.automatic) for r in rows] == [
        ("PRLV SEPA Syndic Démo", -45000, True)
    ]
    unlink(engine, rows[0].id, actor_user_id=None, now=NOW)
    run_rental(engine, now=NOW)
    assert expenses(engine, bien, 2026) == []  # retiré à la main : pas repris
    link(engine, bien, syndic, kind=ExpenseKind.CO_OWNERSHIP, actor_user_id=None, now=NOW)
    assert len(expenses(engine, bien, 2026)) == 1


def test_manual_link_rules_and_totals(engine: Engine) -> None:
    bien = _property(engine)
    tax = _tx(engine, -98000, "DGFIP TAXE FONCIERE", date(2026, 10, 15))
    works = _tx(engine, -120000, "ARTISAN DEMO")
    credit = _tx(engine, 5000, "REMBOURSEMENT")
    link(engine, bien, tax, kind=ExpenseKind.PROPERTY_TAX, actor_user_id=None, now=NOW)
    link(engine, bien, works, kind=ExpenseKind.WORKS, actor_user_id=None, now=NOW)
    with pytest.raises(ExpenseError, match="dépense"):
        link(engine, bien, credit, kind=ExpenseKind.OTHER, actor_user_id=None, now=NOW)
    with pytest.raises(ExpenseError, match="déjà rattachée"):
        link(engine, bien, tax, kind=ExpenseKind.OTHER, actor_user_id=None, now=NOW)
    with pytest.raises(ExpenseError, match="introuvable"):
        link(engine, 99, works, kind=ExpenseKind.OTHER, actor_user_id=None, now=NOW)
    with pytest.raises(ExpenseError, match="court"):
        add_rule(engine, bien, kind=ExpenseKind.OTHER, pattern="ab", actor_user_id=None, now=NOW)
    add_rule(
        engine, bien, kind=ExpenseKind.INSURANCE, pattern="assureur", actor_user_id=None, now=NOW
    )
    with pytest.raises(ExpenseError, match="existe déjà"):
        add_rule(
            engine,
            bien,
            kind=ExpenseKind.INSURANCE,
            pattern="ASSUREUR",
            actor_user_id=None,
            now=NOW,
        )
    totals = totals_by_kind(expenses(engine, bien, 2026))
    assert totals == {ExpenseKind.PROPERTY_TAX: -98000, ExpenseKind.WORKS: -120000}
    assert expenses(engine, bien, 2025) == []
    with pytest.raises(ExpenseError):
        unlink(engine, 999, actor_user_id=None, now=NOW)
    with engine.connect() as connection:
        assert len(connection.scalars(select(PropertyExpense.id)).all()) == 2
