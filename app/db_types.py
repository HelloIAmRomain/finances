"""Types de colonnes SQLAlchemy propres au projet."""

from datetime import UTC, datetime

from sqlalchemy import DateTime, Dialect, LargeBinary
from sqlalchemy.types import TypeDecorator

from app.crypto import decrypt, encrypt
from app.timeutil import ensure_utc


class UTCDateTime(TypeDecorator[datetime]):
    """Instant stocké en UTC (texte ISO triable sous SQLite), relu avec le fuseau UTC.

    Refuse un `datetime` sans fuseau à l'écriture (inv. 9).
    """

    impl = DateTime(timezone=False)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return ensure_utc(value).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


class EncryptedString(TypeDecorator[str]):
    """Texte chiffré au niveau applicatif (AES-GCM, contexte = nom logique de la colonne)."""

    impl = LargeBinary
    cache_ok = True

    def __init__(self, context: str) -> None:
        super().__init__()
        self.context = context

    def process_bind_param(self, value: str | None, dialect: Dialect) -> bytes | None:
        if value is None:
            return None
        return encrypt(value, context=self.context)

    def process_result_value(self, value: bytes | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return decrypt(value, context=self.context)
