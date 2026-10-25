"""Nettoyage des libellés bancaires (fonction pure, appliquée une fois à l'insertion)."""

import re

_CARD_SUFFIX = re.compile(r"\bCARTE\s+X?\d{4}\b")
_DATE = re.compile(r"\b\d{2}/\d{2}(?:/\d{2,4})?\b")
_SPACES = re.compile(r"\s+")


def clean_label(raw: str) -> str:
    """Majuscules, sans date d'opération ni numéro de carte, espaces normalisés."""
    label = raw.upper()
    label = _CARD_SUFFIX.sub(" ", label)
    label = _DATE.sub(" ", label)
    return _SPACES.sub(" ", label).strip()
