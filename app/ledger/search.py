"""Recherche et filtres sur l'historique (ANA-11), base de la liste et de l'export (FIS-06).

Seules les transactions effectives sont listées (doublons confirmés exclus). Le filtre texte
porte sur le libellé nettoyé ; la contrepartie, chiffrée, n'est pas cherchable.
"""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import Connection, Engine, Select, exists, func, select

from app.ledger.views import (
    confirmed_transfer_legs,
    is_effective,
    visible_accounts,
)
from app.models import Account, Categorization, Category, LedgerTransaction

UNCATEGORIZED = 0  # valeur du filtre « non catégorisé »


@dataclass(frozen=True)
class TransactionFilters:
    account_id: int | None = None
    date_from: date | None = None
    date_to: date | None = None
    text: str | None = None
    amount_min: int | None = None
    amount_max: int | None = None
    category_id: int | None = None
    direction: str | None = None  # "debit" | "credit"

    def as_query(self) -> dict[str, str]:
        return {key: str(value) for key, value in self.__dict__.items() if value not in (None, "")}


@dataclass(frozen=True)
class TransactionRow:
    id: int
    booking_date: date
    account_name: str
    label: str
    amount_cents: int
    is_transfer: bool
    categories: list[str] = field(default_factory=list)


def matching(filters: TransactionFilters) -> Select[int]:
    """Identifiants des transactions effectives et visibles qui passent les filtres."""
    query = select(LedgerTransaction.id).where(
        is_effective(),
        LedgerTransaction.account_id.in_(visible_accounts()),
    )
    conditions = []
    if filters.account_id is not None:
        conditions.append(LedgerTransaction.account_id == filters.account_id)
    if filters.date_from is not None:
        conditions.append(LedgerTransaction.booking_date >= filters.date_from)
    if filters.date_to is not None:
        conditions.append(LedgerTransaction.booking_date <= filters.date_to)
    if filters.text:
        pattern = "%" + filters.text.upper().replace("%", "").replace("_", "") + "%"
        conditions.append(LedgerTransaction.label_clean.like(pattern))
    if filters.amount_min is not None:
        conditions.append(LedgerTransaction.amount_cents >= filters.amount_min)
    if filters.amount_max is not None:
        conditions.append(LedgerTransaction.amount_cents <= filters.amount_max)
    if filters.direction == "debit":
        conditions.append(LedgerTransaction.amount_cents < 0)
    elif filters.direction == "credit":
        conditions.append(LedgerTransaction.amount_cents > 0)
    active = (Categorization.transaction_id == LedgerTransaction.id) & (
        Categorization.superseded_at.is_(None)
    )
    if filters.category_id == UNCATEGORIZED:
        conditions.append(~exists().where(active))
    elif filters.category_id is not None:
        in_tree = select(Category.id).where(
            (Category.id == filters.category_id) | (Category.parent_id == filters.category_id)
        )
        conditions.append(exists().where(active, Categorization.category_id.in_(in_tree)))
    return query.where(*conditions)


def _category_names(connection: Connection, ids: list[int]) -> dict[int, list[str]]:
    parent = Category.__table__.alias("parent")
    rows = connection.execute(
        select(Categorization.transaction_id, Category.name, parent.c.name)
        .join(Category, Category.id == Categorization.category_id)
        .outerjoin(parent, parent.c.id == Category.parent_id)
        .where(Categorization.transaction_id.in_(ids), Categorization.superseded_at.is_(None))
        .order_by(Categorization.id)
    ).all()
    names: dict[int, list[str]] = {}
    for tx_id, name, parent_name in rows:
        names.setdefault(tx_id, []).append(f"{parent_name} › {name}" if parent_name else name)
    return names


def _listing(filters: TransactionFilters) -> Select[int, date, str, str, int]:
    """Opérations du filtre, des plus récentes aux plus anciennes (page et export)."""
    ids = matching(filters).subquery()
    return (
        select(
            LedgerTransaction.id,
            LedgerTransaction.booking_date,
            Account.name,
            LedgerTransaction.label_clean,
            LedgerTransaction.amount_cents,
        )
        .join(Account, Account.id == LedgerTransaction.account_id)
        .where(LedgerTransaction.id.in_(select(ids.c.id)))
        .order_by(LedgerTransaction.booking_date.desc(), LedgerTransaction.id.desc())
    )


def _rows(connection: Connection, rows: Sequence[Any], transfers: set[int]) -> list[TransactionRow]:
    names = _category_names(connection, [row[0] for row in rows])
    return [
        TransactionRow(
            id=tx_id,
            booking_date=booking_date,
            account_name=account_name,
            label=label,
            amount_cents=amount,
            is_transfer=tx_id in transfers,
            categories=names.get(tx_id, []),
        )
        for tx_id, booking_date, account_name, label, amount in rows
    ]


def search(
    engine: Engine, filters: TransactionFilters, *, limit: int = 50, offset: int = 0
) -> tuple[list[TransactionRow], int]:
    """Une page du filtre et le nombre total d'opérations correspondantes."""
    with engine.connect() as connection:
        total = int(
            connection.execute(
                select(func.count()).select_from(matching(filters).subquery())
            ).scalar_one()
        )
        rows = connection.execute(_listing(filters).offset(offset).limit(limit)).all()
        transfers = set(connection.scalars(confirmed_transfer_legs()))
        return _rows(connection, rows, transfers), total


def iter_all(
    engine: Engine, filters: TransactionFilters, batch: int = 500
) -> Iterator[TransactionRow]:
    """Toutes les opérations du filtre (export CSV), lues en une seule requête par paquets :
    ni décompte ni décalage répétés, coût proportionnel au nombre d'opérations."""
    with engine.connect() as connection:
        transfers = set(connection.scalars(confirmed_transfer_legs()))
        result = connection.execution_options(yield_per=batch).execute(_listing(filters))
        for chunk in result.partitions(batch):
            yield from _rows(connection, chunk, transfers)


def category_choices(engine: Engine) -> list[tuple[int, str]]:
    """Catégories pour les listes déroulantes : « Parent › Enfant », dans l'ordre de l'arbre."""
    parent = Category.__table__.alias("parent")
    with engine.connect() as connection:
        rows = connection.execute(
            select(
                Category.id, Category.name, parent.c.name, parent.c.sort_order, Category.sort_order
            )
            .outerjoin(parent, parent.c.id == Category.parent_id)
            .where(Category.archived_at.is_(None))
        ).all()
    ordered = sorted(
        rows,
        key=lambda r: (r[3] if r[2] else r[4], r[2] or r[1], 0 if r[2] is None else 1, r[4]),
    )
    return [(row[0], f"{row[2]} › {row[1]}" if row[2] else row[1]) for row in ordered]
