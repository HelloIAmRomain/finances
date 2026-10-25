"""Import de relevés CSV selon un profil par banque (COL-02).

- La ligne d'en-tête est cherchée dans le fichier (les banques ajoutent souvent des lignes
  d'introduction : numéro de compte, solde…).
- Montant en une colonne signée, ou en deux colonnes débit / crédit.
- Tout ou rien : la moindre ligne invalide lève `CsvFormatError` (avec son numéro), avant
  toute écriture en base.
"""

import csv
import io
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime

from app.connectors.base import Page, RawResponse, RemoteTransaction
from app.money import Cents, DecimalSeparator, InvalidAmountError, parse_amount

Cell = Callable[[list[str], str | None], str]


class CsvFormatError(ValueError):
    """Fichier CSV illisible ou non conforme au profil."""


@dataclass(frozen=True)
class ColumnMap:
    date: str
    label: tuple[str, ...]  # première colonne non vide retenue
    amount: str | None = None
    debit: str | None = None
    credit: str | None = None
    value_date: str | None = None
    currency: str | None = None
    external_id: str | None = None

    def required(self) -> list[str]:
        names = [self.date, *self.label]
        names += [self.amount] if self.amount else [c for c in (self.debit, self.credit) if c]
        return names


@dataclass(frozen=True)
class CsvProfile:
    name: str
    institution: str
    encoding: str
    delimiter: str
    decimal_sep: DecimalSeparator
    date_formats: tuple[str, ...]
    columns: ColumnMap
    default_currency: str = "EUR"


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.strip().strip("﻿").lower())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _decode(content: bytes, encoding: str) -> str:
    try:
        return content.decode(encoding)
    except UnicodeDecodeError as exc:
        raise CsvFormatError(
            f"encodage inattendu : le fichier n'est pas en {encoding} (octet {exc.start})"
        ) from None


def _parse_date(value: str, formats: tuple[str, ...], line: int) -> date:
    for fmt in formats:
        try:
            return datetime.strptime(value.strip(), fmt).date()  # noqa: DTZ007 (date calendaire)
        except ValueError:
            continue
    raise CsvFormatError(f"ligne {line} : date invalide {value!r} (formats {', '.join(formats)})")


def parse_csv(content: bytes, profile: CsvProfile) -> list[RemoteTransaction]:
    text = _decode(content, profile.encoding)
    rows = list(csv.reader(io.StringIO(text), delimiter=profile.delimiter))
    wanted = [_normalize(name) for name in profile.columns.required()]
    header_index = next(
        (
            i
            for i, row in enumerate(rows)
            if all(name in [_normalize(c) for c in row] for name in wanted)
        ),
        None,
    )
    if header_index is None:
        raise CsvFormatError(
            f"en-tête introuvable : colonnes attendues {', '.join(profile.columns.required())}"
        )
    positions = {_normalize(name): i for i, name in enumerate(rows[header_index])}

    def cell(row: list[str], name: str | None) -> str:
        if name is None:
            return ""
        index = positions.get(_normalize(name))
        return row[index].strip() if index is not None and index < len(row) else ""

    transactions = []
    for offset, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        if not any(value.strip() for value in row):
            continue
        transactions.append(_transaction(row, offset, profile, cell))
    return transactions


def _transaction(row: list[str], line: int, profile: CsvProfile, cell: Cell) -> RemoteTransaction:
    columns = profile.columns
    try:
        if columns.amount:
            amount = parse_amount(cell(row, columns.amount), decimal_sep=profile.decimal_sep)
        else:
            debit, credit = cell(row, columns.debit), cell(row, columns.credit)
            if bool(debit) == bool(credit):
                raise CsvFormatError(f"ligne {line} : débit et crédit tous deux vides ou remplis")
            value = parse_amount(debit or credit, decimal_sep=profile.decimal_sep)
            amount = Cents(-abs(value) if debit else abs(value))
    except InvalidAmountError as exc:
        raise CsvFormatError(f"ligne {line} : {exc}") from None
    label = next((cell(row, name) for name in columns.label if cell(row, name)), "")
    value_date = cell(row, columns.value_date)
    return RemoteTransaction(
        external_id=cell(row, columns.external_id) or None,
        booking_date=_parse_date(cell(row, columns.date), profile.date_formats, line),
        value_date=_parse_date(value_date, profile.date_formats, line) if value_date else None,
        amount=amount,
        currency=cell(row, columns.currency) or profile.default_currency,
        label=label,
        counterparty_name=None,
        counterparty_iban=None,
        bank_tx_code=None,
    )


def csv_pages(content: bytes, profile: CsvProfile) -> list[Page[RemoteTransaction]]:
    """Le fichier entier forme une page ; son contenu brut est conservé (COL-08)."""
    return [Page(parse_csv(content, profile), RawResponse(content, "text/csv"))]
