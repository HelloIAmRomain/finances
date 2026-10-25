"""Entretien nocturne de la base : purge de l'historique des tâches, compactage si utile."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, func, insert, select, text

from app.models import ProviderCall
from app.models.ops import JobRun, JobStatus
from app.ops.maintenance import database_size, maintain, purge_job_runs

NOW = datetime(2026, 10, 7, 2, 20, tzinfo=UTC)


def _run(engine: Engine, name: str, age: timedelta, status: JobStatus = JobStatus.SUCCESS) -> int:
    started = NOW - age
    with engine.begin() as connection:
        return int(
            connection.execute(
                insert(JobRun)
                .values(
                    job_name=name, slot_start=started, status=status, started_at=started,
                    finished_at=started, attempt=1, stats={},
                )
                .returning(JobRun.id)
            ).scalar_one()
        )  # fmt: skip


def _ids(engine: Engine) -> set[int]:
    with engine.connect() as connection:
        return set(connection.scalars(select(JobRun.id)))


def test_old_runs_are_purged_but_latest_cited_and_running_are_kept(engine: Engine) -> None:
    old = _run(engine, "alerts.dispatch", timedelta(days=200))
    latest_old = _run(engine, "ops.integrity", timedelta(days=200))  # seule exécution de la tâche
    _run(engine, "alerts.dispatch", timedelta(days=1))
    cited = _run(engine, "bank.sync.1", timedelta(days=150))
    _run(engine, "bank.sync.1", timedelta(days=2))
    stuck = _run(engine, "rental.run", timedelta(days=120), JobStatus.RUNNING)
    _run(engine, "rental.run", timedelta(days=3))
    with engine.begin() as connection:
        connection.execute(
            insert(ProviderCall).values(
                requested_at=NOW - timedelta(days=150), unattended=True, method="GET",
                path_template="/accounts/{id}/transactions", http_status=200, job_run_id=cited,
            )
        )  # fmt: skip
    assert purge_job_runs(engine, now=NOW) == 1
    remaining = _ids(engine)
    assert old not in remaining
    assert {latest_old, cited, stuck} <= remaining
    assert purge_job_runs(engine, now=NOW) == 0  # rejouable


def test_maintenance_compacts_only_when_much_space_is_free(engine: Engine) -> None:
    stats = maintain(engine, now=NOW)
    assert stats["compactee"] == 0 and stats["taille_octets"] > 0
    old = NOW - timedelta(days=400)
    with engine.begin() as connection:  # beaucoup de données, puis supprimées : place libre
        connection.execute(
            insert(JobRun),
            [
                {"job_name": f"essai.{n}", "slot_start": old, "status": JobStatus.SUCCESS,
                 "started_at": old, "attempt": 1, "stats": {"remplissage": "x" * 2000}}
                for n in range(2000)
            ],
        )  # fmt: skip
    _run(engine, "garde", timedelta(days=1))
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM job_run WHERE job_name LIKE 'essai.%'"))
    before = database_size(engine)
    stats = maintain(engine, now=NOW)
    assert stats["compactee"] == 1
    assert stats["taille_octets"] < before
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(JobRun)) == 1


def test_database_size_without_file(engine: Engine) -> None:
    from sqlalchemy import create_engine  # noqa: PLC0415

    assert database_size(create_engine("sqlite://")) == 0
