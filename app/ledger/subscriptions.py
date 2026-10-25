"""Abonnements et opérations récurrentes (ANA-07), anomalies (ANA-08) : lecture et alertes.

Périmètre : transactions effectives des comptes visibles, hors cartes (elles répètent le compte
courant, Q15) et hors virements internes confirmés ; 25 mois d'historique (une série annuelle
demande trois occurrences).

Alertes (dépenses seulement) :
- « hausse de prix » : le dernier palier d'une série active est plus cher que le précédent ;
- « prélèvement attendu non reçu » : une échéance manquée (série disparue récemment) ;
- anomalies des 7 derniers jours : montant inhabituel, nouveau créancier, frais bancaires,
  doublon possible (20 € au moins, pour ne pas signaler deux cafés).
Une alerte n'est levée qu'une fois par fait (clé stable) et se résout d'elle-même quand le fait
disparaît (la série reprend, le changement de prix vieillit). Titres sans montant ni nom
(DT §6) : le libellé et les montants restent dans le détail de l'alerte, dans l'application.
"""

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import Engine, select

from app.alerts.engine import raise_alert, resolve
from app.ledger.recurring import Operation, Series, anomalies, detect, merchant_key
from app.ledger.views import (
    confirmed_transfer_legs,
    is_effective,
    visible_accounts,
)
from app.models import Account, Alert, LedgerTransaction
from app.models.alerts import AlertStatus, Severity
from app.models.ledger import AccountType
from app.money import Cents, format_eur
from app.timeutil import format_date_fr, paris_date

HISTORY = timedelta(days=25 * 31)
ANOMALY_HISTORY = timedelta(days=365)
ANOMALY_WINDOW = timedelta(days=7)
DUPLICATE_MIN_CENTS = 2000
ANOMALY_TITLES = {
    "frais": "Frais bancaires prélevés",
    "montant_inhabituel": "Montant inhabituel",
    "nouveau_crediteur": "Nouveau créancier",
    "doublon": "Doublon possible",
}


@dataclass(frozen=True)
class WantedAlert:
    severity: Severity
    title: str
    subject: tuple[str, int]
    details: dict[str, str]


def _eur(cents: int) -> str:
    return format_eur(Cents(cents))


@dataclass(frozen=True)
class RecurringView:
    active_debits: list[Series]
    active_credits: list[Series]
    missed: list[Series]
    account_names: dict[int, str]

    @property
    def monthly_debits_cents(self) -> int:
        return sum(s.monthly_cents for s in self.active_debits)

    @property
    def annual_debits_cents(self) -> int:
        return sum(s.annual_cents for s in self.active_debits)


def operations(engine: Engine, since: date, *, per_account: bool = False) -> list[Operation]:
    """Opérations effectives des comptes visibles hors cartes (qui répètent le compte courant).

    Par défaut (abonnements, anomalies) : virements internes exclus, une série par commerçant
    tous comptes confondus. `per_account` (prévision) : virements internes compris, car ils font
    varier le solde de chaque compte, et une série par compte."""
    conditions = [
        LedgerTransaction.booking_date >= since,
        LedgerTransaction.account_id.in_(visible_accounts()),
        LedgerTransaction.account_id.in_(
            select(Account.id).where(Account.type != AccountType.CARD)
        ),
        is_effective(),
    ]
    if not per_account:
        conditions.append(LedgerTransaction.id.not_in(confirmed_transfer_legs()))
    with engine.connect() as connection:
        rows = connection.execute(
            select(
                LedgerTransaction.id,
                LedgerTransaction.booking_date,
                LedgerTransaction.amount_cents,
                LedgerTransaction.label_clean,
                LedgerTransaction.account_id,
            ).where(*conditions)
        ).all()
    result = []
    for row in rows:
        label = row.label_clean or ""
        key = merchant_key(label)
        result.append(
            Operation(
                row.id,
                row.booking_date,
                row.amount_cents,
                f"{row.account_id}|{key}" if per_account else key,
                row.account_id,
                label,
            )
        )
    return result


def _account_names(engine: Engine) -> dict[int, str]:
    with engine.connect() as connection:
        return {row.id: row.name for row in connection.execute(select(Account.id, Account.name))}


def recurring_view(engine: Engine, today: date) -> RecurringView:
    series = detect(operations(engine, today - HISTORY))
    active = [s for s in series if s.active(today)]
    return RecurringView(
        active_debits=[s for s in active if s.debit],
        active_credits=sorted((s for s in active if not s.debit), key=lambda s: -s.annual_cents),
        missed=[s for s in series if s.missed(today)],
        account_names=_account_names(engine),
    )


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _price_increase_is_recent(series: Series, today: date) -> bool:
    """Le nouveau prix date de moins d'une période (au-delà, il est devenu l'habitude)."""
    return (
        series.change_date is not None
        and series.previous_cents is not None
        and series.current_cents < series.previous_cents  # dépense plus forte (négatifs)
        and today - series.change_date <= timedelta(days=series.frequency.days) + series.tolerance
    )


def _wanted_alerts(engine: Engine, today: date) -> dict[str, WantedAlert]:
    """Alertes qui doivent être ouvertes aujourd'hui, par clé."""
    wanted: dict[str, WantedAlert] = {}
    for series in detect(operations(engine, today - HISTORY)):
        if not series.debit:
            continue
        digest = _digest(series.key)
        subject = ("transaction", series.last.id)
        if (
            series.active(today)
            and series.previous_cents is not None
            and series.change_date is not None
            and _price_increase_is_recent(series, today)
        ):
            wanted[f"recurring:{digest}:prix:{series.change_date}"] = WantedAlert(
                Severity.INFO,
                "Abonnement : hausse de prix",
                subject,
                {
                    "libelle": series.key,
                    "avant": _eur(-series.previous_cents),
                    "maintenant": _eur(-series.current_cents),
                    "depuis": format_date_fr(series.change_date),
                },
            )
        elif series.missed(today):
            wanted[f"recurring:{digest}:manque:{series.next_date}"] = WantedAlert(
                Severity.INFO,
                "Prélèvement régulier attendu non reçu",
                subject,
                {
                    "libelle": series.key,
                    "attendu_le": format_date_fr(series.next_date),
                    "montant_habituel": _eur(-series.current_cents),
                },
            )
    since = today - ANOMALY_WINDOW
    for anomaly in anomalies(operations(engine, today - ANOMALY_HISTORY), since):
        operation = anomaly.operation
        if anomaly.kind == "doublon" and -operation.amount_cents < DUPLICATE_MIN_CENTS:
            continue
        details = {
            "libelle": operation.label,
            "date": format_date_fr(operation.booking_date),
            "montant": _eur(operation.amount_cents),
        }
        if anomaly.usual_cents is not None:
            details["habituellement"] = _eur(anomaly.usual_cents)
        wanted[f"anomaly:{anomaly.kind}:{operation.id}"] = WantedAlert(
            Severity.WARNING if anomaly.kind == "doublon" else Severity.INFO,
            ANOMALY_TITLES[anomaly.kind],
            ("transaction", operation.id),
            details,
        )
    return wanted


def check_recurring(engine: Engine, *, now: datetime) -> int:
    """Lève les alertes de récurrents et d'anomalies ; résout celles qui n'ont plus lieu d'être.
    Les anomalies, liées à une opération, restent ouvertes jusqu'à ce que je les traite."""
    wanted = _wanted_alerts(engine, paris_date(now))
    for key, alert in wanted.items():
        raise_alert(
            engine,
            type=key.split(":", 1)[0],
            severity=alert.severity,
            key=key,
            title=alert.title,
            subject=alert.subject,
            details=dict(alert.details),
            now=now,
            once=True,
        )
    with engine.connect() as connection:
        stale = [
            alert_id
            for alert_id, key in connection.execute(
                select(Alert.id, Alert.dedupe_key).where(
                    Alert.type == "recurring", Alert.status != AlertStatus.RESOLVED
                )
            )
            if key.partition("#")[0] not in wanted
        ]
    for alert_id in stale:
        resolve(engine, alert_id, now=now)
    return len(wanted)
