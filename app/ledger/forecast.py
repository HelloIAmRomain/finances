"""Prévision de trésorerie par compte (ANA-10) et alerte « solde bas prévu » (ALR-07).

Pour chaque compte visible (hors cartes, qui répètent le compte courant) :
- point de départ : le solde de référence connu ;
- opérations attendues : séries récurrentes de **ce compte** (abonnements, salaire,
  prélèvements, et aussi virements réguliers entre mes comptes, qui font bien varier le solde
  de chacun), à leurs prochaines dates ; loyers et remboursements attendus sur ce compte (reste
  du mois en cours, puis chaque échéance à venir), sauf s'ils sont déjà vus comme une recette
  récurrente du même montant (à 10 % près) ;
- résultat : solde projeté à l'horizon, point bas et sa date.

Estimation : les dépenses ponctuelles ne sont pas prévues. Montants en centimes (inv. 2).
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import Engine, select, update

from app.alerts.engine import raise_alert, resolve
from app.audit import audit
from app.db import write_transaction
from app.ledger.balances import reference_balances
from app.ledger.recurring import Series, detect
from app.ledger.subscriptions import operations
from app.models import Account, Alert
from app.models.alerts import AlertStatus, Severity
from app.models.ledger import AccountType
from app.money import Cents, format_eur
from app.months import month_of, shift_month
from app.rental.views import lease_views
from app.timeutil import format_date_fr, paris_date

HISTORY = timedelta(days=400)
DEFAULT_HORIZON = 30
ALERT_HORIZON = 30


@dataclass(frozen=True)
class ForecastEvent:
    day: date
    amount_cents: int
    label: str
    account_id: int
    source: str  # « récurrent » ou « loyer »


@dataclass(frozen=True)
class Projection:
    start_cents: int
    end_cents: int
    low_cents: int
    low_date: date


@dataclass(frozen=True)
class AccountForecast:
    account_id: int
    name: str
    counted: bool
    balance_cents: int | None
    balance_date: date | None
    threshold_cents: int | None
    events: list[ForecastEvent]
    projection: Projection | None  # None : solde inconnu

    @property
    def below_threshold(self) -> bool:
        return (
            self.threshold_cents is not None
            and self.projection is not None
            and self.projection.low_cents < self.threshold_cents
        )


def project(start_cents: int, events: Iterable[ForecastEvent], today: date) -> Projection:
    """Solde jour après jour (fonction pure) : fin de période et point bas."""
    balance, low, low_date = start_cents, start_cents, today
    for event in sorted(events, key=lambda e: (e.day, e.amount_cents)):
        balance += event.amount_cents
        if balance < low:
            low, low_date = balance, event.day
    return Projection(start_cents, balance, low, low_date)


def _occurrences(series: Series, today: date, horizon: date) -> list[date]:
    if not series.active(today):
        return []
    step = timedelta(days=series.frequency.days)
    day = series.next_date
    while day < today:  # en retard mais encore attendu : prévu aujourd'hui
        day = today if day + step > today else day + step
    days = []
    while day <= horizon:
        days.append(day)
        day += step
    return days


def _recurring_events(
    engine: Engine, today: date, horizon: date
) -> tuple[list[ForecastEvent], list[Series]]:
    series = detect(operations(engine, today - HISTORY, per_account=True))
    events = [
        ForecastEvent(day, s.current_cents, s.key.split("|", 1)[1], s.last.account_id, "récurrent")
        for s in series
        for day in _occurrences(s, today, horizon)
    ]
    return events, [s for s in series if s.active(today)]


def _rent_events(
    engine: Engine, today: date, horizon: date, recurring: Sequence[Series]
) -> list[ForecastEvent]:
    events: list[ForecastEvent] = []
    month = month_of(today)
    for lease in lease_views(engine, today):
        credits = [
            s.current_cents
            for s in recurring
            if not s.debit and s.last.account_id == lease.account_id
        ]
        if any(abs(c - lease.monthly_cents) * 10 <= lease.monthly_cents for c in credits):
            continue  # déjà prévu comme recette récurrente
        for due in lease.dues:
            if due.period == month and due.remaining_cents > 0:
                events.append(
                    ForecastEvent(
                        today, due.remaining_cents, lease.label, lease.account_id, "loyer"
                    )
                )
        # Pas d'échéance avant le début du bail (bail signé pour plus tard).
        following = max(shift_month(month, 1), month_of(lease.start_date))
        while True:
            day = date.fromisoformat(f"{following}-{lease.due_day:02d}")
            if day > horizon or (lease.end_date is not None and day > lease.end_date):
                break
            events.append(
                ForecastEvent(day, lease.monthly_cents, lease.label, lease.account_id, "loyer")
            )
            following = shift_month(following, 1)
    return events


def forecast(engine: Engine, today: date, *, days: int = DEFAULT_HORIZON) -> list[AccountForecast]:
    horizon = today + timedelta(days=days)
    recurring_events, recurring = _recurring_events(engine, today, horizon)
    events = recurring_events + _rent_events(engine, today, horizon, recurring)
    balance = reference_balances()
    with engine.connect() as connection:
        accounts = connection.execute(
            select(
                Account.id,
                Account.name,
                Account.include_in_net_worth,
                Account.low_balance_cents,
                balance.c.amount_cents,
                balance.c.as_of_date,
            )
            .outerjoin(balance, balance.c.account_id == Account.id)
            .where(
                Account.ignored_at.is_(None),
                Account.closed_at.is_(None),
                Account.type != AccountType.CARD,
            )
            .order_by(Account.include_in_net_worth.desc(), Account.name)
        ).all()
    result = []
    for row in accounts:
        mine = sorted((e for e in events if e.account_id == row.id), key=lambda e: e.day)
        result.append(
            AccountForecast(
                account_id=row.id,
                name=row.name,
                counted=bool(row.include_in_net_worth),
                balance_cents=row.amount_cents,
                balance_date=row.as_of_date,
                threshold_cents=row.low_balance_cents,
                events=mine,
                projection=project(row.amount_cents, mine, today)
                if row.amount_cents is not None
                else None,
            )
        )
    return result


def total_projection(forecasts: Sequence[AccountForecast], today: date) -> Projection | None:
    """Comptes comptés au solde connu, ensemble (le patrimoine liquide projeté)."""
    counted = [f for f in forecasts if f.counted and f.balance_cents is not None]
    if not counted:
        return None
    start = sum(f.balance_cents or 0 for f in counted)
    return project(start, [e for f in counted for e in f.events], today)


class ForecastError(ValueError):
    """Réglage refusé (message affichable)."""


def set_threshold(
    engine: Engine,
    account_id: int,
    threshold_cents: int | None,
    *,
    actor_user_id: int | None,
    now: datetime,
) -> None:
    """Seuil d'alerte d'un compte (nul : pas d'alerte), audité."""
    with write_transaction(engine) as connection:
        result = connection.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(low_balance_cents=threshold_cents)
        )
        if result.rowcount != 1:
            raise ForecastError("Compte introuvable.")
        audit(
            connection,
            "account.low_balance",
            ("account", account_id),
            now=now,
            actor_user_id=actor_user_id,
            details={"seuil": threshold_cents},
        )


def check_low_balances(engine: Engine, *, now: datetime) -> int:
    """Alerte « solde bas prévu » (ALR-07) par compte à seuil, sur 30 jours ; résolue quand la
    prévision repasse au-dessus du seuil ou que le point bas change."""
    today = paris_date(now)
    wanted: dict[str, AccountForecast] = {}
    for item in forecast(engine, today, days=ALERT_HORIZON):
        if item.below_threshold and item.projection is not None:
            wanted[f"low_balance:{item.account_id}:{item.projection.low_date}"] = item
    for key, item in wanted.items():
        assert item.projection is not None  # noqa: S101 (garanti ci-dessus)
        assert item.threshold_cents is not None  # noqa: S101
        raise_alert(
            engine,
            type="low_balance",
            severity=Severity.WARNING,
            key=key,
            title="Solde bas prévu",
            subject=("account", item.account_id),
            details={
                "compte": item.name,
                "date": format_date_fr(item.projection.low_date),
                "solde_prevu": format_eur(Cents(item.projection.low_cents)),
                "seuil": format_eur(Cents(item.threshold_cents)),
            },
            now=now,
            once=True,
        )
    with engine.connect() as connection:
        stale = [
            alert_id
            for alert_id, key in connection.execute(
                select(Alert.id, Alert.dedupe_key).where(
                    Alert.type == "low_balance", Alert.status != AlertStatus.RESOLVED
                )
            )
            if key.partition("#")[0] not in wanted
        ]
    for alert_id in stale:
        resolve(engine, alert_id, now=now)
    return len(wanted)
