"""Mois calendaires, écrits « AAAA-MM » comme en base et dans les adresses (un seul jeu d'outils
pour tout le projet, domaine pur)."""

import re
from calendar import monthrange
from datetime import date

MONTHS_IN_YEAR = 12
MONTH_RE = re.compile(r"\d{4}-(0[1-9]|1[0-2])")
MONTH_NAMES = (
    "janvier",
    "février",
    "mars",
    "avril",
    "mai",
    "juin",
    "juillet",
    "août",
    "septembre",
    "octobre",
    "novembre",
    "décembre",
)


def is_month(text: str) -> bool:
    return MONTH_RE.fullmatch(text) is not None


def _index(month: str) -> int:
    year, number = map(int, month.split("-"))
    return year * MONTHS_IN_YEAR + number - 1


def _from_index(index: int) -> str:
    year, number = divmod(index, MONTHS_IN_YEAR)
    return f"{year:04d}-{number + 1:02d}"


def month_of(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def shift_month(month: str, delta: int) -> str:
    return _from_index(_index(month) + delta)


def next_month(month: str) -> str:
    return shift_month(month, 1)


def months_between(first: str, last: str) -> int:
    """Nombre de mois de `first` à `last` inclus (0 si `last` précède `first`)."""
    return max(_index(last) - _index(first) + 1, 0)


def month_range(first: str, last: str) -> list[str]:
    """Mois de `first` à `last` inclus."""
    start = _index(first)
    return [_from_index(start + offset) for offset in range(months_between(first, last))]


def month_start(month: str) -> date:
    year, number = map(int, month.split("-"))
    return date(year, number, 1)


def month_end(month: str) -> date:
    year, number = map(int, month.split("-"))
    return date(year, number, monthrange(year, number)[1])


def add_months(day: date, months: int) -> date:
    """Même jour, `months` mois plus tard (dernier jour du mois si ce jour n'existe pas)."""
    year, number = map(int, shift_month(month_of(day), months).split("-"))
    return date(year, number, min(day.day, monthrange(year, number)[1]))


def month_label(month: str) -> str:
    """« 2026-10 » → « octobre 2026 »."""
    year, number = month.split("-")
    return f"{MONTH_NAMES[int(number) - 1]} {year}"
