"""Échéances administratives et fiscales (FIS-04, LOC-10) : taxe foncière, CFE, déclaration…

- Une échéance peut se rattacher à un bien et à l'opération qui l'a réglée (lien, jamais une
  copie : inv. 4). Une opération ne règle qu'une échéance (contrainte unique).
- Montants en centimes (inv. 2). `recoverable_cents` : part récupérable sur le locataire (taxe
  d'enlèvement des ordures ménagères comprise dans la taxe foncière).
- `yearly` : une fois réglée, l'échéance de l'année suivante est créée (même jour, montant à
  saisir à réception de l'avis).
- Dates calendaires sans fuseau (inv. 9) ; `done_at` est un instant UTC.
"""

import enum
from datetime import date, datetime

from sqlalchemy import Boolean, CheckConstraint, Date, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.db_types import UTCDateTime
from app.models._common import created_at_column, text_enum


class DeadlineKind(enum.StrEnum):
    PROPERTY_TAX = "taxe_fonciere"
    CFE = "cfe"  # cotisation foncière des entreprises (loueur en meublé)
    INCOME_TAX_RETURN = "declaration"  # déclaration des revenus
    TAX_PAYMENT = "impot"  # solde ou acompte d'impôt
    INSURANCE = "assurance"  # assurance du bien (PNO), attestation du locataire
    OTHER = "autre"


class Deadline(Base):
    __tablename__ = "deadline"
    __table_args__ = (
        Index(None, "due_date"),
        CheckConstraint("amount_cents IS NULL OR amount_cents > 0", name="amount"),
        CheckConstraint(
            "recoverable_cents IS NULL OR (amount_cents IS NOT NULL"
            " AND recoverable_cents BETWEEN 0 AND amount_cents)",
            name="recoverable",
        ),
        CheckConstraint("remind_days BETWEEN 0 AND 90", name="remind_days"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[DeadlineKind] = mapped_column(text_enum(DeadlineKind, "deadline_kind"))
    label: Mapped[str | None] = mapped_column(String(128))  # précision libre (« avis 2026 »)
    due_date: Mapped[date] = mapped_column(Date)
    property_id: Mapped[int | None] = mapped_column(
        ForeignKey("property.id", ondelete="CASCADE"), index=True
    )
    amount_cents: Mapped[int | None] = mapped_column(Integer)
    recoverable_cents: Mapped[int | None] = mapped_column(Integer)
    remind_days: Mapped[int] = mapped_column(Integer, default=7)
    yearly: Mapped[bool] = mapped_column(Boolean, default=True)
    transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("ledger_transaction.id"), unique=True
    )
    done_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = created_at_column()
