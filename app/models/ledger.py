"""Connexions, comptes, imports, transactions et liaisons du grand livre (COL-01 à COL-08)."""

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
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.db_types import EncryptedString, UTCDateTime
from app.models._common import created_at_column, text_enum

# Invariant 4 : une transaction importée n'est jamais modifiée (la suppression reste possible
# pour l'effacement d'un compte, NF-DATA-01). Utilisé par create_all ET par les migrations.
LEDGER_TRANSACTION_IMMUTABLE_SQL = """
CREATE TRIGGER ledger_transaction_immutable
BEFORE UPDATE ON ledger_transaction
BEGIN
    SELECT RAISE(ABORT, 'ledger_transaction est immuable (invariant 4)');
END
"""


class Provider(enum.StrEnum):
    ENABLE_BANKING = "enable_banking"
    FILE = "file"


class ConnectionStatus(enum.StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    EXPIRED = "expired"
    ERROR = "error"
    REVOKED = "revoked"


class AccountType(enum.StrEnum):
    CHECKING = "checking"
    SAVINGS = "savings"
    CARD = "card"
    OTHER = "other"


class ImportSource(enum.StrEnum):
    ENABLE_BANKING = "enable_banking"
    CSV = "csv"
    OFX = "ofx"


class FileFormat(enum.StrEnum):
    CSV = "csv"
    OFX = "ofx"


class ImportStatus(enum.StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class BalanceType(enum.StrEnum):
    BOOKED = "booked"
    AVAILABLE = "available"
    OTHER = "other"


class LinkMode(enum.StrEnum):
    AUTO = "auto"
    MANUAL = "manual"


class TransferStatus(enum.StrEnum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class DuplicateReason(enum.StrEnum):
    REFERENCE = "reference"
    EXACT = "exact"
    MULTISET = "multiset"
    PROBABLE = "probable"


class DuplicateStatus(enum.StrEnum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    REVERTED = "reverted"


class Connection(Base):
    __tablename__ = "connection"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[Provider] = mapped_column(text_enum(Provider, "provider"))
    institution_name: Mapped[str] = mapped_column(String(128))
    country: Mapped[str] = mapped_column(String(2))
    status: Mapped[ConnectionStatus] = mapped_column(
        text_enum(ConnectionStatus, "connection_status")
    )
    provider_session_enc: Mapped[str | None] = mapped_column(
        EncryptedString("connection.provider_session")
    )
    consent_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = created_at_column()


class Account(Base):
    __tablename__ = "account"
    __table_args__ = (
        UniqueConstraint("connection_id", "provider_account_uid"),
        CheckConstraint("syncs_per_day BETWEEN 1 AND 4", name="syncs_per_day"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Nul pour un compte alimenté uniquement par import de fichiers.
    connection_id: Mapped[int | None] = mapped_column(ForeignKey("connection.id"))
    provider_account_uid: Mapped[str | None] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(128))
    # Rôle du compte en clair, saisi par moi (« compte courant principal, salaire »…) : aide l'IA
    # à comprendre ce qu'il contient ; pseudonymisé avant envoi (inv. 8).
    description: Mapped[str | None] = mapped_column(String(500))
    type: Mapped[AccountType] = mapped_column(text_enum(AccountType, "account_type"))
    iban_enc: Mapped[str | None] = mapped_column(EncryptedString("account.iban"))
    iban_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    # Empreinte (HMAC) de l'identifiant stable du fournisseur : retrouve au renouvellement du
    # consentement un compte sans IBAN (carte).
    provider_account_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    include_in_net_worth: Mapped[bool] = mapped_column(Boolean, default=True)
    syncs_per_day: Mapped[int] = mapped_column(Integer, default=2)
    opened_at: Mapped[date | None] = mapped_column(Date)
    closed_at: Mapped[date | None] = mapped_column(Date)
    # Compte ignoré (Q19) : plus de synchro, invisible partout sauf dans « Comptes ignorés » ;
    # ses données restent en base, intactes (inv. 4). Nul = compte suivi.
    ignored_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Seuil d'alerte « solde bas prévu » (ALR-07), en centimes ; nul = pas d'alerte.
    low_balance_cents: Mapped[int | None] = mapped_column(Integer)


class AccountSyncState(Base):
    __tablename__ = "account_sync_state"

    account_id: Mapped[int] = mapped_column(ForeignKey("account.id"), primary_key=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_booking_date_seen: Mapped[date | None] = mapped_column(Date)
    last_error: Mapped[str | None] = mapped_column(Text)


class ProviderCall(Base):
    """Chaque requête envoyée à une banque : budget DSP2 (inv. 10), reprises comprises."""

    __tablename__ = "provider_call"
    __table_args__ = (Index(None, "account_id", "requested_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("account.id"))
    connection_id: Mapped[int | None] = mapped_column(ForeignKey("connection.id"))
    requested_at: Mapped[datetime] = mapped_column(UTCDateTime)
    unattended: Mapped[bool] = mapped_column(Boolean)
    method: Mapped[str] = mapped_column(String(8))
    path_template: Mapped[str] = mapped_column(String(255))
    http_status: Mapped[int | None] = mapped_column(Integer)
    job_run_id: Mapped[int | None] = mapped_column(ForeignKey("job_run.id"))


class ImportProfile(Base):
    __tablename__ = "import_profile"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    institution_name: Mapped[str] = mapped_column(String(128))
    format: Mapped[FileFormat] = mapped_column(text_enum(FileFormat, "file_format"))
    encoding: Mapped[str] = mapped_column(String(32), default="utf-8")
    delimiter: Mapped[str] = mapped_column(String(1), default=";")
    decimal_sep: Mapped[str] = mapped_column(String(1), default=",")
    date_format: Mapped[str] = mapped_column(String(32), default="%d/%m/%Y")
    skip_rows: Mapped[int] = mapped_column(Integer, default=0)
    column_map: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ImportBatch(Base):
    __tablename__ = "import_batch"
    __table_args__ = (Index(None, "account_id", "started_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id"))
    source: Mapped[ImportSource] = mapped_column(text_enum(ImportSource, "import_source"))
    profile_id: Mapped[int | None] = mapped_column(ForeignKey("import_profile.id"))
    file_name: Mapped[str | None] = mapped_column(String(255))
    file_sha256: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = created_at_column()
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    status: Mapped[ImportStatus] = mapped_column(text_enum(ImportStatus, "import_status"))
    n_seen: Mapped[int] = mapped_column(Integer, default=0)
    n_inserted: Mapped[int] = mapped_column(Integer, default=0)
    n_duplicates: Mapped[int] = mapped_column(Integer, default=0)
    job_run_id: Mapped[int | None] = mapped_column(ForeignKey("job_run.id"))


class RawPayload(Base):
    """Donnée brute reçue, compressée puis chiffrée : permet de rejouer un import (COL-08)."""

    __tablename__ = "raw_payload"

    id: Mapped[int] = mapped_column(primary_key=True)
    import_batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batch.id"))
    source: Mapped[ImportSource] = mapped_column(text_enum(ImportSource, "raw_payload_source"))
    received_at: Mapped[datetime] = created_at_column()
    content_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    content_type: Mapped[str] = mapped_column(String(64))
    content_enc: Mapped[bytes] = mapped_column(LargeBinary)


class LedgerTransaction(Base):
    """Transaction importée, **immuable** (inv. 4) ; le reste passe par des tables de liaison."""

    __tablename__ = "ledger_transaction"
    __table_args__ = (
        UniqueConstraint("account_id", "dedupe_key"),
        Index(None, "account_id", "booking_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id"))
    # « ext:<source>:<identifiant externe> », sinon « fp:<source>:<empreinte>:<rang> » (I-07) :
    # propre à la source ; les doublons entre sources passent par duplicate_link (T1.16).
    dedupe_key: Mapped[str] = mapped_column(String(160))
    external_id: Mapped[str | None] = mapped_column(String(128))
    fingerprint: Mapped[str] = mapped_column(String(64))
    occurrence: Mapped[int] = mapped_column(Integer, default=1)
    booking_date: Mapped[date] = mapped_column(Date, index=True)
    value_date: Mapped[date | None] = mapped_column(Date)
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    counterparty_name_enc: Mapped[str | None] = mapped_column(
        EncryptedString("ledger_transaction.counterparty_name")
    )
    counterparty_iban_enc: Mapped[str | None] = mapped_column(
        EncryptedString("ledger_transaction.counterparty_iban")
    )
    counterparty_iban_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    label_raw: Mapped[str] = mapped_column(Text)
    label_clean: Mapped[str] = mapped_column(Text)
    bank_tx_code: Mapped[str | None] = mapped_column(String(64))
    raw_payload_id: Mapped[int | None] = mapped_column(ForeignKey("raw_payload.id"))
    import_batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batch.id"))
    created_at: Mapped[datetime] = created_at_column()


class BalanceSnapshot(Base):
    __tablename__ = "balance_snapshot"
    __table_args__ = (UniqueConstraint("account_id", "as_of_date", "balance_type"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id"))
    as_of_date: Mapped[date] = mapped_column(Date)
    balance_type: Mapped[BalanceType] = mapped_column(text_enum(BalanceType, "balance_type"))
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    fetched_at: Mapped[datetime] = created_at_column()
    raw_payload_id: Mapped[int | None] = mapped_column(ForeignKey("raw_payload.id"))


_ACTIVE_LINK = text("status IN ('proposed', 'confirmed')")


class DuplicateLink(Base):
    """Doublon entre sources (Q5) : l'API est canonique ; réversible, rien n'est supprimé."""

    __tablename__ = "duplicate_link"
    __table_args__ = (
        CheckConstraint("canonical_tx_id <> duplicate_tx_id", name="distinct_transactions"),
        Index(
            "uq_duplicate_link_active_duplicate",
            "duplicate_tx_id",
            unique=True,
            sqlite_where=_ACTIVE_LINK,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    canonical_tx_id: Mapped[int] = mapped_column(ForeignKey("ledger_transaction.id"))
    duplicate_tx_id: Mapped[int] = mapped_column(ForeignKey("ledger_transaction.id"))
    mode: Mapped[LinkMode] = mapped_column(text_enum(LinkMode, "duplicate_mode"))
    reason: Mapped[DuplicateReason] = mapped_column(text_enum(DuplicateReason, "duplicate_reason"))
    status: Mapped[DuplicateStatus] = mapped_column(text_enum(DuplicateStatus, "duplicate_status"))
    created_at: Mapped[datetime] = created_at_column()
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class TransferLink(Base):
    """Virement interne entre deux comptes du périmètre (COL-06)."""

    __tablename__ = "transfer_link"
    __table_args__ = (
        CheckConstraint("debit_tx_id <> credit_tx_id", name="distinct_transactions"),
        Index(
            "uq_transfer_link_active_debit", "debit_tx_id", unique=True, sqlite_where=_ACTIVE_LINK
        ),
        Index(
            "uq_transfer_link_active_credit", "credit_tx_id", unique=True, sqlite_where=_ACTIVE_LINK
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    debit_tx_id: Mapped[int] = mapped_column(ForeignKey("ledger_transaction.id"))
    credit_tx_id: Mapped[int] = mapped_column(ForeignKey("ledger_transaction.id"))
    method: Mapped[LinkMode] = mapped_column(text_enum(LinkMode, "transfer_method"))
    status: Mapped[TransferStatus] = mapped_column(text_enum(TransferStatus, "transfer_status"))
    carried_by_link_id: Mapped[int | None] = mapped_column(ForeignKey("duplicate_link.id"))
    superseded_by_link_id: Mapped[int | None] = mapped_column(ForeignKey("duplicate_link.id"))
    created_at: Mapped[datetime] = created_at_column()
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class AssetKind(enum.StrEnum):
    SAVINGS = "livret"
    LIFE_INSURANCE = "assurance_vie"
    PEA = "pea"
    RETIREMENT = "per"
    SECURITIES = "titres"
    REAL_ESTATE = "immobilier"
    LOAN = "credit"  # capital restant dû : valeur négative
    LOAN_GIVEN = "pret_accorde"  # créance sur un proche
    OTHER = "autre"


class ManualAsset(Base):
    """Actif ou dette saisi à la main (COL-05) : livret, assurance-vie, PEA, bien, crédit…"""

    __tablename__ = "manual_asset"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[AssetKind] = mapped_column(text_enum(AssetKind, "asset_kind"))
    # Compté dans le patrimoine net ; un bien en indivision peut ne compter qu'à hauteur de ma
    # quote-part : la valeur saisie est alors ma part.
    counted: Mapped[bool] = mapped_column(Boolean, default=True)
    property_id: Mapped[int | None] = mapped_column(ForeignKey("property.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = created_at_column()
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class AssetValuation(Base):
    """Valeur d'un actif à une date (centimes ; négative pour une dette), une par jour."""

    __tablename__ = "asset_valuation"
    __table_args__ = (UniqueConstraint("asset_id", "as_of_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("manual_asset.id", ondelete="CASCADE"))
    as_of_date: Mapped[date] = mapped_column(Date)
    value_cents: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = created_at_column()
