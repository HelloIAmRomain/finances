"""Quittances de loyer (LOC-07, LEG-04 ; loi du 6 juillet 1989, art. 21 et 25-3).

- Une quittance n'est établie que pour une échéance **entièrement payée** (paiements confirmés) ;
  un paiement partiel ne donne droit qu'à un reçu, que l'outil ne produit pas.
- Elle distingue le loyer et les charges, et indique la période et la date du dernier paiement.
- Elle n'est **jamais envoyée** par l'outil (inv. 6) : le bailleur la télécharge et la remet ;
  chaque téléchargement est journalisé.
- Données imprimées : bailleur(s) et adresse du logement (fiche du bien), locataire(s) du bail.
"""

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import Engine, func, select, update

from app.audit import audit
from app.db import write_transaction
from app.models import Lease, LedgerTransaction, Party, Property, RentAllocation, RentDue
from app.models.rental import AllocationStatus, LeaseKind, PartyRole
from app.money import Cents, format_eur
from app.pdf import Document
from app.timeutil import format_date_fr

MAX_TEXT = 400


class ReceiptError(ValueError):
    """Quittance impossible pour l'instant (message affiché tel quel, avec la marche à suivre)."""


@dataclass(frozen=True)
class Receipt:
    lease_id: int
    due_id: int
    property_id: int
    period: str  # « AAAA-MM »
    landlord: str
    tenants: tuple[str, ...]
    address: str
    rent_cents: int
    charges_cents: int
    paid_on: date
    furnished: bool = True

    @property
    def total_cents(self) -> int:
        return self.rent_cents + self.charges_cents

    @property
    def period_start(self) -> date:
        year, month = (int(part) for part in self.period.split("-"))
        return date(year, month, 1)

    @property
    def period_end(self) -> date:
        start = self.period_start
        return start.replace(day=monthrange(start.year, start.month)[1])

    @property
    def filename(self) -> str:
        return f"quittance-{self.period}.pdf"


@dataclass(frozen=True)
class Missing:
    """Ce qui manque pour établir les quittances d'un bail (affiché avant tout téléchargement)."""

    landlord: bool
    address: bool
    tenant: bool

    @property
    def any(self) -> bool:
        return self.landlord or self.address or self.tenant


def missing(engine: Engine, lease_id: int) -> Missing:
    with engine.connect() as connection:
        found = connection.execute(
            select(Property.landlord_enc, Property.address_enc)
            .join(Lease, Lease.property_id == Property.id)
            .where(Lease.id == lease_id)
        ).first()
        tenants = connection.scalar(
            select(func.count())
            .select_from(Party)
            .where(Party.lease_id == lease_id, Party.role == PartyRole.TENANT)
        )
    return Missing(
        landlord=found is None or not (found.landlord_enc or "").strip(),
        address=found is None or not (found.address_enc or "").strip(),
        tenant=not tenants,
    )


def receipt(engine: Engine, lease_id: int, due_id: int) -> Receipt:
    with engine.connect() as connection:
        due = connection.execute(
            select(
                RentDue.period,
                RentDue.amount_cents,
                RentDue.charges_cents,
                Lease.kind,
                Lease.property_id,
            )
            .join(Lease, Lease.id == RentDue.lease_id)
            .where(RentDue.id == due_id, RentDue.lease_id == lease_id)
        ).first()
        if due is None:
            raise ReceiptError("Échéance introuvable.")
        kind, property_id = due.kind, due.property_id
        if kind is not LeaseKind.RENT or property_id is None:
            raise ReceiptError("Une quittance ne s'établit que pour le loyer d'un bien.")
        paid, last = connection.execute(
            select(
                func.coalesce(func.sum(RentAllocation.amount_cents), 0),
                func.max(LedgerTransaction.booking_date),
            )
            .join(LedgerTransaction, LedgerTransaction.id == RentAllocation.transaction_id)
            .where(
                RentAllocation.due_id == due_id,
                RentAllocation.status == AllocationStatus.CONFIRMED,
            )
        ).one()
        prop = connection.execute(
            select(Property.landlord_enc, Property.address_enc, Property.furnished).where(
                Property.id == property_id
            )
        ).one()
        tenants = connection.scalars(
            select(Party.name_enc)
            .where(Party.lease_id == lease_id, Party.role == PartyRole.TENANT)
            .order_by(Party.id)
        ).all()
    if int(paid) < due.amount_cents or last is None:
        raise ReceiptError(
            "Échéance pas encore entièrement payée : la quittance s'établit après le paiement "
            "complet (un paiement partiel ne donne droit qu'à un reçu)."
        )
    gaps = Missing(
        landlord=not (prop.landlord_enc or "").strip(),
        address=not (prop.address_enc or "").strip(),
        tenant=not tenants,
    )
    if gaps.any:
        raise ReceiptError(missing_message(gaps))
    charges = min(due.charges_cents, due.amount_cents)
    return Receipt(
        lease_id=lease_id,
        due_id=due_id,
        property_id=property_id,
        period=due.period,
        landlord=prop.landlord_enc.strip(),
        tenants=tuple(name.strip() for name in tenants),
        address=prop.address_enc.strip(),
        rent_cents=due.amount_cents - charges,
        charges_cents=charges,
        paid_on=last,
        furnished=prop.furnished,
    )


def set_details(
    engine: Engine,
    property_id: int,
    *,
    landlord: str,
    address: str,
    actor_user_id: int | None,
    now: datetime,
) -> None:
    """Bailleur(s) et adresse du logement, imprimés sur les quittances (chiffrés en base)."""
    landlord, address = landlord.strip(), address.strip()
    if len(landlord) > MAX_TEXT or len(address) > MAX_TEXT:
        raise ReceiptError(f"Texte trop long : {MAX_TEXT} caractères au plus par champ.")
    with write_transaction(engine) as connection:
        found = connection.execute(
            update(Property)
            .where(Property.id == property_id)
            .values(landlord_enc=landlord or None, address_enc=address or None)
        )
        if not found.rowcount:
            raise ReceiptError("Bien introuvable.")
        # Valeurs non journalisées : données personnelles (inv. 5), seul le fait est tracé.
        audit(
            connection,
            "property.receipt_details",
            ("property", property_id),
            now=now,
            actor_user_id=actor_user_id,
            details={"bailleur": bool(landlord), "adresse": bool(address)},
        )


def missing_message(gaps: Missing) -> str:
    parts = [
        text
        for flag, text in (
            (gaps.landlord, "le nom et l'adresse du bailleur (fiche du bien)"),
            (gaps.address, "l'adresse du logement (fiche du bien)"),
            (gaps.tenant, "le nom du locataire (payeur de rôle « locataire » sur le bail)"),
        )
        if flag
    ]
    return "Pour établir une quittance, renseignez d'abord " + " et ".join(parts) + "."


def _eur(cents: int) -> str:
    return format_eur(Cents(cents))


def render(found: Receipt, issued_on: date) -> bytes:
    """Quittance au format PDF (une page)."""
    doc = Document()
    start, end = format_date_fr(found.period_start), format_date_fr(found.period_end)
    tenants = " et ".join(found.tenants)
    doc.paragraph("Quittance de loyer", size=20, bold=True)
    doc.paragraph(f"Période du {start} au {end}", size=12)
    doc.space(14)
    doc.paragraph("Bailleur", bold=True)
    for line in found.landlord.splitlines():
        doc.paragraph(line.strip())
    doc.space(8)
    doc.paragraph("Locataire", bold=True)
    doc.paragraph(tenants)
    doc.space(8)
    doc.paragraph(
        "Logement loué (location meublée)" if found.furnished else "Logement loué", bold=True
    )
    for line in found.address.splitlines():
        doc.paragraph(line.strip())
    doc.space(14)
    doc.paragraph(
        f"Le bailleur déclare avoir reçu de {tenants} la somme de {_eur(found.total_cents)} "
        f"au titre du loyer et des charges de la période du {start} au {end}, "
        f"réglée en totalité le {format_date_fr(found.paid_on)}, et lui en donne quittance, "
        "sous réserve "
        "de tous ses droits."
    )
    doc.space(10)
    doc.paragraph("Détail du règlement", bold=True)
    doc.rule()
    doc.row("Loyer hors charges", _eur(found.rent_cents))
    doc.row("Provision pour charges", _eur(found.charges_cents))
    doc.rule()
    doc.row("Total reçu", _eur(found.total_cents), bold=True)
    doc.space(14)
    doc.paragraph(
        "Cette quittance annule tous les reçus qui auraient pu être établis précédemment en cas "
        "de paiement partiel du montant de cette période. Délivrée gratuitement "
        "(loi n° 89-462 du 6 juillet 1989, article 21).",
        size=9,
    )
    doc.space(18)
    doc.paragraph(f"Établie le {format_date_fr(issued_on)}.")
    doc.paragraph("Le bailleur")
    return doc.render(title=f"Quittance de loyer {found.period}")


def issue(
    engine: Engine,
    lease_id: int,
    due_id: int,
    *,
    issued_on: date,
    actor_user_id: int | None,
    now: datetime,
) -> tuple[Receipt, bytes]:
    """Établit la quittance (PDF) et journalise le téléchargement ; rien n'est envoyé."""
    found = receipt(engine, lease_id, due_id)
    pdf = render(found, issued_on)
    with write_transaction(engine) as connection:
        audit(
            connection,
            "receipt.issued",
            ("rent_due", due_id),
            now=now,
            actor_user_id=actor_user_id,
            details={"bail": lease_id, "periode": found.period, "total": found.total_cents},
        )
    return found, pdf
