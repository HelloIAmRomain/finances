"""Décisions sur les doublons entre sources (T1.16b) : confirmer, rejeter, annuler.

Quand un lien est confirmé, les catégorisations et le virement interne de la transaction doublon
sont **reportés** sur la canonique :
- catégorisation : priorité manuel > règle > IA. Celle du doublon remplace celle de la canonique
  seulement si elle est de rang supérieur. Deux catégorisations manuelles différentes → celle de
  la canonique est gardée et un « conflit » est ouvert dans la file de validation ;
- virement interne : un lien manuel l'emporte sur un lien automatique ; deux liens manuels
  différents → celui de la canonique est gardé, conflit ouvert.

Rien n'est supprimé : les lignes remplacées sont marquées (`superseded_*`), les lignes reportées
portent le lien qui les a créées (`carried_by_link_id`). La liste exacte des changements est
inscrite dans le journal d'audit (ajout seul) ; l'annulation la relit et restaure **exactement**
l'état effectif antérieur.
"""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, Engine, func, insert, or_, select, update

from app.audit import audit
from app.db import inserted_id, write_transaction
from app.models import AuditLog, Categorization, DuplicateLink, ReviewItem, TransferLink
from app.models.categorize import CategorizationSource, ReviewKind, ReviewStatus
from app.models.ledger import DuplicateStatus, LinkMode, TransferStatus

PRIORITY = {
    CategorizationSource.MANUAL: 3,
    CategorizationSource.RULE: 2,
    CategorizationSource.AI: 1,
}
_ACTIVE_TRANSFER = (TransferStatus.PROPOSED, TransferStatus.CONFIRMED)
_CONFIRM_ACTIONS = ("duplicate.confirmed", "duplicate.auto_confirmed")


class DuplicateDecisionError(ValueError):
    """Transition impossible pour ce lien (déjà rejeté, pas encore confirmé…)."""


# --- Report -------------------------------------------------------------------------------------


def _active_categorizations(connection: Connection, tx_id: int) -> list[Any]:
    return list(
        connection.execute(
            select(Categorization)
            .where(Categorization.transaction_id == tx_id, Categorization.superseded_at.is_(None))
            .order_by(Categorization.id)
        )
    )


def _level(rows: list[Any]) -> int:
    return max((PRIORITY[row.source] for row in rows), default=0)


def _signature(rows: list[Any]) -> Counter[tuple[int, int]]:
    return Counter((row.category_id, row.amount_cents) for row in rows)


def _active_transfer(connection: Connection, tx_id: int) -> Any | None:
    return connection.execute(
        select(TransferLink).where(
            or_(TransferLink.debit_tx_id == tx_id, TransferLink.credit_tx_id == tx_id),
            TransferLink.status.in_(_ACTIVE_TRANSFER),
        )
    ).first()


def _open_conflict(
    connection: Connection,
    kind: ReviewKind,
    *,
    link_id: int,
    epoch: int,
    now: datetime,
    details: dict[str, Any],
) -> int:
    result = connection.execute(
        insert(ReviewItem).values(
            kind=kind,
            dedupe_key=f"{kind}:{link_id}:{epoch}",
            duplicate_link_id=link_id,
            status=ReviewStatus.OPEN,
            details=details,
            created_at=now,
        )
    )
    return inserted_id(result)


@dataclass
class _Carry:
    """Report d'un doublon confirmé sur l'opération conservée, et changements faits (annulables)."""

    connection: Connection
    link_id: int
    epoch: int  # nombre de confirmations précédentes du lien (conflits rouverts à neuf)
    canonical: int
    duplicate: int
    now: datetime
    changes: dict[str, Any]


def _carry_categorizations(c: _Carry) -> None:
    """Catégorisation du doublon reprise si elle est plus sûre ; conflit si deux choix manuels
    diffèrent."""
    on_duplicate = _active_categorizations(c.connection, c.duplicate)
    on_canonical = _active_categorizations(c.connection, c.canonical)
    if on_duplicate:
        if _level(on_duplicate) > _level(on_canonical):
            for row in on_canonical:
                c.connection.execute(
                    update(Categorization)
                    .where(Categorization.id == row.id)
                    .values(superseded_at=c.now, superseded_by_link_id=c.link_id)
                )
                c.changes["superseded_categorizations"].append(row.id)
            for row in on_duplicate:
                result = c.connection.execute(
                    insert(Categorization).values(
                        transaction_id=c.canonical,
                        category_id=row.category_id,
                        amount_cents=row.amount_cents,
                        source=row.source,
                        rule_id=row.rule_id,
                        confidence_permille=row.confidence_permille,
                        carried_by_link_id=c.link_id,
                        created_at=c.now,
                    )
                )
                c.changes["carried_categorizations"].append(inserted_id(result))
        elif _level(on_duplicate) == _level(on_canonical) == PRIORITY[
            CategorizationSource.MANUAL
        ] and _signature(on_duplicate) != _signature(on_canonical):
            c.changes["reviews"].append(
                _open_conflict(
                    c.connection,
                    ReviewKind.CATEGORY_CONFLICT,
                    link_id=c.link_id,
                    epoch=c.epoch,
                    now=c.now,
                    details={"canonical": c.canonical, "duplicate": c.duplicate},
                )
            )


def _carry_transfer(c: _Carry) -> None:
    """Virement interne du doublon reporté sur l'opération conservée ; conflit si les deux liens
    sont manuels."""
    transfer_duplicate = _active_transfer(c.connection, c.duplicate)
    transfer_canonical = _active_transfer(c.connection, c.canonical)
    if transfer_duplicate is not None:
        wins = transfer_canonical is None or (
            transfer_duplicate.method is LinkMode.MANUAL
            and transfer_canonical.method is not LinkMode.MANUAL
        )
        if wins:
            for superseded in (transfer_canonical, transfer_duplicate):
                if superseded is None:
                    continue
                c.connection.execute(
                    update(TransferLink)
                    .where(TransferLink.id == superseded.id)
                    .values(status=TransferStatus.SUPERSEDED, superseded_by_link_id=c.link_id)
                )
                c.changes["superseded_transfers"].append([superseded.id, str(superseded.status)])
            debit, credit = transfer_duplicate.debit_tx_id, transfer_duplicate.credit_tx_id
            result = c.connection.execute(
                insert(TransferLink).values(
                    debit_tx_id=c.canonical if debit == c.duplicate else debit,
                    credit_tx_id=c.canonical if credit == c.duplicate else credit,
                    method=transfer_duplicate.method,
                    status=transfer_duplicate.status,
                    carried_by_link_id=c.link_id,
                    created_at=c.now,
                    decided_at=transfer_duplicate.decided_at,
                )
            )
            c.changes["carried_transfers"].append(inserted_id(result))
        elif (
            transfer_canonical is not None
            and transfer_duplicate.method is LinkMode.MANUAL
            and transfer_canonical.method is LinkMode.MANUAL
        ):
            c.changes["reviews"].append(
                _open_conflict(
                    c.connection,
                    ReviewKind.TRANSFER_CONFLICT,
                    link_id=c.link_id,
                    epoch=c.epoch,
                    now=c.now,
                    details={"canonical": c.canonical, "duplicate": c.duplicate},
                )
            )


def carry_over(
    connection: Connection, *, link_id: int, canonical: int, duplicate: int, now: datetime
) -> dict[str, Any]:
    """Reporte catégorisations et virement du doublon sur la canonique ; renvoie les changements."""
    epoch = int(
        connection.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.object_id == link_id, AuditLog.action.in_(_CONFIRM_ACTIONS))
        ).scalar_one()
    )
    carry = _Carry(
        connection,
        link_id,
        epoch,
        canonical,
        duplicate,
        now,
        changes={
            "carried_categorizations": [],
            "superseded_categorizations": [],
            "carried_transfers": [],
            "superseded_transfers": [],
            "reviews": [],
        },
    )

    _carry_categorizations(carry)
    _carry_transfer(carry)
    return carry.changes


def _undo(connection: Connection, link_id: int, changes: dict[str, Any], now: datetime) -> None:
    for categorization_id in changes["carried_categorizations"]:
        connection.execute(
            update(Categorization)
            .where(Categorization.id == categorization_id)
            .values(superseded_at=now, superseded_by_link_id=link_id)
        )
    for categorization_id in changes["superseded_categorizations"]:
        connection.execute(
            update(Categorization)
            .where(Categorization.id == categorization_id)
            .values(superseded_at=None, superseded_by_link_id=None)
        )
    # D'abord désactiver les liens reportés, puis rétablir les anciens (unicité des liens actifs).
    for transfer_id in changes["carried_transfers"]:
        connection.execute(
            update(TransferLink)
            .where(TransferLink.id == transfer_id)
            .values(status=TransferStatus.SUPERSEDED, superseded_by_link_id=link_id)
        )
    for transfer_id, previous_status in changes["superseded_transfers"]:
        connection.execute(
            update(TransferLink)
            .where(TransferLink.id == transfer_id)
            .values(status=TransferStatus(previous_status), superseded_by_link_id=None)
        )
    for review_id in changes["reviews"]:
        connection.execute(
            update(ReviewItem)
            .where(ReviewItem.id == review_id)
            .values(status=ReviewStatus.DISMISSED, resolved_at=now)
        )


# --- Décisions -----------------------------------------------------------------------------------


def _load_link(connection: Connection, link_id: int) -> Any:
    link = connection.execute(select(DuplicateLink).where(DuplicateLink.id == link_id)).first()
    if link is None:
        raise DuplicateDecisionError(f"lien de doublon {link_id} introuvable")
    return link


def _close_link_review(
    connection: Connection, link_id: int, status: ReviewStatus, now: datetime
) -> None:
    connection.execute(
        update(ReviewItem)
        .where(
            ReviewItem.duplicate_link_id == link_id,
            ReviewItem.kind == ReviewKind.DUPLICATE,
            ReviewItem.status == ReviewStatus.OPEN,
        )
        .values(status=status, resolved_at=now)
    )


def confirm_link(engine: Engine, link_id: int, *, actor_user_id: int | None, now: datetime) -> None:
    with write_transaction(engine) as connection:
        link = _load_link(connection, link_id)
        if link.status is DuplicateStatus.CONFIRMED:
            return  # confirmer deux fois = confirmer une fois
        if link.status not in (DuplicateStatus.PROPOSED, DuplicateStatus.REVERTED):
            raise DuplicateDecisionError(f"lien {link_id} {link.status} : confirmation impossible")
        connection.execute(
            update(DuplicateLink)
            .where(DuplicateLink.id == link_id)
            .values(status=DuplicateStatus.CONFIRMED, mode=LinkMode.MANUAL, decided_at=now)
        )
        changes = carry_over(
            connection,
            link_id=link_id,
            canonical=link.canonical_tx_id,
            duplicate=link.duplicate_tx_id,
            now=now,
        )
        _close_link_review(connection, link_id, ReviewStatus.RESOLVED, now)
        audit(
            connection,
            "duplicate.confirmed",
            ("duplicate_link", link_id),
            now=now,
            actor_user_id=actor_user_id,
            details={"changes": changes},
        )


def reject_link(engine: Engine, link_id: int, *, actor_user_id: int | None, now: datetime) -> None:
    with write_transaction(engine) as connection:
        link = _load_link(connection, link_id)
        if link.status is not DuplicateStatus.PROPOSED:
            raise DuplicateDecisionError(f"lien {link_id} {link.status} : rejet impossible")
        connection.execute(
            update(DuplicateLink)
            .where(DuplicateLink.id == link_id)
            .values(status=DuplicateStatus.REJECTED, mode=LinkMode.MANUAL, decided_at=now)
        )
        _close_link_review(connection, link_id, ReviewStatus.DISMISSED, now)
        audit(
            connection,
            "duplicate.rejected",
            ("duplicate_link", link_id),
            now=now,
            actor_user_id=actor_user_id,
        )


def revert_link(engine: Engine, link_id: int, *, actor_user_id: int | None, now: datetime) -> None:
    """Annule une confirmation : l'état effectif antérieur est restauré exactement."""
    with write_transaction(engine) as connection:
        link = _load_link(connection, link_id)
        if link.status is not DuplicateStatus.CONFIRMED:
            raise DuplicateDecisionError(f"lien {link_id} {link.status} : annulation impossible")
        details: dict[str, Any] = connection.execute(
            select(AuditLog.details)
            .where(AuditLog.object_id == link_id, AuditLog.action.in_(_CONFIRM_ACTIONS))
            .order_by(AuditLog.id.desc())
            .limit(1)
        ).scalar_one()
        _undo(connection, link_id, details.get("changes", {}), now)
        connection.execute(
            update(DuplicateLink)
            .where(DuplicateLink.id == link_id)
            .values(status=DuplicateStatus.REVERTED, mode=LinkMode.MANUAL, decided_at=now)
        )
        audit(
            connection,
            "duplicate.reverted",
            ("duplicate_link", link_id),
            now=now,
            actor_user_id=actor_user_id,
        )
