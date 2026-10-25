"""Environnement Alembic : migrations sur SQLite en mode « batch » (ALTER limité sous SQLite)."""

import logging.config
from pathlib import Path
from typing import Any, Literal

from alembic import context
from alembic.autogenerate.api import AutogenContext
from sqlalchemy import Enum
from sqlalchemy.types import TypeDecorator

import app.models  # noqa: F401 (enregistre toutes les tables dans Base.metadata)
from app.config import Settings
from app.db import Base, make_engine

config = context.config
if config.config_file_name is not None:
    logging.config.fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def render_item(kind: str, obj: Any, autogen_context: AutogenContext) -> str | Literal[False]:
    """Les migrations n'importent pas le code applicatif : un type maison (UTCDateTime,
    EncryptedString) est écrit sous son type SQL sous-jacent, stable dans le temps."""
    if kind == "type" and isinstance(obj, TypeDecorator):
        return f"sa.{type(obj.impl).__name__}()"
    if kind == "type" and isinstance(obj, Enum):
        # La contrainte CHECK de l'énumération est rendue à part, une seule fois.
        return f"sa.String(length={obj.length})"
    return False


def _database_path() -> Path:
    override = context.get_x_argument(as_dictionary=True).get("db")
    return Path(override) if override else Settings().database_path


def run_migrations_offline() -> None:
    context.configure(
        url=f"sqlite:///{_database_path()}",
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection")
    if connectable is None:
        connectable = make_engine(_database_path())
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
            render_item=render_item,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
