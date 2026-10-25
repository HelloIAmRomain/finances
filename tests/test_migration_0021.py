"""Migration 0021 (quittances) : part des charges reprise sur les échéances existantes."""

from pathlib import Path

from alembic import command
from sqlalchemy import text

from app.db import make_engine
from tests.test_models import NOW, _account, _alembic


def test_existing_dues_get_the_lease_charges_capped_by_amount(tmp_path: Path) -> None:
    db_path = tmp_path / "base.db"
    config = _alembic(db_path)
    command.upgrade(config, "0020")
    engine = make_engine(db_path)
    with engine.begin() as db:
        account = _account(db)
        db.execute(
            text("INSERT INTO property (name, furnished, created_at) VALUES ('A', 1, :n)"),
            {"n": NOW},
        )
        db.execute(
            text(
                "INSERT INTO lease (kind, label, property_id, account_id, start_date, rent_cents,"
                " charges_cents, due_day, alert_day, unpaid_day, deposit_cents, created_at)"
                " VALUES ('rent', 'Bail', 1, :a, '2026-09-01', 70000, 5000, 1, 5, 10, 0, :n)"
            ),
            {"a": account, "n": NOW},
        )
        for period, amount in (("2026-09", 75000), ("2026-10", 3000)):
            db.execute(
                text(
                    "INSERT INTO rent_due (lease_id, period, due_date, amount_cents, created_at)"
                    " VALUES (1, :p, :p || '-01', :m, :n)"
                ),
                {"p": period, "m": amount, "n": NOW},
            )
    engine.dispose()

    command.upgrade(config, "head")
    engine = make_engine(db_path)
    with engine.connect() as db:
        rows = db.execute(text("SELECT period, charges_cents FROM rent_due ORDER BY period")).all()
        assert [tuple(row) for row in rows] == [("2026-09", 5000), ("2026-10", 3000)]
        assert db.execute(text("SELECT landlord_enc FROM property")).scalar_one() is None
    engine.dispose()
    command.downgrade(config, "0020")
