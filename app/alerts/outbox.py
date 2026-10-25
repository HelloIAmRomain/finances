"""File d'envoi des notifications (ALR-02) : au moins une fois, reprises espacées.

- Une notification en attente est envoyée quand son heure est venue ; en cas d'échec, elle est
  retentée après 1, 5, 15 puis 60 min, et abandonnée après 6 tentatives (l'alerte reste visible
  dans l'interface).
- Un canal non configuré laisse ses notifications en attente ; une alerte résolue entre-temps
  n'est plus envoyée (pas de rafale d'alertes périmées le jour où le canal est configuré).
- Toucher la notification ouvre la page de l'alerte (en-tête ntfy « Click »).
- Secours (I-19) : une alerte critique toujours ni vue ni résolue 30 min après son ouverture
  part aussi par e-mail, une seule fois.
- Heures silencieuses (ALR-03, ALR-04) : seules les alertes critiques partent ; les autres
  attendent la fin de la plage.
- Regroupement (ALR-04) : plusieurs notifications non critiques dues au même moment sur un même
  canal partent en un seul message (titres listés, lien vers la page Alertes) ; une alerte dont la
  notification et le rappel sont dus ensemble ne part qu'une fois.
- Arriéré : une notification en attente depuis plus d'une heure sans tentative (canal configuré
  tard, Pi arrêté), même critique, rejoint le récapitulatif (au plus un avertissement) au lieu
  de partir seule : pas de rafale.
- Cadence par canal (`MIN_INTERVAL`) : **au plus un message par 24 h et par canal**, e-mail comme
  téléphone (décision du 17/10/2026 : pas de harcèlement). Tout ce qui est dû part en un seul
  message, critiques comprises ; ce qui arrive ensuite attend la fin des 24 h puis part regroupé.
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Engine, func, insert, select, update
from sqlalchemy.orm import Session

from app.alerts.channels import Message, Sender, SendError
from app.alerts.preferences import QuietHours
from app.db import write_transaction
from app.models import Alert, Notification
from app.models.alerts import AlertStatus, Channel, NotificationKind, NotificationStatus, Severity

BACKOFF = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=15), timedelta(hours=1))
MAX_ATTEMPTS = 6
FALLBACK_AFTER = timedelta(minutes=30)
STALE = "sans objet : alerte résolue avant l'envoi"
BACKLOG_AFTER = timedelta(hours=1)
DAY = timedelta(hours=24)
MIN_INTERVAL: Mapping[Channel, timedelta] = {Channel.EMAIL: DAY, Channel.NTFY: DAY}


def _message(alert: Alert, base_url: str, kind: NotificationKind) -> Message:
    prefix = {NotificationKind.REMINDER: "Rappel : ", NotificationKind.FALLBACK: "Non lue : "}
    link = f"{base_url.rstrip('/')}/alertes/{alert.id}"
    return Message(
        title=f"{prefix.get(kind, '')}{alert.title}",
        body=f"Détails dans l'interface : {link}",
        severity=alert.severity,
        link=link,
    )


def _grouped(alerts: Sequence[Alert], base_url: str) -> Message:
    """Un seul message pour plusieurs alertes ; jamais plus qu'un avertissement (pas de sonnerie
    forcée pour un récapitulatif)."""
    severity = (
        Severity.WARNING if any(a.severity is not Severity.INFO for a in alerts) else Severity.INFO
    )
    titles = "\n".join(f"• {alert.title}" for alert in alerts)
    link = f"{base_url.rstrip('/')}/alertes"
    return Message(
        title=f"{len(alerts)} nouvelles alertes",
        body=f"{titles}\nDétails dans l'interface : {link}",
        severity=severity,
        link=link,
    )


def _is_backlog(notification: Any, now: datetime) -> bool:
    """En attente depuis longtemps sans tentative : canal configuré tard, ou Pi arrêté."""
    return bool(
        notification.attempt_count == 0 and notification.next_attempt_at <= now - BACKLOG_AFTER
    )


def _record(engine: Engine, notifications: Sequence[Any], error: str | None, now: datetime) -> None:
    for notification in notifications:
        attempt = notification.attempt_count + 1
        if error is None:
            values = {"attempt_count": attempt, "status": NotificationStatus.SENT, "sent_at": now}
        else:
            values = {
                "attempt_count": attempt,
                "last_error": error[:500],
                "status": NotificationStatus.FAILED
                if attempt >= MAX_ATTEMPTS
                else NotificationStatus.PENDING,
                "next_attempt_at": now + BACKOFF[min(attempt, len(BACKOFF)) - 1],
            }
        with write_transaction(engine) as connection:
            connection.execute(
                update(Notification).where(Notification.id == notification.id).values(**values)
            )


def _drop(engine: Engine, notifications: Sequence[Any]) -> None:
    """Notifications d'alertes résolues avant l'envoi : abandonnées, sans tentative."""
    with write_transaction(engine) as connection:
        connection.execute(
            update(Notification)
            .where(Notification.id.in_([n.id for n in notifications]))
            .values(status=NotificationStatus.FAILED, last_error=STALE)
        )


def _batch(
    channel: Channel, groups: Sequence[tuple[Alert, list[Any]]], base_url: str
) -> tuple[Channel, list[Any], Message]:
    """Une alerte : son message ; plusieurs (ou un arriéré) : un seul récapitulatif."""
    notifications = [n for _, group in groups for n in group]
    if len(groups) == 1:
        alert, group = groups[0]
        return channel, notifications, _message(alert, base_url, group[0].kind)
    return channel, notifications, _grouped([alert for alert, _ in groups], base_url)


def _resting_channels(
    engine: Engine, min_interval: Mapping[Channel, timedelta], now: datetime
) -> set[Channel]:
    """Canaux cadencés dont le dernier envoi réussi date de moins que leur intervalle."""
    resting = set()
    with engine.connect() as connection:
        for channel, interval in min_interval.items():
            last = connection.scalar(
                select(func.max(Notification.sent_at)).where(
                    Notification.channel == channel,
                    Notification.status == NotificationStatus.SENT,
                )
            )
            if last is not None and last > now - interval:
                resting.add(channel)
    return resting


def process_outbox(
    engine: Engine,
    senders: Mapping[Channel, Sender],
    *,
    base_url: str,
    now: datetime,
    quiet: QuietHours | None = None,
    min_interval: Mapping[Channel, timedelta] = MIN_INTERVAL,
) -> int:
    """Envoie les notifications dues ; renvoie le nombre de messages envoyés."""
    resting = _resting_channels(engine, min_interval, now)
    with Session(engine) as session:
        due = session.execute(
            select(Notification, Alert)
            .join(Alert, Alert.id == Notification.alert_id)
            .where(
                Notification.status == NotificationStatus.PENDING,
                Notification.next_attempt_at <= now,
                Notification.channel.in_([c for c in senders if c not in resting]),
            )
            .order_by(Notification.id)
        ).all()
    silent = quiet is not None and quiet.contains(now)
    stale = [notification for notification, alert in due if alert.status is AlertStatus.RESOLVED]
    if stale:
        _drop(engine, stale)
    # Par canal et par alerte : la notification et son rappel dus ensemble partent une fois.
    # Une alerte critique part seule et tout de suite, sauf si elle traîne un arriéré (sa
    # notification n'a jamais pu partir) : elle rejoint alors le récapitulatif avec son rappel.
    per_alert: dict[tuple[Channel, int], tuple[Alert, list[Any]]] = {}
    for notification, alert in due:
        if alert.status is not AlertStatus.RESOLVED:
            key = (notification.channel, alert.id)
            per_alert.setdefault(key, (alert, []))[1].append(notification)
    urgent: dict[Channel, dict[int, tuple[Alert, list[Any]]]] = defaultdict(dict)
    calm: dict[Channel, dict[int, tuple[Alert, list[Any]]]] = defaultdict(dict)
    for (channel, alert_id), (alert, group) in per_alert.items():
        late = any(_is_backlog(notification, now) for notification in group)
        alone = alert.severity is Severity.CRITICAL and not late and channel not in min_interval
        bucket = urgent if alone else calm
        bucket[channel][alert_id] = (alert, group)
    batches = [
        (channel, group, _message(alert, base_url, group[0].kind))
        for channel, items in urgent.items()
        for alert, group in items.values()
    ]
    for channel, items in calm.items():
        groups = list(items.values())
        if silent:  # heures silencieuses : seules les critiques partent, le reste attend le matin
            groups = [g for g in groups if g[0].severity is Severity.CRITICAL]
        if groups:
            batches.append(_batch(channel, groups, base_url))
    sent = 0
    for channel, notifications, message in batches:
        try:
            senders[channel].send(message)
        except SendError as exc:
            _record(engine, notifications, str(exc), now)
        else:
            _record(engine, notifications, None, now)
            sent += 1
    return sent


def enqueue_critical_fallbacks(engine: Engine, *, now: datetime) -> int:
    with write_transaction(engine) as connection:
        already = select(Notification.alert_id).where(
            Notification.kind == NotificationKind.FALLBACK
        )
        due = connection.scalars(
            select(Alert.id).where(
                Alert.severity == Severity.CRITICAL,
                Alert.status == AlertStatus.OPEN,
                Alert.opened_at <= now - FALLBACK_AFTER,
                Alert.id.not_in(already),
            )
        ).all()
        for alert_id in due:
            connection.execute(
                insert(Notification).values(
                    alert_id=alert_id,
                    channel=Channel.EMAIL,
                    kind=NotificationKind.FALLBACK,
                    sequence=1,
                    attempt_count=0,
                    next_attempt_at=now,
                    status=NotificationStatus.PENDING,
                )
            )
        return len(due)
