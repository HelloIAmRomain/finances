"""Import d'un relevé de fichier de bout en bout (COL-02) : lecture, ingestion, puis liaisons
(doublons entre sources, virements internes) et règles de catégorisation."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Engine, select

from app.categorize.service import apply_rules
from app.connectors.base import Page, RemoteTransaction
from app.connectors.file_import.csv_import import CsvFormatError, csv_pages
from app.connectors.file_import.ofx_import import OfxFormatError, ofx_pages
from app.connectors.file_import.profiles import BUILTIN_PROFILES
from app.ledger.cross_dedupe import detect_duplicates
from app.ledger.ingest import IngestStats, ingest
from app.ledger.transfers import detect_transfers
from app.models import LedgerTransaction
from app.models.ledger import ImportSource

MAX_FILE_BYTES = 5 * 1024 * 1024
OFX_FORMAT = "ofx"


class ImportFileError(ValueError):
    """Fichier refusé (format, taille, contenu)."""


@dataclass(frozen=True)
class ImportOutcome:
    stats: IngestStats
    duplicates_confirmed: int
    duplicates_proposed: int


def read_pages(
    content: bytes, file_format: str
) -> tuple[ImportSource, list[Page[RemoteTransaction]]]:
    if len(content) > MAX_FILE_BYTES:
        raise ImportFileError("Fichier trop volumineux (5 Mo au plus).")
    try:
        if file_format == OFX_FORMAT:
            return ImportSource.OFX, ofx_pages(content)
        profile = BUILTIN_PROFILES.get(file_format)
        if profile is None:
            raise ImportFileError(f"Format inconnu : {file_format}.")
        return ImportSource.CSV, csv_pages(content, profile)
    except (CsvFormatError, OfxFormatError) as exc:
        raise ImportFileError(str(exc)) from None


def import_file(
    engine: Engine,
    *,
    account_id: int,
    content: bytes,
    file_format: str,
    file_name: str,
    now: datetime,
) -> ImportOutcome:
    source, pages = read_pages(content, file_format)
    stats = ingest(engine, account_id=account_id, source=source, pages=pages, file_name=file_name)
    duplicates = detect_duplicates(engine, account_id=account_id)
    detect_transfers(engine)
    with engine.connect() as connection:
        new_ids = list(
            connection.scalars(
                select(LedgerTransaction.id).where(
                    LedgerTransaction.import_batch_id == stats.batch_id
                )
            )
        )
    apply_rules(engine, now=now, transaction_ids=new_ids)
    return ImportOutcome(stats, duplicates.confirmed, duplicates.proposed)
