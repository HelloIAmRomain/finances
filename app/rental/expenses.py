"""Charges du bien (LOC-10) : dépenses rattachées à la main ou par motif de libellé.

- Seules des dépenses (débits) effectives d'un compte suivi sont rattachables ; une opération
  n'est rattachée qu'à un seul bien (contrainte unique).
- Une règle rattache, à chaque passage du moteur des loyers, les dépenses dont le libellé
  contient son motif : rejouable, jamais de doublon (inv. 3). Un rattachement retiré à la main
  n'est pas refait par la règle (il est mémorisé comme écarté).
- Totaux en centimes par année civile et par nature, pour le récapitulatif fiscal (FIS-01).
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import Connection, Engine, delete, func, insert, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.audit import audit
from app.db import inserted_id, write_transaction
from app.ledger.views import is_effective, visible_accounts
from app.models import (
    AuditLog,
    LedgerTransaction,
    Property,
    PropertyExpense,
    PropertyExpenseRule,
)
from app.models.rental import ExpenseKind
from app.rental.leases import normalize_label

MIN_PATTERN = 3
EXPENSE_LABELS = {
    ExpenseKind.CO_OWNERSHIP: "copropriété",
    ExpenseKind.PROPERTY_TAX: "taxe foncière",
    ExpenseKind.INSURANCE: "assurance",
    ExpenseKind.WORKS: "travaux",
    ExpenseKind.LOAN: "crédit",
    ExpenseKind.MAINTENANCE: "entretien",
    ExpenseKind.OTHER: "autre",
}


class ExpenseError(ValueError):
    """Rattachement de charge impossible."""


@dataclass(frozen=True)
class ExpenseRow:
    id: int
    booking_date: date
    label: str
    amount_cents: int
    kind: ExpenseKind
    automatic: bool


def _property_exists(connection: Connection, property_id: int) -> None:
    if connection.scalar(select(Property.id).where(Property.id == property_id)) is None:
        raise ExpenseError("Bien introuvable.")


def add_rule(
    engine: Engine,
    property_id: int,
    *,
    kind: ExpenseKind,
    pattern: str,
    actor_user_id: int | None,
    now: datetime,
) -> int:
    motif = normalize_label(pattern)
    if len(motif) < MIN_PATTERN:
        raise ExpenseError("Motif trop court (3 caractères au moins).")
    with write_transaction(engine) as connection:
        _property_exists(connection, property_id)
        if connection.scalar(
            select(PropertyExpenseRule.id).where(
                PropertyExpenseRule.property_id == property_id,
                PropertyExpenseRule.pattern == motif,
            )
        ):
            raise ExpenseError("Ce motif existe déjà pour ce bien.")
        result = connection.execute(
            insert(PropertyExpenseRule).values(
                property_id=property_id, kind=kind, pattern=motif, created_at=now
            )
        )
        rule_id = inserted_id(result)
        audit(
            connection,
            "property.expense_rule",
            ("property", property_id),
            now=now,
            actor_user_id=actor_user_id,
            details={"motif": motif, "nature": str(kind)},
        )
    return rule_id


def link(
    engine: Engine,
    property_id: int,
    transaction_id: int,
    *,
    kind: ExpenseKind,
    actor_user_id: int | None,
    now: datetime,
) -> int:
    with write_transaction(engine) as connection:
        _property_exists(connection, property_id)
        amount = connection.scalar(
            select(LedgerTransaction.amount_cents).where(LedgerTransaction.id == transaction_id)
        )
        if amount is None or amount >= 0:
            raise ExpenseError("Seule une dépense (débit) peut être une charge du bien.")
        if connection.scalar(
            select(PropertyExpense.id).where(PropertyExpense.transaction_id == transaction_id)
        ):
            raise ExpenseError("Opération déjà rattachée à un bien.")
        result = connection.execute(
            insert(PropertyExpense).values(
                property_id=property_id, transaction_id=transaction_id, kind=kind, created_at=now
            )
        )
        expense_id = inserted_id(result)
        audit(
            connection,
            "property.expense_linked",
            ("property_expense", expense_id),
            now=now,
            actor_user_id=actor_user_id,
            details={"bien": property_id, "transaction": transaction_id, "nature": str(kind)},
        )
    return expense_id


def unlink(engine: Engine, expense_id: int, *, actor_user_id: int | None, now: datetime) -> None:
    """Retire le rattachement ; l'opération est mémorisée pour que la règle ne la reprenne pas."""
    with write_transaction(engine) as connection:
        row = connection.execute(
            select(PropertyExpense).where(PropertyExpense.id == expense_id)
        ).first()
        if row is None:
            raise ExpenseError("Charge introuvable.")
        connection.execute(delete(PropertyExpense).where(PropertyExpense.id == expense_id))
        audit(
            connection,
            "property.expense_unlinked",
            ("property_expense", expense_id),
            now=now,
            actor_user_id=actor_user_id,
            details={"bien": row.property_id, "transaction": row.transaction_id},
        )


def _unlinked_by_hand(connection: Connection) -> set[int]:
    rows: list[dict[str, Any]] = list(
        connection.scalars(
            select(AuditLog.details).where(AuditLog.action == "property.expense_unlinked")
        )
    )
    return {int(details["transaction"]) for details in rows if "transaction" in details}


def apply_rules(connection: Connection) -> int:
    """Rattache les dépenses correspondant aux motifs ; renvoie le nombre de rattachements."""
    rules = connection.execute(select(PropertyExpenseRule).order_by(PropertyExpenseRule.id)).all()
    if not rules:
        return 0
    refused = _unlinked_by_hand(connection)
    linked = set(connection.scalars(select(PropertyExpense.transaction_id)))
    candidates = connection.execute(
        select(LedgerTransaction.id, LedgerTransaction.label_raw).where(
            LedgerTransaction.amount_cents < 0,
            LedgerTransaction.account_id.in_(visible_accounts()),
            is_effective(),
        )
    ).all()
    created = 0
    for tx_id, label in candidates:
        if tx_id in linked or tx_id in refused:
            continue
        normalized = normalize_label(label or "")
        rule = next((r for r in rules if r.pattern in normalized), None)
        if rule is None:
            continue
        result = connection.execute(
            sqlite_insert(PropertyExpense)
            .values(
                property_id=rule.property_id,
                transaction_id=tx_id,
                kind=rule.kind,
                rule_id=rule.id,
            )
            .on_conflict_do_nothing(index_elements=["transaction_id"])
        )
        created += result.rowcount
        linked.add(tx_id)
    return created


def expenses(engine: Engine, property_id: int, year: int) -> list[ExpenseRow]:
    with engine.connect() as connection:
        rows = connection.execute(
            select(
                PropertyExpense.id,
                LedgerTransaction.booking_date,
                LedgerTransaction.label_clean,
                LedgerTransaction.amount_cents,
                PropertyExpense.kind,
                PropertyExpense.rule_id,
            )
            .join(LedgerTransaction, LedgerTransaction.id == PropertyExpense.transaction_id)
            .where(
                PropertyExpense.property_id == property_id,
                func.strftime("%Y", LedgerTransaction.booking_date) == f"{year:04d}",
                is_effective(),
            )
            .order_by(LedgerTransaction.booking_date.desc())
        ).all()
    return [
        ExpenseRow(
            r.id, r.booking_date, r.label_clean, r.amount_cents, r.kind, r.rule_id is not None
        )
        for r in rows
    ]


def totals_by_kind(rows: list[ExpenseRow]) -> dict[ExpenseKind, int]:
    totals: dict[ExpenseKind, int] = {}
    for row in rows:
        totals[row.kind] = totals.get(row.kind, 0) + row.amount_cents
    return totals
