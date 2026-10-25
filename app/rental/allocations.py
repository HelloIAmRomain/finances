"""Décisions de l'administrateur sur le rapprochement (LOC-06, L-05, L-06), toutes auditées.

- Confirmer ou refuser une affectation proposée (émetteur inconnu).
- Affecter à la main une transaction à une échéance, avec une justification d'écart facultative.
- Dissocier une affectation (elle n'est plus reproposée automatiquement).
- Trancher un trop-perçu : report sur l'échéance ouverte suivante, ou remboursement.
Aucune affectation ne dépasse le reste dû de l'échéance ni le montant de la transaction.
"""

from datetime import datetime

from sqlalchemy import Connection, Engine, func, insert, select, update

from app.audit import audit
from app.db import inserted_id, write_transaction
from app.models import LedgerTransaction, Overpayment, RentAllocation, RentDue
from app.models.rental import AllocationMethod, AllocationStatus, OverpaymentDecision


class AllocationError(ValueError):
    """Décision de rapprochement impossible."""


def _due_remaining(connection: Connection, due_id: int) -> int:
    amount = connection.scalar(select(RentDue.amount_cents).where(RentDue.id == due_id))
    if amount is None:
        raise AllocationError("Échéance introuvable.")
    paid = connection.scalar(
        select(func.coalesce(func.sum(RentAllocation.amount_cents), 0)).where(
            RentAllocation.due_id == due_id,
            RentAllocation.status == AllocationStatus.CONFIRMED,
        )
    )
    return int(amount) - int(paid or 0)


def _transaction_available(connection: Connection, transaction_id: int) -> int:
    amount = connection.scalar(
        select(LedgerTransaction.amount_cents).where(LedgerTransaction.id == transaction_id)
    )
    if amount is None or amount <= 0:
        raise AllocationError("Seul un encaissement (crédit) peut être affecté à une échéance.")
    used = connection.scalar(
        select(func.coalesce(func.sum(RentAllocation.amount_cents), 0)).where(
            RentAllocation.transaction_id == transaction_id,
            RentAllocation.status == AllocationStatus.CONFIRMED,
        )
    )
    over = connection.scalar(
        select(func.coalesce(func.sum(Overpayment.amount_cents), 0)).where(
            Overpayment.transaction_id == transaction_id
        )
    )
    return int(amount) - int(used or 0) - int(over or 0)


def confirm_proposal(
    engine: Engine, allocation_id: int, *, actor_user_id: int | None, now: datetime
) -> None:
    with write_transaction(engine) as connection:
        row = connection.execute(
            select(RentAllocation).where(RentAllocation.id == allocation_id)
        ).first()
        if row is None or row.status is not AllocationStatus.PROPOSED:
            raise AllocationError("Aucune proposition en attente à confirmer.")
        if row.amount_cents > _due_remaining(connection, row.due_id):
            raise AllocationError("L'échéance est déjà soldée : proposition sans objet.")
        connection.execute(
            update(RentAllocation)
            .where(RentAllocation.id == allocation_id)
            .values(status=AllocationStatus.CONFIRMED, decided_at=now)
        )
        audit(
            connection,
            "rent.proposal_confirmed",
            ("rent_allocation", allocation_id),
            now=now,
            actor_user_id=actor_user_id,
            details={"echeance": row.due_id, "transaction": row.transaction_id},
        )


def reject_proposal(
    engine: Engine, allocation_id: int, *, actor_user_id: int | None, now: datetime
) -> None:
    with write_transaction(engine) as connection:
        result = connection.execute(
            update(RentAllocation)
            .where(
                RentAllocation.id == allocation_id,
                RentAllocation.status == AllocationStatus.PROPOSED,
            )
            .values(status=AllocationStatus.REJECTED, decided_at=now)
        )
        if result.rowcount != 1:
            raise AllocationError("Aucune proposition en attente à refuser.")
        audit(
            connection,
            "rent.proposal_rejected",
            ("rent_allocation", allocation_id),
            now=now,
            actor_user_id=actor_user_id,
            details={},
        )


def allocate_manually(
    engine: Engine,
    *,
    due_id: int,
    transaction_id: int,
    amount_cents: int | None,
    note: str | None,
    actor_user_id: int | None,
    now: datetime,
) -> int:
    """Affecte `amount_cents` (par défaut : le maximum possible) de la transaction à l'échéance."""
    with write_transaction(engine) as connection:
        available = _transaction_available(connection, transaction_id)
        remaining = _due_remaining(connection, due_id)
        amount = min(available, remaining) if amount_cents is None else amount_cents
        if amount <= 0:
            raise AllocationError("Rien à affecter : échéance soldée ou transaction déjà utilisée.")
        if amount > available or amount > remaining:
            raise AllocationError(
                "Montant trop élevé : il dépasse le reste dû ou le disponible de la transaction."
            )
        result = connection.execute(
            insert(RentAllocation).values(
                due_id=due_id,
                transaction_id=transaction_id,
                amount_cents=amount,
                status=AllocationStatus.CONFIRMED,
                method=AllocationMethod.MANUAL,
                reason="manuel",
                note=(note or "").strip() or None,
                created_at=now,
                decided_at=now,
            )
        )
        allocation_id = inserted_id(result)
        audit(
            connection,
            "rent.allocated",
            ("rent_allocation", allocation_id),
            now=now,
            actor_user_id=actor_user_id,
            details={"echeance": due_id, "transaction": transaction_id, "montant": amount},
        )
        return allocation_id


def dissociate(
    engine: Engine,
    allocation_id: int,
    *,
    note: str | None,
    actor_user_id: int | None,
    now: datetime,
) -> None:
    with write_transaction(engine) as connection:
        result = connection.execute(
            update(RentAllocation)
            .where(
                RentAllocation.id == allocation_id,
                RentAllocation.status == AllocationStatus.CONFIRMED,
            )
            .values(
                status=AllocationStatus.REJECTED, decided_at=now, note=(note or "").strip() or None
            )
        )
        if result.rowcount != 1:
            raise AllocationError("Affectation introuvable ou déjà annulée.")
        audit(
            connection,
            "rent.dissociated",
            ("rent_allocation", allocation_id),
            now=now,
            actor_user_id=actor_user_id,
            details={},
        )


def decide_overpayment(
    engine: Engine,
    overpayment_id: int,
    decision: OverpaymentDecision,
    *,
    actor_user_id: int | None,
    now: datetime,
) -> None:
    if decision is OverpaymentDecision.PENDING:
        raise AllocationError("Décision attendue : report ou remboursement.")
    with write_transaction(engine) as connection:
        over = connection.execute(
            select(Overpayment).where(Overpayment.id == overpayment_id)
        ).first()
        if over is None or over.decision is not OverpaymentDecision.PENDING:
            raise AllocationError("Trop-perçu introuvable ou déjà traité.")
        if decision is OverpaymentDecision.CARRY_FORWARD:
            _carry_forward(connection, over, now)
        connection.execute(
            update(Overpayment)
            .where(Overpayment.id == overpayment_id)
            .values(decision=decision, decided_at=now)
        )
        audit(
            connection,
            "rent.overpayment",
            ("overpayment", overpayment_id),
            now=now,
            actor_user_id=actor_user_id,
            details={"decision": str(decision)},
        )


def _carry_forward(connection: Connection, over: object, now: datetime) -> None:
    """L'excédent solde les échéances ouvertes suivantes du bail, dans l'ordre."""
    lease_id: int = over.lease_id  # type: ignore[attr-defined]
    left: int = over.amount_cents  # type: ignore[attr-defined]
    transaction_id: int = over.transaction_id  # type: ignore[attr-defined]
    for due_id in connection.scalars(
        select(RentDue.id).where(RentDue.lease_id == lease_id).order_by(RentDue.period)
    ):
        if left == 0:
            break
        remaining = _due_remaining(connection, due_id)
        if remaining <= 0:
            continue
        part = min(left, remaining)
        connection.execute(
            insert(RentAllocation).values(
                due_id=due_id,
                transaction_id=transaction_id,
                amount_cents=part,
                status=AllocationStatus.CONFIRMED,
                method=AllocationMethod.MANUAL,
                reason="report",
                created_at=now,
                decided_at=now,
            )
        )
        left -= part
    if left:
        raise AllocationError(
            "Aucune échéance ouverte pour reporter le trop-perçu : attendre la suivante ou "
            "choisir le remboursement."
        )
