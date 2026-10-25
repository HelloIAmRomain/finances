"""Exports CSV destinés à un tableur (export des opérations, récapitulatif fiscal FIS-06).

Un texte qui commence par « = », « + », « - », « @ », une tabulation ou un retour chariot serait
lu comme une formule par le tableur : un libellé bancaire tel que « =HYPERLINK(…) » s'exécuterait
à l'ouverture (injection de formule). Ces cellules de texte sont préfixées d'une apostrophe, qui
les garde telles quelles à l'affichage. Les montants, passés en centimes (`int`), deviennent des
nombres à la française (« -1234,56 ») et ne sont jamais préfixés.
"""

import csv
import io
from collections.abc import Iterable, Iterator, Sequence

from app.money import Cents, format_decimal_fr

FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")
BOM = "\ufeff"  # ouverture directe dans un tableur, accents compris
CHUNK_CHARS = 64 * 1024  # caractères par bloc envoyé


def text_cell(value: str) -> str:
    return f"'{value}" if value.startswith(FORMULA_STARTS) else value


def cell(value: str | int) -> str:
    """Texte neutralisé, ou montant en centimes formaté."""
    if isinstance(value, bool):
        raise TypeError("booléen : écrire « oui » ou une cellule vide")
    if isinstance(value, int):
        return format_decimal_fr(Cents(value))
    return text_cell(value)


def csv_lines(rows: Iterable[Sequence[str | int]]) -> Iterator[str]:
    """Le fichier CSV (séparateur « ; », BOM en tête), produit par blocs d'environ 64 Ko : la
    mémoire reste bornée pour les gros exports, sans un envoi réseau par ligne (60 000
    opérations : 13 s ligne à ligne, moins d'une seconde par blocs)."""
    buffer = io.StringIO()
    buffer.write(BOM)
    writer = csv.writer(buffer, delimiter=";")
    for row in rows:
        writer.writerow([cell(value) for value in row])
        if buffer.tell() >= CHUNK_CHARS:
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate()
    yield buffer.getvalue()
