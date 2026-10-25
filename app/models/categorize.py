"""Catégories, règles, catégorisations et file de validation (ANA-01, ANA-04, T1.16b)."""

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.db_types import UTCDateTime
from app.models._common import created_at_column, text_enum


class CategoryKind(enum.StrEnum):
    EXPENSE = "expense"
    INCOME = "income"
    TRANSFER = "transfer"
    NEUTRAL = "neutral"


class CategorizationSource(enum.StrEnum):
    # Priorité décroissante : manuel > règle > IA (T1.16b, ANA-01).
    MANUAL = "manual"
    RULE = "rule"
    AI = "ai"


class ReviewKind(enum.StrEnum):
    DUPLICATE = "duplicate"
    CATEGORY_CONFLICT = "category_conflict"
    TRANSFER_CONFLICT = "transfer_conflict"
    TRANSFER_CANDIDATE = "transfer_candidate"
    AI_SUGGESTION = "ai_suggestion"  # catégorie proposée par l'IA sous le seuil de confiance


class ReviewStatus(enum.StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class Category(Base):
    __tablename__ = "category"
    __table_args__ = (UniqueConstraint("parent_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("category.id"))
    name: Mapped[str] = mapped_column(String(64))
    kind: Mapped[CategoryKind] = mapped_column(text_enum(CategoryKind, "category_kind"))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Rule(Base):
    __tablename__ = "rule"
    __table_args__ = (Index(None, "is_active", "priority"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    priority: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    conditions: Mapped[dict[str, Any]] = mapped_column(JSON)
    category_id: Mapped[int] = mapped_column(ForeignKey("category.id"))
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = created_at_column()


class Categorization(Base):
    """Ligne de catégorisation ; plusieurs lignes actives = ventilation (ANA-04).

    Historique par remplacement : une ligne n'est jamais modifiée sur le fond, elle est
    marquée remplacée (`superseded_at`) et une nouvelle ligne est créée.
    """

    __tablename__ = "categorization"
    __table_args__ = (
        CheckConstraint(
            "confidence_permille IS NULL OR confidence_permille BETWEEN 0 AND 1000",
            name="confidence_range",
        ),
        Index(
            "ix_categorization_active_transaction",
            "transaction_id",
            sqlite_where=text("superseded_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("ledger_transaction.id"))
    category_id: Mapped[int] = mapped_column(ForeignKey("category.id"))
    amount_cents: Mapped[int] = mapped_column(Integer)
    source: Mapped[CategorizationSource] = mapped_column(
        text_enum(CategorizationSource, "categorization_source")
    )
    rule_id: Mapped[int | None] = mapped_column(ForeignKey("rule.id"))
    confidence_permille: Mapped[int | None] = mapped_column(Integer)
    carried_by_link_id: Mapped[int | None] = mapped_column(ForeignKey("duplicate_link.id"))
    created_at: Mapped[datetime] = created_at_column()
    superseded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    superseded_by_link_id: Mapped[int | None] = mapped_column(ForeignKey("duplicate_link.id"))


class ReviewItem(Base):
    """Élément de la file de validation (doublon, conflit, virement proposé), idempotent."""

    __tablename__ = "review_item"
    __table_args__ = (Index(None, "status", "kind"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[ReviewKind] = mapped_column(text_enum(ReviewKind, "review_kind"))
    dedupe_key: Mapped[str] = mapped_column(String(160), unique=True)
    duplicate_link_id: Mapped[int | None] = mapped_column(ForeignKey("duplicate_link.id"))
    transaction_id: Mapped[int | None] = mapped_column(ForeignKey("ledger_transaction.id"))
    other_transaction_id: Mapped[int | None] = mapped_column(ForeignKey("ledger_transaction.id"))
    status: Mapped[ReviewStatus] = mapped_column(text_enum(ReviewStatus, "review_status"))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = created_at_column()
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Budget(Base):
    """Enveloppe mensuelle d'une catégorie (ANA-06) ; sur une racine, sous-catégories comprises."""

    __tablename__ = "budget"
    __table_args__ = (CheckConstraint("monthly_cents > 0", name="monthly_positive"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("category.id"), unique=True)
    monthly_cents: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = created_at_column()
