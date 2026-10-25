"""Récapitulatif annuel d'un bien (FIS-01) et répartition entre indivisaires (LOC-12).

Pour une année civile et un bien :
- loyers **dus** (échéances de l'année) et **encaissés** (paiements affectés, datés de l'année),
  charges et provisions comprises ;
- charges rattachées au bien, par nature (LOC-10), dont la part récupérable sur le locataire
  saisie sur les avis de taxe foncière (ordures ménagères) ;
- résultat de trésorerie (encaissé moins charges) ;
- part de chaque indivisaire au prorata de sa quote-part (méthode des plus forts restes : la
  somme des parts égale exactement le total, inv. 2). Une quote-part non attribuée (total des
  parts inférieur à 100 %) apparaît comme telle.

**Indicatif** : ce n'est ni une déclaration ni un conseil fiscal (LEG-03). Recettes encaissées
au sens du micro-BIC (CGI art. 50-0) ; au réel, l'expert-comptable retient les créances acquises
et ajoute amortissements et frais non rattachés ici.
"""

from dataclasses import dataclass
from datetime import date

from sqlalchemy import Engine, func, select

from app.models import (
    Deadline,
    Lease,
    LedgerTransaction,
    OwnerShare,
    Property,
    RentAllocation,
    RentDue,
)
from app.models.rental import AllocationStatus, ExpenseKind, LeaseKind
from app.models.tax import DeadlineKind
from app.money import Cents, allocate, format_share_bp
from app.rental.expenses import expenses, totals_by_kind

FULL_BP = 10_000
UNASSIGNED = "non attribuée"
SOURCES = (
    "Micro-BIC : CGI art. 50-0 (recettes encaissées, abattement de 50 %) ; "
    "réel : CGI art. 39 et 39 C (charges, amortissements). Calcul indicatif, à faire valider."
)


class SummaryError(ValueError):
    """Bien introuvable."""


@dataclass(frozen=True)
class OwnerPart:
    label: str
    share_bp: int
    received_cents: int
    charges_cents: int

    @property
    def result_cents(self) -> int:
        return self.received_cents + self.charges_cents


@dataclass(frozen=True)
class PropertyYear:
    property_id: int
    name: str
    year: int
    due_cents: int
    received_cents: int
    charges_by_kind: dict[ExpenseKind, int]  # négatives
    recoverable_cents: int
    owners: list[OwnerPart]

    @property
    def charges_cents(self) -> int:
        return sum(self.charges_by_kind.values())

    @property
    def result_cents(self) -> int:
        return self.received_cents + self.charges_cents


def _year_bounds(year: int) -> tuple[date, date]:
    return date(year, 1, 1), date(year, 12, 31)


def property_year(engine: Engine, property_id: int, year: int) -> PropertyYear:
    first, last = _year_bounds(year)
    with engine.connect() as connection:
        name = connection.scalar(select(Property.name).where(Property.id == property_id))
        if name is None:
            raise SummaryError("Bien introuvable.")
        leases = select(Lease.id).where(
            Lease.property_id == property_id, Lease.kind == LeaseKind.RENT
        )
        due = connection.scalar(
            select(func.coalesce(func.sum(RentDue.amount_cents), 0)).where(
                RentDue.lease_id.in_(leases), RentDue.period.like(f"{year:04d}-%")
            )
        )
        received = connection.scalar(
            select(func.coalesce(func.sum(RentAllocation.amount_cents), 0))
            .join(RentDue, RentDue.id == RentAllocation.due_id)
            .join(LedgerTransaction, LedgerTransaction.id == RentAllocation.transaction_id)
            .where(
                RentDue.lease_id.in_(leases),
                RentAllocation.status == AllocationStatus.CONFIRMED,
                LedgerTransaction.booking_date >= first,
                LedgerTransaction.booking_date <= last,
                # Pas de filtre « effective » : une affectation confirmée reste sur le virement
                # même reconnu ensuite comme doublon (L-12), comme sur les pages des loyers.
            )
        )
        recoverable = connection.scalar(
            select(func.coalesce(func.sum(Deadline.recoverable_cents), 0)).where(
                Deadline.property_id == property_id,
                Deadline.kind == DeadlineKind.PROPERTY_TAX,
                Deadline.due_date >= first,
                Deadline.due_date <= last,
            )
        )
        shares = connection.execute(
            select(OwnerShare.owner_label, OwnerShare.share_bp)
            .where(OwnerShare.property_id == property_id)
            .order_by(OwnerShare.owner_label)
        ).all()
    charges = totals_by_kind(expenses(engine, property_id, year))
    received_cents = int(received or 0)
    owners = [(label, bp) for label, bp in shares]
    assigned = sum(bp for _, bp in owners)
    if assigned < FULL_BP:
        owners.append((UNASSIGNED, FULL_BP - assigned))
    weights = [bp for _, bp in owners]
    incomes = allocate(Cents(received_cents), weights)
    # Charges réparties nature par nature : la part d'un indivisaire est la somme de ses parts,
    # cohérente au centime avec le détail exporté.
    costs = [0] * len(owners)
    for total in charges.values():
        for index, part in enumerate(allocate(Cents(total), weights)):
            costs[index] += part
    return PropertyYear(
        property_id=property_id,
        name=str(name),
        year=year,
        due_cents=int(due or 0),
        received_cents=received_cents,
        charges_by_kind=charges,
        recoverable_cents=int(recoverable or 0),
        owners=[
            OwnerPart(label, bp, int(income), int(cost))
            for (label, bp), income, cost in zip(owners, incomes, costs, strict=True)
        ],
    )


def summary_csv_rows(
    summary: PropertyYear, labels: dict[ExpenseKind, str]
) -> list[list[str | int]]:
    """Lignes du récapitulatif pour l'export CSV (FIS-06) : libellé, total, puis une colonne par
    indivisaire ; montants en centimes, mis en forme par `app.spreadsheet`."""
    owners = summary.owners
    header: list[str | int] = [
        "Poste",
        "Total",
        *(f"{o.label} ({format_share_bp(o.share_bp)} %)" for o in owners),
    ]
    weights = [o.share_bp for o in owners]

    def split(cents: int) -> list[int]:
        return [int(part) for part in allocate(Cents(cents), weights)]

    rows: list[list[str | int]] = [
        header,
        ["Loyers dus", summary.due_cents, *split(summary.due_cents)],
    ]
    rows.append(["Loyers encaissés", summary.received_cents, *[o.received_cents for o in owners]])
    for kind, total in sorted(summary.charges_by_kind.items(), key=lambda kv: labels[kv[0]]):
        rows.append([f"Charges : {labels[kind]}", total, *split(total)])
    rows.append(["Total des charges", summary.charges_cents, *[o.charges_cents for o in owners]])
    rows.append(
        [
            "Résultat (encaissé - charges)",
            summary.result_cents,
            *[o.result_cents for o in owners],
        ]
    )
    if summary.recoverable_cents:
        rows.append(
            [
                "Dont récupérable sur le locataire (taxe foncière)",
                summary.recoverable_cents,
                *split(summary.recoverable_cents),
            ]
        )
    rows.append(["Mention", SOURCES])
    return rows
