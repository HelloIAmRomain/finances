"""Calendrier des échéances (FIS-04, LOC-09, LOC-13, COL-07) : une seule liste datée.

Sources :
- les échéances saisies (taxe foncière, CFE, déclaration…, `app.tax.deadlines`) ;
- l'expiration du consentement de chaque banque reliée (renouvellement, COL-07) ;
- pour chaque bail en cours, l'anniversaire (révision annuelle du loyer, LOC-09) et la fin
  prévue du bail (LOC-13) ;
- après la remise des clés, la date limite de restitution du dépôt de garantie (LEG-04), tant
  qu'il reste quelque chose à restituer (rappel 7 jours avant, `app.rental.deposit`).

Les événements dérivés ne sont pas stockés : ils suivent les baux et les connexions. Leurs
rappels : anniversaire et fin de bail 30 jours avant ; le consentement a déjà les siens
(`app.ops.checks.check_consents`).

Titres sans montant ni nom de personne : ils partent dans l'agenda du téléphone (export ICS) et
dans les notifications. Un bail est désigné par son bien, jamais par son libellé (qui contient
souvent le nom du locataire).
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import Engine, or_, select

from app.alerts.engine import raise_alert, resolve
from app.models import Alert, Connection, Deadline, Lease, Property
from app.models.alerts import AlertStatus, Severity
from app.models.ledger import ConnectionStatus
from app.models.rental import LeaseKind
from app.rental.deposit import REMIND_BEFORE, deposit_state, exit_lease_ids
from app.tax.deadlines import KIND_LABELS
from app.timeutil import format_date_fr, paris_date

LEASE_NOTICE = timedelta(days=30)
CONSENT_NOTICE_DAYS = 14
DEFAULT_HORIZON = timedelta(days=400)
DONE_SHOWN_FOR = timedelta(days=60)


@dataclass(frozen=True)
class CalendarEvent:
    day: date
    title: str
    category: str  # « fiscal », « bail », « banque »
    href: str
    uid: str  # stable : même événement d'un export à l'autre
    remind_days: int
    deadline_id: int | None = None
    amount_cents: int | None = None
    recoverable_cents: int | None = None
    done: bool = False

    def days_left(self, today: date) -> int:
        return (self.day - today).days


def _property_names(engine: Engine) -> dict[int, str]:
    with engine.connect() as connection:
        return {row.id: row.name for row in connection.execute(select(Property.id, Property.name))}


def deadline_title(kind_label: str, property_name: str | None, label: str | None) -> str:
    title = kind_label + (f" · {property_name}" if property_name else "")
    return title + (f" ({label})" if label else "")


def _on_year(start: date, year: int) -> date:
    try:
        return start.replace(year=year)
    except ValueError:  # bail commencé un 29 février
        return date(year, 2, 28)


def anniversary(start: date, today: date) -> date:
    """Prochain anniversaire du bail, aujourd'hui compris (jamais le jour de début)."""
    year = max(start.year + 1, today.year)
    while _on_year(start, year) < today:
        year += 1
    return _on_year(start, year)


def _lease_rows(engine: Engine, today: date) -> list[tuple[int, date, date | None, int | None]]:
    with engine.connect() as connection:
        return [
            (row.id, row.start_date, row.end_date, row.property_id)
            for row in connection.execute(
                select(Lease.id, Lease.start_date, Lease.end_date, Lease.property_id).where(
                    Lease.kind == LeaseKind.RENT,
                    or_(Lease.end_date.is_(None), Lease.end_date >= today),
                )
            )
        ]


def lease_events(engine: Engine, today: date, horizon: timedelta) -> list[CalendarEvent]:
    names = _property_names(engine)
    events = []
    for lease_id, start, end, property_id in _lease_rows(engine, today):
        place = names.get(property_id) if property_id else None
        suffix = f" · {place}" if place else ""
        due = anniversary(start, today)
        if due <= today + horizon and (end is None or due <= end):
            events.append(
                CalendarEvent(
                    day=due,
                    title=f"Révision annuelle du loyer{suffix}",
                    category="bail",
                    href=f"/loyers/{lease_id}",
                    uid=f"bail-{lease_id}-revision-{due.isoformat()}",
                    remind_days=LEASE_NOTICE.days,
                )
            )
        if end is not None and end <= today + horizon:
            events.append(
                CalendarEvent(
                    day=end,
                    title=f"Fin du bail{suffix}",
                    category="bail",
                    href=f"/loyers/{lease_id}",
                    uid=f"bail-{lease_id}-fin-{end.isoformat()}",
                    remind_days=LEASE_NOTICE.days,
                )
            )
    return events


def deposit_events(engine: Engine, today: date, horizon: timedelta) -> list[CalendarEvent]:
    events = []
    for lease_id in exit_lease_ids(engine):
        state = deposit_state(engine, lease_id)
        day = state.deadline
        if day is None or state.to_return_cents <= 0 or day > today + horizon:
            continue
        place = f" · {state.property_name}" if state.property_name else ""
        events.append(
            CalendarEvent(
                day=day,
                title=f"Restituer le dépôt de garantie{place}",
                category="bail",
                href=f"/loyers/{lease_id}#depot",
                uid=f"bail-{lease_id}-depot-{day.isoformat()}",
                remind_days=REMIND_BEFORE.days,
            )
        )
    return events


def consent_events(engine: Engine, today: date, horizon: timedelta) -> list[CalendarEvent]:
    with engine.connect() as connection:
        rows = connection.execute(
            select(Connection.id, Connection.institution_name, Connection.consent_expires_at).where(
                Connection.status.in_([ConnectionStatus.ACTIVE, ConnectionStatus.EXPIRED]),
                Connection.consent_expires_at.is_not(None),
            )
        ).all()
    events = []
    for connection_id, institution, expires_at in rows:
        if expires_at is None:  # pragma: no cover (exclu par la requête)
            continue
        day = paris_date(expires_at)
        if today - DONE_SHOWN_FOR <= day <= today + horizon:
            events.append(
                CalendarEvent(
                    day=day,
                    title=f"Renouveler l'accès à la banque · {institution}",
                    category="banque",
                    href="/connexions",
                    uid=f"banque-{connection_id}-{day.isoformat()}",
                    remind_days=CONSENT_NOTICE_DAYS,
                )
            )
    return events


def deadline_events(engine: Engine, today: date, horizon: timedelta) -> list[CalendarEvent]:
    names = _property_names(engine)
    with engine.connect() as connection:
        rows = connection.execute(
            select(Deadline).where(
                Deadline.due_date <= today + horizon,
                or_(Deadline.done_at.is_(None), Deadline.due_date >= today - DONE_SHOWN_FOR),
            )
        ).all()
    return [
        CalendarEvent(
            day=row.due_date,
            title=deadline_title(
                KIND_LABELS[row.kind],
                names.get(row.property_id) if row.property_id else None,
                row.label,
            ),
            category="fiscal",
            href=f"/echeances/{row.id}",
            uid=f"echeance-{row.id}-{row.due_date.isoformat()}",
            remind_days=row.remind_days,
            deadline_id=row.id,
            amount_cents=row.amount_cents,
            recoverable_cents=row.recoverable_cents,
            done=row.done_at is not None,
        )
        for row in rows
    ]


def calendar(
    engine: Engine, today: date, *, horizon: timedelta = DEFAULT_HORIZON
) -> list[CalendarEvent]:
    events = (
        deadline_events(engine, today, horizon)
        + consent_events(engine, today, horizon)
        + lease_events(engine, today, horizon)
        + deposit_events(engine, today, horizon)
    )
    return sorted(events, key=lambda e: (e.day, e.category, e.title))


def remind_leases(engine: Engine, *, now: datetime) -> int:
    """Rappels 30 jours avant l'anniversaire (révision du loyer) et la fin d'un bail ; résolus
    d'eux-mêmes une fois la date passée."""
    today = paris_date(now)
    with engine.connect() as connection:
        stale = [
            alert_id
            for alert_id, details in connection.execute(
                select(Alert.id, Alert.details).where(
                    Alert.type == "lease_event", Alert.status != AlertStatus.RESOLVED
                )
            )
            if date.fromisoformat(details.get("date", today.isoformat())) < today
        ]
    for alert_id in stale:
        resolve(engine, alert_id, now=now)
    signalled = 0
    for event in lease_events(engine, today, LEASE_NOTICE):
        lease_id = int(event.href.rsplit("/", 1)[1])
        for title in (f"{event.title} le {format_date_fr(event.day)}"[:160], event.uid):
            try:
                raise_alert(
                    engine,
                    type="lease_event",
                    severity=Severity.INFO,
                    key=f"lease_event:{event.uid}",
                    title=title,
                    subject=("lease", lease_id),
                    once=True,
                    details={"date": event.day.isoformat()},
                    now=now,
                )
                break
            except ValueError:  # nom du bien qui ressemble à un montant
                continue
        signalled += 1
    return signalled
