"""Import de relevés OFX (COL-02), versions 1.x (SGML, balises non fermées) et 2.x (XML).

Lecteur volontairement minimal et typé, à la place de la bibliothèque `ofxparse` (plus maintenue
depuis 2021, sans types) : seuls les champs utiles d'une opération sont lus.
- `FITID` (identifiant de l'opération chez la banque) devient l'identifiant externe (`ext:`) ;
- la date comptable est la partie calendaire de `DTPOSTED`, sans conversion de fuseau (inv. 9) ;
- le libellé réunit `NAME` et `MEMO`.
"""

import html
import re
from datetime import date, datetime

from app.connectors.base import Page, RawResponse, RemoteTransaction
from app.money import DecimalSeparator, InvalidAmountError, parse_amount

_TRANSACTION = re.compile(r"<STMTTRN>(.*?)</STMTTRN>", re.IGNORECASE | re.DOTALL)
_XML_ENCODING = re.compile(rb"""<\?xml[^>]*encoding=["']([A-Za-z0-9_-]+)["']""")
_SGML_CHARSET = re.compile(rb"^CHARSET:\s*(\S+)", re.MULTILINE)
_SGML_ENCODING = re.compile(rb"^ENCODING:\s*(\S+)", re.MULTILINE)


class OfxFormatError(ValueError):
    """Fichier OFX illisible ou opération incomplète."""


def _field(block: str, name: str) -> str:
    match = re.search(rf"<{name}>([^<\r\n]*)", block, re.IGNORECASE)
    return html.unescape(match.group(1)).strip() if match else ""


def _encoding(content: bytes) -> str:
    if match := _XML_ENCODING.search(content[:200]):
        return match.group(1).decode()
    encoding = _SGML_ENCODING.search(content[:1000])
    if encoding and encoding.group(1).upper() in {b"UTF-8", b"UTF8"}:
        return "utf-8"
    charset = _SGML_CHARSET.search(content[:1000])
    if charset and charset.group(1).isdigit():
        return f"cp{charset.group(1).decode()}"
    return "cp1252"


def _decode(content: bytes) -> str:
    encoding = _encoding(content)
    try:
        return content.decode(encoding)
    except (UnicodeDecodeError, LookupError) as exc:
        raise OfxFormatError(f"encodage OFX illisible ({encoding}) : {exc}") from None


def _posted_date(raw: str, fitid: str) -> date:
    try:
        return datetime.strptime(raw[:8], "%Y%m%d").date()  # noqa: DTZ007 (date calendaire)
    except ValueError:
        raise OfxFormatError(f"opération {fitid} : date invalide {raw!r}") from None


def parse_ofx(content: bytes) -> list[RemoteTransaction]:
    text = _decode(content)
    if "<OFX>" not in text.upper():
        raise OfxFormatError("fichier OFX invalide : balise <OFX> absente")
    currency = _field(text, "CURDEF") or "EUR"
    transactions = []
    for index, match in enumerate(_TRANSACTION.finditer(text), start=1):
        block = match.group(1)
        fitid = _field(block, "FITID")
        if not fitid:
            raise OfxFormatError(f"opération n° {index} sans FITID")
        amount_text = _field(block, "TRNAMT")
        separator: DecimalSeparator = "," if "," in amount_text and "." not in amount_text else "."
        try:
            amount = parse_amount(amount_text, decimal_sep=separator)
        except InvalidAmountError:
            raise OfxFormatError(f"opération {fitid} : montant invalide {amount_text!r}") from None
        name, memo = _field(block, "NAME"), _field(block, "MEMO")
        label = f"{name} {memo}".strip() if memo and memo not in name else name
        transactions.append(
            RemoteTransaction(
                external_id=fitid,
                booking_date=_posted_date(_field(block, "DTPOSTED"), fitid),
                value_date=None,
                amount=amount,
                currency=currency,
                label=label,
                counterparty_name=None,
                counterparty_iban=None,
                bank_tx_code=_field(block, "TRNTYPE") or None,
            )
        )
    return transactions


def ofx_pages(content: bytes) -> list[Page[RemoteTransaction]]:
    return [Page(parse_ofx(content), RawResponse(content, "application/x-ofx"))]
