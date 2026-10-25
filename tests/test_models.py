"""Schéma du lot 1 (PLAN.md §5) : contraintes, immuabilité, équivalence modèles / migration."""

import argparse
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import IntegrityError

import app.models  # noqa: F401 (enregistre les tables)
from app.db import Base, make_engine

ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-09-27 10:00:00"


def _alembic(db_path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.cmd_opts = argparse.Namespace(x=[f"db={db_path}"])
    return config


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    db_path = tmp_path / "migrated.db"
    command.upgrade(_alembic(db_path), "head")
    return make_engine(db_path)


@pytest.fixture
def db(engine: Engine) -> Iterator[Connection]:
    with engine.connect() as connection:
        yield connection


def _account(db: Connection) -> int:
    db.execute(
        text(
            "INSERT INTO account (name, type, currency, include_in_net_worth, syncs_per_day)"
            " VALUES ('Courant', 'checking', 'EUR', 1, 4)"
        )
    )
    return int(db.execute(text("SELECT max(id) FROM account")).scalar_one())


def _transaction(db: Connection, account_id: int, dedupe_key: str, amount: int = -1250) -> int:
    db.execute(
        text(
            "INSERT INTO ledger_transaction (account_id, dedupe_key, fingerprint, occurrence,"
            " booking_date, amount_cents, currency, label_raw, label_clean, created_at)"
            " VALUES (:account, :key, 'fp', 1, '2026-09-01', :amount, 'EUR', 'CB CAFE', 'CAFE',"
            " :now)"
        ),
        {"account": account_id, "key": dedupe_key, "amount": amount, "now": NOW},
    )
    return int(db.execute(text("SELECT max(id) FROM ledger_transaction")).scalar_one())


def _schema_objects(engine: Engine) -> set[tuple[str, str]]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT type, name FROM sqlite_master"
                " WHERE name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
            )
        )
        return {(row[0], row[1]) for row in rows}


def test_all_lot1_tables_created(engine: Engine) -> None:
    tables = {name for kind, name in _schema_objects(engine) if kind == "table"}
    assert tables == set(Base.metadata.tables)
    assert len(tables) == 49  # socle 21, link_nonce, lot 2 : 8 + 2, appareils 2, clés 2, IA 6,
    # échéances 1, budgets 1, réglages 1, IRL 1, patrimoine 2, dépôt de garantie 1


def test_create_all_matches_migration(engine: Engine, tmp_path: Path) -> None:
    """Mêmes tables, index et déclencheurs, que la base vienne de la migration ou des modèles."""
    other = make_engine(tmp_path / "create_all.db")
    Base.metadata.create_all(other)
    assert _schema_objects(other) == _schema_objects(engine)


def test_triggers_present(engine: Engine) -> None:
    triggers = {name for kind, name in _schema_objects(engine) if kind == "trigger"}
    assert triggers == {
        "ledger_transaction_immutable",
        "audit_log_no_update",
        "audit_log_no_delete",
    }


def test_ledger_transaction_is_immutable(db: Connection) -> None:
    tx = _transaction(db, _account(db), "ext:A1")
    with pytest.raises(IntegrityError, match="immuable"):
        db.execute(
            text("UPDATE ledger_transaction SET amount_cents = 1 WHERE id = :id"), {"id": tx}
        )


def test_ledger_transaction_can_be_deleted(db: Connection) -> None:
    # Effacement en fin de relation (NF-DATA-01) : la suppression reste possible.
    tx = _transaction(db, _account(db), "ext:A1")
    db.execute(text("DELETE FROM ledger_transaction WHERE id = :id"), {"id": tx})


def test_dedupe_key_unique_per_account(db: Connection) -> None:
    first, second = _account(db), _account(db)
    _transaction(db, first, "ext:A1")
    _transaction(db, second, "ext:A1")  # même clé, autre compte : autorisé
    with pytest.raises(IntegrityError):
        _transaction(db, first, "ext:A1")


def test_audit_log_is_append_only(db: Connection) -> None:
    db.execute(
        text("INSERT INTO audit_log (at, action, details) VALUES (:now, 'login', '{}')"),
        {"now": NOW},
    )
    with pytest.raises(IntegrityError, match="ajout seul"):
        db.execute(text("UPDATE audit_log SET action = 'autre'"))
    with pytest.raises(IntegrityError, match="ajout seul"):
        db.execute(text("DELETE FROM audit_log"))


def test_enum_values_are_checked(db: Connection) -> None:
    with pytest.raises(IntegrityError, match="CHECK"):
        db.execute(
            text(
                "INSERT INTO account (name, type, currency, include_in_net_worth, syncs_per_day)"
                " VALUES ('X', 'crypto', 'EUR', 1, 2)"
            )
        )


def test_syncs_per_day_bounded_by_dsp2(db: Connection) -> None:
    with pytest.raises(IntegrityError, match="CHECK"):
        db.execute(
            text(
                "INSERT INTO account (name, type, currency, include_in_net_worth, syncs_per_day)"
                " VALUES ('X', 'checking', 'EUR', 1, 5)"
            )
        )


def _duplicate_link(db: Connection, canonical: int, duplicate: int, status: str) -> None:
    db.execute(
        text(
            "INSERT INTO duplicate_link (canonical_tx_id, duplicate_tx_id, mode, reason, status,"
            " created_at) VALUES (:c, :d, 'auto', 'exact', :s, :now)"
        ),
        {"c": canonical, "d": duplicate, "s": status, "now": NOW},
    )


def test_only_one_active_duplicate_link_per_transaction(db: Connection) -> None:
    account = _account(db)
    api, csv, other = (_transaction(db, account, key) for key in ("ext:A", "fp:x:1", "fp:y:1"))
    _duplicate_link(db, api, csv, "rejected")
    _duplicate_link(db, api, csv, "confirmed")  # un lien rejeté n'empêche pas un nouveau lien
    with pytest.raises(IntegrityError):
        _duplicate_link(db, other, csv, "proposed")


def test_transaction_cannot_duplicate_itself(db: Connection) -> None:
    tx = _transaction(db, _account(db), "ext:A")
    with pytest.raises(IntegrityError, match="CHECK"):
        _duplicate_link(db, tx, tx, "proposed")


def test_foreign_keys_enforced(db: Connection) -> None:
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        _transaction(db, 999, "ext:orphelin")


def test_job_run_unique_per_slot(db: Connection) -> None:
    insert = text(
        "INSERT INTO job_run (job_name, slot_start, status, started_at, attempt, stats)"
        " VALUES ('sync', '2026-09-27 06:00:00', 'running', :now, 1, '{}')"
    )
    db.execute(insert, {"now": NOW})
    with pytest.raises(IntegrityError):
        db.execute(insert, {"now": NOW})


def test_downgrade_then_upgrade(tmp_path: Path) -> None:
    config = _alembic(tmp_path / "cycle.db")
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    engine = make_engine(tmp_path / "cycle.db")
    assert _schema_objects(engine) == set()
    command.upgrade(config, "head")
    assert len(_schema_objects(engine)) > 21
