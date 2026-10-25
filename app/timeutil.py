"""Temps (inv. 9) : instants en UTC, dates calendaires sans fuseau, affichage Europe/Paris.

Un instant (`*_at`) est toujours un `datetime` avec fuseau, stocké en UTC. Une date comptable,
de valeur ou d'échéance est une `date` : elle n'est jamais convertie d'un fuseau à l'autre.
"""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(instant: datetime) -> datetime:
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError(f"datetime sans fuseau refusé : {instant.isoformat()}")
    return instant.astimezone(UTC)


def to_paris(instant: datetime) -> datetime:
    return ensure_utc(instant).astimezone(PARIS)


def paris_date(instant: datetime) -> date:
    """Jour calendaire à Paris de cet instant (ex. « aujourd'hui » pour les échéances)."""
    return to_paris(instant).date()


def format_date_fr(day: date) -> str:
    """Affichage JJ/MM/AAAA (UI-05)."""
    return day.strftime("%d/%m/%Y")
