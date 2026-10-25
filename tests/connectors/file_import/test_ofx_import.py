"""Import OFX 1.x (SGML) et 2.x (XML) (COL-02)."""

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import Engine

from app.connectors.file_import.ofx_import import OfxFormatError, ofx_pages, parse_ofx
from app.ledger.ingest import ingest
from app.models.ledger import ImportSource

DATA = Path(__file__).resolve().parents[2] / "data" / "ofx"


def test_sgml_statement() -> None:
    transactions = parse_ofx((DATA / "releve_sgml_exemple.ofx").read_bytes())
    assert [t.external_id for t in transactions] == ["F0001", "F0002", "F0003"]
    assert [t.amount for t in transactions] == [-420, 120000, -3590]  # virgule décimale tolérée
    assert transactions[1].booking_date == date(2026, 9, 5)
    assert transactions[1].label == "VIR LOYER SEPTEMBRE Café Démo"  # CHARSET:1252 respecté
    assert transactions[2].label == "PRLV ASSURANCE"
    assert all(t.currency == "EUR" for t in transactions)


def test_xml_statement() -> None:
    transactions = parse_ofx((DATA / "releve_xml_exemple.ofx").read_bytes())
    assert [t.amount for t in transactions] == [-420, 85000]
    assert transactions[0].booking_date == date(2026, 9, 3)
    assert transactions[1].label == "VIR LOYER Chloé Démo & co"  # entités XML décodées


def test_import_twice_is_idempotent(engine: Engine) -> None:
    content = (DATA / "releve_sgml_exemple.ofx").read_bytes()
    first = ingest(engine, account_id=1, source=ImportSource.OFX, pages=ofx_pages(content))
    second = ingest(engine, account_id=1, source=ImportSource.OFX, pages=ofx_pages(content))
    assert (first.inserted, second.inserted) == (3, 0)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"pas un fichier ofx", "OFX"),
        (b"<OFX><STMTTRN><DTPOSTED>20260901<TRNAMT>-1.00</STMTTRN></OFX>", "FITID"),
        (b"<OFX><STMTTRN><FITID>A<DTPOSTED>2026<TRNAMT>-1.00</STMTTRN></OFX>", "date"),
        (b"<OFX><STMTTRN><FITID>A<DTPOSTED>20260901<TRNAMT>abc</STMTTRN></OFX>", "montant"),
    ],
)
def test_malformed_statement(content: bytes, message: str) -> None:
    with pytest.raises(OfxFormatError, match=message):
        parse_ofx(content)
