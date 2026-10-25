"""Bilan hebdomadaire (ALR-05)."""

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import Engine, insert, select

from app.alerts.digest import digest_details, digest_enabled, set_digest_enabled, weekly_digest
from app.alerts.engine import raise_alert
from app.categorize.budgets import set_budget
from app.categorize.seed import seed_categories
from app.categorize.service import categorize_manually
from app.models import Alert, Category, LedgerTransaction
from app.models.alerts import AlertStatus, Severity
from app.models.tax import DeadlineKind
from app.tax.deadlines import DeadlineInput, create

MONDAY = datetime(2026, 10, 12, 6, 10, tzinfo=UTC)  # 8 h 10 à Paris


def _spend(engine: Engine, tx_id: int, amount: int, day: date, category: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            insert(LedgerTransaction).values(
                id=tx_id, account_id=1, dedupe_key=f"ext:t:{tx_id}", fingerprint="x",
                occurrence=1, booking_date=day, amount_cents=amount, currency="EUR",
                label_raw="CB RESTO", label_clean="RESTO", created_at=MONDAY,
            )
        )  # fmt: skip
    with engine.connect() as connection:
        category_id = connection.scalar(select(Category.id).where(Category.name == category))
    categorize_manually(
        engine, tx_id, [(int(category_id or 0), amount)], actor_user_id=None, now=MONDAY
    )


def test_monday_digest_once_and_previous_one_resolved(engine: Engine) -> None:
    seed_categories(engine)
    _spend(engine, 1, -4_500, date(2026, 10, 9), "Restaurants")
    _spend(engine, 2, -1_000, date(2026, 10, 1), "Restaurants")  # plus de 7 jours
    with engine.connect() as connection:
        alimentation = connection.scalar(select(Category.id).where(Category.name == "Alimentation"))
    set_budget(engine, int(alimentation or 0), 6_000, actor_user_id=None, now=MONDAY)
    create(
        engine,
        DeadlineInput(kind=DeadlineKind.PROPERTY_TAX, due_date=date(2026, 10, 15)),
        actor_user_id=None,
        now=MONDAY,
    )
    raise_alert(engine, type="t", severity=Severity.WARNING, key="x", title="Autre", now=MONDAY)

    assert not weekly_digest(engine, now=MONDAY - timedelta(days=1))  # dimanche
    assert weekly_digest(engine, now=MONDAY)
    assert not weekly_digest(engine, now=MONDAY + timedelta(hours=1))
    with engine.connect() as connection:
        digest = connection.execute(select(Alert).where(Alert.type == "digest")).one()
    assert (digest.title, digest.severity, digest.dedupe_key) == (
        "Bilan de la semaine",
        Severity.INFO,
        "digest:2026-S42",
    )
    assert digest.details["Dépenses des 7 derniers jours"].startswith("-45,00")
    assert digest.details["Budgets à surveiller"] == "Alimentation 91 %"
    assert digest.details["Alertes en cours"] == "1"
    assert digest.details["Échéances (14 jours)"] == "Taxe foncière le 15/10/2026"

    assert weekly_digest(engine, now=MONDAY + timedelta(days=7))
    with engine.connect() as connection:
        statuses = [
            s
            for (s,) in connection.execute(
                select(Alert.status).where(Alert.type == "digest").order_by(Alert.id)
            )
        ]
    assert statuses == [AlertStatus.RESOLVED, AlertStatus.OPEN]


def test_digest_can_be_disabled_and_is_minimal_without_data(engine: Engine) -> None:
    assert digest_enabled(engine)
    set_digest_enabled(engine, False, actor_user_id=None, now=MONDAY)
    assert not digest_enabled(engine)
    assert not weekly_digest(engine, now=MONDAY)
    set_digest_enabled(engine, True, actor_user_id=None, now=MONDAY)
    assert digest_enabled(engine)
    assert set(digest_details(engine, MONDAY.date())) == {
        "Patrimoine",
        "Dépenses des 7 derniers jours",
        "Alertes en cours",
    }
