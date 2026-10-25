"""Éléments communs aux modèles : horodatage UTC, énumérations stockées en texte contrôlé."""

import enum
from datetime import datetime

from sqlalchemy import Enum
from sqlalchemy.orm import Mapped, mapped_column

from app.db_types import UTCDateTime
from app.timeutil import utcnow


def text_enum(enum_class: type[enum.StrEnum], name: str) -> Enum:
    """Énumération stockée en TEXT avec contrainte CHECK (pas de type natif sous SQLite)."""
    return Enum(
        enum_class,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=max(len(member.value) for member in enum_class),
        values_callable=lambda members: [member.value for member in members],
        validate_strings=True,
    )


def created_at_column() -> Mapped[datetime]:
    return mapped_column(UTCDateTime, default=utcnow, nullable=False)
