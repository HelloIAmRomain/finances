import argparse
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text

from app.db import BUSY_TIMEOUT_MS, make_engine

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return make_engine(tmp_path / "test.db")


def _pragma(engine: Engine, name: str) -> object:
    with engine.connect() as connection:
        return connection.execute(text(f"PRAGMA {name}")).scalar_one()


def test_pragmas_applied(engine: Engine) -> None:
    assert _pragma(engine, "journal_mode") == "wal"
    assert _pragma(engine, "foreign_keys") == 1
    assert _pragma(engine, "busy_timeout") == BUSY_TIMEOUT_MS
    assert _pragma(engine, "synchronous") == 1  # NORMAL


def test_pragmas_applied_to_every_new_connection(engine: Engine) -> None:
    with engine.connect() as first, engine.connect() as second:
        for connection in (first, second):
            assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1


def _alembic_config(db_path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.cmd_opts = argparse.Namespace(x=[f"db={db_path}"])
    return config


def test_upgrade_head_on_empty_database(tmp_path: Path) -> None:
    db_path = tmp_path / "migrate.db"
    command.upgrade(_alembic_config(db_path), "head")
    assert db_path.exists()


def test_models_match_migrations(tmp_path: Path) -> None:
    """`alembic check` : aucune différence entre les modèles et la dernière migration."""
    config = _alembic_config(tmp_path / "check.db")
    command.upgrade(config, "head")
    command.check(config)
