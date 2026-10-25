"""Migration 0014 (échéances, nature « CFE ») sur une base qui contient déjà des charges."""

import re
from pathlib import Path

from alembic import command
from sqlalchemy import Engine, text

from app.db import Base, make_engine
from tests.test_models import NOW, _account, _alembic, _transaction


def _table_sql(engine: Engine, table: str) -> str:
    with engine.connect() as connection:
        sql = str(
            connection.execute(
                text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :name"),
                {"name": table},
            ).scalar_one()
        )
    return re.sub(r"\s+", " ", sql.replace('"', "")).replace("( ", "(").replace(" )", ")")


def _constraints(sql: str) -> set[str]:
    return {part.strip().rstrip(")") for part in re.findall(r"CONSTRAINT [^,]+(?:\([^)]*\))?", sql)}


def test_upgrade_keeps_rule_links_and_allows_cfe(tmp_path: Path) -> None:
    db_path = tmp_path / "base.db"
    config = _alembic(db_path)
    command.upgrade(config, "0013")
    engine = make_engine(db_path)
    with engine.begin() as db:
        account = _account(db)
        tx = _transaction(db, account, "ext:1")
        db.execute(
            text("INSERT INTO property (name, furnished, created_at) VALUES ('A', 1, :n)"),
            {"n": NOW},
        )
        db.execute(
            text(
                "INSERT INTO property_expense_rule (property_id, kind, pattern, created_at)"
                " VALUES (1, 'copropriete', 'SYNDIC', :n)"
            ),
            {"n": NOW},
        )
        db.execute(
            text(
                "INSERT INTO property_expense (property_id, transaction_id, kind, rule_id,"
                " created_at) VALUES (1, :tx, 'copropriete', 1, :n)"
            ),
            {"tx": tx, "n": NOW},
        )
    engine.dispose()

    command.upgrade(config, "head")
    engine = make_engine(db_path)
    with engine.begin() as db:
        assert db.execute(text("SELECT rule_id FROM property_expense")).scalar_one() == 1
        db.execute(text("UPDATE property_expense SET kind = 'cfe'"))
        db.execute(
            text(
                "INSERT INTO deadline (kind, due_date, remind_days, yearly, created_at)"
                " VALUES ('taxe_fonciere', '2026-10-15', 7, 1, :n)"
            ),
            {"n": NOW},
        )
    # Mêmes contraintes que les modèles (create_all).
    reference = make_engine(tmp_path / "modeles.db")
    Base.metadata.create_all(reference)
    for table in ("property_expense", "property_expense_rule", "deadline"):
        assert _constraints(_table_sql(engine, table)) == _constraints(
            _table_sql(reference, table)
        ), table
    engine.dispose()

    command.downgrade(config, "0013")
    engine = make_engine(db_path)
    with engine.connect() as db:
        assert db.execute(text("SELECT kind, rule_id FROM property_expense")).one() == ("autre", 1)


def test_review_item_accepts_ai_suggestions_after_0016(tmp_path: Path) -> None:
    db_path = tmp_path / "base.db"
    config = _alembic(db_path)
    command.upgrade(config, "0015")
    engine = make_engine(db_path)
    with engine.begin() as db:
        db.execute(
            text(
                "INSERT INTO review_item (kind, dedupe_key, status, details, created_at)"
                " VALUES ('duplicate', 'd:1', 'open', '{}', :n)"
            ),
            {"n": NOW},
        )
    engine.dispose()
    command.upgrade(config, "head")
    engine = make_engine(db_path)
    with engine.begin() as db:
        db.execute(
            text(
                "INSERT INTO review_item (kind, dedupe_key, status, details, created_at)"
                " VALUES ('ai_suggestion', 'ai:1', 'open', '{}', :n)"
            ),
            {"n": NOW},
        )
    reference = make_engine(tmp_path / "modeles.db")
    Base.metadata.create_all(reference)
    assert _constraints(_table_sql(engine, "review_item")) == _constraints(
        _table_sql(reference, "review_item")
    )
    engine.dispose()
    command.downgrade(config, "0015")
    engine = make_engine(db_path)
    with engine.connect() as db:
        assert db.execute(text("SELECT dedupe_key FROM review_item")).scalars().all() == ["d:1"]
