"""Bilan hebdomadaire (ALR-05) : chaque lundi matin, une alerte d'information dont le détail
résume la situation : patrimoine, dépenses des 7 derniers jours, budgets du mois, loyers du mois,
alertes en cours, échéances des 14 prochains jours.

- Une fois par semaine (clé « digest:AAAA-Sss », inv. 3) ; le bilan précédent est résolu.
- Titre sans montant (DT §6) : les chiffres sont dans le détail, visible dans l'application.
- Désactivable sur la page Alertes (activé par défaut).
"""

from datetime import date, datetime, timedelta

from sqlalchemy import Engine, func, insert, select, update

from app.alerts.engine import raise_alert, resolve
from app.audit import audit
from app.categorize.budgets import budget_statuses
from app.db import write_transaction
from app.ledger.aggregates import counted_lines, net_worth
from app.models import Alert, AppSetting
from app.models.alerts import AlertStatus, Severity
from app.money import Cents, format_eur
from app.months import month_of
from app.rental.views import STATE_LABELS, lease_views
from app.tax.calendar import calendar
from app.timeutil import format_date_fr, paris_date

DIGEST_KEY = "alertes.bilan_hebdomadaire"
MONDAY = 0
UPCOMING = timedelta(days=14)


def digest_enabled(engine: Engine) -> bool:
    with engine.connect() as connection:
        stored = connection.scalar(select(AppSetting.value).where(AppSetting.key == DIGEST_KEY))
    return stored is not False


def set_digest_enabled(
    engine: Engine, enabled: bool, *, actor_user_id: int | None, now: datetime
) -> None:
    with write_transaction(engine) as connection:
        if connection.scalar(select(AppSetting.key).where(AppSetting.key == DIGEST_KEY)) is None:
            connection.execute(
                insert(AppSetting).values(key=DIGEST_KEY, value=enabled, updated_at=now)
            )
        else:
            connection.execute(
                update(AppSetting)
                .where(AppSetting.key == DIGEST_KEY)
                .values(value=enabled, updated_at=now)
            )
        audit(
            connection,
            "alerts.digest",
            ("app_setting", None),
            now=now,
            actor_user_id=actor_user_id,
            details={"actif": enabled},
        )


def _eur(cents: int) -> str:
    return format_eur(Cents(cents))


def _spent_last_week(engine: Engine, today: date) -> int:
    lines = counted_lines()
    with engine.connect() as connection:
        total = connection.scalar(
            select(func.coalesce(func.sum(lines.c.amount), 0)).where(
                lines.c.amount < 0,
                lines.c.booking_date >= today - timedelta(days=7),
                lines.c.booking_date < today,
            )
        )
    return int(total or 0)


def digest_details(engine: Engine, today: date) -> dict[str, str]:
    month = month_of(today)
    details = {
        "Patrimoine": _eur(net_worth(engine).total),
        "Dépenses des 7 derniers jours": _eur(_spent_last_week(engine, today)),
    }
    budgets = [s for s in budget_statuses(engine, month) if s.level != "ok"]
    if budgets:
        details["Budgets à surveiller"] = " · ".join(f"{s.name} {s.percent} %" for s in budgets)
    rents = [
        f"{lease.label} : {STATE_LABELS[due.state]}"
        for lease in lease_views(engine, today)
        for due in lease.dues
        if due.period == month
    ]
    if rents:
        details["Loyers du mois"] = " · ".join(rents)
    with engine.connect() as connection:
        open_alerts = connection.scalar(
            select(func.count()).where(Alert.status != AlertStatus.RESOLVED, Alert.type != "digest")
        )
    details["Alertes en cours"] = str(open_alerts or 0)
    upcoming = [e for e in calendar(engine, today, horizon=UPCOMING) if not e.done]
    if upcoming:
        details["Échéances (14 jours)"] = " · ".join(
            f"{e.title} le {format_date_fr(e.day)}" for e in upcoming
        )
    return details


def weekly_digest(engine: Engine, *, now: datetime) -> bool:
    """Le lundi : lève le bilan de la semaine (une fois) ; renvoie True s'il vient d'être créé."""
    today = paris_date(now)
    if today.weekday() != MONDAY or not digest_enabled(engine):
        return False
    year, week, _ = today.isocalendar()
    key = f"digest:{year}-S{week:02d}"
    with engine.connect() as connection:
        previous = list(
            connection.scalars(
                select(Alert.id).where(
                    Alert.type == "digest",
                    Alert.status != AlertStatus.RESOLVED,
                    Alert.dedupe_key != key,
                )
            )
        )
    for alert_id in previous:
        resolve(engine, alert_id, now=now)
    result = raise_alert(
        engine,
        type="digest",
        severity=Severity.INFO,
        key=key,
        title="Bilan de la semaine",
        details=digest_details(engine, today),
        now=now,
        once=True,
    )
    return result.created
