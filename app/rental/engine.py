"""Moteur des loyers (LOC-03, LOC-04, LOC-05, LOC-14, Q16, Q17) : un passage complet, rejouable.

À chaque passage (après les synchros, et à la demande) :
1. **Échéances** : une par mois et par bail, du début du bail au mois courant, plus le mois
   suivant à partir du 25 (paiements anticipés). Contrainte unique : jamais de doublon (inv. 3).
2. **Rejets** : un débit du compte d'encaissement dont le libellé signale un retour ou un rejet,
   du même montant qu'un virement affecté dans les 45 jours précédents, annule cette affectation
   (une seule fois : l'écriture inverse est mémorisée).
3. **Rapprochement** des crédits non encore traités du compte d'encaissement :
   - payeur reconnu (IBAN, ou motif de libellé, ou nom) : affectation **confirmée** aux échéances
     ouvertes, de la plus ancienne à la plus récente (paiement tardif, virement couvrant plusieurs
     mois) ; l'affectation est plafonnée au reste dû, l'excédent est un **trop-perçu** à décider ;
   - payeur inconnu mais montant exact d'une échéance ouverte : affectation **proposée**, à valider,
     jamais silencieuse ;
   - sinon : ce n'est pas un loyer.
   Une échéance est ouverte au rapprochement à partir du 25 du mois précédent.
4. **États** (fonction pure, `status.py`), **incidents** (retard, impayé, caution, rejet,
   trop-perçu, conservés même après paiement) et **alertes** (une par fait, par épisode).

Les transactions ne sont jamais modifiées (inv. 4) ; les comptes ignorés et les doublons confirmés
sont exclus.
"""

import logging
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import CompoundSelect, Connection, Engine, func, select, union, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.alerts.engine import open_keys, raise_alert, resolve_by_key
from app.db import write_transaction
from app.ledger.views import is_effective, visible_accounts
from app.models import (
    DepositEvent,
    DuplicateLink,
    Lease,
    LedgerTransaction,
    Overpayment,
    Party,
    RentAllocation,
    RentDue,
    RentIncident,
)
from app.models.alerts import Severity
from app.models.ledger import DuplicateStatus
from app.models.rental import (
    AllocationMethod,
    AllocationStatus,
    IncidentKind,
    LeaseKind,
    OverpaymentDecision,
    PartyRole,
)
from app.months import month_of, month_start, next_month
from app.rental.expenses import apply_rules as apply_expense_rules
from app.rental.leases import normalize_label, party_patterns
from app.rental.status import DueStatus, DueTerms, RentState, compute_status
from app.timeutil import paris_date

log = logging.getLogger(__name__)

EARLY_PAYMENT_DAY = 25  # une échéance s'ouvre au rapprochement le 25 du mois précédent
REJECTION_WINDOW = timedelta(days=45)
REJECTION_WORDS = ("REJET", "RETOUR", "IMPAYE", "RENVOI", "ANNULATION")
REPEATED_LATE_WINDOW = timedelta(days=183)  # six mois glissants
REPEATED_LATE_COUNT = 2
CUMULATED_UNPAID_COUNT = 2
EARLY_LOOKBACK = timedelta(days=7)  # paiement anticipé du premier loyer


@dataclass
class RunStats:
    dues_created: int = 0
    confirmed: int = 0
    proposed: int = 0
    overpayments: int = 0
    rejections: int = 0
    alerts: int = 0
    expenses_linked: int = 0
    states: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class _Payer:
    role: PartyRole
    iban_hash: str | None
    patterns: tuple[str, ...]


def opens_on(period: str) -> date:
    """Premier jour où un paiement peut être affecté à cette échéance (le 25 du mois d'avant)."""
    return (month_start(period) - timedelta(days=1)).replace(day=EARLY_PAYMENT_DAY)


def first_tracked_day(lease: Any) -> date:
    """Début du bail, ou début du suivi s'il est postérieur."""
    tracked: date | None = lease.tracking_start
    start: date = lease.start_date
    return max(start, tracked) if tracked else start


def _terms(lease: Any, due: Any) -> DueTerms:
    return DueTerms(due.period, due.amount_cents, lease.alert_day, lease.unpaid_day)


# --- 1. Échéances -----------------------------------------------------------------------------


def _generate_dues(connection: Connection, lease: Any, today: date) -> int:
    last = month_of(today)
    if today.day >= EARLY_PAYMENT_DAY:
        last = next_month(last)
    if lease.end_date is not None:
        last = min(last, month_of(lease.end_date))
    created, period = 0, month_of(first_tracked_day(lease))
    while period <= last:
        due_date = month_start(period).replace(day=lease.due_day)
        result = connection.execute(
            sqlite_insert(RentDue)
            .values(
                lease_id=lease.id,
                period=period,
                due_date=due_date,
                amount_cents=lease.rent_cents + lease.charges_cents,
                charges_cents=lease.charges_cents,
            )
            .on_conflict_do_nothing(index_elements=["lease_id", "period"])
        )
        created += result.rowcount
        period = next_month(period)
    return created


# --- 2 et 3. Rejets et rapprochement ----------------------------------------------------------


def _eligible_transactions(connection: Connection, lease: Any, credit: bool) -> list[Any]:
    used = union(
        select(RentAllocation.transaction_id.label("tx")),
        select(Overpayment.transaction_id.label("tx")),
    ).subquery()
    # Dépôt de garantie (encaissé ou restitué) : jamais un loyer.
    deposits = select(DepositEvent.transaction_id).where(DepositEvent.transaction_id.is_not(None))
    confirmed_link = DuplicateLink.status == DuplicateStatus.CONFIRMED
    handled: CompoundSelect[int] = union(
        select(used.c.tx),
        select(RentAllocation.cancelled_by_transaction_id).where(
            RentAllocation.cancelled_by_transaction_id.is_not(None)
        ),
        # L-12 : une opération affectée puis reconnue comme doublon d'une autre (API et fichier)
        # garde son affectation ; l'autre exemplaire n'est pas rapproché une seconde fois.
        select(DuplicateLink.canonical_tx_id).where(
            confirmed_link, DuplicateLink.duplicate_tx_id.in_(select(used.c.tx))
        ),
    )
    sign = LedgerTransaction.amount_cents > 0 if credit else LedgerTransaction.amount_cents < 0
    return list(
        connection.execute(
            select(
                LedgerTransaction.id,
                LedgerTransaction.booking_date,
                LedgerTransaction.amount_cents,
                LedgerTransaction.label_raw,
                LedgerTransaction.counterparty_iban_hash,
                LedgerTransaction.counterparty_name_enc,
            )
            .where(
                LedgerTransaction.account_id == lease.account_id,
                LedgerTransaction.account_id.in_(visible_accounts()),
                is_effective(),
                LedgerTransaction.id.not_in(handled),
                LedgerTransaction.id.not_in(deposits),
                LedgerTransaction.booking_date >= first_tracked_day(lease) - EARLY_LOOKBACK,
                sign,
            )
            .order_by(LedgerTransaction.booking_date, LedgerTransaction.id)
        )
    )


def _payers(connection: Connection, lease_id: int) -> list[_Payer]:
    return [
        _Payer(
            role=party.role,
            iban_hash=party.iban_hash,
            patterns=tuple(
                dict.fromkeys(
                    [*party_patterns(party.label_patterns_enc), normalize_label(party.name_enc)]
                )
            ),
        )
        for party in connection.execute(select(Party).where(Party.lease_id == lease_id))
    ]


def _identify(transaction: Any, payers: list[_Payer]) -> tuple[_Payer, str] | None:
    label = normalize_label(transaction.label_raw or "")
    name = normalize_label(transaction.counterparty_name_enc or "")
    for payer in payers:  # l'IBAN d'abord : c'est la preuve la plus forte
        if payer.iban_hash and payer.iban_hash == transaction.counterparty_iban_hash:
            return payer, "iban"
    for payer in payers:
        for pattern in payer.patterns:
            if pattern and (pattern in label or (name and pattern in name)):
                return payer, "motif"
    return None


def _open_dues(connection: Connection, lease_id: int) -> list[tuple[Any, int]]:
    """(échéance, reste dû) des échéances non soldées, de la plus ancienne à la plus récente."""
    paid = dict(
        connection.execute(
            select(RentAllocation.due_id, func.sum(RentAllocation.amount_cents))
            .join(RentDue, RentDue.id == RentAllocation.due_id)
            .where(
                RentDue.lease_id == lease_id,
                RentAllocation.status == AllocationStatus.CONFIRMED,
            )
            .group_by(RentAllocation.due_id)
        ).all()
    )
    dues = connection.execute(
        select(RentDue).where(RentDue.lease_id == lease_id).order_by(RentDue.period)
    ).all()
    return [
        (due, due.amount_cents - int(paid.get(due.id, 0)))
        for due in dues
        if due.amount_cents - int(paid.get(due.id, 0)) > 0
    ]


def _cancel_rejections(connection: Connection, lease: Any, now: datetime) -> list[tuple[int, int]]:
    """Annule les affectations dont le virement a été rejeté ; renvoie (échéance, débit)."""
    cancelled = []
    # Seul un débit du montant d'un paiement affecté peut l'annuler : les autres sont écartés
    # avant toute analyse de libellé (des milliers de dépenses ordinaires sur un compte courant).
    paid_amounts = set(
        connection.scalars(
            select(LedgerTransaction.amount_cents)
            .join(RentAllocation, RentAllocation.transaction_id == LedgerTransaction.id)
            .where(
                RentAllocation.status == AllocationStatus.CONFIRMED,
                LedgerTransaction.account_id == lease.account_id,
            )
        )
    )
    for debit in _eligible_transactions(connection, lease, credit=False):
        if -debit.amount_cents not in paid_amounts:
            continue
        label = normalize_label(debit.label_raw or "")
        if not any(word in label for word in REJECTION_WORDS):
            continue
        target = connection.execute(
            select(RentAllocation.id, RentAllocation.due_id)
            .join(LedgerTransaction, LedgerTransaction.id == RentAllocation.transaction_id)
            .where(
                RentAllocation.status == AllocationStatus.CONFIRMED,
                LedgerTransaction.account_id == lease.account_id,
                LedgerTransaction.amount_cents == -debit.amount_cents,
                LedgerTransaction.booking_date <= debit.booking_date,
                LedgerTransaction.booking_date >= debit.booking_date - REJECTION_WINDOW,
            )
            .order_by(LedgerTransaction.booking_date.desc())
            .limit(1)
        ).first()
        if target is None:
            continue
        connection.execute(
            update(RentAllocation)
            .where(RentAllocation.id == target.id)
            .values(
                status=AllocationStatus.CANCELLED,
                cancelled_by_transaction_id=debit.id,
                decided_at=now,
            )
        )
        cancelled.append((target.due_id, debit.id))
    return cancelled


def _allocate(connection: Connection, lease: Any, now: datetime, stats: RunStats) -> None:
    payers = _payers(connection, lease.id)
    open_dues = _open_dues(connection, lease.id)
    remaining = {due.id: rest for due, rest in open_dues}
    # Échéances de la plus ancienne à la plus récente : celles déjà ouvertes au paiement forment
    # un préfixe de la liste, trouvé par dichotomie.
    openings = [opens_on(due.period) for due, _ in open_dues]
    for transaction in _eligible_transactions(connection, lease, credit=True):
        opened = open_dues[: bisect_right(openings, transaction.booking_date)]
        eligible = [due for due, _ in opened if remaining[due.id] > 0]
        found = _identify(transaction, payers)
        if found is None:
            # Émetteur inconnu : seulement un montant exact, et seulement proposé.
            exact = next((d for d in eligible if remaining[d.id] == transaction.amount_cents), None)
            if exact is not None:
                connection.execute(
                    sqlite_insert(RentAllocation).values(
                        due_id=exact.id,
                        transaction_id=transaction.id,
                        amount_cents=transaction.amount_cents,
                        status=AllocationStatus.PROPOSED,
                        method=AllocationMethod.AUTO,
                        reason="montant",
                        created_at=now,
                    )
                )
                stats.proposed += 1
            continue
        payer, reason = found
        left = transaction.amount_cents
        for due in eligible:
            if left == 0:
                break
            part = min(left, remaining[due.id])
            connection.execute(
                sqlite_insert(RentAllocation).values(
                    due_id=due.id,
                    transaction_id=transaction.id,
                    amount_cents=part,
                    status=AllocationStatus.CONFIRMED,
                    method=AllocationMethod.AUTO,
                    payer_role=payer.role,
                    reason=reason,
                    created_at=now,
                    decided_at=now,
                )
            )
            remaining[due.id] -= part
            left -= part
            stats.confirmed += 1
        if left > 0:
            connection.execute(
                sqlite_insert(Overpayment).values(
                    lease_id=lease.id,
                    transaction_id=transaction.id,
                    amount_cents=left,
                    decision=OverpaymentDecision.PENDING,
                    created_at=now,
                )
            )
            stats.overpayments += 1


# --- 4. États, incidents, alertes ------------------------------------------------------------


def _incident(
    connection: Connection,
    due: tuple[int, int],
    kind: IncidentKind,
    *,
    occurrence: int,
    on: date,
    details: dict[str, Any] | None = None,
) -> bool:
    """`due` : (bail, échéance). Une ligne par (échéance, nature, épisode) : rejouable."""
    lease_id, due_id = due
    result = connection.execute(
        sqlite_insert(RentIncident)
        .values(
            lease_id=lease_id,
            due_id=due_id,
            kind=kind,
            occurrence=occurrence,
            occurred_on=on,
            details=details or {},
        )
        .on_conflict_do_nothing(index_elements=["due_id", "kind", "occurrence"])
    )
    return bool(result.rowcount)


@dataclass(frozen=True)
class _DueView:
    due: Any
    status: DueStatus
    last_payment: date | None
    episode: int  # 1 + nombre d'affectations annulées (rejets)
    guarantor_paid: bool


def _due_views(connection: Connection, lease: Any, today: date) -> list[_DueView]:
    rows = connection.execute(
        select(
            RentAllocation.due_id,
            RentAllocation.status,
            RentAllocation.amount_cents,
            RentAllocation.payer_role,
            LedgerTransaction.booking_date,
        )
        .join(RentDue, RentDue.id == RentAllocation.due_id)
        .join(LedgerTransaction, LedgerTransaction.id == RentAllocation.transaction_id)
        .where(RentDue.lease_id == lease.id)
    ).all()
    by_due: dict[int, list[Any]] = defaultdict(list)
    for row in rows:
        by_due[row.due_id].append(row)
    views = []
    for due in connection.execute(
        select(RentDue).where(RentDue.lease_id == lease.id).order_by(RentDue.period)
    ):
        allocations = by_due.get(due.id, [])
        confirmed = [a for a in allocations if a.status is AllocationStatus.CONFIRMED]
        views.append(
            _DueView(
                due=due,
                status=compute_status(
                    _terms(lease, due), [a.amount_cents for a in confirmed], today
                ),
                last_payment=max((a.booking_date for a in confirmed), default=None),
                episode=1 + sum(1 for a in allocations if a.status is AllocationStatus.CANCELLED),
                guarantor_paid=any(a.payer_role is PartyRole.GUARANTOR for a in confirmed),
            )
        )
    return views


REJECTION_ALERT_WINDOW = timedelta(days=31)  # reprise après un arrêt, sans tout relire
TITLES = {
    LeaseKind.RENT: {
        "retard": "Loyer non reçu",
        "impaye": "Loyer impayé",
        "partiel": "Loyer reçu en partie",
        "caution": "Loyer payé par la caution",
        "rejete": "Virement de loyer rejeté",
        "trop_percu": "Loyer : trop-perçu à traiter",
        "a_valider": "Loyer reçu d'un émetteur inconnu : à valider",
        "retards_repetes": "Retards répétés sur un bail",
        "impayes_cumules": "Plusieurs loyers impayés",
    },
    LeaseKind.LOAN: {
        "retard": "Remboursement non reçu",
        "impaye": "Remboursement impayé",
        "partiel": "Remboursement reçu en partie",
        "caution": "Remboursement payé par un tiers",
        "rejete": "Virement de remboursement rejeté",
        "trop_percu": "Remboursement : trop-perçu à traiter",
        "a_valider": "Remboursement d'un émetteur inconnu : à valider",
        "retards_repetes": "Retards répétés sur un remboursement",
        "impayes_cumules": "Plusieurs remboursements impayés",
    },
}
STATE_ALERTS = {
    RentState.PARTIEL: ("partiel", Severity.WARNING),
    RentState.EN_RETARD: ("retard", Severity.WARNING),
    RentState.IMPAYE: ("impaye", Severity.CRITICAL),
}


def _alerts_for_lease(
    engine: Engine, lease: Any, views: list[_DueView], today: date, now: datetime
) -> int:
    titles = TITLES[lease.kind]
    raised = 0

    def alert(name: str, severity: Severity, key: str, details: dict[str, Any]) -> None:
        nonlocal raised
        result = raise_alert(
            engine,
            type=f"loyer_{name}" if not name.endswith(("repetes", "cumules")) else f"bail_{name}",
            severity=severity,
            key=key,
            title=titles[name],
            subject=("lease", lease.id),
            details={"bail": lease.label, **details},
            now=now,
        )
        raised += result.created

    for view in views:
        due, status = view.due, view.status
        wanted = STATE_ALERTS.get(status.state)
        for name, severity in STATE_ALERTS.values():
            key = f"rent:{due.id}:{name}"
            if wanted is not None and wanted[0] == name:
                alert(name, severity, key, {"periode": due.period, "reste_du": status.remaining})
            else:
                resolve_by_key(engine, key, now=now)
        if view.guarantor_paid:
            alert("caution", Severity.WARNING, f"rent:{due.id}:caution", {"periode": due.period})
    late = sum(
        1
        for view in views
        if view.last_payment is not None
        and view.status.state is RentState.PAYE
        and view.last_payment >= _terms(lease, view.due).alert_date
        and view.last_payment >= today - REPEATED_LATE_WINDOW
    )
    key = f"lease:{lease.id}:retards_repetes"
    if late >= REPEATED_LATE_COUNT:
        alert("retards_repetes", Severity.WARNING, key, {"retards_6_mois": late})
    else:
        resolve_by_key(engine, key, now=now)
    unpaid = sum(1 for view in views if view.status.state is RentState.IMPAYE)
    key = f"lease:{lease.id}:impayes_cumules"
    if unpaid >= CUMULATED_UNPAID_COUNT:
        alert("impayes_cumules", Severity.CRITICAL, key, {"impayes": unpaid})
    else:
        resolve_by_key(engine, key, now=now)
    return raised


def _record_incidents(
    connection: Connection, lease: Any, views: list[_DueView], today: date
) -> None:
    for view in views:
        due, terms = view.due, _terms(lease, view.due)
        paid_on = view.last_payment if view.status.state is RentState.PAYE else None
        if paid_on is not None and paid_on >= terms.alert_date:
            _incident(
                connection,
                (lease.id, due.id),
                IncidentKind.LATE,
                occurrence=view.episode,
                on=paid_on,
            )
        unpaid_seen = (paid_on is None and today >= terms.unpaid_date) or (
            paid_on is not None and paid_on >= terms.unpaid_date
        )
        if unpaid_seen:
            _incident(
                connection,
                (lease.id, due.id),
                IncidentKind.UNPAID,
                occurrence=view.episode,
                on=terms.unpaid_date,
            )
        if view.guarantor_paid and paid_on is not None:
            _incident(
                connection,
                (lease.id, due.id),
                IncidentKind.GUARANTOR,
                occurrence=view.episode,
                on=paid_on,
            )


def run_rental(engine: Engine, *, now: datetime) -> RunStats:
    today = paris_date(now)
    stats = RunStats()
    with write_transaction(engine) as connection:
        leases = connection.execute(select(Lease).order_by(Lease.id)).all()
        for lease in leases:
            if lease.start_date > today + timedelta(days=31):
                continue
            stats.dues_created += _generate_dues(connection, lease, today)
            for due_id, debit_id in _cancel_rejections(connection, lease, now):
                # Incident écrit avec l'annulation, dans la même transaction : après un arrêt
                # entre les deux, le rejet serait déjà « traité » et son alerte perdue.
                _record_rejection(connection, lease.id, due_id, debit_id, today)
                stats.rejections += 1
            _allocate(connection, lease, now, stats)
        stats.expenses_linked = apply_expense_rules(connection)
    stats.alerts += _rejection_alerts(engine, today, now)
    for lease in leases:
        with write_transaction(engine) as connection:
            views = _due_views(connection, lease, today)
            _record_incidents(connection, lease, views, today)
        stats.alerts += _alerts_for_lease(engine, lease, views, today, now)
        for view in views:
            stats.states[str(view.status.state)] = stats.states.get(str(view.status.state), 0) + 1
    stats.alerts += _pending_decisions(engine, now)
    return stats


def _record_rejection(
    connection: Connection, lease_id: int, due_id: int, debit_id: int, today: date
) -> None:
    occurrence = 1 + int(
        connection.scalar(
            select(func.count()).where(
                RentIncident.due_id == due_id, RentIncident.kind == IncidentKind.REJECTED
            )
        )
        or 0
    )
    _incident(
        connection,
        (lease_id, due_id),
        IncidentKind.REJECTED,
        occurrence=occurrence,
        on=today,
        details={"ecriture_inverse": debit_id},
    )


def _rejection_alerts(engine: Engine, today: date, now: datetime) -> int:
    """Une alerte par rejet récent, tirée des incidents (rejouable : un rejet n'est signalé
    qu'une fois, même après résolution de l'alerte)."""
    with engine.connect() as connection:
        rejected = connection.execute(
            select(RentIncident.due_id, RentIncident.details, Lease.id, Lease.kind, Lease.label)
            .join(Lease, Lease.id == RentIncident.lease_id)
            .where(
                RentIncident.kind == IncidentKind.REJECTED,
                RentIncident.occurred_on >= today - REJECTION_ALERT_WINDOW,
            )
        ).all()
    raised = 0
    for due_id, details, lease_id, kind, label in rejected:
        result = raise_alert(
            engine,
            type="loyer_rejete",
            severity=Severity.CRITICAL,
            key=f"rent:{due_id}:rejete:{details.get('ecriture_inverse')}",
            title=TITLES[kind]["rejete"],
            subject=("lease", lease_id),
            details={"bail": label},
            now=now,
            once=True,
        )
        raised += result.created
    return raised


def _proposal_alerts(engine: Engine, now: datetime) -> int:
    """Une alerte par paiement proposé (montant seul) à valider, résolue après décision."""
    with engine.connect() as connection:
        proposals = connection.execute(
            select(
                RentAllocation.id,
                RentAllocation.status,
                RentDue.period,
                Lease.id.label("lease_id"),
                Lease.kind,
                Lease.label,
            )
            .join(RentDue, RentDue.id == RentAllocation.due_id)
            .join(Lease, Lease.id == RentDue.lease_id)
            .where(RentAllocation.reason == "montant")
        ).all()
    still_open = open_keys(engine, "rent-match:")
    raised = 0
    for row in proposals:
        key = f"rent-match:{row.id}"
        if row.status is AllocationStatus.PROPOSED:
            raised += raise_alert(
                engine,
                type="loyer_a_valider",
                severity=Severity.INFO,
                key=key,
                title=TITLES[row.kind]["a_valider"],
                subject=("lease", row.lease_id),
                details={"bail": row.label, "periode": row.period},
                now=now,
            ).created
        elif key in still_open:
            resolve_by_key(engine, key, now=now)
    return raised


def _overpayment_alerts(engine: Engine, now: datetime) -> int:
    """Une alerte et un incident par trop-perçu à décider, alerte résolue après décision."""
    with engine.connect() as connection:
        overpayments = connection.execute(
            select(
                Overpayment.id,
                Overpayment.transaction_id,
                Overpayment.amount_cents,
                Overpayment.decision,
                Overpayment.created_at,
                Lease.id.label("lease_id"),
                Lease.kind,
                Lease.label,
            ).join(Lease, Lease.id == Overpayment.lease_id)
        ).all()
        first_due = dict(
            connection.execute(
                select(RentAllocation.transaction_id, func.max(RentAllocation.due_id))
                .where(RentAllocation.transaction_id.in_(select(Overpayment.transaction_id)))
                .group_by(RentAllocation.transaction_id)
            ).all()
        )
    still_open = open_keys(engine, "overpayment:")
    raised = 0
    for over in overpayments:
        key = f"overpayment:{over.id}"
        if over.decision is not OverpaymentDecision.PENDING:
            if key in still_open:
                resolve_by_key(engine, key, now=now)
            continue
        raised += raise_alert(
            engine,
            type="loyer_trop_percu",
            severity=Severity.INFO,
            key=key,
            title=TITLES[over.kind]["trop_percu"],
            subject=("lease", over.lease_id),
            details={"bail": over.label, "excedent": over.amount_cents},
            now=now,
        ).created
        due_id = first_due.get(over.transaction_id)
        if due_id is not None:
            with write_transaction(engine) as connection:
                _incident(
                    connection,
                    (over.lease_id, due_id),
                    IncidentKind.OVERPAID,
                    occurrence=1,
                    on=paris_date(over.created_at),
                    details={"excedent": over.amount_cents},
                )
    return raised


def _pending_decisions(engine: Engine, now: datetime) -> int:
    """Propositions à valider et trop-perçus à décider : une alerte chacun, résolue une fois
    la décision prise."""
    return _proposal_alerts(engine, now) + _overpayment_alerts(engine, now)
