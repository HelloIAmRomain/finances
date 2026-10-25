"""Dépôt de garantie (LEG-04 ; loi du 6 juillet 1989, art. 22 et 25-6).

- Plafond : deux mois de loyer hors charges en location meublée, un mois en location nue
  (signalé, jamais bloquant : le bail est déjà signé).
- Restitution : un mois après la remise des clés si l'état des lieux de sortie est conforme à
  celui d'entrée, deux mois sinon ; tant que l'état des lieux n'est pas renseigné, le délai le
  plus court est retenu (rappel au plus tôt).
- Retenues : chacune avec son motif (justificatifs à conserver), jamais au-delà du dépôt encaissé.
- Les mouvements sont saisis par le bailleur, avec l'opération bancaire quand il y en a une ;
  une opération rattachée au dépôt sort du rapprochement des loyers. Rien n'est envoyé au
  locataire (inv. 6). Tout est journalisé.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import Engine, delete, insert, or_, select, update

from app.alerts.engine import raise_alert, resolve
from app.audit import audit
from app.db import write_transaction
from app.ledger.views import is_effective, visible_accounts
from app.models import (
    Alert,
    DepositEvent,
    Lease,
    LedgerTransaction,
    Overpayment,
    Property,
    RentAllocation,
    RentDue,
)
from app.models.alerts import AlertStatus, Severity
from app.models.rental import DepositEventKind, LeaseKind
from app.months import add_months
from app.timeutil import format_date_fr, paris_date

REMIND_BEFORE = timedelta(days=7)
CANDIDATES_LOOKBACK = timedelta(days=400)
CANDIDATES_SHOWN = 40
NOTE_MAX = 200

KIND_LABELS = {
    DepositEventKind.RECEIVED: "encaissé",
    DepositEventKind.WITHHELD: "retenue",
    DepositEventKind.RETURNED: "restitué",
}
STATUS_LABELS = {
    "attendu": "à encaisser",
    "encaisse": "encaissé",
    "a_restituer": "à restituer",
    "en_retard": "restitution en retard",
    "solde": "soldé",
}


class DepositError(ValueError):
    """Saisie refusée (message affiché tel quel, avec la marche à suivre)."""


# --- Règles (domaine pur) -----------------------------------------------------------------


def legal_max(rent_cents: int, *, furnished: bool) -> int:
    return rent_cents * (2 if furnished else 1)


def restitution_deadline(keys_returned_on: date, *, inspection_ok: bool | None) -> date:
    return add_months(keys_returned_on, 2 if inspection_ok is False else 1)


# --- État d'un bail -------------------------------------------------------------------------


@dataclass(frozen=True)
class EventRow:
    id: int
    kind: DepositEventKind
    amount_cents: int
    on_date: date
    note: str | None
    operation: str | None  # libellé de l'opération rattachée


@dataclass(frozen=True)
class DepositState:
    lease_id: int
    property_name: str | None
    expected_cents: int
    legal_max_cents: int
    furnished: bool
    keys_returned_on: date | None
    exit_inspection_ok: bool | None
    events: tuple[EventRow, ...]

    def _total(self, kind: DepositEventKind) -> int:
        return sum(e.amount_cents for e in self.events if e.kind is kind)

    @property
    def received_cents(self) -> int:
        return self._total(DepositEventKind.RECEIVED)

    @property
    def withheld_cents(self) -> int:
        return self._total(DepositEventKind.WITHHELD)

    @property
    def returned_cents(self) -> int:
        return self._total(DepositEventKind.RETURNED)

    @property
    def to_return_cents(self) -> int:
        return self.received_cents - self.withheld_cents - self.returned_cents

    @property
    def above_legal_max(self) -> bool:
        return max(self.expected_cents, self.received_cents) > self.legal_max_cents

    @property
    def deadline(self) -> date | None:
        if self.keys_returned_on is None:
            return None
        return restitution_deadline(self.keys_returned_on, inspection_ok=self.exit_inspection_ok)

    def status(self, today: date) -> str:
        if self.received_cents == 0:
            return "attendu"
        if self.to_return_cents <= 0 and self.keys_returned_on is not None:
            return "solde"
        if self.deadline is None:
            return "encaisse"
        return "en_retard" if today > self.deadline else "a_restituer"


def deposit_state(engine: Engine, lease_id: int) -> DepositState:
    with engine.connect() as connection:
        lease = connection.execute(
            select(
                Lease.deposit_cents,
                Lease.rent_cents,
                Lease.keys_returned_on,
                Lease.exit_inspection_ok,
                Property.name,
                Property.furnished,
            )
            .outerjoin(Property, Property.id == Lease.property_id)
            .where(Lease.id == lease_id)
        ).first()
        if lease is None:
            raise DepositError("Bail introuvable.")
        rows = connection.execute(
            select(
                DepositEvent.id,
                DepositEvent.kind,
                DepositEvent.amount_cents,
                DepositEvent.on_date,
                DepositEvent.note,
                LedgerTransaction.label_clean,
            )
            .outerjoin(LedgerTransaction, LedgerTransaction.id == DepositEvent.transaction_id)
            .where(DepositEvent.lease_id == lease_id)
            .order_by(DepositEvent.on_date, DepositEvent.id)
        ).all()
    furnished = True if lease.furnished is None else bool(lease.furnished)
    return DepositState(
        lease_id=lease_id,
        property_name=lease.name,
        expected_cents=lease.deposit_cents,
        legal_max_cents=legal_max(lease.rent_cents, furnished=furnished),
        furnished=furnished,
        keys_returned_on=lease.keys_returned_on,
        exit_inspection_ok=lease.exit_inspection_ok,
        events=tuple(
            EventRow(
                id=row.id,
                kind=row.kind,
                amount_cents=row.amount_cents,
                on_date=row.on_date,
                note=row.note,
                operation=row.label_clean,
            )
            for row in rows
        ),
    )


# --- Saisies --------------------------------------------------------------------------------


def _rent_lease(engine: Engine, lease_id: int) -> None:
    with engine.connect() as connection:
        kind = connection.scalar(select(Lease.kind).where(Lease.id == lease_id))
    if kind is not LeaseKind.RENT:
        raise DepositError("Le dépôt de garantie ne concerne qu'un bail de location.")


def _rent_period(engine: Engine, transaction_id: int) -> str | None:
    with engine.connect() as connection:
        period = connection.scalar(
            select(RentDue.period)
            .join(RentAllocation, RentAllocation.due_id == RentDue.id)
            .where(RentAllocation.transaction_id == transaction_id)
        )
        if period is None and connection.scalar(
            select(Overpayment.id).where(Overpayment.transaction_id == transaction_id)
        ):
            return "trop-perçu"
    return period


def _operation(engine: Engine, transaction_id: int, kind: DepositEventKind) -> tuple[int, date]:
    """Montant (positif) et date de l'opération, après vérification de son sens et de son usage."""
    if kind is DepositEventKind.WITHHELD:
        raise DepositError("Une retenue ne correspond à aucune opération : saisissez son montant.")
    with engine.connect() as connection:
        found = connection.execute(
            select(LedgerTransaction.amount_cents, LedgerTransaction.booking_date).where(
                LedgerTransaction.id == transaction_id
            )
        ).first()
        used = connection.scalar(
            select(DepositEvent.id).where(DepositEvent.transaction_id == transaction_id)
        )
    if found is None:
        raise DepositError("Opération introuvable.")
    if kind is DepositEventKind.RECEIVED and found.amount_cents <= 0:
        raise DepositError("L'encaissement du dépôt doit être une entrée d'argent.")
    if kind is DepositEventKind.RETURNED and found.amount_cents >= 0:
        raise DepositError("La restitution du dépôt doit être une sortie d'argent.")
    if used is not None:
        raise DepositError("Cette opération est déjà rattachée à un dépôt de garantie.")
    period = _rent_period(engine, transaction_id)
    if period is not None:
        raise DepositError(
            f"Cette opération est affectée au loyer ({period}) : dissociez-la d'abord sur la "
            "fiche du bail."
        )
    return abs(found.amount_cents), found.booking_date


def record(  # noqa: PLR0913 (bail, nature, montant, date, opération, motif, auteur, horloge)
    engine: Engine,
    lease_id: int,
    kind: DepositEventKind,
    *,
    amount_cents: int | None = None,
    on_date: date | None = None,
    transaction_id: int | None = None,
    note: str | None = None,
    actor_user_id: int | None,
    now: datetime,
) -> int:
    """Enregistre un encaissement, une retenue ou une restitution ; renvoie son identifiant."""
    _rent_lease(engine, lease_id)
    if transaction_id is not None:
        tx_amount, tx_date = _operation(engine, transaction_id, kind)
        amount_cents = amount_cents or tx_amount
        on_date = on_date or tx_date
    if amount_cents is None or amount_cents <= 0:
        raise DepositError("Indiquez un montant positif, par exemple 1 400,00.")
    note = (note or "").strip() or None
    if kind is DepositEventKind.WITHHELD and note is None:
        raise DepositError(
            "Indiquez le motif de la retenue (réparation, loyer impayé…) : il doit pouvoir être "
            "justifié auprès du locataire."
        )
    if note is not None and len(note) > NOTE_MAX:
        raise DepositError(f"Motif trop long : {NOTE_MAX} caractères au plus.")
    state = deposit_state(engine, lease_id)
    if kind is not DepositEventKind.RECEIVED:
        if state.received_cents == 0:
            raise DepositError("Enregistrez d'abord le dépôt encaissé.")
        if amount_cents > state.to_return_cents:
            raise DepositError(
                "Ce montant dépasse ce qui reste du dépôt (encaissé moins retenues et "
                "restitutions déjà enregistrées)."
            )
    with write_transaction(engine) as connection:
        event_id = int(
            connection.execute(
                insert(DepositEvent)
                .values(
                    lease_id=lease_id,
                    kind=kind,
                    amount_cents=amount_cents,
                    on_date=on_date or paris_date(now),
                    transaction_id=transaction_id,
                    note=note,
                    created_at=now,
                )
                .returning(DepositEvent.id)
            ).scalar_one()
        )
        audit(
            connection,
            f"deposit.{kind.value}",
            ("lease", lease_id),
            now=now,
            actor_user_id=actor_user_id,
            details={"montant": amount_cents, "operation": transaction_id},
        )
    return event_id


def delete_event(
    engine: Engine, lease_id: int, event_id: int, *, actor_user_id: int | None, now: datetime
) -> None:
    """Retire une saisie erronée ; l'opération rattachée redevient disponible."""
    with write_transaction(engine) as connection:
        found = connection.execute(
            select(DepositEvent.kind, DepositEvent.amount_cents).where(
                DepositEvent.id == event_id, DepositEvent.lease_id == lease_id
            )
        ).first()
        if found is None:
            raise DepositError("Mouvement du dépôt introuvable.")
        connection.execute(delete(DepositEvent).where(DepositEvent.id == event_id))
        audit(
            connection,
            "deposit.deleted",
            ("lease", lease_id),
            now=now,
            actor_user_id=actor_user_id,
            details={"nature": found.kind.value, "montant": found.amount_cents},
        )


def set_exit(
    engine: Engine,
    lease_id: int,
    *,
    keys_returned_on: date | None,
    inspection_ok: bool | None,
    actor_user_id: int | None,
    now: datetime,
) -> None:
    """Remise des clés et état des lieux de sortie (fixent le délai de restitution)."""
    _rent_lease(engine, lease_id)
    with write_transaction(engine) as connection:
        connection.execute(
            update(Lease)
            .where(Lease.id == lease_id)
            .values(keys_returned_on=keys_returned_on, exit_inspection_ok=inspection_ok)
        )
        audit(
            connection,
            "lease.exit",
            ("lease", lease_id),
            now=now,
            actor_user_id=actor_user_id,
            details={
                "remise_des_cles": keys_returned_on.isoformat() if keys_returned_on else None,
                "etat_des_lieux_conforme": inspection_ok,
            },
        )


@dataclass(frozen=True)
class Candidate:
    id: int
    booking_date: date
    amount_cents: int
    label: str


def candidates(engine: Engine, lease_id: int, *, credit: bool, today: date) -> list[Candidate]:
    """Opérations récentes à rattacher (entrées pour l'encaissement, sorties pour la restitution),
    hors opérations déjà affectées au loyer ou au dépôt."""
    with engine.connect() as connection:
        account = connection.scalar(select(Lease.account_id).where(Lease.id == lease_id))
        sign = LedgerTransaction.amount_cents > 0 if credit else LedgerTransaction.amount_cents < 0
        rows = connection.execute(
            select(
                LedgerTransaction.id,
                LedgerTransaction.booking_date,
                LedgerTransaction.amount_cents,
                LedgerTransaction.label_clean,
            )
            .where(
                sign,
                LedgerTransaction.account_id.in_(visible_accounts()),
                is_effective(),
                LedgerTransaction.booking_date >= today - CANDIDATES_LOOKBACK,
                LedgerTransaction.id.not_in(select(RentAllocation.transaction_id)),
                LedgerTransaction.id.not_in(select(Overpayment.transaction_id)),
                LedgerTransaction.id.not_in(
                    select(DepositEvent.transaction_id).where(
                        DepositEvent.transaction_id.is_not(None)
                    )
                ),
            )
            .order_by(
                (LedgerTransaction.account_id != account),  # compte du bail d'abord
                LedgerTransaction.booking_date.desc(),
                LedgerTransaction.id.desc(),
            )
            .limit(CANDIDATES_SHOWN)
        ).all()
    return [Candidate(row.id, row.booking_date, row.amount_cents, row.label_clean) for row in rows]


# --- Rappels --------------------------------------------------------------------------------


def exit_lease_ids(engine: Engine) -> Sequence[int]:
    """Baux de location avec une remise des clés ou un mouvement de dépôt."""
    with engine.connect() as connection:
        return connection.scalars(
            select(Lease.id).where(
                Lease.kind == LeaseKind.RENT,
                or_(
                    Lease.keys_returned_on.is_not(None),
                    Lease.id.in_(select(DepositEvent.lease_id)),
                ),
            )
        ).all()


def remind_deposits(engine: Engine, *, now: datetime) -> int:
    """Rappel 7 jours avant la date limite de restitution, alerte en cas de retard ; les alertes
    se résolvent d'elles-mêmes une fois le dépôt soldé. Rejouable (clés uniques)."""
    today = paris_date(now)
    signalled = 0
    for lease_id in exit_lease_ids(engine):
        state = deposit_state(engine, lease_id)
        deadline = state.deadline
        if deadline is None or state.to_return_cents <= 0:
            _resolve_lease(engine, lease_id, now)
            continue
        place = f" · {state.property_name}" if state.property_name else ""
        if today > deadline:
            raise_alert(
                engine,
                type="deposit_late",
                severity=Severity.WARNING,
                key=f"deposit_late:{lease_id}:{deadline.isoformat()}",
                title=f"Dépôt de garantie : restitution en retard{place}",
                subject=("lease", lease_id),
                once=True,
                now=now,
            )
            signalled += 1
        elif today >= deadline - REMIND_BEFORE:
            raise_alert(
                engine,
                type="deposit",
                severity=Severity.INFO,
                key=f"deposit:{lease_id}:{deadline.isoformat()}",
                title=f"Dépôt de garantie à restituer avant le {format_date_fr(deadline)}{place}",
                subject=("lease", lease_id),
                once=True,
                now=now,
            )
            signalled += 1
    return signalled


def _resolve_lease(engine: Engine, lease_id: int, now: datetime) -> None:
    with engine.connect() as connection:
        open_ids = connection.scalars(
            select(Alert.id).where(
                Alert.type.in_(["deposit", "deposit_late"]),
                Alert.subject_type == "lease",
                Alert.subject_id == lease_id,
                Alert.status != AlertStatus.RESOLVED,
            )
        ).all()
    for alert_id in open_ids:
        resolve(engine, alert_id, now=now)
