"""Import CSV avec profils par banque (COL-02, COL-03)."""

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select

from app.connectors.file_import.csv_import import (
    ColumnMap,
    CsvFormatError,
    CsvProfile,
    csv_pages,
    parse_csv,
)
from app.connectors.file_import.profiles import BOURSORAMA, SOCIETE_GENERALE
from app.ledger.ingest import ingest
from app.models import LedgerTransaction
from app.models.ledger import ImportSource

DATA = Path(__file__).resolve().parents[2] / "data" / "csv"


def test_boursorama_sample() -> None:
    transactions = parse_csv((DATA / "boursorama_exemple.csv").read_bytes(), BOURSORAMA)
    assert [t.amount for t in transactions] == [-420, -420, 85000, -110235]
    assert transactions[0].booking_date == date(2026, 9, 3)
    assert transactions[0].label == "CARTE 02/09/26 BOULANGERIE EXEMPLE CB*1234"
    assert all(t.currency == "EUR" and t.external_id is None for t in transactions)


def test_societe_generale_sample_skips_preamble_and_decodes_cp1252() -> None:
    transactions = parse_csv((DATA / "societe_generale_exemple.csv").read_bytes(), SOCIETE_GENERALE)
    assert [t.amount for t in transactions] == [-420, 120000, -3590]
    assert transactions[1].label == "DE: BRUNO FICTIF MOTIF: LOYER SEPTEMBRE"
    assert transactions[0].booking_date == date(2026, 9, 3)


def test_same_csv_imported_twice_creates_no_duplicate(engine: Engine) -> None:
    """Scénario d'acceptation du CDC : « Même fichier CSV importé deux fois »."""
    content = (DATA / "boursorama_exemple.csv").read_bytes()
    first = ingest(
        engine, account_id=1, source=ImportSource.CSV, pages=csv_pages(content, BOURSORAMA)
    )
    second = ingest(
        engine, account_id=1, source=ImportSource.CSV, pages=csv_pages(content, BOURSORAMA)
    )
    assert (first.inserted, second.inserted, second.duplicates) == (4, 0, 4)
    with engine.connect() as connection:
        count = connection.execute(select(func.count()).select_from(LedgerTransaction)).scalar_one()
    assert count == 4  # dont les deux achats identiques du 03/09


def _profile(**overrides: object) -> CsvProfile:
    base: dict[str, object] = {
        "name": "test",
        "institution": "Banque Exemple",
        "encoding": "utf-8",
        "delimiter": ";",
        "decimal_sep": ",",
        "date_formats": ("%d/%m/%Y",),
        "columns": ColumnMap(date="Date", label=("Libellé",), amount="Montant"),
    }
    base.update(overrides)
    return CsvProfile(**base)  # type: ignore[arg-type]


def test_malformed_amount_names_the_line_and_imports_nothing() -> None:
    content = "Date;Libellé;Montant\n01/09/2026;A;-1,00\n02/09/2026;B;douze\n".encode()
    with pytest.raises(CsvFormatError, match=r"ligne 3.*douze"):
        parse_csv(content, _profile())


def test_bad_date_is_reported() -> None:
    content = "Date;Libellé;Montant\n2026-09-01;A;-1,00\n".encode()
    with pytest.raises(CsvFormatError, match=r"ligne 2.*date"):
        parse_csv(content, _profile())


def test_missing_header_is_reported() -> None:
    with pytest.raises(CsvFormatError, match="en-tête introuvable"):
        parse_csv(b"a;b;c\n1;2;3\n", _profile())


def test_wrong_encoding_is_reported() -> None:
    content = "Date;Libellé;Montant\n01/09/2026;Café;-1,00\n".encode("cp1252")
    with pytest.raises(CsvFormatError, match="encodage"):
        parse_csv(content, _profile())


def test_debit_and_credit_columns() -> None:
    profile = _profile(
        columns=ColumnMap(date="Date", label=("Libellé",), debit="Débit", credit="Crédit")
    )
    content = "Date;Libellé;Débit;Crédit\n01/09/2026;A;12,50;\n02/09/2026;B;;1 000,00\n".encode()
    assert [t.amount for t in parse_csv(content, profile)] == [-1250, 100000]


def test_row_with_both_debit_and_credit_is_rejected() -> None:
    profile = _profile(
        columns=ColumnMap(date="Date", label=("Libellé",), debit="Débit", credit="Crédit")
    )
    with pytest.raises(CsvFormatError, match="ligne 2"):
        parse_csv("Date;Libellé;Débit;Crédit\n01/09/2026;A;1,00;2,00\n".encode(), profile)


def test_header_only_gives_no_transaction() -> None:
    assert parse_csv("Date;Libellé;Montant\n".encode(), _profile()) == []


def test_blank_lines_are_ignored() -> None:
    content = "Date;Libellé;Montant\n\n01/09/2026;A;-1,00\n;;\n".encode()
    assert len(parse_csv(content, _profile())) == 1


def test_raw_content_is_kept_for_replay() -> None:
    content = (DATA / "boursorama_exemple.csv").read_bytes()
    [page] = csv_pages(content, BOURSORAMA)
    assert page.raw.content == content
    assert page.raw.content_type == "text/csv"
