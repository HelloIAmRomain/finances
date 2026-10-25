"""File de validation (T1.16b, T1.25c) : lecture des éléments ouverts et décisions sur les conflits.

Les décisions sur les doublons et les virements proposés sont dans `app.ledger.duplicates` et
`app.ledger.transfers` ; ici, les conflits ouverts lors d'un report (deux choix manuels
différents sur la transaction conservée et sur son doublon).
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import Engine, select, update

from app.audit import audit
from app.categorize.service import categorize_manually
from app.db import write_transaction
from app.models import Account, Categorization, LedgerTransaction, ReviewItem
from app.models.categorize import CategorizationSource, ReviewKind, ReviewStatus

KIND_LABELS = {
    ReviewKind.DUPLICATE: (
        "Doublon probable : la même opération reçue de la banque et d'un fichier importé"
    ),
    ReviewKind.TRANSFER_CANDIDATE: "Virement probable entre deux de vos comptes",
    ReviewKind.CATEGORY_CONFLICT: "Deux catégories choisies à la main pour la même opération",
    ReviewKind.TRANSFER_CONFLICT: (
        "Deux virements entre vos comptes choisis à la main pour une opération"
    ),
    ReviewKind.AI_SUGGESTION: "Catégorie à confirmer",
}


class ReviewError(ValueError):
    """Élément introuvable, déjà traité, ou choix impossible."""


@dataclass(frozen=True)
class TransactionView:
    id: int
    booking_date: date
    account_name: str
    label: str
    amount_cents: int


@dataclass(frozen=True)
class OpenItem:
    id: int
    kind: ReviewKind
    created_at: datetime
    details: dict[str, Any]
    duplicate_link_id: int | None
    first: TransactionView | None
    second: TransactionView | None


def _transactions(engine: Engine, ids: set[int]) -> dict[int, TransactionView]:
    with engine.connect() as connection:
        rows = connection.execute(
            select(
                LedgerTransaction.id,
                LedgerTransaction.booking_date,
                Account.name,
                LedgerTransaction.label_clean,
                LedgerTransaction.amount_cents,
            )
            .join(Account, Account.id == LedgerTransaction.account_id)
            .where(LedgerTransaction.id.in_(ids))
        ).all()
    return {row[0]: TransactionView(*row) for row in rows}


def open_items(engine: Engine) -> list[OpenItem]:
    with engine.connect() as connection:
        items = connection.execute(
            select(ReviewItem).where(ReviewItem.status == ReviewStatus.OPEN).order_by(ReviewItem.id)
        ).all()

    def pair(item: Any) -> tuple[int | None, int | None]:
        if item.kind in (ReviewKind.CATEGORY_CONFLICT, ReviewKind.TRANSFER_CONFLICT):
            return item.details.get("canonical"), item.details.get("duplicate")
        return item.other_transaction_id, item.transaction_id

    ids = {tx for item in items for tx in pair(item) if tx is not None}
    with engine.connect() as connection:
        hidden = set(  # transactions d'un compte ignoré (Q19)
            connection.scalars(
                select(LedgerTransaction.id)
                .join(Account, Account.id == LedgerTransaction.account_id)
                .where(LedgerTransaction.id.in_(ids), Account.ignored_at.is_not(None))
            )
        )
    items = [item for item in items if not hidden.intersection(pair(item))]
    views = _transactions(engine, ids)

    def view(tx_id: int | None) -> TransactionView | None:
        return views.get(tx_id) if tx_id is not None else None

    return [
        OpenItem(
            id=item.id,
            kind=item.kind,
            created_at=item.created_at,
            details=item.details,
            duplicate_link_id=item.duplicate_link_id,
            first=view(pair(item)[0]),
            second=view(pair(item)[1]),
        )
        for item in items
    ]


def _open_item(engine: Engine, item_id: int, kind: ReviewKind) -> Any:
    with engine.connect() as connection:
        item = connection.execute(select(ReviewItem).where(ReviewItem.id == item_id)).first()
    if item is None or item.kind is not kind or item.status is not ReviewStatus.OPEN:
        raise ReviewError(f"élément {item_id} introuvable ou déjà traité")
    return item


def _close(engine: Engine, item_id: int, *, actor: int | None, now: datetime, choice: str) -> None:
    with write_transaction(engine) as connection:
        connection.execute(
            update(ReviewItem)
            .where(ReviewItem.id == item_id)
            .values(status=ReviewStatus.RESOLVED, resolved_at=now)
        )
        audit(
            connection,
            "review.resolved",
            ("review_item", item_id),
            now=now,
            actor_user_id=actor,
            details={"choix": choice},
        )


def resolve_category_conflict(
    engine: Engine, item_id: int, *, take_duplicate: bool, actor_user_id: int | None, now: datetime
) -> None:
    """Garde la catégorie de la transaction conservée, ou y reporte celle du doublon."""
    item = _open_item(engine, item_id, ReviewKind.CATEGORY_CONFLICT)
    if take_duplicate:
        with engine.connect() as connection:
            lines = connection.execute(
                select(Categorization.category_id, Categorization.amount_cents).where(
                    Categorization.transaction_id == item.details["duplicate"],
                    Categorization.superseded_at.is_(None),
                    Categorization.source == CategorizationSource.MANUAL,
                )
            ).all()
        if not lines:
            raise ReviewError("le doublon n'a plus de catégorie manuelle")
        categorize_manually(
            engine,
            item.details["canonical"],
            [(category, amount) for category, amount in lines],
            actor_user_id=actor_user_id,
            now=now,
        )
    _close(
        engine,
        item_id,
        actor=actor_user_id,
        now=now,
        choice="doublon" if take_duplicate else "conservée",
    )


def keep_current_transfer(
    engine: Engine, item_id: int, *, actor_user_id: int | None, now: datetime
) -> None:
    _open_item(engine, item_id, ReviewKind.TRANSFER_CONFLICT)
    _close(engine, item_id, actor=actor_user_id, now=now, choice="conservée")


def decide_ai_suggestion(
    engine: Engine,
    item_id: int,
    *,
    category_id: int | None,
    actor_user_id: int | None,
    now: datetime,
) -> int:
    """Accepte la catégorie proposée par l'IA (`category_id` nul) ou en choisit une autre :
    catégorisation manuelle, jamais remplacée ensuite par une règle ni par l'IA (ANA-01).
    Renvoie la transaction, pour proposer une règle (ANA-03)."""
    item = _open_item(engine, item_id, ReviewKind.AI_SUGGESTION)
    chosen = category_id if category_id is not None else item.details.get("categorie")
    if chosen is None:
        raise ReviewError("choisir une catégorie")
    with engine.connect() as connection:
        amount = connection.scalar(
            select(LedgerTransaction.amount_cents).where(
                LedgerTransaction.id == item.transaction_id
            )
        )
    if amount is None:  # pragma: no cover (transaction immuable, jamais supprimée sous un élément)
        raise ReviewError("opération introuvable")
    categorize_manually(
        engine,
        item.transaction_id,
        [(int(chosen), int(amount))],
        actor_user_id=actor_user_id,
        now=now,
    )
    _close(
        engine,
        item_id,
        actor=actor_user_id,
        now=now,
        choice="suggestion" if category_id is None else "autre catégorie",
    )
    return int(item.transaction_id)


def dismiss_ai_suggestion(
    engine: Engine, item_id: int, *, actor_user_id: int | None, now: datetime
) -> None:
    """Laisse l'opération sans catégorie ; l'IA ne la reprendra pas."""
    _open_item(engine, item_id, ReviewKind.AI_SUGGESTION)
    with write_transaction(engine) as connection:
        connection.execute(
            update(ReviewItem)
            .where(ReviewItem.id == item_id)
            .values(status=ReviewStatus.DISMISSED, resolved_at=now)
        )
        audit(
            connection,
            "review.dismissed",
            ("review_item", item_id),
            now=now,
            actor_user_id=actor_user_id,
        )
