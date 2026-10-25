"""Entretien de la base, chaque nuit (optimisation, docs/feuille-de-route.md) :

- `PRAGMA optimize` : statistiques du planificateur de requêtes à jour, pages qui restent rapides
  avec les années d'historique ;
- purge de l'historique des tâches planifiées de plus de 90 jours (environ 2 500 lignes par
  jour, utiles seulement pour l'idempotence du dernier créneau et le diagnostic récent) ; une
  exécution citée par un appel à la banque ou un import est gardée, comme la dernière de chaque
  tâche ;
- compactage (`VACUUM`) seulement quand la place libre dépasse 20 % du fichier : il réécrit toute
  la base, inutile de l'user plus souvent ;
- taille du fichier relevée pour la page « État du système ».
"""

from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import Engine, delete, func, select

from app.db import write_transaction
from app.models import ImportBatch, ProviderCall
from app.models.ops import JobRun, JobStatus

KEEP_RUNS = timedelta(days=90)
VACUUM_FREE_PERCENT = 20
VACUUM_MIN_FREE_PAGES = 256


def database_size(engine: Engine) -> int:
    """Taille en octets du fichier de base et de son journal WAL (0 si la base n'est pas un
    fichier)."""
    database = engine.url.database
    if not database:
        return 0
    path = Path(database)
    return sum(
        candidate.stat().st_size
        for candidate in (path, path.with_name(path.name + "-wal"))
        if candidate.exists()
    )


def _pragma(engine: Engine, name: str) -> int:
    with engine.connect() as connection:
        return int(connection.exec_driver_sql(f"PRAGMA {name}").scalar_one())


def _outside_transaction(engine: Engine, statement: str) -> None:
    """VACUUM et optimize s'exécutent hors transaction (le pilote est en mode autocommit)."""
    raw = engine.raw_connection()
    try:
        raw.driver_connection.execute(statement)  # type: ignore[union-attr]
    finally:
        raw.close()


def purge_job_runs(engine: Engine, *, now: datetime) -> int:
    cutoff = now - KEEP_RUNS
    latest = select(func.max(JobRun.id)).group_by(JobRun.job_name)
    cited = (
        select(ProviderCall.job_run_id)
        .where(ProviderCall.job_run_id.is_not(None))
        .union(select(ImportBatch.job_run_id).where(ImportBatch.job_run_id.is_not(None)))
    )
    with write_transaction(engine) as connection:
        result = connection.execute(
            delete(JobRun).where(
                JobRun.started_at < cutoff,
                JobRun.status != JobStatus.RUNNING,
                JobRun.id.not_in(latest),
                JobRun.id.not_in(cited),
            )
        )
    return int(result.rowcount)


def maintain(engine: Engine, *, now: datetime) -> dict[str, int]:
    purged = purge_job_runs(engine, now=now)
    _outside_transaction(engine, "PRAGMA optimize")
    pages, free = _pragma(engine, "page_count"), _pragma(engine, "freelist_count")
    compact = free >= VACUUM_MIN_FREE_PAGES and free * 100 >= pages * VACUUM_FREE_PERCENT
    if compact:
        _outside_transaction(engine, "VACUUM")
        _outside_transaction(engine, "PRAGMA wal_checkpoint(TRUNCATE)")
    return {
        "taille_octets": database_size(engine),
        "executions_purgees": purged,
        "compactee": int(compact),
    }
