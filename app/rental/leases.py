"""Biens, baux (ou encaissements attendus) et payeurs (LOC-01, LOC-02, LOC-15, Q17).

Montants en centimes (inv. 2), noms, IBAN et motifs de libellé chiffrés (inv. 5), créations et
modifications auditées.
"""

import json
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from typing import Any

from sqlalchemy import Connection, Engine, delete, func, insert, select, update

from app.alerts.engine import resolve_prefix
from app.audit import audit
from app.crypto import blind_index
from app.db import inserted_id, write_transaction
from app.iban import is_valid_iban, normalize_iban
from app.models import (
    Account,
    Lease,
    LedgerTransaction,
    OwnerShare,
    Party,
    Property,
    RentAllocation,
    RentDue,
)
from app.models.rental import AllocationMethod, AllocationStatus, LeaseKind, PartyRole
from app.months import month_of, month_start, next_month

FULL_SHARE_BP = 10000
FULL_PERCENT = 100
BP_PER_PERCENT = FULL_SHARE_BP // FULL_PERCENT


class LeaseError(ValueError):
    """Saisie de bien ou de bail invalide."""


class LeaseNotFoundError(LeaseError):
    """Bail inexistant (ou supprimé) : 404 côté API."""

    def __init__(self) -> None:
        super().__init__("Bail introuvable.")


def normalize_label(text: str) -> str:
    """Majuscules, sans accents ni ponctuation superflue : base de la reconnaissance des payeurs
    par motif (« Vir. de M. Élie » et « VIR DE M ELIE » se ressemblent)."""
    decomposed = unicodedata.normalize("NFKD", text)
    letters = "".join(c for c in decomposed if not unicodedata.combining(c)).upper()
    cleaned = "".join(c if c.isalnum() else " " for c in letters)
    return " ".join(cleaned.split())


def create_property(
    engine: Engine,
    *,
    name: str,
    address: str | None,
    furnished: bool,
    shares: Sequence[tuple[str, int]],
    now: datetime,
    actor_user_id: int | None = None,
) -> int:
    """`shares` : (indivisaire, quote-part en dix-millièmes) ; la somme doit faire 100 %."""
    name = name.strip()
    if not name:
        raise LeaseError("Le nom du bien est obligatoire.")
    if shares and sum(bp for _, bp in shares) != FULL_SHARE_BP:
        raise LeaseError("Les quote-parts doivent totaliser 100 %.")
    with write_transaction(engine) as connection:
        result = connection.execute(
            insert(Property).values(
                name=name,
                address_enc=(address or "").strip() or None,
                furnished=furnished,
                created_at=now,
            )
        )
        property_id = inserted_id(result)
        for label, bp in shares:
            connection.execute(
                insert(OwnerShare).values(
                    property_id=property_id, owner_label=label.strip(), share_bp=bp
                )
            )
        audit(
            connection,
            "property.created",
            ("property", property_id),
            now=now,
            actor_user_id=actor_user_id,
            details={"nom": name, "quote_parts": len(shares)},
        )
    return property_id


@dataclass(frozen=True)
class LeaseTerms:
    """Conditions d'un bail ou d'un encaissement attendu (montants en centimes)."""

    kind: LeaseKind
    label: str
    account_id: int  # compte d'encaissement (L-10)
    start_date: date
    rent_cents: int  # loyer hors charges, ou mensualité de remboursement
    property_id: int | None = None
    charges_cents: int = 0
    due_day: int = 1
    alert_day: int = 5  # Q16
    unpaid_day: int = 10  # Q16
    deposit_cents: int = 0
    end_date: date | None = None
    tracking_start: date | None = None  # premier mois suivi (historique bancaire disponible)

    def check(self) -> None:
        if not self.label.strip():
            raise LeaseError("Le nom du bail est obligatoire.")
        if self.kind is LeaseKind.RENT and self.property_id is None:
            raise LeaseError("Un bail de location est rattaché à un bien.")
        if self.rent_cents <= 0 or self.charges_cents < 0 or self.deposit_cents < 0:
            raise LeaseError(
                "Montants invalides : loyer positif, charges et dépôt positifs ou nuls."
            )
        if not 1 <= self.due_day <= self.alert_day < self.unpaid_day <= 28:  # noqa: PLR2004
            raise LeaseError(
                "Jours invalides : échéance ≤ jour d'alerte < jour d'impayé, entre le 1 et le 28."
            )
        if self.end_date is not None and self.end_date < self.start_date:
            raise LeaseError("La date de fin précède la date de début.")


def create_lease(
    engine: Engine, terms: LeaseTerms, *, now: datetime, actor_user_id: int | None = None
) -> int:
    terms.check()
    with write_transaction(engine) as connection:
        if connection.scalar(select(Account.id).where(Account.id == terms.account_id)) is None:
            raise LeaseError("Compte d'encaissement introuvable.")
        values = {**asdict(terms), "label": terms.label.strip(), "created_at": now}
        result = connection.execute(insert(Lease).values(**values))
        lease_id = inserted_id(result)
        audit(
            connection,
            "lease.created",
            ("lease", lease_id),
            now=now,
            actor_user_id=actor_user_id,
            details={"type": str(terms.kind), "compte": terms.account_id},
        )
    return lease_id


def end_lease(
    engine: Engine, lease_id: int, *, end_date: date, now: datetime, actor_user_id: int | None
) -> None:
    with write_transaction(engine) as connection:
        start = connection.scalar(select(Lease.start_date).where(Lease.id == lease_id))
        if start is None:
            raise LeaseNotFoundError
        if end_date < start:
            raise LeaseError("La date de fin précède la date de début.")
        connection.execute(update(Lease).where(Lease.id == lease_id).values(end_date=end_date))
        audit(
            connection,
            "lease.ended",
            ("lease", lease_id),
            now=now,
            actor_user_id=actor_user_id,
            details={"fin": end_date.isoformat()},
        )


@dataclass(frozen=True)
class LeaseChanges:
    """Modifications d'un bail ; un champ laissé à None ne change pas."""

    label: str | None = None
    tracking_start: date | None = None
    rent_cents: int | None = None
    charges_cents: int | None = None
    amounts_from: str | None = None  # « AAAA-MM » : premier mois au nouveau montant
    due_day: int | None = None
    alert_day: int | None = None
    unpaid_day: int | None = None


def update_lease(
    engine: Engine,
    lease_id: int,
    changes: LeaseChanges,
    *,
    actor_user_id: int | None,
    now: datetime,
) -> list[int]:
    """Applique les modifications ; renvoie les échéances supprimées (antérieures au suivi).

    - Suivi déplacé plus tard : les échéances antérieures disparaissent (leurs propositions et
      incidents avec elles), sauf si un paiement y est affecté (à dissocier d'abord).
    - Nouveau montant : appliqué au bail et aux échéances à partir de `amounts_from` ; jamais
      en dessous de ce qui a déjà été affecté à une échéance.
    """
    with write_transaction(engine) as connection:
        lease = connection.execute(select(Lease).where(Lease.id == lease_id)).first()
        if lease is None:
            raise LeaseNotFoundError

        def pick(new: Any, old: Any) -> Any:
            return old if new is None else new

        merged = LeaseTerms(
            kind=lease.kind,
            label=pick(changes.label and changes.label.strip(), lease.label),
            account_id=lease.account_id,
            start_date=lease.start_date,
            rent_cents=pick(changes.rent_cents, lease.rent_cents),
            property_id=lease.property_id,
            charges_cents=pick(changes.charges_cents, lease.charges_cents),
            due_day=pick(changes.due_day, lease.due_day),
            alert_day=pick(changes.alert_day, lease.alert_day),
            unpaid_day=pick(changes.unpaid_day, lease.unpaid_day),
            end_date=lease.end_date,
            tracking_start=pick(changes.tracking_start, lease.tracking_start),
        )
        values = {
            "label": merged.label,
            "rent_cents": merged.rent_cents,
            "charges_cents": merged.charges_cents,
            "due_day": merged.due_day,
            "alert_day": merged.alert_day,
            "unpaid_day": merged.unpaid_day,
            "tracking_start": merged.tracking_start,
        }
        merged.check()
        removed = _drop_untracked_dues(connection, lease, changes.tracking_start)
        if changes.amounts_from is not None and (
            changes.rent_cents is not None or changes.charges_cents is not None
        ):
            _reprice_dues(
                connection,
                lease_id,
                changes.amounts_from,
                merged.rent_cents + merged.charges_cents,
                merged.charges_cents,
            )
        connection.execute(update(Lease).where(Lease.id == lease_id).values(**values))
        audit(
            connection,
            "lease.updated",
            ("lease", lease_id),
            now=now,
            actor_user_id=actor_user_id,
            details={
                **{k: (v.isoformat() if isinstance(v, date) else v) for k, v in values.items()},
                "montants_a_partir_de": changes.amounts_from,
                "echeances_retirees": len(removed),
            },
        )
    for due_id in removed:  # leurs alertes n'ont plus d'objet
        resolve_prefix(engine, f"rent:{due_id}:", now=now)
    return removed


def _confirmed(connection: Connection, due_id: int) -> int:
    return int(
        connection.scalar(
            select(func.coalesce(func.sum(RentAllocation.amount_cents), 0)).where(
                RentAllocation.due_id == due_id,
                RentAllocation.status == AllocationStatus.CONFIRMED,
            )
        )
        or 0
    )


def _drop_untracked_dues(connection: Connection, lease: Any, tracking: date | None) -> list[int]:
    if tracking is None:
        return []
    first = month_of(max(lease.start_date, tracking))
    dues = connection.scalars(
        select(RentDue.id).where(RentDue.lease_id == lease.id, RentDue.period < first)
    ).all()
    # Les affectations automatiques disparaissent avec leurs échéances (le moteur les refera) ;
    # une affectation manuelle est une décision de l'administrateur : on ne la perd jamais.
    manual = connection.scalar(
        select(func.count()).where(
            RentAllocation.due_id.in_(dues),
            RentAllocation.status == AllocationStatus.CONFIRMED,
            RentAllocation.method == AllocationMethod.MANUAL,
        )
    )
    if manual:
        raise LeaseError(
            "Des paiements sont affectés à la main à des échéances antérieures au suivi : "
            "dissociez-les d'abord."
        )
    if dues:
        connection.execute(delete(RentDue).where(RentDue.id.in_(dues)))
    return list(dues)


def _reprice_dues(
    connection: Connection, lease_id: int, from_month: str, amount: int, charges: int
) -> None:
    for due_id, period in connection.execute(
        select(RentDue.id, RentDue.period).where(
            RentDue.lease_id == lease_id, RentDue.period >= from_month
        )
    ).all():
        if _confirmed(connection, due_id) > amount:
            raise LeaseError(
                f"Le nouveau montant est inférieur à ce qui est déjà affecté à {period} : "
                "il dépasse le montant dû. Dissociez d'abord le paiement concerné."
            )
        connection.execute(
            update(RentDue)
            .where(RentDue.id == due_id)
            .values(amount_cents=amount, charges_cents=charges)
        )


def add_party(
    engine: Engine,
    lease_id: int,
    *,
    role: PartyRole,
    name: str,
    iban: str | None = None,
    patterns: Iterable[str] = (),
) -> int:
    name = name.strip()
    if not name:
        raise LeaseError("Le nom du payeur est obligatoire.")
    iban = normalize_iban(iban) if iban and iban.strip() else None
    if iban is not None and not is_valid_iban(iban):
        raise LeaseError("IBAN invalide (clé de contrôle incorrecte).")
    motifs = [m for m in (normalize_label(p) for p in patterns) if len(m) >= 3]  # noqa: PLR2004
    with write_transaction(engine) as connection:
        if connection.scalar(select(Lease.id).where(Lease.id == lease_id)) is None:
            raise LeaseNotFoundError
        result = connection.execute(
            insert(Party).values(
                lease_id=lease_id,
                role=role,
                name_enc=name,
                iban_enc=iban,
                iban_hash=blind_index(iban, context="iban") if iban else None,
                label_patterns_enc=json.dumps(motifs, ensure_ascii=False) if motifs else None,
            )
        )
        return inserted_id(result)


def party_patterns(encoded: str | None) -> list[str]:
    return list(json.loads(encoded)) if encoded else []


@dataclass(frozen=True)
class NewParty:
    role: PartyRole
    name: str
    iban: str | None = None
    patterns: tuple[str, ...] = ()


def owner_shares(my_percent: int) -> list[tuple[str, int]]:
    """Quote-parts d'un nouveau bien : la mienne, le reste à « co-indivisaire » (LOC-12)."""
    if not 0 < my_percent <= FULL_PERCENT:
        raise LeaseError("Quote-part entre 1 et 100 %.")
    shares = [("moi", my_percent * BP_PER_PERCENT)]
    if my_percent < FULL_PERCENT:
        shares.append(("co-indivisaire", (FULL_PERCENT - my_percent) * BP_PER_PERCENT))
    return shares


def first_history_month(engine: Engine, account_id: int) -> date | None:
    """Premier mois **complet** dont la banque fournit l'historique (souvent 90 jours) : les mois
    antérieurs ne peuvent pas être vérifiés et ne donnent pas d'échéance. Un historique qui
    commence le 30/06 ne montre pas le loyer de juin : le suivi part de juillet."""
    with engine.connect() as connection:
        first = connection.scalar(
            select(func.min(LedgerTransaction.booking_date)).where(
                LedgerTransaction.account_id == account_id
            )
        )
    if first is None:
        return None
    return first if first.day == 1 else month_start(next_month(month_of(first)))


@dataclass(frozen=True)
class UntrackedDues:
    """Échéances antérieures à l'historique bancaire : impossibles à vérifier, elles attirent à
    tort les paiements récents (un paiement va d'abord à l'impayé le plus ancien)."""

    count: int
    first_period: str
    last_period: str
    suggested_start: date  # premier mois complet d'historique


def untracked_dues(engine: Engine, lease_id: int) -> UntrackedDues | None:
    """Échéances du bail antérieures au premier mois complet d'historique, s'il y en a."""
    account_id = lease_account(engine, lease_id)
    start = first_history_month(engine, account_id) if account_id is not None else None
    if start is None:
        return None
    with engine.connect() as connection:
        periods = connection.scalars(
            select(RentDue.period)
            .where(RentDue.lease_id == lease_id, RentDue.period < month_of(start))
            .order_by(RentDue.period)
        ).all()
    if not periods:
        return None
    return UntrackedDues(len(periods), periods[0], periods[-1], start)


def lease_account(engine: Engine, lease_id: int) -> int | None:
    """Compte d'encaissement du bail."""
    with engine.connect() as connection:
        return connection.scalar(select(Lease.account_id).where(Lease.id == lease_id))


def create_lease_with_parties(
    engine: Engine,
    terms: LeaseTerms,
    *,
    new_property: tuple[str, int] | None,
    parties: Sequence[NewParty],
    now: datetime,
    actor_user_id: int | None,
) -> int:
    """Bail complet, comme le saisit une personne (page web) ou l'IA : bien créé au besoin
    (`new_property` : nom et ma quote-part en %), suivi depuis le premier mois d'historique si
    rien n'est précisé, payeur obligatoire puis autres parties (caution…)."""
    if not parties:
        raise LeaseError("Le payeur (locataire ou emprunteur) est obligatoire.")
    property_id = terms.property_id
    if terms.kind is LeaseKind.RENT and property_id is None:
        if new_property is None:
            raise LeaseError("Un loyer se rattache à un bien : choisissez-le ou nommez-en un.")
        name, my_percent = new_property
        property_id = create_property(
            engine,
            name=name,
            address=None,
            furnished=True,
            shares=owner_shares(my_percent),
            now=now,
            actor_user_id=actor_user_id,
        )
    tracking = terms.tracking_start or first_history_month(engine, terms.account_id)
    lease_id = create_lease(
        engine,
        replace(
            terms,
            property_id=property_id if terms.kind is LeaseKind.RENT else None,
            tracking_start=tracking,
        ),
        now=now,
        actor_user_id=actor_user_id,
    )
    for party in parties:
        add_party(
            engine,
            lease_id,
            role=party.role,
            name=party.name,
            iban=party.iban,
            patterns=party.patterns,
        )
    return lease_id
