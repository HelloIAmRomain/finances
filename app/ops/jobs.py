"""Exécution unique d'une tâche par créneau (inv. 3, NF-RES-01).

La table `job_run` est la seule source de vérité (I-14) :
- une ligne par (tâche, créneau), réservée par un INSERT unique : deux processus ne lancent
  jamais la même tâche pour le même créneau ;
- une tâche en cours signale qu'elle est vivante (`heartbeat`) ; une exécution dont le signal
  est trop ancien (coupure de courant, plantage) est reprise au passage suivant ;
- une tâche en échec est retentée, au plus `max_attempts` fois pour ce créneau ; une erreur
  marquée `retryable = False` (refus définitif, par ex. un 4xx de la banque) clôt le créneau.
"""

import enum
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Engine, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.db import write_transaction
from app.models.ops import JobRun, JobStatus
from app.timeutil import ensure_utc, utcnow

log = logging.getLogger(__name__)

STALE_AFTER = timedelta(minutes=10)
MAX_ATTEMPTS = 3
ERROR_MAX_LENGTH = 1000


class Outcome(enum.Enum):
    SUCCESS = "success"
    FAILED = "failed"
    ALREADY_DONE = "already_done"
    IN_PROGRESS = "in_progress"
    EXHAUSTED = "exhausted"


@dataclass(frozen=True)
class JobResult:
    outcome: Outcome
    run_id: int | None = None
    attempt: int | None = None


@dataclass(frozen=True)
class JobContext:
    engine: Engine
    run_id: int
    attempt: int
    slot_start: datetime
    now: Callable[[], datetime]

    def heartbeat(self) -> None:
        """À appeler régulièrement pendant une tâche longue (moins de `STALE_AFTER`)."""
        with write_transaction(self.engine) as connection:
            connection.execute(
                update(JobRun).where(JobRun.id == self.run_id).values(heartbeat_at=self.now())
            )


JobFunction = Callable[[JobContext], Mapping[str, Any]]


def run_once(
    engine: Engine,
    job_name: str,
    slot_start: datetime,
    job: JobFunction,
    *,
    now: Callable[[], datetime] = utcnow,
    stale_after: timedelta = STALE_AFTER,
    max_attempts: int = MAX_ATTEMPTS,
) -> JobResult:
    slot = ensure_utc(slot_start)
    claim = _claim(
        engine, job_name, slot, started=now(), stale_after=stale_after, max_attempts=max_attempts
    )
    if isinstance(claim, JobResult):
        return claim
    run_id, attempt = claim
    context = JobContext(engine, run_id, attempt, slot, now)
    try:
        stats = dict(job(context))
    except Exception as exc:
        log.exception("tâche %s (créneau %s) en échec", job_name, slot.isoformat())
        error = f"{type(exc).__name__}: {exc}"[:ERROR_MAX_LENGTH]
        final = not getattr(exc, "retryable", True)
        _finish(
            engine,
            run_id,
            JobStatus.FAILED,
            now(),
            error=error,
            attempt=max_attempts if final else None,
        )
        return JobResult(Outcome.FAILED, run_id, attempt)
    _finish(engine, run_id, JobStatus.SUCCESS, now(), stats=stats)
    return JobResult(Outcome.SUCCESS, run_id, attempt)


def _claim(
    engine: Engine,
    job_name: str,
    slot: datetime,
    *,
    started: datetime,
    stale_after: timedelta,
    max_attempts: int,
) -> tuple[int, int] | JobResult:
    running = {"status": JobStatus.RUNNING, "started_at": started, "heartbeat_at": started}
    # Pas de chevauchement : tant qu'une exécution de cette tâche (autre créneau) est vivante,
    # le nouveau créneau n'est pas réservé ; il le sera au tour suivant.
    with engine.connect() as connection:
        busy = connection.execute(
            select(JobRun.id, JobRun.attempt).where(
                JobRun.job_name == job_name,
                JobRun.slot_start != slot,
                JobRun.status == JobStatus.RUNNING,
                JobRun.heartbeat_at > started - stale_after,
            )
        ).first()
    if busy is not None:
        return JobResult(Outcome.IN_PROGRESS, busy.id, busy.attempt)
    try:
        with write_transaction(engine) as connection:
            result = connection.execute(
                insert(JobRun).values(
                    job_name=job_name, slot_start=slot, attempt=1, stats={}, **running
                )
            )
            key = result.inserted_primary_key
            if key is None:
                raise RuntimeError("identifiant de job_run non renvoyé par la base")
            return int(key[0]), 1
    except IntegrityError:
        pass  # créneau déjà réservé : examiner la ligne existante

    with write_transaction(engine) as connection:
        row = connection.execute(
            select(JobRun.id, JobRun.status, JobRun.attempt, JobRun.heartbeat_at).where(
                JobRun.job_name == job_name, JobRun.slot_start == slot
            )
        ).one()
        if row.status in (JobStatus.SUCCESS, JobStatus.SKIPPED):
            return JobResult(Outcome.ALREADY_DONE, row.id, row.attempt)
        alive = row.heartbeat_at is not None and row.heartbeat_at > started - stale_after
        if row.status is JobStatus.RUNNING and alive:
            return JobResult(Outcome.IN_PROGRESS, row.id, row.attempt)
        if row.attempt >= max_attempts:
            return JobResult(Outcome.EXHAUSTED, row.id, row.attempt)
        connection.execute(
            update(JobRun)
            .where(JobRun.id == row.id)
            .values(attempt=row.attempt + 1, finished_at=None, error=None, **running)
        )
        log.warning(
            "tâche %s (créneau %s) reprise, tentative %d",
            job_name,
            slot.isoformat(),
            row.attempt + 1,
        )
        return int(row.id), int(row.attempt) + 1


def _finish(
    engine: Engine,
    run_id: int,
    status: JobStatus,
    finished: datetime,
    *,
    stats: dict[str, Any] | None = None,
    error: str | None = None,
    attempt: int | None = None,
) -> None:
    values: dict[str, Any] = {"status": status, "finished_at": finished, "error": error}
    if attempt is not None:  # créneau clos : plus de reprise
        values["attempt"] = attempt
    if stats is not None:
        values["stats"] = stats
    with write_transaction(engine) as connection:
        connection.execute(update(JobRun).where(JobRun.id == run_id).values(**values))
