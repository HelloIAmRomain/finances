"""Budgets mensuels (ANA-06) : dépense nette, alertes à 80 et 100 %, mois suivant, moyenne."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import Engine, insert, select, update

from app.categorize.budgets import (
    BudgetError,
    average_spending,
    budget_statuses,
    check_budgets,
    remove_budget,
    set_budget,
)
from app.categorize.seed import seed_categories
from app.categorize.service import categorize_manually
from app.models import Account, Alert, Category, LedgerTransaction
from app.models.alerts import AlertStatus, Severity

NOW = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
_ids = iter(range(1, 1_000_000))


@pytest.fixture
def db(engine: Engine) -> Engine:
    seed_categories(engine)
    return engine


def _cat(engine: Engine, name: str) -> int:
    with engine.connect() as connection:
        return int(connection.scalars(select(Category.id).where(Category.name == name)).one())


def _spend(engine: Engine, amount: int, day: date, category: str | None, account: int = 1) -> int:
    tx_id = next(_ids)
    with engine.begin() as connection:
        connection.execute(
            insert(LedgerTransaction).values(
                id=tx_id,
                account_id=account,
                dedupe_key=f"ext:t:{tx_id}",
                fingerprint="x",
                occurrence=1,
                booking_date=day,
                amount_cents=amount,
                currency="EUR",
                label_raw="CB MAGASIN",
                label_clean="MAGASIN",
                created_at=NOW,
            )
        )
    if category:
        categorize_manually(
            engine, tx_id, [(_cat(engine, category), amount)], actor_user_id=None, now=NOW
        )
    return tx_id


def _alerts(engine: Engine) -> list[tuple[str, Severity, AlertStatus, str]]:
    with engine.connect() as connection:
        return [
            (row.dedupe_key, row.severity, row.status, row.title)
            for row in connection.execute(select(Alert).order_by(Alert.id))
        ]


def test_root_budget_counts_subcategories_and_refunds(db: Engine) -> None:
    budget_id = set_budget(db, _cat(db, "Alimentation"), 40_000, actor_user_id=None, now=NOW)
    _spend(db, -20_000, date(2026, 10, 3), "Courses")
    _spend(db, -15_000, date(2026, 10, 9), "Restaurants")
    _spend(db, 3_000, date(2026, 10, 10), "Courses")  # remboursement
    _spend(db, -9_000, date(2026, 9, 30), "Courses")  # autre mois
    _spend(db, -5_000, date(2026, 10, 9), "Courses", account=2)
    with db.begin() as connection:  # compte en consultation seule : non compté
        connection.execute(
            update(Account).where(Account.id == 2).values(include_in_net_worth=False)
        )
    [status] = budget_statuses(db, "2026-10")
    assert (status.id, status.name, status.spent_cents, status.remaining_cents) == (
        budget_id,
        "Alimentation",
        32_000,
        8_000,
    )
    assert (status.percent, status.level) == (80, "proche")


def test_alerts_at_80_and_100_percent_once_per_month(db: Engine) -> None:
    set_budget(db, _cat(db, "Restaurants"), 10_000, actor_user_id=None, now=NOW)
    _spend(db, -7_000, date(2026, 10, 2), "Restaurants")
    assert check_budgets(db, now=NOW) == 0
    assert _alerts(db) == []

    _spend(db, -1_000, date(2026, 10, 5), "Restaurants")
    assert check_budgets(db, now=NOW) == 1
    assert check_budgets(db, now=NOW) == 1  # rejoué : rien de plus
    assert _alerts(db) == [
        (
            "budget:1:2026-10:80",
            Severity.INFO,
            AlertStatus.OPEN,
            "Budget « Alimentation › Restaurants » : 80 % atteint",
        )
    ]

    exceed = _spend(db, -2_500, date(2026, 10, 6), "Restaurants")
    check_budgets(db, now=NOW)
    assert [(key, severity) for key, severity, _, _ in _alerts(db)] == [
        ("budget:1:2026-10:80", Severity.INFO),
        ("budget:1:2026-10:100", Severity.WARNING),
    ]
    assert "dépassé" in _alerts(db)[1][3]

    # Correction de catégorie : la dépense repasse sous 100 %, l'alerte se résout.
    categorize_manually(db, exceed, [(_cat(db, "Sorties"), -2_500)], actor_user_id=None, now=NOW)
    check_budgets(db, now=NOW)
    assert [status for _, _, status, _ in _alerts(db)] == [AlertStatus.OPEN, AlertStatus.RESOLVED]

    # Mois suivant : les alertes d'octobre sont closes.
    check_budgets(db, now=datetime(2026, 11, 1, 8, 0, tzinfo=UTC))
    assert {status for _, _, status, _ in _alerts(db)} == {AlertStatus.RESOLVED}


def test_category_name_like_an_amount_is_left_out(db: Engine) -> None:
    with db.begin() as connection:
        connection.execute(
            update(Category).where(Category.name == "Sport").values(name="Club 12,50")
        )
    set_budget(db, _cat(db, "Club 12,50"), 1_000, actor_user_id=None, now=NOW)
    _spend(db, -1_000, date(2026, 10, 2), "Club 12,50")
    check_budgets(db, now=NOW)
    assert _alerts(db)[0][3] == "Budget : 80 % atteint"


def test_set_update_remove_and_errors(db: Engine) -> None:
    category = _cat(db, "Courses")
    first = set_budget(db, category, 30_000, actor_user_id=None, now=NOW)
    assert set_budget(db, category, 35_000, actor_user_id=None, now=NOW) == first
    assert budget_statuses(db, "2026-10")[0].monthly_cents == 35_000
    _spend(db, -40_000, date(2026, 10, 2), "Courses")
    check_budgets(db, now=NOW)
    remove_budget(db, first, actor_user_id=None, now=NOW)
    assert budget_statuses(db, "2026-10") == []
    assert {status for _, _, status, _ in _alerts(db)} == {AlertStatus.RESOLVED}
    with pytest.raises(BudgetError, match="introuvable"):
        remove_budget(db, first, actor_user_id=None, now=NOW)
    with pytest.raises(BudgetError, match="positif"):
        set_budget(db, category, 0, actor_user_id=None, now=NOW)
    with pytest.raises(BudgetError, match="Catégorie introuvable"):
        set_budget(db, 9999, 100, actor_user_id=None, now=NOW)
    with pytest.raises(BudgetError, match="dépenses"):
        set_budget(db, _cat(db, "Salaire"), 100, actor_user_id=None, now=NOW)


def test_average_of_the_three_previous_months(db: Engine) -> None:
    _spend(db, -30_000, date(2026, 7, 5), "Courses")
    _spend(db, -30_000, date(2026, 8, 5), "Courses")
    _spend(db, -30_001, date(2026, 9, 5), "Restaurants")
    _spend(db, -99_999, date(2026, 10, 5), "Courses")  # mois en cours : exclu
    _spend(db, -99_999, date(2026, 6, 5), "Courses")  # trop ancien
    averages = average_spending(db, "2026-10")
    assert averages[_cat(db, "Courses")] == 20_000
    assert averages[_cat(db, "Restaurants")] == 10_000  # arrondi vers le bas
    assert averages[_cat(db, "Alimentation")] == 30_000
    assert _cat(db, "Salaire") not in averages
