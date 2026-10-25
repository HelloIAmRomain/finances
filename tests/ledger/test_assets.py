"""Patrimoine : actifs et dettes saisis (COL-05), patrimoine net."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import Engine, insert

from app.ledger.assets import (
    AssetError,
    archive_asset,
    assets,
    create_asset,
    record_value,
    wealth,
)
from app.models import BalanceSnapshot
from app.models.ledger import AssetKind, BalanceType

NOW = datetime(2026, 10, 7, 8, 0, tzinfo=UTC)


def test_assets_history_and_net_wealth(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            insert(BalanceSnapshot).values(
                account_id=1, as_of_date=date(2026, 10, 7), balance_type=BalanceType.BOOKED,
                amount_cents=150_000, currency="EUR", fetched_at=NOW,
            )
        )  # fmt: skip
    livret = create_asset(
        engine, name="  Livret   A ", kind=AssetKind.SAVINGS, actor_user_id=None, now=NOW
    )
    loan = create_asset(
        engine, name="Crédit auto", kind=AssetKind.LOAN, actor_user_id=None, now=NOW
    )
    other = create_asset(
        engine, name="Montre", kind=AssetKind.OTHER, counted=False, actor_user_id=None, now=NOW
    )
    record_value(
        engine, livret, as_of=date(2026, 9, 1), value_cents=1_000_000, actor_user_id=None, now=NOW
    )
    record_value(
        engine, livret, as_of=date(2026, 10, 1), value_cents=1_020_000, actor_user_id=None, now=NOW
    )
    record_value(
        engine, livret, as_of=date(2026, 10, 1), value_cents=1_025_000, actor_user_id=None, now=NOW
    )
    record_value(
        engine, loan, as_of=date(2026, 10, 1), value_cents=800_000, actor_user_id=None, now=NOW
    )
    record_value(
        engine, other, as_of=date(2026, 10, 1), value_cents=50_000, actor_user_id=None, now=NOW
    )
    rows = {r.id: r for r in assets(engine)}
    assert rows[livret].name == "Livret A"
    assert [v.value_cents for v in rows[livret].history] == [1_025_000, 1_000_000]
    assert rows[livret].change_cents == 25_000  # correction du même jour : une seule valeur
    assert [v.value_cents for v in rows[loan].history] == [-800_000]
    assert rows[loan].change_cents is None
    total = wealth(engine)
    assert (total.accounts_cents, total.assets_cents, total.debts_cents) == (
        150_000,
        1_025_000,
        -800_000,
    )
    assert total.total_cents == 375_000

    archive_asset(engine, other, actor_user_id=None, now=NOW)
    assert other not in {r.id for r in assets(engine)}
    with pytest.raises(AssetError, match="introuvable"):
        record_value(
            engine, other, as_of=date(2026, 10, 2), value_cents=1, actor_user_id=None, now=NOW
        )
    with pytest.raises(AssetError, match="introuvable"):
        archive_asset(engine, other, actor_user_id=None, now=NOW)
    with pytest.raises(AssetError, match="Nom"):
        create_asset(engine, name=" ", kind=AssetKind.PEA, actor_user_id=None, now=NOW)
    with pytest.raises(AssetError, match="Bien"):
        create_asset(
            engine,
            name="Appart",
            kind=AssetKind.REAL_ESTATE,
            property_id=9,
            actor_user_id=None,
            now=NOW,
        )
