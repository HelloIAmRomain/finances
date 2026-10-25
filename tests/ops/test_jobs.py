"""Exécution unique par tâche et par créneau (inv. 3, NF-RES-01)."""

import multiprocessing
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, insert, select, update
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.db import Base, make_engine
from app.models import AuditLog, JobRun
from app.models.ops import JobStatus
from app.ops.jobs import JobContext, Outcome, run_once

SLOT = datetime(2026, 9, 27, 6, 0, tzinfo=UTC)
NOW = datetime(2026, 9, 27, 6, 0, 30, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    engine = make_engine(tmp_path / "jobs.db")
    Base.metadata.create_all(engine)
    return engine


def _clock(start: datetime = NOW) -> Callable[[], datetime]:
    return lambda: start


def _recorder(calls: list[int]) -> Callable[[JobContext], dict[str, int]]:
    def job(ctx: JobContext) -> dict[str, int]:
        calls.append(ctx.attempt)
        return {}

    return job


def _runs(engine: Engine) -> list[JobRun]:
    with Session(engine) as session:
        return list(session.scalars(select(JobRun).order_by(JobRun.id)))


def test_runs_once_then_already_done(engine: Engine) -> None:
    calls: list[int] = []

    def job(ctx: JobContext) -> dict[str, int]:
        calls.append(1)
        return {"transactions": 12}

    first = run_once(engine, "sync", SLOT, job, now=_clock())
    second = run_once(engine, "sync", SLOT, job, now=_clock())
    assert first.outcome is Outcome.SUCCESS
    assert second.outcome is Outcome.ALREADY_DONE
    assert calls == [1]
    [run] = _runs(engine)
    assert run.status is JobStatus.SUCCESS
    assert run.stats == {"transactions": 12}
    assert run.finished_at == NOW


def test_other_slot_runs_again(engine: Engine) -> None:
    calls: list[int] = []
    for slot in (SLOT, SLOT + timedelta(hours=6)):
        run_once(engine, "sync", slot, _recorder(calls), now=_clock())
    assert len(calls) == 2


def test_failure_is_recorded_and_retried(engine: Engine) -> None:
    attempts: list[int] = []

    def flaky(ctx: JobContext) -> dict[str, int]:
        attempts.append(ctx.attempt)
        if len(attempts) == 1:
            raise RuntimeError("banque indisponible")
        return {}

    failed = run_once(engine, "sync", SLOT, flaky, now=_clock())
    assert failed.outcome is Outcome.FAILED
    assert _runs(engine)[0].error is not None
    assert "banque indisponible" in (_runs(engine)[0].error or "")

    retried = run_once(engine, "sync", SLOT, flaky, now=_clock())
    assert retried.outcome is Outcome.SUCCESS
    assert attempts == [1, 2]
    assert len(_runs(engine)) == 1  # toujours une seule ligne par créneau


def test_gives_up_after_max_attempts(engine: Engine) -> None:
    def broken(ctx: JobContext) -> dict[str, int]:
        raise RuntimeError("toujours en panne")

    outcomes = [
        run_once(engine, "sync", SLOT, broken, now=_clock(), max_attempts=3).outcome
        for _ in range(5)
    ]
    assert outcomes == [Outcome.FAILED] * 3 + [Outcome.EXHAUSTED] * 2
    assert _runs(engine)[0].attempt == 3


def _crashed_run(engine: Engine, heartbeat: datetime) -> None:
    with engine.begin() as connection:
        connection.execute(
            insert(JobRun).values(
                job_name="sync",
                slot_start=SLOT,
                status=JobStatus.RUNNING,
                started_at=heartbeat,
                heartbeat_at=heartbeat,
                attempt=1,
                stats={},
            )
        )


def test_crashed_run_is_resumed(engine: Engine) -> None:
    """Coupure électrique en pleine tâche : au redémarrage, le créneau est repris (NF-RES-01)."""
    _crashed_run(engine, heartbeat=NOW - timedelta(minutes=30))
    calls: list[int] = []
    result = run_once(engine, "sync", SLOT, _recorder(calls), now=_clock())
    assert result.outcome is Outcome.SUCCESS
    assert calls == [2]


def test_live_run_is_left_alone(engine: Engine) -> None:
    _crashed_run(engine, heartbeat=NOW - timedelta(seconds=20))
    calls: list[int] = []
    result = run_once(engine, "sync", SLOT, _recorder(calls), now=_clock())
    assert result.outcome is Outcome.IN_PROGRESS
    assert calls == []


def test_heartbeat_keeps_long_job_alive(engine: Engine) -> None:
    times = iter([NOW, NOW + timedelta(minutes=8), NOW + timedelta(minutes=9)])

    def long_job(ctx: JobContext) -> dict[str, int]:
        ctx.heartbeat()
        return {}

    run_once(engine, "sync", SLOT, long_job, now=lambda: next(times))
    assert _runs(engine)[0].heartbeat_at == NOW + timedelta(minutes=8)


def test_concurrent_threads_run_once(engine: Engine) -> None:
    barrier = threading.Barrier(4)
    calls: list[int] = []
    lock = threading.Lock()

    def job(ctx: JobContext) -> dict[str, int]:
        with lock:
            calls.append(1)
        return {}

    def worker() -> None:
        barrier.wait()
        run_once(engine, "sync", SLOT, job, now=_clock())

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert calls == [1]


def _process_worker(db_path: str, marker: str, barrier: object) -> None:
    engine = make_engine(Path(db_path))
    barrier.wait()  # type: ignore[attr-defined]

    def job(ctx: JobContext) -> dict[str, int]:
        with open(marker, "a", encoding="utf-8") as handle:
            handle.write("x")
        return {}

    run_once(engine, "sync", SLOT, job, now=_clock())


def test_concurrent_processes_run_once(engine: Engine, tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(3)
    marker = tmp_path / "marker"
    marker.touch()
    db_path = str(engine.url.database)
    processes = [
        context.Process(target=_process_worker, args=(db_path, str(marker), barrier))
        for _ in range(3)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0
    assert marker.read_text(encoding="utf-8") == "x"


def test_slot_must_be_aware(engine: Engine) -> None:
    with pytest.raises(ValueError, match="sans fuseau"):
        run_once(engine, "sync", datetime(2026, 9, 27), lambda ctx: {}, now=_clock())  # noqa: DTZ001


def test_success_row_cannot_be_reclaimed(engine: Engine) -> None:
    run_once(engine, "sync", SLOT, lambda ctx: {}, now=_clock())
    with engine.begin() as connection:
        connection.execute(update(JobRun).values(heartbeat_at=NOW - timedelta(days=1)))
    calls: list[int] = []
    run_once(engine, "sync", SLOT, _recorder(calls), now=_clock())
    assert calls == []


class _Refused(RuntimeError):
    retryable = False


def test_non_retryable_error_closes_the_slot(engine: Engine) -> None:
    """Un refus définitif (4xx de la banque) ne se retente pas sur le même créneau : chaque
    reprise serait une requête comptée dans le budget DSP2 (cas réel du 5 octobre 2026)."""
    calls: list[int] = []

    def refused(ctx: JobContext) -> dict[str, int]:
        calls.append(ctx.attempt)
        raise _Refused("balances : erreur 400 (ASPSP_ERROR)")

    outcomes = [
        run_once(engine, "bank.sync.5", SLOT, refused, now=_clock(), max_attempts=2).outcome
        for _ in range(3)
    ]
    assert outcomes == [Outcome.FAILED, Outcome.EXHAUSTED, Outcome.EXHAUSTED]
    assert calls == [1]
    (run,) = _runs(engine)
    assert (
        run.status is JobStatus.FAILED and run.attempt == 2 and "ASPSP_ERROR" in (run.error or "")
    )


def test_failure_never_logs_bound_values(engine: Engine, caplog: pytest.LogCaptureFixture) -> None:
    sentinel = "Libellé-Personnel-7f3a"

    def leaky(ctx: JobContext) -> dict[str, int]:
        row = {"id": 1, "action": "test", "details": {"libelle": sentinel}}
        with ctx.engine.begin() as connection:
            connection.execute(insert(AuditLog).values(row))
            connection.execute(insert(AuditLog).values(row))  # clé en double : IntegrityError
        return {}

    assert run_once(engine, "sync", SLOT, leaky, now=_clock()).outcome is Outcome.FAILED
    [run] = _runs(engine)
    assert "IntegrityError" in (run.error or "")
    assert sentinel not in (run.error or "")
    assert sentinel not in caplog.text
