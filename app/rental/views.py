"""Lectures pour l'interface des loyers (LOC-04, LOC-06, LOC-14) : états dérivés, affectations,
propositions, trop-perçus, incidents, opérations affectables. Aucune écriture ici."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.ledger.views import is_effective, visible_accounts
from app.models import (
    Account,
    Lease,
    LedgerTransaction,
    Overpayment,
    Party,
    Property,
    PropertyExpense,
    RentAllocation,
    RentDue,
    RentIncident,
)
from app.models.rental import (
    AllocationStatus,
    IncidentKind,
    LeaseKind,
    OverpaymentDecision,
    PartyRole,
)
from app.rental.leases import party_patterns
from app.rental.status import DueTerms, RentState, compute_status

STATE_LABELS = {
    RentState.ATTENDU: "attendu",
    RentState.PARTIEL: "partiel",
    RentState.EN_RETARD: "en retard",
    RentState.IMPAYE: "impayé",
    RentState.PAYE: "payé",
}
INCIDENT_LABELS = {
    IncidentKind.LATE: "paiement en retard",
    IncidentKind.UNPAID: "impayé",
    IncidentKind.GUARANTOR: "payé par la caution",
    IncidentKind.REJECTED: "virement rejeté",
    IncidentKind.OVERPAID: "trop-perçu",
}
ROLE_LABELS = {
    PartyRole.TENANT: "locataire",
    PartyRole.GUARANTOR: "caution",
    PartyRole.THIRD_PAYER: "tiers payeur",
    PartyRole.BORROWER: "emprunteur",
}
KIND_LABELS = {LeaseKind.RENT: "Loyer", LeaseKind.LOAN: "Remboursement attendu"}


@dataclass(frozen=True)
class AllocationRow:
    id: int
    transaction_id: int
    booking_date: date
    label: str
    amount_cents: int
    status: AllocationStatus
    method: str
    note: str | None


@dataclass(frozen=True)
class DueRow:
    id: int
    period: str
    due_date: date
    amount_cents: int
    paid_cents: int
    remaining_cents: int
    state: RentState
    allocations: list[AllocationRow]


@dataclass(frozen=True)
class PartyRow:
    role: PartyRole
    name: str
    iban_masked: str | None
    patterns: list[str]


@dataclass
class LeaseView:
    id: int
    kind: LeaseKind
    label: str
    property_id: int | None
    property_name: str | None
    account_name: str
    start_date: date
    end_date: date | None
    tracking_start: date | None
    monthly_cents: int
    rent_cents: int
    charges_cents: int
    due_day: int
    alert_day: int
    unpaid_day: int
    dues: list[DueRow] = field(default_factory=list)
    parties: list[PartyRow] = field(default_factory=list)
    incidents: list[tuple[date, str, str]] = field(default_factory=list)
    account_id: int = 0  # compte d'encaissement (L-10)

    @property
    def current(self) -> DueRow | None:
        return self.dues[-1] if self.dues else None


def _allocations(engine: Engine, due_ids: list[int]) -> dict[int, list[AllocationRow]]:
    with engine.connect() as connection:
        rows = connection.execute(
            select(
                RentAllocation.id,
                RentAllocation.due_id,
                RentAllocation.transaction_id,
                LedgerTransaction.booking_date,
                LedgerTransaction.label_clean,
                RentAllocation.amount_cents,
                RentAllocation.status,
                RentAllocation.method,
                RentAllocation.note,
            )
            .join(LedgerTransaction, LedgerTransaction.id == RentAllocation.transaction_id)
            .where(RentAllocation.due_id.in_(due_ids))
            .order_by(LedgerTransaction.booking_date, RentAllocation.id)
        ).all()
    result: dict[int, list[AllocationRow]] = {}
    for row in rows:
        result.setdefault(row.due_id, []).append(
            AllocationRow(
                id=row.id,
                transaction_id=row.transaction_id,
                booking_date=row.booking_date,
                label=row.label_clean,
                amount_cents=row.amount_cents,
                status=row.status,
                method=str(row.method),
                note=row.note,
            )
        )
    return result


def _mask(iban: str | None) -> str | None:
    return f"{iban[:4]} •••• {iban[-4:]}" if iban else None


def lease_views(engine: Engine, today: date, lease_id: int | None = None) -> list[LeaseView]:
    query = (
        select(Lease, Property.name.label("property_name"), Account.name.label("account_name"))
        .outerjoin(Property, Property.id == Lease.property_id)
        .join(Account, Account.id == Lease.account_id)
        .order_by(Lease.kind, Lease.label, Lease.id)
    )
    if lease_id is not None:
        query = query.where(Lease.id == lease_id)
    with Session(engine) as session:
        leases = session.execute(query).all()
    lease_ids = [row.Lease.id for row in leases]
    dues_of: dict[int, list[Any]] = defaultdict(list)
    parties_of: dict[int, list[Any]] = defaultdict(list)
    incidents_of: dict[int, list[Any]] = defaultdict(list)
    with engine.connect() as connection:
        for due in connection.execute(
            select(RentDue).where(RentDue.lease_id.in_(lease_ids)).order_by(RentDue.period)
        ):
            dues_of[due.lease_id].append(due)
        for party in connection.execute(
            select(Party).where(Party.lease_id.in_(lease_ids)).order_by(Party.role, Party.id)
        ):
            parties_of[party.lease_id].append(party)
        for incident in connection.execute(
            select(
                RentIncident.lease_id, RentIncident.occurred_on, RentIncident.kind, RentDue.period
            )
            .join(RentDue, RentDue.id == RentIncident.due_id)
            .where(RentIncident.lease_id.in_(lease_ids))
            .order_by(RentIncident.occurred_on.desc())
        ):
            incidents_of[incident.lease_id].append(incident)
    allocations = _allocations(engine, [due.id for dues in dues_of.values() for due in dues])
    views = []
    for row in leases:
        lease = row.Lease
        view = LeaseView(
            id=lease.id,
            kind=lease.kind,
            label=lease.label,
            property_id=lease.property_id,
            property_name=row.property_name,
            account_name=row.account_name,
            start_date=lease.start_date,
            end_date=lease.end_date,
            tracking_start=lease.tracking_start,
            monthly_cents=lease.rent_cents + lease.charges_cents,
            rent_cents=lease.rent_cents,
            charges_cents=lease.charges_cents,
            due_day=lease.due_day,
            alert_day=lease.alert_day,
            unpaid_day=lease.unpaid_day,
            account_id=lease.account_id,
        )
        for due in dues_of[lease.id]:
            rows = allocations.get(due.id, [])
            confirmed = [a.amount_cents for a in rows if a.status is AllocationStatus.CONFIRMED]
            status = compute_status(
                DueTerms(due.period, due.amount_cents, lease.alert_day, lease.unpaid_day),
                confirmed,
                today,
            )
            view.dues.append(
                DueRow(
                    id=due.id,
                    period=due.period,
                    due_date=due.due_date,
                    amount_cents=due.amount_cents,
                    paid_cents=status.paid,
                    remaining_cents=status.remaining,
                    state=status.state,
                    allocations=rows,
                )
            )
        view.parties = [
            PartyRow(p.role, p.name_enc, _mask(p.iban_enc), party_patterns(p.label_patterns_enc))
            for p in parties_of[lease.id]
        ]
        view.incidents = [(i.occurred_on, str(i.kind), i.period) for i in incidents_of[lease.id]]
        views.append(view)
    return views


def pending_proposals(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        return [
            dict(row._mapping)
            for row in connection.execute(
                select(
                    RentAllocation.id,
                    RentAllocation.amount_cents,
                    RentDue.period,
                    Lease.label.label("lease"),
                    LedgerTransaction.booking_date,
                    LedgerTransaction.label_clean.label("label"),
                )
                .join(RentDue, RentDue.id == RentAllocation.due_id)
                .join(Lease, Lease.id == RentDue.lease_id)
                .join(LedgerTransaction, LedgerTransaction.id == RentAllocation.transaction_id)
                .where(RentAllocation.status == AllocationStatus.PROPOSED)
                .order_by(LedgerTransaction.booking_date)
            )
        ]


def pending_overpayments(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        return [
            dict(row._mapping)
            for row in connection.execute(
                select(
                    Overpayment.id,
                    Overpayment.amount_cents,
                    Lease.label.label("lease"),
                    LedgerTransaction.booking_date,
                    LedgerTransaction.label_clean.label("label"),
                )
                .join(Lease, Lease.id == Overpayment.lease_id)
                .join(LedgerTransaction, LedgerTransaction.id == Overpayment.transaction_id)
                .where(Overpayment.decision == OverpaymentDecision.PENDING)
                .order_by(LedgerTransaction.booking_date)
            )
        ]


@dataclass(frozen=True)
class CreditSearch:
    """Recherche d'encaissements à affecter à la main (LOC-06). `account_id` None : tous les
    comptes suivis (un locataire peut payer une fois sur un autre compte)."""

    account_id: int | None
    date_from: date | None = None
    date_to: date | None = None
    text: str | None = None
    amount_cents: int | None = None  # montant exact recherché


def search_credits(engine: Engine, search: CreditSearch, limit: int = 100) -> list[dict[str, Any]]:
    """Encaissements dont il reste une part non affectée, avec ce disponible."""
    confirmed = (
        select(
            RentAllocation.transaction_id.label("tx"),
            func.sum(RentAllocation.amount_cents).label("used"),
        )
        .where(RentAllocation.status == AllocationStatus.CONFIRMED)
        .group_by(RentAllocation.transaction_id)
        .subquery()
    )
    over = (
        select(
            Overpayment.transaction_id.label("tx"), func.sum(Overpayment.amount_cents).label("used")
        )
        .group_by(Overpayment.transaction_id)
        .subquery()
    )
    available = (
        LedgerTransaction.amount_cents
        - func.coalesce(confirmed.c.used, 0)
        - func.coalesce(over.c.used, 0)
    )
    conditions = [
        LedgerTransaction.amount_cents > 0,
        LedgerTransaction.account_id.in_(visible_accounts()),
        is_effective(),
        available > 0,
    ]
    if search.account_id is not None:
        conditions.append(LedgerTransaction.account_id == search.account_id)
    if search.date_from is not None:
        conditions.append(LedgerTransaction.booking_date >= search.date_from)
    if search.date_to is not None:
        conditions.append(LedgerTransaction.booking_date <= search.date_to)
    if search.text:
        pattern = "%" + search.text.upper().replace("%", "").replace("_", "") + "%"
        conditions.append(func.upper(LedgerTransaction.label_raw).like(pattern))
    if search.amount_cents is not None:
        conditions.append(LedgerTransaction.amount_cents == search.amount_cents)
    with engine.connect() as connection:
        return [
            dict(row._mapping)
            for row in connection.execute(
                select(
                    LedgerTransaction.id,
                    LedgerTransaction.booking_date,
                    LedgerTransaction.amount_cents,
                    available.label("available_cents"),
                    LedgerTransaction.label_clean.label("label"),
                    Account.name.label("account"),
                )
                .join(Account, Account.id == LedgerTransaction.account_id)
                .outerjoin(confirmed, confirmed.c.tx == LedgerTransaction.id)
                .outerjoin(over, over.c.tx == LedgerTransaction.id)
                .where(*conditions)
                .order_by(LedgerTransaction.booking_date.desc(), LedgerTransaction.id.desc())
                .limit(limit)
            )
        ]


def recent_debits(engine: Engine, today: date) -> list[dict[str, Any]]:
    """Dépenses récentes non encore rattachées à un bien (rattachement manuel, LOC-10)."""
    with engine.connect() as connection:
        return [
            dict(row._mapping)
            for row in connection.execute(
                select(
                    LedgerTransaction.id,
                    LedgerTransaction.booking_date,
                    LedgerTransaction.amount_cents,
                    LedgerTransaction.label_clean.label("label"),
                )
                .where(
                    LedgerTransaction.amount_cents < 0,
                    LedgerTransaction.account_id.in_(visible_accounts()),
                    is_effective(),
                    LedgerTransaction.id.not_in(select(PropertyExpense.transaction_id)),
                    LedgerTransaction.booking_date >= today - timedelta(days=400),
                )
                .order_by(LedgerTransaction.booking_date.desc())
                .limit(80)
            )
        ]
