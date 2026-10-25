"""Anonymisation des réponses Enable Banking réelles, avant qu'elles quittent la machine (inv. 5).

Sert à produire des cassettes de test à partir de vraies réponses de Boursorama et de la SG
(porte du lot 1) : la **structure** est conservée à l'identique (noms de champs, types, listes,
formats de dates et de montants, codes techniques), le **contenu** jamais.

Liste blanche, pas liste noire : un champ inconnu est remplacé par défaut.
- Noms de champs : conservés (schéma de l'API).
- Codes techniques (`BOOK`, `CRDT`, `CLBD`, `EUR`…) : conservés seulement pour des clés connues et
  s'ils ont la forme d'un code (majuscules, chiffres, `_`, 2 à 12 caractères).
- Dates et instants : décalés d'un nombre de jours fixe (format inchangé).
- Montants : remplacés par des montants factices au même nombre de décimales.
- IBAN : IBAN fictif à clé valide (code banque 99999, accepté par gitleaks).
- Tout autre texte (libellés, noms, références, identifiants) : `<clé-n>` numéroté ; deux valeurs
  égales donnent le même remplaçant, pour garder les égalités (doublons, références répétées).
"""

import re
from datetime import date, datetime, timedelta
from typing import Any

from app.iban import with_check_digits

CODE_KEYS = frozenset(
    {
        "balance_type",
        "cash_account_type",
        "code",
        "credit_debit_indicator",
        "currency",
        "domain",
        "family",
        "psu_type",
        "scheme_name",
        "status",
        "sub_code",
        "usage",
    }
)
CODE = re.compile(r"^[A-Z0-9_]{2,12}$")
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
INSTANT = re.compile(r"^\d{4}-\d{2}-\d{2}T")
AMOUNT = re.compile(r"^-?\d+(\.\d+)?$")
AMOUNT_KEYS = frozenset({"amount", "exchange_rate", "unit_currency_amount"})
IBAN_KEYS = frozenset({"iban"})


class Anonymizer:
    def __init__(self, *, day_shift: int = -1000) -> None:
        self.shift = timedelta(days=day_shift)
        self._replacements: dict[tuple[str, str], str] = {}
        self._counters: dict[str, int] = {}
        self._amounts = 0

    def __call__(self, node: Any, key: str = "") -> Any:
        if isinstance(node, dict):
            return {name: self(value, name) for name, value in node.items()}
        if isinstance(node, list):
            return [self(item, key) for item in node]
        if isinstance(node, str):
            return self._string(node, key)
        return node  # nombres, booléens, null : aucune donnée textuelle

    def _string(self, value: str, key: str) -> str:
        if key in CODE_KEYS and CODE.match(value):
            return value
        if DAY.match(value):
            return (date.fromisoformat(value) + self.shift).isoformat()
        if INSTANT.match(value):
            return self._instant(value, key)
        if key in AMOUNT_KEYS and AMOUNT.match(value):
            return self._amount(value)
        return self._iban(value) if key in IBAN_KEYS else self._placeholder(value, key)

    def _instant(self, value: str, key: str) -> str:
        try:
            return (datetime.fromisoformat(value) + self.shift).isoformat()
        except ValueError:
            return self._placeholder(value, key)

    def _placeholder(self, value: str, key: str) -> str:
        found = self._replacements.get((key, value))
        if found is None:
            self._counters[key] = self._counters.get(key, 0) + 1
            found = f"<{key or 'texte'}-{self._counters[key]}>"
            self._replacements[(key, value)] = found
        return found

    def _amount(self, value: str) -> str:
        decimals = len(value.split(".", 1)[1]) if "." in value else 0
        self._amounts += 1
        cents = (self._amounts * 7919) % 250000 + 100  # factice, déterministe, jamais nul
        whole, fraction = divmod(cents, 100)
        text = (
            f"{whole}.{f'{fraction:02d}'.ljust(decimals, '0')[:decimals]}"
            if decimals
            else str(whole)
        )
        return ("-" if value.startswith("-") else "") + text

    def _iban(self, value: str) -> str:
        found = self._replacements.get(("iban", value))
        if found is None:
            self._counters["iban"] = self._counters.get("iban", 0) + 1
            number = self._counters["iban"]
            found = with_check_digits("FR", f"9999900001{number:011d}00")
            self._replacements[("iban", value)] = found
        return found


def anonymize(document: Any, *, day_shift: int = -1000) -> Any:
    return Anonymizer(day_shift=day_shift)(document)
