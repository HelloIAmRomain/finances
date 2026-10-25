"""Export des échéances au format iCalendar (RFC 5545), pour l'agenda du téléphone.

Fonction pure, sans dépendance : événements « journée entière », identifiant stable (un nouvel
import met à jour l'événement au lieu de le dupliquer), rappel `remind_days` jours avant à 9 h.
Ni montant ni nom de personne : l'agenda est souvent synchronisé chez un tiers (inv. 8, DT §6).
"""

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from app.tax.calendar import CalendarEvent

PRODID = "-//Finances//Echeances//FR"
LINE_OCTETS = 75


def _escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")  # un retour chariot seul couperait aussi la ligne
    )


def _fold(line: str) -> str:
    """Lignes de 75 octets au plus, continuées par CRLF + espace, sans couper un caractère."""
    parts: list[str] = []
    current = ""
    for char in line:
        limit = LINE_OCTETS if not parts else LINE_OCTETS - 1
        if len((current + char).encode("utf-8")) > limit:
            parts.append(current)
            current = char
        else:
            current += char
    parts.append(current)
    return "\r\n ".join(parts)


def to_ics(events: Iterable[CalendarEvent], *, base_url: str, now: datetime) -> str:
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Échéances (Finances)",
    ]
    for event in events:
        if event.done:
            continue
        end = event.day + timedelta(days=1)
        lines += [
            "BEGIN:VEVENT",
            f"UID:{event.uid}@finances",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{event.day:%Y%m%d}",
            f"DTEND;VALUE=DATE:{end:%Y%m%d}",
            f"SUMMARY:{_escape(event.title)}",
            f"URL:{base_url.rstrip('/')}{event.href}",
            "TRANSP:TRANSPARENT",
        ]
        if event.remind_days > 0:
            # Rappel à 9 h, n jours avant : début du jour moins (n - 1) jours et 15 h.
            lines += [
                "BEGIN:VALARM",
                "ACTION:DISPLAY",
                f"DESCRIPTION:{_escape(event.title)}",
                f"TRIGGER:-P{event.remind_days - 1}DT15H",
                "END:VALARM",
            ]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
