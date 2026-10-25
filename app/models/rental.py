"""Biens, baux et encaissements attendus, échéances, rapprochement, incidents (lot 2).

- Un **bail** porte son compte d'encaissement (L-10). Le type `loan` décrit un encaissement
  attendu hors bail (remboursement d'un prêt accordé à un proche, Q17) : mêmes échéances, même
  rapprochement, mêmes alertes, sans bien ni caution ni quittance.
- Les **payeurs** (locataire, caution, tiers payeur, emprunteur) sont reconnus par IBAN (empreinte
  HMAC) ou par un **motif de libellé** : sur les réponses réelles, Boursorama et la SG ne
  transmettent ni nom ni IBAN d'émetteur, seulement le libellé.
- L'état d'une échéance est **dérivé** (`app.rental.status`) des allocations confirmées et de la
  date : il n'est pas stocké.
- Données brutes intactes (inv. 4) : une allocation relie une transaction à une échéance, rien
  n'est écrit sur la transaction.
"""

import enum
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.db_types import EncryptedString, UTCDateTime
from app.models._common import created_at_column, text_enum


class LeaseKind(enum.StrEnum):
    RENT = "rent"  # loyer d'un bien
    LOAN = "loan"  # remboursement attendu (prêt à un proche)


class PartyRole(enum.StrEnum):
    TENANT = "tenant"
    GUARANTOR = "guarantor"
    THIRD_PAYER = "third_payer"
    BORROWER = "borrower"


class AllocationStatus(enum.StrEnum):
    PROPOSED = "proposed"  # correspondance faible : à valider, n'entre pas dans l'état
    CONFIRMED = "confirmed"
    REJECTED = "rejected"  # proposition refusée par l'administrateur
    CANCELLED = "cancelled"  # virement rejeté par la banque (écriture inverse)


class AllocationMethod(enum.StrEnum):
    AUTO = "auto"
    MANUAL = "manual"


class IncidentKind(enum.StrEnum):
    LATE = "late"  # payé après le jour d'alerte
    UNPAID = "unpaid"  # reste dû au jour d'impayé
    GUARANTOR = "guarantor"  # payé par la caution
    REJECTED = "rejected"  # virement rejeté après encaissement
    OVERPAID = "overpaid"  # trop-perçu


class OverpaymentDecision(enum.StrEnum):
    PENDING = "pending"
    CARRY_FORWARD = "carry_forward"  # report sur l'échéance suivante
    REFUND = "refund"  # à rembourser


class Property(Base):
    __tablename__ = "property"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    address_enc: Mapped[str | None] = mapped_column(EncryptedString("property.address"))
    # Nom(s) et adresse du ou des bailleurs, imprimés sur les quittances (LOC-07).
    landlord_enc: Mapped[str | None] = mapped_column(EncryptedString("property.landlord"))
    furnished: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = created_at_column()
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class OwnerShare(Base):
    """Quote-part d'un indivisaire, en dix-millièmes (50 % = 5000) : jamais de flottant."""

    __tablename__ = "owner_share"
    __table_args__ = (
        UniqueConstraint("property_id", "owner_label"),
        CheckConstraint("share_bp BETWEEN 1 AND 10000", name="share_bp"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("property.id", ondelete="CASCADE"))
    owner_label: Mapped[str] = mapped_column(String(64))  # « moi », « co-indivisaire »
    share_bp: Mapped[int] = mapped_column(Integer)


class Lease(Base):
    __tablename__ = "lease"
    __table_args__ = (
        CheckConstraint("due_day BETWEEN 1 AND 28", name="due_day"),
        CheckConstraint("alert_day BETWEEN 1 AND 28", name="alert_day"),
        CheckConstraint("unpaid_day BETWEEN 1 AND 28", name="unpaid_day"),
        CheckConstraint("alert_day < unpaid_day", name="alert_before_unpaid"),
        CheckConstraint("due_day <= alert_day", name="due_before_alert"),
        CheckConstraint("rent_cents > 0 AND charges_cents >= 0", name="amounts"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[LeaseKind] = mapped_column(text_enum(LeaseKind, "lease_kind"))
    label: Mapped[str] = mapped_column(String(128))
    property_id: Mapped[int | None] = mapped_column(ForeignKey("property.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id"))  # compte d'encaissement
    start_date: Mapped[date] = mapped_column(Date)
    # Premier mois suivi : l'historique bancaire ne remonte qu'à 90 jours (DSP2) ; les mois
    # antérieurs, invérifiables, ne donnent pas d'échéance. Nul : début du bail.
    tracking_start: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    rent_cents: Mapped[int] = mapped_column(Integer)  # loyer hors charges, ou mensualité
    charges_cents: Mapped[int] = mapped_column(Integer, default=0)
    due_day: Mapped[int] = mapped_column(Integer, default=1)
    alert_day: Mapped[int] = mapped_column(Integer, default=5)  # Q16
    unpaid_day: Mapped[int] = mapped_column(Integer, default=10)  # Q16
    deposit_cents: Mapped[int] = mapped_column(Integer, default=0)
    # Trimestre de l'indice de référence des loyers (« 2025-T2 ») : celui du bail, puis de la
    # dernière révision (LOC-09). Nul : révision non suivie.
    irl_reference_quarter: Mapped[str | None] = mapped_column(String(7))
    # Fin de bail (dépôt de garantie) : remise des clés et état des lieux de sortie conforme
    # (None : pas encore fait). Délai de restitution : 1 mois si conforme, 2 mois sinon.
    keys_returned_on: Mapped[date | None] = mapped_column(Date)
    exit_inspection_ok: Mapped[bool | None] = mapped_column(Boolean)
    created_at: Mapped[datetime] = created_at_column()


class IrlIndex(Base):
    """Indice de référence des loyers publié par l'INSEE, en centièmes (145,17 → 14517)."""

    __tablename__ = "irl_index"
    __table_args__ = (CheckConstraint("value_hundredths > 0", name="value_positive"),)

    quarter: Mapped[str] = mapped_column(String(7), primary_key=True)  # « 2026-T2 »
    value_hundredths: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = created_at_column()


class Party(Base):
    __tablename__ = "party"

    id: Mapped[int] = mapped_column(primary_key=True)
    lease_id: Mapped[int] = mapped_column(ForeignKey("lease.id", ondelete="CASCADE"), index=True)
    role: Mapped[PartyRole] = mapped_column(text_enum(PartyRole, "party_role"))
    name_enc: Mapped[str] = mapped_column(EncryptedString("party.name"))
    iban_enc: Mapped[str | None] = mapped_column(EncryptedString("party.iban"))
    iban_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    # Motifs cherchés dans le libellé (majuscules, sans accents), chiffrés : ils contiennent
    # souvent le nom du payeur.
    label_patterns_enc: Mapped[str | None] = mapped_column(EncryptedString("party.patterns"))


class RentDue(Base):
    __tablename__ = "rent_due"
    __table_args__ = (UniqueConstraint("lease_id", "period"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    lease_id: Mapped[int] = mapped_column(ForeignKey("lease.id", ondelete="CASCADE"))
    period: Mapped[str] = mapped_column(String(7))  # « AAAA-MM »
    due_date: Mapped[date] = mapped_column(Date)
    amount_cents: Mapped[int] = mapped_column(Integer)
    # Part des charges dans le montant dû, distinguée sur la quittance (loi de 1989, art. 21).
    charges_cents: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = created_at_column()


class RentAllocation(Base):
    """Part d'une transaction affectée à une échéance (plafonnée au reste dû)."""

    __tablename__ = "rent_allocation"
    __table_args__ = (
        Index(None, "transaction_id"),
        Index(None, "due_id", "status"),
        CheckConstraint("amount_cents > 0", name="amount_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    due_id: Mapped[int] = mapped_column(ForeignKey("rent_due.id", ondelete="CASCADE"))
    transaction_id: Mapped[int] = mapped_column(ForeignKey("ledger_transaction.id"))
    amount_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[AllocationStatus] = mapped_column(
        text_enum(AllocationStatus, "allocation_status")
    )
    method: Mapped[AllocationMethod] = mapped_column(
        text_enum(AllocationMethod, "allocation_method")
    )
    payer_role: Mapped[PartyRole | None] = mapped_column(text_enum(PartyRole, "payer_role"))
    reason: Mapped[str | None] = mapped_column(String(64))  # « motif », « iban », « montant »…
    note: Mapped[str | None] = mapped_column(Text)  # justification d'un écart (LOC-06)
    # Écriture inverse qui a annulé cette allocation (virement rejeté) : un rejet n'annule
    # qu'une allocation, une seule fois, même si le moteur repasse.
    cancelled_by_transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("ledger_transaction.id"), unique=True
    )
    created_at: Mapped[datetime] = created_at_column()
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Overpayment(Base):
    """Excédent d'un encaissement au-delà du montant dû (L-06) : report ou remboursement."""

    __tablename__ = "overpayment"
    __table_args__ = (UniqueConstraint("transaction_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    lease_id: Mapped[int] = mapped_column(ForeignKey("lease.id", ondelete="CASCADE"))
    transaction_id: Mapped[int] = mapped_column(ForeignKey("ledger_transaction.id"))
    amount_cents: Mapped[int] = mapped_column(Integer)
    decision: Mapped[OverpaymentDecision] = mapped_column(
        text_enum(OverpaymentDecision, "overpayment_decision")
    )
    created_at: Mapped[datetime] = created_at_column()
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class RentIncident(Base):
    """Historique des incidents par bail (LOC-14), conservé même une fois l'échéance payée."""

    __tablename__ = "rent_incident"
    __table_args__ = (UniqueConstraint("due_id", "kind", "occurrence"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    lease_id: Mapped[int] = mapped_column(ForeignKey("lease.id", ondelete="CASCADE"), index=True)
    due_id: Mapped[int] = mapped_column(ForeignKey("rent_due.id", ondelete="CASCADE"))
    kind: Mapped[IncidentKind] = mapped_column(text_enum(IncidentKind, "incident_kind"))
    occurrence: Mapped[int] = mapped_column(Integer, default=1)  # après un rejet, un nouveau
    occurred_on: Mapped[date] = mapped_column(Date)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = created_at_column()


class DepositEventKind(enum.StrEnum):
    RECEIVED = "received"  # dépôt encaissé
    WITHHELD = "withheld"  # retenue justifiée sur la restitution
    RETURNED = "returned"  # somme restituée au locataire


class DepositEvent(Base):
    """Mouvement du dépôt de garantie d'un bail (LEG-04) : encaissement, retenue, restitution.

    Le solde à restituer est dérivé (encaissé - retenues - restitué), jamais stocké. Une opération
    bancaire rattachée l'est une seule fois et n'entre plus dans le rapprochement des loyers.
    """

    __tablename__ = "deposit_event"
    __table_args__ = (CheckConstraint("amount_cents > 0", name="amount_positive"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    lease_id: Mapped[int] = mapped_column(ForeignKey("lease.id", ondelete="CASCADE"), index=True)
    kind: Mapped[DepositEventKind] = mapped_column(text_enum(DepositEventKind, "deposit_kind"))
    amount_cents: Mapped[int] = mapped_column(Integer)
    on_date: Mapped[date] = mapped_column(Date)
    transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("ledger_transaction.id"), unique=True
    )
    note: Mapped[str | None] = mapped_column(String(200))  # motif d'une retenue
    created_at: Mapped[datetime] = created_at_column()


class ExpenseKind(enum.StrEnum):
    CO_OWNERSHIP = "copropriete"
    PROPERTY_TAX = "taxe_fonciere"
    INSURANCE = "assurance"
    WORKS = "travaux"
    LOAN = "credit"
    MAINTENANCE = "entretien"
    CFE = "cfe"
    OTHER = "autre"


class PropertyExpenseRule(Base):
    """Motif de libellé qui rattache automatiquement une dépense au bien (« SYNDIC X »)."""

    __tablename__ = "property_expense_rule"
    __table_args__ = (UniqueConstraint("property_id", "pattern"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("property.id", ondelete="CASCADE"))
    kind: Mapped[ExpenseKind] = mapped_column(text_enum(ExpenseKind, "expense_kind"))
    pattern: Mapped[str] = mapped_column(String(64))  # normalisé (majuscules, sans accents)
    created_at: Mapped[datetime] = created_at_column()


class PropertyExpense(Base):
    """Charge du bien (LOC-10) : lien vers une dépense, jamais une copie (inv. 4)."""

    __tablename__ = "property_expense"
    __table_args__ = (UniqueConstraint("transaction_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("property.id", ondelete="CASCADE"))
    transaction_id: Mapped[int] = mapped_column(ForeignKey("ledger_transaction.id"))
    kind: Mapped[ExpenseKind] = mapped_column(text_enum(ExpenseKind, "property_expense_kind"))
    rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("property_expense_rule.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = created_at_column()
