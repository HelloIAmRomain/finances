"""Moteur SQLite et base déclarative commune.

Chaque connexion reçoit les mêmes pragmas (DT §4) :
- WAL : lecteurs et écrivain concurrents (web + worker), copie cohérente par la sauvegarde ;
- clés étrangères appliquées ;
- attente de 5 s sur un verrou plutôt qu'une erreur immédiate ;
- `synchronous=NORMAL`, suffisant et sûr en mode WAL.

Transactions explicites (recette SQLAlchemy pour pysqlite) : le pilote ne démarre plus de
transaction implicite ; SQLAlchemy émet `BEGIN`, ou `BEGIN IMMEDIATE` via `write_transaction`
pour une transaction qui lit puis écrit (sinon deux processus concurrents peuvent échouer en
« database is locked » au moment d'écrire).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, CursorResult, Engine, MetaData, create_engine, event
from sqlalchemy.orm import DeclarativeBase

BUSY_TIMEOUT_MS = 5000

PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA foreign_keys=ON",
    f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}",
    "PRAGMA synchronous=NORMAL",
)

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def sqlite_url(path: Path) -> str:
    return f"sqlite+pysqlite:///{path}"


def _apply_pragmas(dbapi_connection: Any, _record: Any) -> None:
    dbapi_connection.isolation_level = None  # transactions pilotées par SQLAlchemy
    cursor = dbapi_connection.cursor()
    try:
        for pragma in PRAGMAS:
            cursor.execute(pragma)
    finally:
        cursor.close()


def _begin(connection: Connection) -> None:
    mode = connection.get_execution_options().get("sqlite_begin", "DEFERRED")
    connection.exec_driver_sql(f"BEGIN {mode}")


def make_engine(path: Path) -> Engine:
    # hide_parameters : une erreur SQL ne recopie jamais les valeurs (libellés, noms) dans les
    # journaux ni dans job_run.error (inv. 5).
    engine = create_engine(sqlite_url(path), hide_parameters=True)
    event.listen(engine, "connect", _apply_pragmas)
    event.listen(engine, "begin", _begin)
    return engine


@contextmanager
def write_transaction(engine: Engine) -> Iterator[Connection]:
    """Transaction d'écriture prise d'emblée (`BEGIN IMMEDIATE`), validée en sortie."""
    with engine.connect().execution_options(sqlite_begin="IMMEDIATE") as connection:
        with connection.begin():
            yield connection


def inserted_id(result: CursorResult[Any]) -> int:
    """Identifiant de la ligne que vient d'insérer `result` (INSERT d'une seule ligne)."""
    key = result.inserted_primary_key
    if key is None:  # pragma: no cover (SQLAlchemy lève avant : INSERT multiple ou non-INSERT)
        raise RuntimeError("aucune ligne insérée")
    return int(key[0])
