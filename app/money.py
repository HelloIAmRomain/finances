"""Montants en centimes entiers (inv. 2).

- Aucun `float` : les montants sont lus depuis du texte, avec un séparateur décimal explicite
  (connu par banque ou par API), jamais deviné.
- Aucun arrondi à la lecture : une fraction de centime est une erreur.
- Un seul arrondi, au centime, en fin de calcul (`round_to_cents`), au demi supérieur.
- `allocate` répartit un montant selon des poids sans créer ni perdre de centime.
"""

import re
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from typing import Literal, NewType

Cents = NewType("Cents", int)

DecimalSeparator = Literal[",", "."]

# Espaces admises comme séparateurs de milliers : espace, insécable, fine insécable.
_SPACES = " \u00a0\u202f"
_THOUSANDS_GROUP = "\u202f"  # affichage : espace fine insécable (typographie française)


class InvalidAmountError(ValueError):
    """Texte qui n'est pas un montant en euros valide au centime près."""


def _amount_pattern(decimal_sep: DecimalSeparator) -> re.Pattern[str]:
    thousands = "." if decimal_sep == "," else ","
    group = f"[{re.escape(_SPACES + thousands)}]"
    return re.compile(
        rf"(?P<sign>[+-])?(?P<int>\d{{1,3}}(?:{group}\d{{3}})+|\d+)"
        rf"(?:{re.escape(decimal_sep)}(?P<frac>\d+))?"
    )


_PATTERNS = {sep: _amount_pattern(sep) for sep in (",", ".")}


def parse_amount(text: str, *, decimal_sep: DecimalSeparator) -> Cents:
    if not isinstance(text, str):
        raise TypeError(f"montant attendu sous forme de texte, reçu {type(text).__name__}")
    match = _PATTERNS[decimal_sep].fullmatch(text.strip())
    if match is None:
        raise InvalidAmountError(f"montant invalide : {text!r}")
    frac = match["frac"] or ""
    if frac[2:].strip("0"):
        raise InvalidAmountError(f"fraction de centime : {text!r}")
    units = int(re.sub(r"\D", "", match["int"]))
    value = units * 100 + int(frac[:2].ljust(2, "0"))
    return Cents(-value if match["sign"] == "-" else value)


def parse_optional_user_amount(text: str | None) -> Cents | None:
    """Montant saisi dans un formulaire : espaces de milliers et « € » ignorés, virgule ou point
    décimal (« 1 234,56 », « 1234.56 », « -12 € »). `None` si le champ est vide ; sinon
    `InvalidAmountError` avec un message en français qui montre l'exemple attendu."""
    compact = "".join(char for char in text or "" if char not in _SPACES + "€")
    if not compact:
        return None
    separator: DecimalSeparator = "," if "," in compact else "."
    try:
        return parse_amount(compact, decimal_sep=separator)
    except InvalidAmountError as exc:
        raise InvalidAmountError(
            f"Montant illisible ({(text or '').strip()}) : écrivez par exemple 1 234,56."
        ) from exc


def parse_user_amount(text: str | None) -> Cents:
    """Comme `parse_optional_user_amount`, pour un champ obligatoire."""
    amount = parse_optional_user_amount(text)
    if amount is None:
        raise InvalidAmountError("Montant obligatoire.")
    return amount


def format_decimal_fr(amount: Cents) -> str:
    """Montant pour un tableur français (exports CSV) : `-1234,56`, sans séparateur de milliers
    ni symbole, pour rester un nombre une fois ouvert."""
    sign = "-" if amount < 0 else ""
    euros, cents = divmod(abs(amount), 100)
    return f"{sign}{euros},{cents:02d}"


def format_eur(amount: Cents) -> str:
    """Affichage français (UI-05) : `-1 234,56 €` (espace fine insécable, puis insécable)."""
    sign = "-" if amount < 0 else ""
    euros, cents = divmod(abs(amount), 100)
    grouped = f"{euros:,}".replace(",", _THOUSANDS_GROUP)
    return f"{sign}{grouped},{cents:02d}\u00a0€"


def round_to_cents(value: Fraction | Decimal | int) -> Cents:
    """Arrondit une valeur exacte **exprimée en centimes** au centime, demi au-dessus (symétrique).

    C'est le seul arrondi autorisé, en fin de calcul (révision IRL, quote-parts…).
    """
    if isinstance(value, bool) or not isinstance(value, Fraction | Decimal | int):
        raise TypeError(f"valeur exacte attendue (Fraction, Decimal, int), reçu {type(value)}")
    if isinstance(value, Decimal):
        return Cents(int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP)))
    exact = Fraction(value)
    quotient, remainder = divmod(abs(exact.numerator), exact.denominator)
    if 2 * remainder >= exact.denominator:
        quotient += 1
    return Cents(-quotient if exact < 0 else quotient)


def allocate(total: Cents, weights: list[int]) -> list[Cents]:
    """Répartit `total` au prorata de `weights` (méthode des plus forts restes).

    La somme des parts vaut exactement `total` ; chaque part est à moins d'un centime de sa valeur
    exacte ; une part de poids nul vaut zéro ; à reste égal, le premier servi est le plus tôt listé.
    """
    if not weights or any(weight < 0 for weight in weights) or not any(weights):
        raise ValueError("poids invalides : au moins un poids positif, aucun négatif")
    weight_sum = sum(weights)
    magnitude = abs(total)
    parts = [magnitude * weight // weight_sum for weight in weights]
    remainders = [magnitude * weight % weight_sum for weight in weights]
    missing = magnitude - sum(parts)
    by_remainder = sorted(range(len(weights)), key=lambda i: (-remainders[i], i))
    for index in by_remainder[:missing]:
        parts[index] += 1
    sign = -1 if total < 0 else 1
    return [Cents(sign * part) for part in parts]


def format_share_bp(share_bp: int) -> str:
    """Quote-part en dix-millièmes, en pourcentage français : 5000 → « 50 », 3333 → « 33,33 »
    (la division entière affichait 33 % pour 33,33 %)."""
    whole, rest = divmod(share_bp, 100)
    return f"{whole},{rest:02d}" if rest else str(whole)
