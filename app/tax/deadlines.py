"""Échéances administratives et fiscales (FIS-04, LOC-10) : taxe foncière, CFE, déclaration…

- **Rappels** : avertissement `remind_days` jours avant (7 par défaut), puis alerte « dépassée »
  le lendemain de l'échéance (critique pour un impôt : majoration de 10 %). Réglée → résolues.
- **Rapprochement** : une échéance dont le montant est connu est réglée automatiquement quand
  une seule dépense de ce montant exact apparaît entre 60 jours avant et 30 jours après la date.
  Sinon, rattachement à la main. Une opération retirée à la main n'est jamais reprise.
- **Charges du bien** : l'opération qui règle une taxe foncière, une CFE ou une assurance d'un
  bien devient une charge de ce bien (LOC-10), sauf si elle en a été retirée à la main.
- **Échéance annuelle** : une fois réglée, celle de l'année suivante est créée (montant à saisir
  à réception de l'avis). Rejouable : jamais deux échéances de même nature, même bien, même jour.
- Données brutes intactes (inv. 4) : seuls des liens sont écrits ; tout est audité.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import Connection, Engine, delete, insert, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.alerts.engine import raise_alert, resolve_by_key, resolve_prefix
from app.audit import audit
from app.db import inserted_id, write_transaction
from app.ledger.views import non_transfer_transactions, visible_accounts
from app.models import AuditLog, Deadline, LedgerTransaction, Property, PropertyExpense
from app.models.alerts import Severity
from app.models.rental import ExpenseKind
from app.models.tax import DeadlineKind
from app.timeutil import format_date_fr, paris_date

KIND_LABELS = {
    DeadlineKind.PROPERTY_TAX: "Taxe foncière",
    DeadlineKind.CFE: "CFE (cotisation foncière des entreprises)",
    DeadlineKind.INCOME_TAX_RETURN: "Déclaration des revenus",
    DeadlineKind.TAX_PAYMENT: "Impôt à payer",
    DeadlineKind.INSURANCE: "Assurance",
    DeadlineKind.OTHER: "Autre échéance",
}
# Un impôt payé en retard est majoré (10 %) : alerte critique une fois la date passée.
TAX_KINDS = frozenset({DeadlineKind.PROPERTY_TAX, DeadlineKind.CFE, DeadlineKind.TAX_PAYMENT})
EXPENSE_OF = {
    DeadlineKind.PROPERTY_TAX: ExpenseKind.PROPERTY_TAX,
    DeadlineKind.CFE: ExpenseKind.CFE,
    DeadlineKind.INSURANCE: ExpenseKind.INSURANCE,
}
MATCH_BEFORE = timedelta(days=60)
MATCH_AFTER = timedelta(days=30)
DEFAULT_REMIND_DAYS = 7
MAX_REMIND_DAYS = 90


class DeadlineError(ValueError):
    """Échéance invalide ou opération impossible (message affiché tel quel)."""


@dataclass(frozen=True)
class DeadlineInput:
    kind: DeadlineKind
    due_date: date
    label: str | None = None
    property_id: int | None = None
    amount_cents: int | None = None
    recoverable_cents: int | None = None
    remind_days: int = DEFAULT_REMIND_DAYS
    yearly: bool = True


@dataclass(frozen=True)
class Template:
    """Échéance habituelle (droit français) proposée d'un clic ; date modifiable ensuite."""

    kind: DeadlineKind
    month: int
    day: int
    per_property: bool
    furnished_only: bool = False
    label: str | None = None
    remind_days: int = DEFAULT_REMIND_DAYS


TEMPLATES = (
    Template(DeadlineKind.PROPERTY_TAX, 10, 15, per_property=True),
    # Loueur en meublé : CFE due au lieu du bien (exonérée l'année de début d'activité).
    Template(DeadlineKind.CFE, 12, 15, per_property=True, furnished_only=True),
    # Date limite en ligne selon le département : la plus précoce (zone 1), à ajuster.
    Template(
        DeadlineKind.INCOME_TAX_RETURN,
        5,
        20,
        per_property=False,
        label="date selon le département, à vérifier sur impots.gouv.fr",
        remind_days=14,
    ),
)


@dataclass(frozen=True)
class Candidate:
    id: int
    booking_date: date
    amount_cents: int
    label: str
    exact: bool


def same_day_next_year(day: date) -> date:
    try:
        return day.replace(year=day.year + 1)
    except ValueError:  # 29 février
        return day.replace(year=day.year + 1, day=28)


def _validate(connection: Connection, data: DeadlineInput) -> DeadlineInput:
    label = (data.label or "").strip()[:128] or None
    if data.amount_cents is not None and data.amount_cents <= 0:
        raise DeadlineError("Le montant doit être positif.")
    if data.recoverable_cents is not None:
        if data.amount_cents is None:
            raise DeadlineError("Indiquez le montant total avant la part récupérable.")
        if not 0 <= data.recoverable_cents <= data.amount_cents:
            raise DeadlineError("La part récupérable ne peut pas dépasser le montant.")
    if not 0 <= data.remind_days <= MAX_REMIND_DAYS:
        raise DeadlineError(f"Rappel entre 0 et {MAX_REMIND_DAYS} jours avant l'échéance.")
    if data.property_id is not None and (
        connection.scalar(select(Property.id).where(Property.id == data.property_id)) is None
    ):
        raise DeadlineError("Bien introuvable.")
    return replace(data, label=label)


def _values(data: DeadlineInput) -> dict[str, Any]:
    return {
        "kind": data.kind,
        "due_date": data.due_date,
        "label": data.label,
        "property_id": data.property_id,
        "amount_cents": data.amount_cents,
        "recoverable_cents": data.recoverable_cents,
        "remind_days": data.remind_days,
        "yearly": data.yearly,
    }


def _exists(connection: Connection, kind: DeadlineKind, property_id: int | None, day: date) -> bool:
    return (
        connection.scalar(
            select(Deadline.id).where(
                Deadline.kind == kind,
                Deadline.property_id.is_(None)
                if property_id is None
                else Deadline.property_id == property_id,
                Deadline.due_date == day,
            )
        )
        is not None
    )


def create(engine: Engine, data: DeadlineInput, *, actor_user_id: int | None, now: datetime) -> int:
    with write_transaction(engine) as connection:
        data = _validate(connection, data)
        if _exists(connection, data.kind, data.property_id, data.due_date):
            raise DeadlineError("Cette échéance existe déjà à cette date.")
        deadline_id = inserted_id(
            connection.execute(insert(Deadline).values(**_values(data), created_at=now))
        )
        audit(
            connection,
            "deadline.created",
            ("deadline", deadline_id),
            now=now,
            actor_user_id=actor_user_id,
            details={"nature": str(data.kind), "date": data.due_date.isoformat()},
        )
    return deadline_id


def update_deadline(
    engine: Engine,
    deadline_id: int,
    data: DeadlineInput,
    *,
    actor_user_id: int | None,
    now: datetime,
) -> None:
    with write_transaction(engine) as connection:
        current = _get(connection, deadline_id)
        data = _validate(connection, data)
        if (data.kind, data.property_id, data.due_date) != (
            current.kind,
            current.property_id,
            current.due_date,
        ) and _exists(connection, data.kind, data.property_id, data.due_date):
            raise DeadlineError("Cette échéance existe déjà à cette date.")
        connection.execute(
            update(Deadline).where(Deadline.id == deadline_id).values(**_values(data))
        )
        audit(
            connection,
            "deadline.updated",
            ("deadline", deadline_id),
            now=now,
            actor_user_id=actor_user_id,
            details={"nature": str(data.kind), "date": data.due_date.isoformat()},
        )
    # Date ou nature changée : les rappels en cours ne valent plus ; le prochain passage les
    # relève si besoin.
    resolve_prefix(engine, f"deadline:{deadline_id}:", now=now)


def delete_deadline(
    engine: Engine, deadline_id: int, *, actor_user_id: int | None, now: datetime
) -> None:
    with write_transaction(engine) as connection:
        current = _get(connection, deadline_id)
        connection.execute(delete(Deadline).where(Deadline.id == deadline_id))
        audit(
            connection,
            "deadline.deleted",
            ("deadline", deadline_id),
            now=now,
            actor_user_id=actor_user_id,
            details={"nature": str(current.kind), "date": current.due_date.isoformat()},
        )
    resolve_prefix(engine, f"deadline:{deadline_id}:", now=now)


def _get(connection: Connection, deadline_id: int) -> Any:
    row = connection.execute(select(Deadline).where(Deadline.id == deadline_id)).first()
    if row is None:
        raise DeadlineError("Échéance introuvable.")
    return row


def _refused(connection: Connection) -> dict[int, set[int]]:
    """Opérations retirées à la main, par échéance : jamais reprises automatiquement."""
    refused: dict[int, set[int]] = {}
    for object_id, details in connection.execute(
        select(AuditLog.object_id, AuditLog.details).where(AuditLog.action == "deadline.reopened")
    ):
        if object_id is not None and details.get("transaction") is not None:
            refused.setdefault(int(object_id), set()).add(int(details["transaction"]))
    return refused


def _expenses_unlinked_by_hand(connection: Connection) -> set[int]:
    rows: Sequence[dict[str, Any]] = list(
        connection.scalars(
            select(AuditLog.details).where(AuditLog.action == "property.expense_unlinked")
        )
    )
    return {int(details["transaction"]) for details in rows if "transaction" in details}


def _settle(
    connection: Connection,
    deadline: Any,
    transaction_id: int | None,
    *,
    method: str,
    actor_user_id: int | None,
    now: datetime,
) -> int | None:
    """Marque réglée, rattache l'opération (et la charge du bien), crée l'échéance suivante."""
    connection.execute(
        update(Deadline)
        .where(Deadline.id == deadline.id)
        .values(done_at=now, transaction_id=transaction_id)
    )
    expense_kind = EXPENSE_OF.get(deadline.kind)
    if (
        transaction_id is not None
        and deadline.property_id is not None
        and expense_kind is not None
        and transaction_id not in _expenses_unlinked_by_hand(connection)
    ):
        connection.execute(
            sqlite_insert(PropertyExpense)
            .values(
                property_id=deadline.property_id,
                transaction_id=transaction_id,
                kind=expense_kind,
                created_at=now,
            )
            .on_conflict_do_nothing(index_elements=["transaction_id"])
        )
    next_id = None
    if deadline.yearly:
        following = same_day_next_year(deadline.due_date)
        if not _exists(connection, deadline.kind, deadline.property_id, following):
            next_id = inserted_id(
                connection.execute(
                    insert(Deadline).values(
                        kind=deadline.kind,
                        due_date=following,
                        label=deadline.label,
                        property_id=deadline.property_id,
                        remind_days=deadline.remind_days,
                        yearly=True,
                        created_at=now,
                    )
                )
            )
    audit(
        connection,
        "deadline.done",
        ("deadline", deadline.id),
        now=now,
        actor_user_id=actor_user_id,
        details={"transaction": transaction_id, "methode": method, "suivante": next_id},
    )
    return next_id


def _check_payment(connection: Connection, deadline_id: int, transaction_id: int) -> None:
    amount = connection.scalar(
        select(LedgerTransaction.amount_cents).where(
            LedgerTransaction.id == transaction_id,
            LedgerTransaction.account_id.in_(visible_accounts()),
            LedgerTransaction.id.in_(non_transfer_transactions()),
        )
    )
    if amount is None or amount >= 0:
        raise DeadlineError("Seule une dépense peut régler une échéance.")
    other = connection.scalar(
        select(Deadline.id).where(
            Deadline.transaction_id == transaction_id, Deadline.id != deadline_id
        )
    )
    if other is not None:
        raise DeadlineError("Cette opération règle déjà une autre échéance.")


def mark_done(
    engine: Engine,
    deadline_id: int,
    *,
    transaction_id: int | None = None,
    actor_user_id: int | None,
    now: datetime,
) -> int | None:
    """Réglée à la main, avec ou sans l'opération de paiement ; renvoie l'échéance suivante."""
    with write_transaction(engine) as connection:
        deadline = _get(connection, deadline_id)
        if deadline.done_at is not None:
            raise DeadlineError("Échéance déjà réglée.")
        if transaction_id is not None:
            _check_payment(connection, deadline_id, transaction_id)
        next_id = _settle(
            connection,
            deadline,
            transaction_id,
            method="manuel",
            actor_user_id=actor_user_id,
            now=now,
        )
    resolve_prefix(engine, f"deadline:{deadline_id}:", now=now)
    return next_id


def reopen(engine: Engine, deadline_id: int, *, actor_user_id: int | None, now: datetime) -> None:
    """Annule le règlement. L'opération retirée n'est plus proposée automatiquement ; la charge
    du bien éventuellement créée reste (à retirer sur la page du bien si elle est fausse)."""
    with write_transaction(engine) as connection:
        deadline = _get(connection, deadline_id)
        if deadline.done_at is None:
            raise DeadlineError("Échéance non réglée.")
        connection.execute(
            update(Deadline)
            .where(Deadline.id == deadline_id)
            .values(done_at=None, transaction_id=None)
        )
        audit(
            connection,
            "deadline.reopened",
            ("deadline", deadline_id),
            now=now,
            actor_user_id=actor_user_id,
            details={"transaction": deadline.transaction_id},
        )


def _candidates(connection: Connection, deadline: Any, refused: set[int]) -> list[Candidate]:
    taken = select(Deadline.transaction_id).where(Deadline.transaction_id.is_not(None))
    rows = connection.execute(
        select(
            LedgerTransaction.id,
            LedgerTransaction.booking_date,
            LedgerTransaction.amount_cents,
            LedgerTransaction.label_clean,
        ).where(
            LedgerTransaction.amount_cents < 0,
            LedgerTransaction.account_id.in_(visible_accounts()),
            LedgerTransaction.id.in_(non_transfer_transactions()),
            LedgerTransaction.id.not_in(taken),
            LedgerTransaction.booking_date >= deadline.due_date - MATCH_BEFORE,
            LedgerTransaction.booking_date <= deadline.due_date + MATCH_AFTER,
        )
    ).all()
    expected = deadline.amount_cents
    found = [
        Candidate(
            row.id,
            row.booking_date,
            row.amount_cents,
            row.label_clean,
            expected is not None and -row.amount_cents == expected,
        )
        for row in rows
        if row.id not in refused
    ]
    found.sort(
        key=lambda c: (
            not c.exact,
            abs(-c.amount_cents - expected) if expected else 0,
            abs((c.booking_date - deadline.due_date).days),
        )
    )
    return found


def candidates(engine: Engine, deadline_id: int, *, limit: int = 30) -> list[Candidate]:
    """Dépenses proches de l'échéance, celles du montant exact d'abord (rattachement manuel)."""
    with engine.connect() as connection:
        deadline = _get(connection, deadline_id)
        return _candidates(connection, deadline, _refused(connection).get(deadline_id, set()))[
            :limit
        ]


def match_payments(engine: Engine, *, now: datetime) -> int:
    """Règle les échéances dont une seule dépense a le montant exact dans la fenêtre."""
    settled: list[int] = []
    with write_transaction(engine) as connection:
        refused = _refused(connection)
        open_deadlines = connection.execute(
            select(Deadline)
            .where(Deadline.done_at.is_(None), Deadline.amount_cents.is_not(None))
            .order_by(Deadline.due_date, Deadline.id)
        ).all()
        for deadline in open_deadlines:
            exact = [
                c
                for c in _candidates(connection, deadline, refused.get(deadline.id, set()))
                if c.exact
            ]
            if len(exact) != 1:
                continue
            _settle(connection, deadline, exact[0].id, method="auto", actor_user_id=None, now=now)
            settled.append(deadline.id)
    for deadline_id in settled:
        resolve_prefix(engine, f"deadline:{deadline_id}:", now=now)
    return len(settled)


def _title(kind_label: str, property_name: str | None, text: str) -> list[str]:
    """Titre sans montant (DT §6) ; sans le nom du bien s'il ressemble à un montant."""
    titles = [f"{kind_label} : {text}"]
    if property_name:
        titles.insert(0, f"{kind_label} ({property_name[:40]}) : {text}")
    return titles


def _raise(
    engine: Engine, *, key: str, severity: Severity, titles: list[str], deadline: Any, now: datetime
) -> None:
    for title in titles:
        try:
            raise_alert(
                engine,
                type="deadline",
                severity=severity,
                key=key,
                title=title,
                subject=("deadline", deadline.id),
                once=True,
                details={"date": format_date_fr(deadline.due_date)},
                now=now,
            )
            return
        except ValueError:
            continue


def remind(engine: Engine, *, now: datetime) -> int:
    """Rappels des échéances non réglées ; renvoie le nombre d'échéances signalées."""
    today = paris_date(now)
    with engine.connect() as connection:
        rows = connection.execute(
            select(Deadline, Property.name.label("property_name"))
            .outerjoin(Property, Property.id == Deadline.property_id)
            .where(Deadline.done_at.is_(None))
        ).all()
    signalled = 0
    for row in rows:
        key = f"deadline:{row.id}:{row.due_date.isoformat()}"
        label = KIND_LABELS[row.kind]
        day = format_date_fr(row.due_date)
        if today > row.due_date:
            resolve_by_key(engine, key, now=now)
            severity = Severity.CRITICAL if row.kind in TAX_KINDS else Severity.WARNING
            titles = _title(label, row.property_name, f"échéance du {day} dépassée")
            _raise(
                engine, key=f"{key}:retard", severity=severity, titles=titles, deadline=row, now=now
            )
            signalled += 1
        elif today >= row.due_date - timedelta(days=row.remind_days):
            titles = _title(label, row.property_name, f"échéance le {day}")
            _raise(engine, key=key, severity=Severity.WARNING, titles=titles, deadline=row, now=now)
            signalled += 1
    return signalled


def _occurrence(template: Template, today: date) -> date:
    day = date(today.year, template.month, template.day)
    return day if day >= today else date(today.year + 1, template.month, template.day)


def apply_templates(engine: Engine, *, actor_user_id: int | None, now: datetime) -> int:
    """Crée les échéances habituelles manquantes (prochaine occurrence) ; rejouable."""
    today = paris_date(now)
    created = 0
    with write_transaction(engine) as connection:
        properties = connection.execute(
            select(Property.id, Property.furnished).where(Property.archived_at.is_(None))
        ).all()
        for template in TEMPLATES:
            targets: list[int | None] = (
                [p.id for p in properties if p.furnished or not template.furnished_only]
                if template.per_property
                else [None]
            )
            day = _occurrence(template, today)
            for property_id in targets:
                near = connection.scalar(
                    select(Deadline.id).where(
                        Deadline.kind == template.kind,
                        Deadline.property_id.is_(None)
                        if property_id is None
                        else Deadline.property_id == property_id,
                        Deadline.due_date > day - timedelta(days=183),
                        Deadline.due_date < day + timedelta(days=183),
                    )
                )
                if near is not None:
                    continue
                deadline_id = inserted_id(
                    connection.execute(
                        insert(Deadline).values(
                            kind=template.kind,
                            due_date=day,
                            label=template.label,
                            property_id=property_id,
                            remind_days=template.remind_days,
                            yearly=True,
                            created_at=now,
                        )
                    )
                )
                audit(
                    connection,
                    "deadline.created",
                    ("deadline", deadline_id),
                    now=now,
                    actor_user_id=actor_user_id,
                    details={"nature": str(template.kind), "date": day.isoformat(), "modele": True},
                )
                created += 1
    return created
