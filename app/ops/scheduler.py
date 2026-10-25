"""Planificateur du worker (DT §4, I-14).

À chaque tour (toutes les 30 s), chaque tâche est lancée pour son **dernier créneau échu** via
`run_once` : la table `job_run` garantit une seule exécution par créneau. Conséquences :
- après un arrêt couvrant plusieurs créneaux, seul le plus récent est exécuté (rattrapage unique) ;
- un redémarrage ne rejoue jamais un créneau déjà réussi ;
- une tâche en échec n'empêche pas les autres de tourner ;
- une tâche qui déborde sur le créneau suivant n'est pas relancée en parallèle (`run_once`).

Les horaires quotidiens sont exprimés à l'heure de Paris et convertis en UTC. Au passage à
l'heure d'été, un horaire de 02:00-03:00 (inexistant ce jour-là) tombe une heure plus tard ; au
retour à l'heure d'hiver, un horaire de 02:00-03:00 (vu deux fois) ne s'exécute qu'une fois.
APScheduler n'est pas utilisé (Q13) : `job_run` suffit à l'idempotence et au rattrapage.
"""

import hashlib
import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Protocol

from sqlalchemy import Engine

from app.ops.jobs import JobFunction, JobResult, run_once
from app.timeutil import PARIS, ensure_utc, utcnow

log = logging.getLogger(__name__)

TICK_INTERVAL = timedelta(seconds=30)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class Schedule(Protocol):
    def latest(self, now: datetime) -> datetime:
        """Dernier créneau (UTC) inférieur ou égal à `now`."""
        ...


class DailyAt:
    """Créneaux quotidiens à des heures fixes de Paris, éventuellement décalés.

    `jitter` : décalage maximal ajouté à chaque créneau. Il est **déterministe** pour une même
    graine (`seed`, par ex. le nom de la tâche) : le créneau reste stable d'un tour à l'autre et
    d'un redémarrage à l'autre, mais deux comptes n'interrogent pas leur banque à la même seconde.
    """

    def __init__(self, *times: time, jitter: timedelta = timedelta(0), seed: str = "") -> None:
        if not times:
            raise ValueError("au moins une heure de déclenchement")
        if jitter < timedelta(0):
            raise ValueError("décalage négatif")
        self.times = tuple(sorted(times))
        span = int(jitter.total_seconds())
        digest = int(hashlib.sha256(seed.encode()).hexdigest(), 16)
        self.offset = timedelta(seconds=digest % (span + 1)) if span else timedelta(0)

    def latest(self, now: datetime) -> datetime:
        now_utc = ensure_utc(now)
        today = now_utc.astimezone(PARIS).date()
        candidates = [
            datetime.combine(day, moment, tzinfo=PARIS).astimezone(UTC) + self.offset
            for day in (today - timedelta(days=2), today - timedelta(days=1), today)
            for moment in self.times
        ]
        return max(slot for slot in candidates if slot <= now_utc)

    def __repr__(self) -> str:
        return f"DailyAt({', '.join(t.strftime('%H:%M') for t in self.times)} Paris)"


class Every:
    """Créneaux réguliers alignés sur des multiples de la période depuis l'époque UTC."""

    def __init__(self, period: timedelta) -> None:
        if period <= timedelta(0):
            raise ValueError("période strictement positive attendue")
        self.period = period

    def latest(self, now: datetime) -> datetime:
        elapsed = ensure_utc(now) - _EPOCH
        return _EPOCH + (elapsed // self.period) * self.period

    def __repr__(self) -> str:
        return f"Every({self.period})"


@dataclass(frozen=True)
class Job:
    name: str
    schedule: Schedule
    run: JobFunction


class Scheduler:
    def __init__(
        self,
        engine: Engine,
        jobs: Sequence[Job],
        *,
        now: Callable[[], datetime] = utcnow,
        tick_interval: timedelta = TICK_INTERVAL,
    ) -> None:
        names = [job.name for job in jobs]
        if len(names) != len(set(names)):
            raise ValueError(f"nom de tâche en double : {names}")
        self.engine = engine
        self.jobs = tuple(jobs)
        self.now = now
        self.tick_interval = tick_interval

    def tick(self) -> dict[str, JobResult]:
        results = {}
        for job in self.jobs:
            slot = job.schedule.latest(self.now())
            results[job.name] = run_once(self.engine, job.name, slot, job.run, now=self.now)
        return results

    def run_forever(self, stop: threading.Event) -> int:
        """Tourne jusqu'à ce que `stop` soit levé ; renvoie le nombre de tours."""
        ticks = 0
        log.info(
            "planificateur démarré : %s", ", ".join(f"{j.name} {j.schedule!r}" for j in self.jobs)
        )
        while not stop.is_set():
            try:
                self.tick()
            except Exception:  # base indisponible, etc. : on retente au tour suivant
                log.exception("tour du planificateur en échec")
            ticks += 1
            stop.wait(self.tick_interval.total_seconds())
        log.info("planificateur arrêté proprement")
        return ticks
