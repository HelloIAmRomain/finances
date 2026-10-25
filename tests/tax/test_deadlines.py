"""Échéances (FIS-04, LOC-10) : rappels, rapprochement du paiement, échéance suivante, modèles."""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import Engine, insert, select, update

from app.models import Account, Alert, AuditLog, Deadline, LedgerTransaction, PropertyExpense
from app.models.alerts import AlertStatus, Severity
from app.models.rental import ExpenseKind
from app.models.tax import DeadlineKind
from app.rental.expenses import unlink
from app.rental.leases import create_property
from app.tax.deadlines import (
    DeadlineError,
    DeadlineInput,
    apply_templates,
    candidates,
    create,
    delete_deadline,
    mark_done,
    match_payments,
    remind,
    reopen,
    same_day_next_year,
    update_deadline,
)

NOW = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)  # 10 h à Paris
TAX_DAY = date(2026, 10, 15)
_ids = iter(range(1, 100_000))


def _property(engine: Engine, *, furnished: bool = True, name: str = "Studio") -> int:
    return create_property(
        engine, name=name, address=None, furnished=furnished, shares=[("moi", 10000)], now=NOW
    )


def _tx(engine: Engine, amount: int, day: date, *, account: int = 1, label: str = "DGFIP") -> int:
    tx_id = next(_ids)
    with engine.begin() as connection:
        connection.execute(
            insert(LedgerTransaction).values(
                id=tx_id,
                account_id=account,
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


def _tax(engine: Engine, property_id: int | None, **overrides: object) -> int:
    data = DeadlineInput(
        kind=DeadlineKind.PROPERTY_TAX,
        due_date=TAX_DAY,
        property_id=property_id,
        amount_cents=123_400,
        recoverable_cents=15_000,
    )
    values = {**data.__dict__, **overrides}
    return create(engine, DeadlineInput(**values), actor_user_id=None, now=NOW)


def _alerts(engine: Engine) -> list[tuple[str, Severity, AlertStatus, str]]:
    with engine.connect() as connection:
        return [
            (row.dedupe_key, row.severity, row.status, row.title)
            for row in connection.execute(select(Alert).order_by(Alert.id))
        ]


def _deadlines(engine: Engine) -> list[tuple[DeadlineKind, date, int | None, bool]]:
    with engine.connect() as connection:
        return [
            (row.kind, row.due_date, row.transaction_id, row.done_at is not None)
            for row in connection.execute(select(Deadline).order_by(Deadline.due_date))
        ]


def test_reminder_before_then_overdue_then_resolved_when_paid(engine: Engine) -> None:
    property_id = _property(engine)
    deadline_id = _tax(engine, property_id)
    assert remind(engine, now=NOW) == 0  # J-14 : trop tôt (rappel à J-7)

    j7 = NOW + timedelta(days=7)
    assert remind(engine, now=j7) == 1
    assert remind(engine, now=j7 + timedelta(hours=1)) == 1  # rejoué : aucune alerte de plus
    key = f"deadline:{deadline_id}:2026-10-15"
    assert _alerts(engine) == [
        (key, Severity.WARNING, AlertStatus.OPEN, "Taxe foncière (Studio) : échéance le 15/10/2026")
    ]

    after = datetime(2026, 10, 16, 8, 0, tzinfo=UTC)
    remind(engine, now=after)
    alerts = _alerts(engine)
    assert alerts[0][2] is AlertStatus.RESOLVED
    assert alerts[1][:3] == (f"{key}:retard", Severity.CRITICAL, AlertStatus.OPEN)
    assert "dépassée" in alerts[1][3]

    mark_done(engine, deadline_id, actor_user_id=None, now=after)
    assert all(status is AlertStatus.RESOLVED for _, _, status, _ in _alerts(engine))
    assert remind(engine, now=after) == 0  # réglée : plus de rappel


def test_overdue_non_tax_deadline_is_only_a_warning(engine: Engine) -> None:
    create(
        engine,
        DeadlineInput(kind=DeadlineKind.INSURANCE, due_date=date(2026, 9, 30), remind_days=0),
        actor_user_id=None,
        now=NOW,
    )
    remind(engine, now=NOW)
    assert [(severity, title) for _, severity, _, title in _alerts(engine)] == [
        (Severity.WARNING, "Assurance : échéance du 30/09/2026 dépassée")
    ]


def test_property_name_looking_like_an_amount_is_left_out_of_the_title(engine: Engine) -> None:
    _tax(engine, _property(engine, name="Lot 12,50"))
    remind(engine, now=NOW + timedelta(days=7))
    assert _alerts(engine)[0][3] == "Taxe foncière : échéance le 15/10/2026"


def test_single_exact_payment_settles_links_charge_and_creates_next_year(engine: Engine) -> None:
    property_id = _property(engine)
    deadline_id = _tax(engine, property_id)
    _tx(engine, -123_400, date(2026, 6, 1))  # hors fenêtre (plus de 60 jours avant)
    _tx(engine, -9_900, date(2026, 10, 14))  # autre montant
    paid = _tx(engine, -123_400, date(2026, 10, 14))
    assert match_payments(engine, now=NOW) == 1
    assert match_payments(engine, now=NOW) == 0  # rejouable
    assert _deadlines(engine) == [
        (DeadlineKind.PROPERTY_TAX, TAX_DAY, paid, True),
        (DeadlineKind.PROPERTY_TAX, date(2027, 10, 15), None, False),
    ]
    with engine.connect() as connection:
        charge = connection.execute(select(PropertyExpense)).one()
        nxt = connection.execute(select(Deadline).where(Deadline.id != deadline_id)).one()
    assert (charge.property_id, charge.transaction_id, charge.kind) == (
        property_id,
        paid,
        ExpenseKind.PROPERTY_TAX,
    )
    # L'année suivante : même bien, montant à saisir à réception de l'avis.
    assert (nxt.property_id, nxt.amount_cents, nxt.recoverable_cents) == (property_id, None, None)


def test_ambiguous_or_ignored_payments_are_not_settled(engine: Engine) -> None:
    deadline_id = _tax(engine, None)
    _tx(engine, -123_400, date(2026, 10, 10))
    _tx(engine, -123_400, date(2026, 10, 12))
    assert match_payments(engine, now=NOW) == 0  # deux candidats : décision manuelle
    assert [c.exact for c in candidates(engine, deadline_id)] == [True, True]

    other = _tax(engine, None, kind=DeadlineKind.CFE, due_date=date(2026, 12, 15))
    with engine.begin() as connection:
        connection.execute(update(Account).where(Account.id == 2).values(ignored_at=NOW))
    _tx(engine, -123_400, date(2026, 12, 1), account=2)  # compte ignoré (Q19)
    assert match_payments(engine, now=NOW) == 0
    assert candidates(engine, other) == []


def test_reopen_refuses_the_payment_for_good(engine: Engine) -> None:
    deadline_id = _tax(engine, None, yearly=False)
    paid = _tx(engine, -123_400, date(2026, 10, 14))
    match_payments(engine, now=NOW)
    reopen(engine, deadline_id, actor_user_id=None, now=NOW)
    assert match_payments(engine, now=NOW) == 0
    assert paid not in [c.id for c in candidates(engine, deadline_id)]
    assert _deadlines(engine) == [(DeadlineKind.PROPERTY_TAX, TAX_DAY, None, False)]
    with pytest.raises(DeadlineError, match="non réglée"):
        reopen(engine, deadline_id, actor_user_id=None, now=NOW)


def test_manual_settlement_checks_the_operation(engine: Engine) -> None:
    property_id = _property(engine)
    first = _tax(engine, property_id, yearly=False)
    second = _tax(engine, property_id, due_date=date(2026, 11, 15), yearly=False)
    credit = _tx(engine, 123_400, date(2026, 10, 14))
    paid = _tx(engine, -120_000, date(2026, 10, 14))
    with pytest.raises(DeadlineError, match="dépense"):
        mark_done(engine, first, transaction_id=credit, actor_user_id=None, now=NOW)
    assert mark_done(engine, first, transaction_id=paid, actor_user_id=None, now=NOW) is None
    with pytest.raises(DeadlineError, match="déjà réglée"):
        mark_done(engine, first, actor_user_id=None, now=NOW)
    with pytest.raises(DeadlineError, match="autre échéance"):
        mark_done(engine, second, transaction_id=paid, actor_user_id=None, now=NOW)
    mark_done(engine, second, actor_user_id=None, now=NOW)  # réglée sans opération
    with engine.connect() as connection:
        actions = list(connection.scalars(select(AuditLog.action).order_by(AuditLog.id)))
    assert actions.count("deadline.done") == 2


def test_charge_removed_by_hand_is_not_relinked(engine: Engine) -> None:
    property_id = _property(engine)
    paid = _tx(engine, -123_400, date(2026, 10, 14))
    first = _tax(engine, property_id, yearly=False)
    match_payments(engine, now=NOW)
    with engine.connect() as connection:
        expense_id = connection.execute(select(PropertyExpense.id)).scalar_one()
    unlink(engine, expense_id, actor_user_id=None, now=NOW)
    reopen(engine, first, actor_user_id=None, now=NOW)
    delete_deadline(engine, first, actor_user_id=None, now=NOW)
    again = _tax(engine, property_id, yearly=False)
    mark_done(engine, again, transaction_id=paid, actor_user_id=None, now=NOW)
    with engine.connect() as connection:
        assert connection.execute(select(PropertyExpense)).all() == []


def test_validation_messages(engine: Engine) -> None:
    with pytest.raises(DeadlineError, match="positif"):
        _tax(engine, None, amount_cents=0, recoverable_cents=None)
    with pytest.raises(DeadlineError, match="montant total"):
        _tax(engine, None, amount_cents=None)
    with pytest.raises(DeadlineError, match="dépasser"):
        _tax(engine, None, recoverable_cents=200_000)
    with pytest.raises(DeadlineError, match="Rappel"):
        _tax(engine, None, remind_days=91)
    with pytest.raises(DeadlineError, match="Bien introuvable"):
        _tax(engine, 99)
    _tax(engine, None, label="  avis 2026  ")
    with pytest.raises(DeadlineError, match="existe déjà"):
        _tax(engine, None)
    with pytest.raises(DeadlineError, match="introuvable"):
        mark_done(engine, 999, actor_user_id=None, now=NOW)
    with engine.connect() as connection:
        assert connection.execute(select(Deadline.label)).scalar_one() == "avis 2026"


def test_update_resolves_stale_reminders_and_refuses_collisions(engine: Engine) -> None:
    first = _tax(engine, None)
    _tax(engine, None, due_date=date(2026, 11, 15))
    remind(engine, now=NOW + timedelta(days=7))
    moved = DeadlineInput(kind=DeadlineKind.PROPERTY_TAX, due_date=date(2026, 11, 15))
    with pytest.raises(DeadlineError, match="existe déjà"):
        update_deadline(engine, first, moved, actor_user_id=None, now=NOW)
    later = DeadlineInput(
        kind=DeadlineKind.PROPERTY_TAX, due_date=date(2026, 10, 20), amount_cents=100
    )
    update_deadline(engine, first, later, actor_user_id=None, now=NOW)
    assert [status for _, _, status, _ in _alerts(engine)] == [AlertStatus.RESOLVED]
    with engine.connect() as connection:
        row = connection.execute(select(Deadline).where(Deadline.id == first)).one()
    assert (row.due_date, row.amount_cents, row.recoverable_cents) == (
        date(2026, 10, 20),
        100,
        None,
    )


def test_templates_create_the_usual_deadlines_once(engine: Engine) -> None:
    furnished = _property(engine)
    _property(engine, furnished=False, name="Nu")
    assert apply_templates(engine, actor_user_id=None, now=NOW) == 4
    assert apply_templates(engine, actor_user_id=None, now=NOW) == 0  # rejouable
    with engine.connect() as connection:
        rows = connection.execute(
            select(Deadline.kind, Deadline.due_date, Deadline.property_id).order_by(
                Deadline.due_date, Deadline.property_id
            )
        ).all()
    assert [(kind, day) for kind, day, _ in rows] == [
        (DeadlineKind.PROPERTY_TAX, date(2026, 10, 15)),
        (DeadlineKind.PROPERTY_TAX, date(2026, 10, 15)),
        (DeadlineKind.CFE, date(2026, 12, 15)),  # bien meublé seulement
        (DeadlineKind.INCOME_TAX_RETURN, date(2027, 5, 20)),  # passée cette année : l'an prochain
    ]
    assert rows[2].property_id == furnished


def test_next_year_of_29_february() -> None:
    assert same_day_next_year(date(2028, 2, 29)) == date(2029, 2, 28)
    assert same_day_next_year(date(2026, 10, 15)) == date(2027, 10, 15)
