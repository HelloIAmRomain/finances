"""Préférences de notification (ALR-03, ALR-04) : heures silencieuses.

Pendant les heures silencieuses (22:00 à 08:00 à Paris par défaut), seules les alertes
**critiques** partent ; les autres attendent la fin de la plage et partent alors **regroupées**
(`app.alerts.outbox`). Réglage modifiable sur la page Alertes, audité.
"""

from dataclasses import dataclass
from datetime import datetime, time

from sqlalchemy import Engine, insert, select, update

from app.audit import audit
from app.db import write_transaction
from app.models import AppSetting
from app.timeutil import to_paris

QUIET_KEY = "alertes.heures_silencieuses"
DEFAULT_START, DEFAULT_END = time(22, 0), time(8, 0)


class PreferenceError(ValueError):
    """Réglage invalide (message affichable)."""


@dataclass(frozen=True)
class QuietHours:
    enabled: bool = True
    start: time = DEFAULT_START
    end: time = DEFAULT_END

    def contains(self, instant: datetime) -> bool:
        """Vrai si l'instant tombe dans la plage (heure de Paris), qui peut passer minuit."""
        if not self.enabled or self.start == self.end:
            return False
        moment = to_paris(instant).time()
        if self.start < self.end:
            return self.start <= moment < self.end
        return moment >= self.start or moment < self.end


def _parse(text: object) -> time | None:
    try:
        return time.fromisoformat(str(text))
    except ValueError:
        return None


def quiet_hours(engine: Engine) -> QuietHours:
    with engine.connect() as connection:
        stored = connection.scalar(select(AppSetting.value).where(AppSetting.key == QUIET_KEY))
    if not isinstance(stored, dict):
        return QuietHours()
    start, end = _parse(stored.get("debut")), _parse(stored.get("fin"))
    return QuietHours(
        enabled=stored.get("actif") is not False,
        start=start or DEFAULT_START,
        end=end or DEFAULT_END,
    )


def set_quiet_hours(
    engine: Engine,
    *,
    enabled: bool,
    start: str,
    end: str,
    actor_user_id: int | None,
    now: datetime,
) -> None:
    begin, finish = _parse(start), _parse(end)
    if begin is None or finish is None:
        raise PreferenceError("Heures attendues au format 22:00 (heures et minutes).")
    value = {"actif": enabled, "debut": begin.strftime("%H:%M"), "fin": finish.strftime("%H:%M")}
    with write_transaction(engine) as connection:
        if connection.scalar(select(AppSetting.key).where(AppSetting.key == QUIET_KEY)) is None:
            connection.execute(
                insert(AppSetting).values(key=QUIET_KEY, value=value, updated_at=now)
            )
        else:
            connection.execute(
                update(AppSetting)
                .where(AppSetting.key == QUIET_KEY)
                .values(value=value, updated_at=now)
            )
        audit(
            connection,
            "alerts.quiet_hours",
            ("app_setting", None),
            now=now,
            actor_user_id=actor_user_id,
            details=value,
        )
