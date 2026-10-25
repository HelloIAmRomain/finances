"""Virements internes entre comptes du périmètre (COL-06).

Un débit et un crédit de montants opposés, sur deux comptes différents, à ±3 jours :
- **correspondance forte** : l'IBAN de la contrepartie est celui de l'autre compte (index HMAC).
  Si un seul candidat fort existe, le lien est automatique et confirmé ;
- sinon (pas de preuve par l'IBAN, ou plusieurs candidats) : le candidat le plus proche est
  **proposé** dans la file de validation. Jamais d'association automatique ambiguë.

Seules les transactions effectives (hors doublons confirmés) sont considérées ; une transaction
appartient au plus à un lien actif ; un lien rejeté n'est jamais reproposé. Idempotent.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import Connection, Engine, func, insert, select, update

from app.audit import audit
from app.db import inserted_id, write_transaction
from app.ledger.views import is_effective
from app.models import Account, LedgerTransaction, ReviewItem, TransferLink
from app.models.categorize import ReviewKind, ReviewStatus
from app.models.ledger import LinkMode, TransferStatus
from app.timeutil import utcnow

WINDOW = timedelta(days=3)
_ACTIVE = (TransferStatus.PROPOSED, TransferStatus.CONFIRMED)


@dataclass(frozen=True)
class _Tx:
    id: int
    account_id: int
    amount: int
    booking_date: date
    counterparty_hash: str | None


@dataclass(frozen=True)
class TransferStats:
    confirmed: int
    proposed: int


def _existing_links(connection: Connection) -> tuple[set[int], set[tuple[int, int]]]:
    """(opérations déjà dans un lien actif, paires débit-crédit rejetées)."""
    busy: set[int] = set()
    refused: set[tuple[int, int]] = set()
    for debit_id, credit_id, status in connection.execute(
        select(TransferLink.debit_tx_id, TransferLink.credit_tx_id, TransferLink.status)
    ):
        if status in _ACTIVE:
            busy |= {debit_id, credit_id}
        elif status is TransferStatus.REJECTED:
            refused.add((debit_id, credit_id))
    return busy, refused


def _free_transactions(connection: Connection, busy: set[int]) -> list[_Tx]:
    rows = connection.execute(
        select(
            LedgerTransaction.id,
            LedgerTransaction.account_id,
            LedgerTransaction.amount_cents,
            LedgerTransaction.booking_date,
            LedgerTransaction.counterparty_iban_hash,
        )
        .where(is_effective())
        .order_by(LedgerTransaction.booking_date, LedgerTransaction.id)
    )
    return [_Tx(*row) for row in rows if row[0] not in busy]


def _best_credit(
    debit: _Tx,
    credits: list[_Tx],
    *,
    busy: set[int],
    refused: set[tuple[int, int]],
    own: dict[str, int],
) -> tuple[_Tx, bool] | None:
    """Crédit à lier au débit, et s'il est certain (seul candidat prouvé par l'IBAN)."""
    candidates = [
        credit
        for credit in credits
        if credit.id not in busy
        and credit.account_id != debit.account_id
        and abs(credit.booking_date - debit.booking_date) <= WINDOW
        and (debit.id, credit.id) not in refused
    ]
    if not candidates:
        return None
    strong = [
        credit
        for credit in candidates
        if own.get(debit.counterparty_hash or "") == credit.account_id
        or own.get(credit.counterparty_hash or "") == debit.account_id
    ]
    pool = strong or candidates
    chosen = min(pool, key=lambda c: (abs(c.booking_date - debit.booking_date), c.id))
    return chosen, len(strong) == 1


def _record_link(
    connection: Connection, debit: _Tx, credit: _Tx, *, certain: bool, now: datetime
) -> None:
    result = connection.execute(
        insert(TransferLink).values(
            debit_tx_id=debit.id,
            credit_tx_id=credit.id,
            method=LinkMode.AUTO,
            status=TransferStatus.CONFIRMED if certain else TransferStatus.PROPOSED,
            created_at=now,
            decided_at=now if certain else None,
        )
    )
    link_id = inserted_id(result)
    if certain:
        audit(
            connection, "transfer.auto_confirmed", ("transfer_link", link_id), now=now,
            actor_user_id=None,
        )  # fmt: skip
        return
    connection.execute(
        insert(ReviewItem).values(
            kind=ReviewKind.TRANSFER_CANDIDATE,
            dedupe_key=f"transfer_candidate:{debit.id}:{credit.id}",
            transaction_id=debit.id,
            other_transaction_id=credit.id,
            status=ReviewStatus.OPEN,
            details={"transfer_link_id": link_id},
            created_at=now,
        )
    )


def detect_transfers(engine: Engine) -> TransferStats:
    """Parcourt les débits du plus ancien au plus récent ; les crédits sont indexés par montant,
    le coût reste proportionnel au nombre d'opérations (10 ans d'historique compris)."""
    now = utcnow()
    confirmed = proposed = 0
    with write_transaction(engine) as connection:
        own = {
            str(iban_hash): int(account_id)
            for iban_hash, account_id in connection.execute(
                select(Account.iban_hash, Account.id).where(Account.iban_hash.is_not(None))
            )
        }
        busy, refused = _existing_links(connection)
        transactions = _free_transactions(connection, busy)
        credits_by_amount: dict[int, list[_Tx]] = defaultdict(list)
        for credit in (t for t in transactions if t.amount > 0):
            credits_by_amount[credit.amount].append(credit)
        for debit in (t for t in transactions if t.amount < 0):
            match = _best_credit(
                debit, credits_by_amount.get(-debit.amount, []), busy=busy, refused=refused, own=own
            )
            if match is None:
                continue
            credit, certain = match
            _record_link(connection, debit, credit, certain=certain, now=now)
            busy |= {debit.id, credit.id}
            confirmed, proposed = (
                (confirmed + 1, proposed) if certain else (confirmed, proposed + 1)
            )
    return TransferStats(confirmed=confirmed, proposed=proposed)


class TransferDecisionError(ValueError):
    """Transition impossible pour ce lien de virement."""


def decide_transfer(
    engine: Engine, link_id: int, *, confirm: bool, actor_user_id: int | None, now: datetime
) -> None:
    """Confirme ou rejette un virement interne proposé ; ferme sa proposition dans la file."""
    status = TransferStatus.CONFIRMED if confirm else TransferStatus.REJECTED
    with write_transaction(engine) as connection:
        result = connection.execute(
            update(TransferLink)
            .where(TransferLink.id == link_id, TransferLink.status == TransferStatus.PROPOSED)
            .values(status=status, method=LinkMode.MANUAL, decided_at=now)
        )
        if result.rowcount != 1:
            raise TransferDecisionError(f"virement {link_id} : aucune proposition en attente")
        connection.execute(
            update(ReviewItem)
            .where(
                ReviewItem.kind == ReviewKind.TRANSFER_CANDIDATE,
                ReviewItem.status == ReviewStatus.OPEN,
                func.json_extract(ReviewItem.details, "$.transfer_link_id") == link_id,
            )
            .values(
                status=ReviewStatus.RESOLVED if confirm else ReviewStatus.DISMISSED, resolved_at=now
            )
        )
        audit(
            connection,
            "transfer.confirmed" if confirm else "transfer.rejected",
            ("transfer_link", link_id),
            now=now,
            actor_user_id=actor_user_id,
        )
