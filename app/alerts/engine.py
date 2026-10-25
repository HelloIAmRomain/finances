"""Moteur d'alertes (ALR-01, ALR-03, ALR-06, inv. 3).

- Une alerte par fait : la clé (`key`) identifie le fait (ex. `sync:<compte>`) ; tant que
  l'alerte n'est pas résolue, le lever à nouveau ne crée rien.
- Épisodes : une fois résolue, si le même fait se reproduit, un nouvel épisode est ouvert avec la
  clé suffixée (`sync:1#2`, `sync:1#3`…), unique en base.
- Cycle de vie : ouverte → vue → résolue (ou directement résolue).
- Notifications mises en file d'envoi (`notification`) selon la gravité ; un rappel unique si
  l'alerte n'a pas été vue sous 48 h.
- Titre court, **sans montant** (ni nom) : il est repris tel quel dans les notifications (DT §6).
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, Engine, case, insert, or_, select, update

from app.db import inserted_id, write_transaction
from app.models import Alert, Notification
from app.models.alerts import (
    AlertStatus,
    Channel,
    NotificationKind,
    NotificationStatus,
    Severity,
)

REMINDER_AFTER = timedelta(hours=48)
RESOLVED_SHOWN = 30  # alertes résolues montrées sous les alertes en cours
CHANNELS: dict[Severity, tuple[Channel, ...]] = {
    Severity.INFO: (Channel.NTFY,),
    Severity.WARNING: (Channel.NTFY,),
    Severity.CRITICAL: (Channel.NTFY, Channel.EMAIL),
}
_AMOUNT = re.compile(r"€|\d+[.,]\d{2}\b")


class AlertTransitionError(ValueError):
    """Changement d'état impossible (alerte déjà résolue…)."""


@dataclass(frozen=True)
class RaiseResult:
    alert_id: int
    created: bool
    dedupe_key: str


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _current_episode(connection: Connection, key: str) -> Any | None:
    return connection.execute(
        select(Alert)
        .where(
            or_(
                Alert.dedupe_key == key,
                Alert.dedupe_key.like(f"{_escape_like(key)}#%", escape="\\"),
            )
        )
        .order_by(Alert.id.desc())
        .limit(1)
    ).first()


def _enqueue(
    connection: Connection,
    alert_id: int,
    severity: Severity,
    kind: NotificationKind,
    now: datetime,
) -> None:
    for channel in CHANNELS[severity]:
        connection.execute(
            insert(Notification).values(
                alert_id=alert_id,
                channel=channel,
                kind=kind,
                sequence=1,
                attempt_count=0,
                next_attempt_at=now,
                status=NotificationStatus.PENDING,
            )
        )


def raise_alert(  # noqa: PLR0913 (fait, gravité, titre, objet, détail, horloge, unicité)
    engine: Engine,
    *,
    type: str,
    severity: Severity,
    key: str,
    title: str,
    now: datetime,
    subject: tuple[str, int] | None = None,
    details: dict[str, Any] | None = None,
    once: bool = False,
) -> RaiseResult:
    """`subject` : objet concerné, par ex. ("account", 3), pour relier l'alerte dans l'interface.

    `once` : fait ponctuel (anomalie d'une opération, seuil d'un budget pour un mois, rappel
    d'une échéance datée) signalé une seule fois, même après résolution à la main : sans cela,
    résoudre l'alerte la ferait renaître au passage suivant tant que la condition dure."""
    if _AMOUNT.search(title):
        raise ValueError(
            f"titre d'alerte avec un montant : {title!r} (le détail reste dans l'interface)"
        )
    if "#" in key:
        raise ValueError("le caractère # est réservé aux numéros d'épisode")
    with write_transaction(engine) as connection:
        latest = _current_episode(connection, key)
        if latest is not None and (once or latest.status is not AlertStatus.RESOLVED):
            return RaiseResult(latest.id, False, latest.dedupe_key)
        episode = 1 if latest is None else int(latest.dedupe_key.partition("#")[2] or 1) + 1
        dedupe_key = key if episode == 1 else f"{key}#{episode}"
        result = connection.execute(
            insert(Alert).values(
                type=type,
                severity=severity,
                status=AlertStatus.OPEN,
                dedupe_key=dedupe_key,
                subject_type=subject[0] if subject else None,
                subject_id=subject[1] if subject else None,
                title=title,
                details=details or {},
                opened_at=now,
            )
        )
        alert_id = inserted_id(result)
        _enqueue(connection, alert_id, severity, NotificationKind.INITIAL, now)
        return RaiseResult(alert_id, True, dedupe_key)


def mark_seen(engine: Engine, alert_id: int, *, now: datetime) -> None:
    with write_transaction(engine) as connection:
        result = connection.execute(
            update(Alert)
            .where(Alert.id == alert_id, Alert.status == AlertStatus.OPEN)
            .values(status=AlertStatus.SEEN, seen_at=now)
        )
        if result.rowcount != 1:
            raise AlertTransitionError(f"alerte {alert_id} : ne peut pas passer à « vue »")


def resolve(engine: Engine, alert_id: int, *, now: datetime) -> None:
    with write_transaction(engine) as connection:
        result = connection.execute(
            update(Alert)
            .where(Alert.id == alert_id, Alert.status != AlertStatus.RESOLVED)
            .values(status=AlertStatus.RESOLVED, resolved_at=now)
        )
        if result.rowcount != 1:
            raise AlertTransitionError(f"alerte {alert_id} déjà résolue ou introuvable")


def resolve_by_key(engine: Engine, key: str, *, now: datetime) -> bool:
    """Résout l'épisode en cours de ce fait (la condition a disparu) ; False s'il n'y en a pas."""
    with write_transaction(engine) as connection:
        latest = _current_episode(connection, key)
        if latest is None or latest.status is AlertStatus.RESOLVED:
            return False
        connection.execute(
            update(Alert)
            .where(Alert.id == latest.id)
            .values(status=AlertStatus.RESOLVED, resolved_at=now)
        )
        return True


def open_keys(engine: Engine, prefix: str) -> set[str]:
    """Clés des alertes ouvertes commençant par `prefix`, sans leur numéro d'épisode : évite
    d'écrire en base pour résoudre des alertes déjà résolues."""
    with engine.connect() as connection:
        keys = connection.scalars(
            select(Alert.dedupe_key).where(
                Alert.dedupe_key.like(f"{_escape_like(prefix)}%", escape="\\"),
                Alert.status != AlertStatus.RESOLVED,
            )
        )
        return {key.split("#", 1)[0] for key in keys}


def resolve_prefix(engine: Engine, prefix: str, *, now: datetime) -> int:
    """Résout toutes les alertes ouvertes dont la clé commence par `prefix` (objet supprimé)."""
    with write_transaction(engine) as connection:
        result = connection.execute(
            update(Alert)
            .where(
                Alert.dedupe_key.like(f"{_escape_like(prefix)}%", escape="\\"),
                Alert.status != AlertStatus.RESOLVED,
            )
            .values(status=AlertStatus.RESOLVED, resolved_at=now)
        )
        return int(result.rowcount)


def send_due_reminders(engine: Engine, *, now: datetime) -> int:
    """Met en file un rappel unique pour chaque alerte ouverte et non vue depuis 48 h."""
    with write_transaction(engine) as connection:
        due = connection.execute(
            select(Alert.id, Alert.severity).where(
                Alert.status == AlertStatus.OPEN,
                Alert.last_reminded_at.is_(None),
                Alert.opened_at <= now - REMINDER_AFTER,
            )
        ).all()
        for alert_id, severity in due:
            _enqueue(connection, alert_id, severity, NotificationKind.REMINDER, now)
            connection.execute(
                update(Alert).where(Alert.id == alert_id).values(last_reminded_at=now)
            )
        return len(due)


def _severity_rank() -> Any:
    return case(
        {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}, value=Alert.severity, else_=3
    )


def current_alerts(engine: Engine) -> list[Any]:
    """Alertes non résolues : les plus graves d'abord, puis les plus récentes (page web et
    application)."""
    with engine.connect() as connection:
        return list(
            connection.execute(
                select(Alert)
                .where(Alert.status != AlertStatus.RESOLVED)
                .order_by(_severity_rank(), Alert.opened_at.desc(), Alert.id.desc())
            ).all()
        )


def recently_resolved(engine: Engine, limit: int) -> list[Any]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                select(Alert)
                .where(Alert.status == AlertStatus.RESOLVED)
                .order_by(Alert.resolved_at.desc(), Alert.id.desc())
                .limit(limit)
            ).all()
        )
